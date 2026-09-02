"""Multi-list sanctions screening index, plus day-over-day change tracking for
the OFAC SDN list.

Screening covers, by name and alias:
  - OFAC Specially Designated Nationals (SDN)           source "SDN"
  - OFAC Consolidated / non-SDN lists                   source "Non-SDN"
  - BIS lists via Trade.gov's Consolidated Screening    source "BIS"
    List (Entity List, Denied Persons, Unverified, MEU)
  - State Department (ITAR Debarred, nonproliferation)  source "State"
  - UN Security Council Consolidated List               source "UN"
  - UK OFSI Consolidated List                           source "UK"
  - EU Consolidated Financial Sanctions List            source "EU"
  - SAM.gov Exclusions (US federal debarment)           source "SAM"
      needs a free SAM.gov public API key in SAM_API_KEY; skipped without it
  - FinCEN Section 311 / 9714 special measures           source "FinCEN 311"
      foreign banks/jurisdictions of primary money laundering concern

Only the OFAC SDN list is diffed day over day (sdn_log.json, the audit trail
behind "Recent list activity"). The other lists are fetched fresh each run and
folded straight into the search index; each fetch is best-effort, so one flaky
source never fails the daily build.

Run order: before dashboard.py.
"""
import csv
import io
import json
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape as xml_escape

import fetcher

# ---- OFAC direct feeds ----
OFAC = "https://sanctionslistservice.ofac.treas.gov/api/download"
SDN_URL, SDN_ALT_URL, SDN_ADD_URL = f"{OFAC}/SDN.CSV", f"{OFAC}/ALT.CSV", f"{OFAC}/ADD.CSV"
CONS_URL, CONS_ALT_URL, CONS_ADD_URL = f"{OFAC}/CONS_PRIM.CSV", f"{OFAC}/CONS_ALT.CSV", f"{OFAC}/CONS_ADD.CSV"

# ---- other lists ----
CSL_URL = "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.csv"
CSL_SOURCE_MAP = {
    "Entity List (EL) - Bureau of Industry and Security": "BIS",
    "Denied Persons List (DPL) - Bureau of Industry and Security": "BIS",
    "Unverified List (UVL) - Bureau of Industry and Security": "BIS",
    "Military End User (MEU) List - Bureau of Industry and Security": "BIS",
    "ITAR Debarred (DTC) - State Department": "State",
    "Nonproliferation Sanctions (ISN) - State Department": "State",
}
FINCEN_311_URL = ("https://www.fincen.gov/resources/statutes-and-regulations/"
                  "311-and-9714-special-measures")
UN_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
UK_URL = "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv"
EU_TOKEN = "dG9rZW4tMjAxNw"
EU_URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/"
          f"csvFullSanctionsList_1_1/content?token={EU_TOKEN}")

# SAM.gov Exclusions (US federal debarment / suspension). The download endpoint
# needs a free SAM.gov "public" API key, supplied via the SAM_API_KEY env var
# (a GitHub Actions secret in CI). Without it fetch_sam() raises and is skipped
# like any other flaky source. Fetched as fileType=EXCLUSION + date; the daily
# file is not always posted by run time, so we try the last few days.
SAM_EXTRACT_URL = "https://api.sam.gov/data-services/v1/extracts"
SAM_UA = {"User-Agent": fetcher.UA["User-Agent"]}
# 0-based column positions in the 31-field comma-separated V2 extract
# (SAM Exclusions Extract V2 Layout v1.5). Only the ones we index are named.
SAM_COL = dict(classification=0, name=1, prefix=2, first=3, middle=4, last=5,
               suffix=6, country=13, uei=17, program=18, agency=19,
               excl_type=21, comments=22, term_date=24, cross_ref=26,
               sam_number=27)

# Committed (must persist across CI runs so the SDN diff has a baseline).
SDN_SNAPSHOT_PATH = "sdn_snapshot.json"
CSL_SNAPSHOT_PATH = "csl_snapshot.json"
SDN_LOG_PATH = "sdn_log.json"

# Derived, gitignored; the workflow copies them into site/.
SDN_INDEX_PATH = "sdn_index.json"          # everything except SAM (~3 MB gz)
SDN_INDEX_SAM_PATH = "sdn_index_sam.json"  # SAM.gov only, loaded after the rest
SDN_COUNTS_PATH = "sdn_counts.json"
SAM_META_PATH = "sam_meta.json"           # {"entries": N} from the last real SAM fetch, reused when SAM is skipped
CHANGES_RSS_PATH = "sdn-changes.xml"
CHANGES_CSV_PATH = "sdn-changes.csv"
CHANGES_WINDOW_DAYS = 120
SITE_BASE = "https://kaufman2699.github.io/Klearance_V1"

FIELDS = ["ent_num", "name", "sdn_type", "program", "title", "call_sign",
          "vess_type", "tonnage", "grt", "vess_flag", "vess_owner", "remarks"]
REMARKS_CAP = 400


def _csv_rows(text):
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        if row and row[0].strip().isdigit():
            yield row


def fetch_primary(url):
    out = {}
    for row in _csv_rows(fetcher.get(url, timeout=180)):
        row = (row + [""] * len(FIELDS))[:len(FIELDS)]
        rec = {FIELDS[i]: row[i].strip() for i in range(len(FIELDS))}
        num = rec.pop("ent_num")
        out[num] = {k: ("" if v == "-0-" else v) for k, v in rec.items()}
    return out


def fetch_alt(url):
    out = {}
    for row in _csv_rows(fetcher.get(url, timeout=180)):
        if len(row) >= 4 and row[3].strip() not in ("", "-0-"):
            out.setdefault(row[0].strip(), [])
            if row[3].strip() not in out[row[0].strip()]:
                out[row[0].strip()].append(row[3].strip())
    return out


def fetch_add(url):
    out = {}
    for row in _csv_rows(fetcher.get(url, timeout=180)):
        if len(row) >= 5 and row[4].strip() not in ("", "-0-") and row[0].strip() not in out:
            out[row[0].strip()] = row[4].strip()
    return out


def ofac_records(primary, alts, countries, source):
    for num, r in primary.items():
        yield {
            "key": num, "name": r["name"], "type": r["sdn_type"], "program": r["program"],
            "source": source, "remarks": r.get("remarks", ""),
            "country": countries.get(num, ""), "aliases": alts.get(num, []),
        }


# ---------------------------------------------------------------- other lists

def fetch_tradegov():
    """BIS + State rows from Trade.gov's Consolidated Screening List. Treasury
    rows are skipped - the OFAC feeds above already carry them, fresher."""
    text = fetcher.get(CSL_URL, timeout=240)
    out = []
    for i, r in enumerate(csv.DictReader(io.StringIO(text))):
        src = CSL_SOURCE_MAP.get(r.get("source", ""))
        name = (r.get("name") or "").strip()
        if not src or not name:
            continue
        rk = " ".join(x for x in [
            (r.get("remarks") or "").strip(),
            f"DOB {r['dates_of_birth']}" if r.get("dates_of_birth") else "",
            f"Nationality: {r['nationalities']}" if r.get("nationalities") else "",
        ] if x)
        out.append({
            "key": r.get("entity_number") or r.get("_id") or f"csl{i}",
            "name": name, "type": (r.get("type") or "").strip(),
            "program": (r.get("programs") or "").strip() or src, "source": src,
            "remarks": rk, "country": (r.get("nationalities") or r.get("citizenships") or "").split(";")[0].strip(),
            "aliases": [a.strip() for a in (r.get("alt_names") or "").split(";") if a.strip()],
        })
    return out


def fetch_un():
    """UN Security Council Consolidated List (individuals + entities)."""
    root = ET.fromstring(fetcher.get(UN_URL, timeout=180))

    def g(el, tag):
        x = el.find(tag) if el is not None else None
        return (x.text or "").strip() if x is not None and x.text else ""

    out = []
    for ind in root.findall("./INDIVIDUALS/INDIVIDUAL"):
        name = " ".join(p for p in (g(ind, "FIRST_NAME"), g(ind, "SECOND_NAME"),
                                    g(ind, "THIRD_NAME"), g(ind, "FOURTH_NAME")) if p)
        if not name:
            continue
        aliases = [g(a, "ALIAS_NAME") for a in ind.findall("INDIVIDUAL_ALIAS")]
        addr = ind.find("INDIVIDUAL_ADDRESS")
        country = g(addr, "COUNTRY")
        if not country:
            country = g(ind.find("NATIONALITY"), "VALUE")
        dob = ind.find("INDIVIDUAL_DATE_OF_BIRTH")
        remarks = f"DOB {g(dob, 'YEAR') or g(dob, 'DATE')}".strip() if dob is not None else ""
        out.append({"key": "UN" + g(ind, "DATAID"), "name": name, "type": "individual",
                    "program": g(ind, "UN_LIST_TYPE") or "UN", "source": "UN",
                    "remarks": remarks, "country": country,
                    "aliases": [a for a in aliases if a]})
    for ent in root.findall("./ENTITIES/ENTITY"):
        name = g(ent, "FIRST_NAME")
        if not name:
            continue
        aliases = [g(a, "ALIAS_NAME") for a in ent.findall("ENTITY_ALIAS")]
        out.append({"key": "UN" + g(ent, "DATAID"), "name": name, "type": "entity",
                    "program": g(ent, "UN_LIST_TYPE") or "UN", "source": "UN",
                    "remarks": "", "country": g(ent.find("ENTITY_ADDRESS"), "COUNTRY"),
                    "aliases": [a for a in aliases if a]})
    return out


def fetch_uk_ofsi():
    """UK OFSI Consolidated List. First line is a 'Last Updated' stamp; the
    real header is line 2. Rows share a Group ID; one is the primary name."""
    lines = fetcher.get(UK_URL, timeout=240).splitlines()
    groups = {}
    for r in csv.DictReader(lines[1:]):
        gid = (r.get("Group ID") or "").strip()
        name = " ".join(x.strip() for x in (r.get("Name 1"), r.get("Name 2"),
                        r.get("Name 3"), r.get("Name 4"), r.get("Name 5"),
                        r.get("Name 6")) if x and x.strip())
        if not gid or not name:
            continue
        grp = groups.setdefault(gid, {"primary": None, "aliases": [], "meta": r})
        if (r.get("Alias Type") or "").strip().lower() == "primary name" and not grp["primary"]:
            grp["primary"], grp["meta"] = name, r
        else:
            grp["aliases"].append(name)
    out = []
    for gid, grp in groups.items():
        prim = grp["primary"] or (grp["aliases"].pop(0) if grp["aliases"] else None)
        if not prim:
            continue
        m = grp["meta"]
        rk = " ".join(x for x in [
            f"DOB {m.get('DOB').strip()}" if m.get("DOB", "").strip() else "",
            (m.get("Other Information") or "").strip(),
        ] if x)
        out.append({"key": "UK" + gid, "name": prim,
                    "type": (m.get("Group Type") or "").strip().lower(),
                    "program": (m.get("Regime") or "UK").strip(), "source": "UK",
                    "remarks": rk, "country": (m.get("Country") or "").strip(),
                    "aliases": grp["aliases"]})
    return out


def fetch_eu():
    """EU Consolidated Financial Sanctions List. Semicolon-delimited, one row
    per name/alias; rows share Entity_LogicalId."""
    text = fetcher.get(EU_URL, timeout=240).lstrip("﻿")
    groups = {}
    for r in csv.DictReader(io.StringIO(text), delimiter=";"):
        lid = (r.get("Entity_LogicalId") or "").strip()
        name = (r.get("NameAlias_WholeName") or "").strip()
        if not lid or not name:
            continue
        groups.setdefault(lid, {"names": [], "meta": r})["names"].append(name)
    out = []
    for lid, grp in groups.items():
        m, names = grp["meta"], grp["names"]
        st = (m.get("Entity_SubjectType") or "").strip().lower()
        out.append({"key": "EU" + lid, "name": names[0],
                    "type": "individual" if st == "person" else ("entity" if st else ""),
                    "program": (m.get("Entity_Regulation_Programme") or "EU").strip() or "EU",
                    "source": "EU", "remarks": (m.get("Entity_Remark") or "").strip(),
                    "country": (m.get("Address_CountryDescription")
                                or m.get("Citizenship_CountryDescription") or "").strip(),
                    "aliases": names[1:]})
    return out


# Rows in the FinCEN 311 table that name a class of transactions rather than an
# entity a name search can match. Kept out of the index; the measure still
# exists, it just has no screenable name.
FINCEN_311_SKIP = {
    "Convertible Virtual Currency Mixing",
    "Mexican Gambling Establishments",
    "Burma",  # "Burmese banking institution" is a class, not a named entity
}
# Measures whose target is a whole jurisdiction, not a legal entity - kept as a
# record (the finding is real) but carrying no address / affiliate enrichment.
FINCEN_311_JURISDICTION = {
    "Democratic People's Republic of Korea",
    "Islamic Republic of Iran",
    "Nauru",
    "Ukraine",
}
FINCEN_311_DETAILS_PATH = "fincen311_details.csv"
FINCEN_311_STALE_PATH = "fincen311_stale.json"   # derived; the freshness check's output
_FINCEN_311_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _fincen_311_date(cell):
    """First MM/DD/YYYY in a stage cell, as YYYY-MM-DD. Cells can carry trailing
    parentheticals ('6/25/2025 (Final Rule)7/9/2025 (Supplement)') - the first
    date is the one that sets the stage."""
    m = _FINCEN_311_DATE.search(cell or "")
    if not m:
        return ""
    mm, dd, yyyy = m.groups()
    return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"


def _fincen_311_slug(name):
    """Stable ASCII join key for a measure name - accent-folded so a source-side
    encoding wobble ('Institucion' vs 'Institución') maps to the same key."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")


def _fincen_311_name(raw):
    """(clean_name, aliases) from a table name cell. Trailing asterisks are
    footnote markers; '(Includes X)' / 'renamed Y' name a covered affiliate or
    a later name; a trailing '(Acronym)' that just repeats a word already in the
    name is a short form, not a new fact."""
    name = re.sub(r"[\s*’']+$", "", raw).strip()
    aliases = []
    inc = re.search(r"\((?:includes|formerly)\s+([^)]+)\)", name, re.I)
    if inc:
        aliases += [a.strip(" *") for a in re.split(r";|,| and ", inc.group(1)) if a.strip(" *")]
    ren = re.search(r"renamed\s+([A-Za-z0-9 .,'&-]+)", name, re.I)
    if ren:
        aliases.append(ren.group(1).strip(" *"))
    name = re.sub(r"\s*\((?:includes|formerly)[^)]*\)", "", name, flags=re.I)
    name = re.sub(r";?\s*renamed\s+.*$", "", name, flags=re.I)
    tail = re.search(r"\s*\(([A-Za-z][A-Za-z0-9 .&-]{1,40})\)\s*$", name)
    if tail:
        token = tail.group(1).strip()
        earlier = name[:tail.start()].lower()
        if re.search(r"\b" + re.escape(token.lower()) + r"\b", earlier):
            aliases.append(token)
            name = name[:tail.start()].rstrip(" ,")
    return re.sub(r"[\s*]+$", "", name).strip(), aliases


def _fincen_311_remarks(parts, source_url):
    """Join remark fragments with sentence spacing, then trim on a word boundary
    so the ' Source: <url>' tail always survives REMARKS_CAP intact."""
    text = ""
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        text += (" " if text else "") + p
        if not text.endswith((".", '"', "!", "?", "…")):
            text += "."
    tail = f" Source: {source_url}" if source_url else ""
    room = max(120, REMARKS_CAP - len(tail))
    if len(text) > room:
        text = text[:room].rsplit(" ", 1)[0].rstrip(" ,;.") + "…"
    return text + tail


def _load_fincen_311_details():
    """fincen311_details.csv grouped by measure slug. Rows are facts hand-mined
    from the rulemaking documents linked in the table (addresses, aliases, and
    the sub-entities the rules name); each row cites the document it came from.
    See the file's header comment for the column contract."""
    details = {}
    if not os.path.exists(FINCEN_311_DETAILS_PATH):
        return details
    with open(FINCEN_311_DETAILS_PATH, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(row for row in fh if not row.lstrip().startswith("#")):
            details.setdefault((r.get("measure") or "").strip(), []).append(r)
    return details


def fetch_fincen311():
    """FinCEN Section 311 / 9714 special measures - foreign banks, jurisdictions
    and transaction classes found to be of primary money laundering concern. A
    final rule most often bars US institutions from holding a correspondent
    account for the target.

    The table on fincen.gov is the only machine-readable index (no data file):
    it gives the target name, the four stage dates and the linked rulemaking
    documents. Addresses, aliases and the sub-entities a rule names live only
    inside those documents - fincen311_details.csv carries what has been mined
    from them by hand, joined here on the measure slug."""
    parser = fetcher.TableRowParser()
    parser.feed(fetcher.get(FINCEN_311_URL, timeout=120))
    details = _load_fincen_311_details()
    seen = set()
    needs_mining = []
    out = []
    for row in parser.rows:
        cells = row["cells"]
        if len(cells) < 5 or cells[0].lstrip().startswith("*"):
            continue  # footnote row / malformed
        raw = re.sub(r"[\s*]+$", "", cells[0]).replace("’", "'").strip()
        if raw in FINCEN_311_SKIP:
            continue
        name, aliases = _fincen_311_name(raw)
        if not name:
            continue
        slug = _fincen_311_slug(name)
        seen.add(slug)
        finding, nprm, final, rescinded = (_fincen_311_date(cells[i]) for i in (1, 2, 3, 4))
        if rescinded:
            status = "rescinded"
        elif final:
            status = "final rule"
        else:
            status = "proposed"
        stages = [f"Finding {finding}" if finding else "",
                  f"NPRM {nprm}" if nprm else "",
                  f"Final rule {final}" if final else "",
                  f"Rescinded {rescinded}" if rescinded else ""]
        doc = row["url"]
        if doc.startswith("/"):
            doc = "https://www.fincen.gov" + doc

        rows = details.get(slug, [])
        self_row = next((r for r in rows if (r.get("relationship") or "").strip() == "self"), None)
        country = ""
        extra = []
        main_src = doc  # table's first link, unless a mined self row cites a better one
        if self_row:
            override = (self_row.get("name") or "").strip()
            if override and override.lower() != name.lower():
                aliases.append(name)
                name = override
            aliases += [a.strip() for a in (self_row.get("alias") or "").split(";") if a.strip()]
            country = (self_row.get("country") or "").strip()
            addr = (self_row.get("address") or "").strip()
            note = (self_row.get("notes") or "").strip()
            main_src = (self_row.get("source_url") or "").strip() or doc
            if addr:
                extra.append(f"Address: {addr}")
            if note:
                extra.append(note)
        if raw in FINCEN_311_JURISDICTION:
            extra.append("Jurisdiction-level finding; no specific legal entity.")

        # Freshness: does the hand-mined enrichment keep up with the table? A
        # rescinded or jurisdiction measure needs none; otherwise flag it when
        # there is no self row, no mined_date, or the table shows a rule newer
        # than the last mine. Written to fincen311_stale.json for the workflow
        # to raise as a GitHub issue.
        if status != "rescinded" and raw not in FINCEN_311_JURISDICTION:
            stage_dates = [d for d in (finding, nprm, final, rescinded) if d]
            latest = max(stage_dates) if stage_dates else ""
            md = (self_row.get("mined_date") or "").strip() if self_row else ""
            if not self_row or not (self_row.get("address") or self_row.get("notes")):
                reason = "no details.csv self row - never enriched"
            elif not md:
                reason = "details.csv self row has no mined_date"
            elif latest and latest > md:
                reason = f"table shows a {status} dated {latest}, newer than mined_date {md}"
            else:
                reason = ""
            if reason:
                needs_mining.append({
                    "measure": slug, "name": name, "status": status,
                    "reason": reason, "latest_doc_date": latest,
                    "mined_date": md, "doc": doc,
                })

        remarks = _fincen_311_remarks(
            ["Section 311 special measure. " + "; ".join(s for s in stages if s)] + extra,
            main_src,
        )
        # de-dup aliases, drop any equal to the name
        seen_al, al = set(), []
        for a in aliases:
            a = a.strip()
            k = a.lower()
            if a and k != name.lower() and k not in seen_al:
                seen_al.add(k)
                al.append(a)
        out.append({
            "key": "fincen311-" + slug,
            "name": name,
            "type": "jurisdiction" if raw in FINCEN_311_JURISDICTION else "",
            "program": f"FinCEN 311 - {status}", "source": "FinCEN 311",
            "remarks": remarks[:REMARKS_CAP], "country": country, "aliases": al,
        })

        # Sub-entities the rule names: each becomes its own screenable row.
        for r in rows:
            rel = (r.get("relationship") or "").strip()
            if rel in ("", "self"):
                continue
            sub_name = (r.get("name") or "").strip()
            if not sub_name:
                continue
            sub_note = (r.get("notes") or "").strip()
            sub_src = (r.get("source_url") or "").strip() or doc
            sr = _fincen_311_remarks(
                [f'FinCEN 311 sub-entity of "{name}" ({rel}).', sub_note,
                 f"Address: {r['address'].strip()}" if r.get("address") else ""],
                sub_src,
            )
            out.append({
                "key": f"fincen311-{slug}-{_fincen_311_slug(sub_name)}",
                "name": sub_name, "type": (r.get("type") or "").strip(),
                "program": f"FinCEN 311 - {status} ({rel})", "source": "FinCEN 311",
                "remarks": sr, "country": (r.get("country") or "").strip(),
                "aliases": [a.strip() for a in (r.get("alias") or "").split(";") if a.strip()],
            })

    orphans = sorted(set(details) - seen)
    if orphans:
        print(f"  FinCEN 311: details.csv measures with no matching table row: {orphans}")
    if needs_mining:
        print(f"  FinCEN 311: {len(needs_mining)} measure(s) need (re-)mining: "
              + ", ".join(m["measure"] for m in needs_mining))
    try:
        with open(FINCEN_311_STALE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "generated": datetime.now(timezone.utc).date().isoformat(),
                "table_url": FINCEN_311_URL,
                "orphans": orphans,
                "measures": needs_mining,
            }, f, indent=2)
    except OSError:
        pass
    return out


def fetch_sam():
    """SAM.gov Exclusions: US federal debarment, suspension and other
    ineligibility records. Needs a free SAM.gov public API key in SAM_API_KEY.
    The file is a daily ZIP holding one comma-separated V2 extract; only Active
    records are in it. Firm/SED rows carry the entity name, Individual rows are
    assembled from the name parts, and the Cross-Reference field holds aliases."""
    if not os.environ.get("FETCH_SAM"):
        # Scheduled / manual builds only. The SAM.gov public API key has a low
        # daily request quota; a burst of push builds exhausts it and every
        # fetch that day then returns 429. FETCH_SAM is set in the workflow for
        # the schedule and workflow_dispatch triggers, not for push.
        raise RuntimeError("SAM fetch skipped for this trigger (schedule/dispatch only)")
    key = os.environ.get("SAM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("SAM_API_KEY not set")

    # fileType=EXCLUSION + date lets SAM build the filename; the daily file for
    # a given day isn't always posted by run time, and a missing file comes back
    # as 400 ("extract file not found") as well as 404, so walk back a few days.
    now = datetime.now(timezone.utc)
    raw, last_err = None, None
    for back in range(5):
        d = now - timedelta(days=back)
        url = (f"{SAM_EXTRACT_URL}?api_key={urllib.parse.quote(key)}"
               f"&fileType=EXCLUSION&date={d:%m/%d/%Y}")
        try:
            req = urllib.request.Request(url, headers=SAM_UA)
            with urllib.request.urlopen(req, timeout=240) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} for {d:%Y-%m-%d}"
            if e.code in (400, 403, 404):   # not posted for that day yet
                continue
            raise
    if raw is None:
        raise RuntimeError(last_err or "no exclusions file in the last 5 days")

    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        raise RuntimeError("empty ZIP")
    text = zf.read(max(names, key=lambda n: zf.getinfo(n).file_size)).decode("utf-8", "ignore")

    C = SAM_COL
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) <= C["sam_number"]:
            continue
        cell = lambda k: row[C[k]].strip()
        if cell("classification").lower() == "classification":  # header row, if any
            continue
        cls = cell("classification")
        if cls.lower() == "individual":
            name = " ".join(x for x in (cell("prefix"), cell("first"),
                                        cell("middle"), cell("last"),
                                        cell("suffix")) if x)
        else:
            name = cell("name")
        if not name:
            continue
        term = cell("term_date")
        remarks = " ".join(x for x in [
            cell("excl_type") or "",
            f"Excluding agency: {cell('agency')}" if cell("agency") else "",
            f"Terminates {term}" if term and term.lower() != "indefinite" else "",
            cell("comments"),
        ] if x)
        aliases = [a.strip() for a in cell("cross_ref").replace(";", ",").split(",")
                   if a.strip()]
        out.append({
            "key": cell("uei") or cell("sam_number") or f"sam{len(out)}",
            "name": name, "type": cls.lower(),
            "program": cell("program") or "Exclusion", "source": "SAM",
            "remarks": remarks, "country": cell("country"), "aliases": aliases,
        })
    return out


def safe(label, fn):
    try:
        recs = fn()
        print(f"  {label}: {len(recs)} entries")
        return recs
    except Exception as e:  # one flaky source must not fail the build
        print(f"  {label}: SKIPPED ({type(e).__name__}: {str(e)[:120]})")
        return []


# ---------------------------------------------------------------- index + diff

def build_index(all_records):
    rows = []
    for rec in all_records:
        remarks = (rec.get("remarks") or "")[:REMARKS_CAP]
        country = rec.get("country", "")
        src, key = rec["source"], rec["key"]
        typ, prog = rec.get("type", ""), rec.get("program", "")
        rows.append([key, rec["name"], typ, prog, src, 0, remarks, country, ""])
        seen = set()
        for al in rec.get("aliases", []):
            al = (al or "").strip()
            if al and al != rec["name"] and al.lower() not in seen:
                seen.add(al.lower())
                rows.append([key, rec["name"], typ, prog, src, 1, remarks, country, al])
    return rows


def diff(previous, current, today, list_name="SDN"):
    events = []
    prev, cur = set(previous), set(current)
    for num in sorted(cur - prev, key=int):
        r = current[num]
        events.append({"date": today, "list": list_name, "ent_num": num, "action": "added",
                       "name": r["name"], "program": r["program"], "sdn_type": r["sdn_type"]})
    for num in sorted(prev - cur, key=int):
        r = previous[num]
        events.append({"date": today, "list": list_name, "ent_num": num, "action": "removed",
                       "name": r["name"], "program": r["program"], "sdn_type": r["sdn_type"]})
    for num in sorted(prev & cur, key=int):
        if previous[num] != current[num]:
            r = current[num]
            events.append({"date": today, "list": list_name, "ent_num": num, "action": "modified",
                           "name": r["name"], "program": r["program"], "sdn_type": r["sdn_type"]})
    return events


def write_change_feeds(log, today):
    cutoff = (datetime.fromisoformat(today) - timedelta(days=CHANGES_WINDOW_DAYS)).date().isoformat()
    recent = sorted((e for e in log if e["date"] >= cutoff),
                    key=lambda e: (e["date"], e["ent_num"]), reverse=True)
    with open(CHANGES_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "list", "action", "ent_num", "name", "program", "sdn_type"])
        for e in recent:
            w.writerow([e["date"], e.get("list", "SDN"), e["action"], e["ent_num"],
                        e["name"], e["program"], e.get("sdn_type", "")])

    def item(e):
        lst = e.get("list", "SDN")
        long_list = "SDN" if lst == "SDN" else "Consolidated (Non-SDN)"
        prefix = "" if lst == "SDN" else f"[{lst}] "
        link = f'https://sanctionssearch.ofac.treas.gov/Details.aspx?id={e["ent_num"]}'
        try:
            dt = datetime.fromisoformat(e["date"]).replace(tzinfo=timezone.utc)
        except ValueError:
            dt = datetime.now(timezone.utc)
        return (f"    <item>\n"
                f"      <title>{xml_escape(prefix + e['action'].title() + ': ' + (e['name'] or '(unnamed)'))}</title>\n"
                f"      <description>{xml_escape(e['action'].title() + ' on the OFAC ' + long_list + ' list. Program: ' + (e['program'] or 'n/a') + '.')}</description>\n"
                f"      <link>{xml_escape(link)}</link>\n"
                f"      <guid isPermaLink=\"false\">{lst.lower().replace('-', '')}-{e['date']}-{e['ent_num']}-{e['action']}</guid>\n"
                f"      <pubDate>{format_datetime(dt)}</pubDate>\n"
                f"    </item>")

    xml = (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<rss version=\"2.0\"><channel>\n"
           f"    <title>Klearance - OFAC SDN and Consolidated (Non-SDN) list changes</title>\n"
           f"    <link>{SITE_BASE}/</link>\n"
           f"    <description>Daily additions, removals and modifications on the OFAC "
           f"Specially Designated Nationals and Consolidated (Non-SDN) lists.</description>\n"
           f"    <language>en-us</language>\n"
           f"    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>\n"
           + "\n".join(item(e) for e in recent[:200]) + "\n</channel></rss>\n")
    with open(CHANGES_RSS_PATH, "w", encoding="utf-8") as f:
        f.write(xml)


def main():
    today = datetime.now(timezone.utc).date().isoformat()

    sdn = fetch_primary(SDN_URL)
    sdn_alt, sdn_ctry = fetch_alt(SDN_ALT_URL), fetch_add(SDN_ADD_URL)
    csl = fetch_primary(CONS_URL)
    csl_alt, csl_ctry = fetch_alt(CONS_ALT_URL), fetch_add(CONS_ADD_URL)
    print(f"  OFAC SDN: {len(sdn)}   OFAC Consolidated: {len(csl)}")

    bis_state = safe("Trade.gov (BIS + State)", fetch_tradegov)
    un = safe("UN Security Council", fetch_un)
    uk = safe("UK OFSI", fetch_uk_ofsi)
    eu = safe("EU FSF", fetch_eu)
    fincen311 = safe("FinCEN 311 special measures", fetch_fincen311)
    sam = safe("SAM.gov Exclusions", fetch_sam)

    # --- SDN + Non-SDN diff + audit log ---
    # Both OFAC lists are diffed day over day against their committed snapshot
    # and appended to the same sdn_log.json, tagged by an event "list" field
    # ("SDN" / "Non-SDN"). The Consolidated (Non-SDN) list is ~480 names against
    # ~17k on the SDN list, so its changes are rare but high-impact - a new
    # NS-CMIC company is a US investment ban, a new NS-MBS entry a menu of
    # trade restrictions.
    def diff_against(snapshot_path, current, list_name):
        if os.path.exists(snapshot_path):
            with open(snapshot_path, encoding="utf-8") as f:
                evs = diff(json.load(f), current, today, list_name)
        else:
            evs = []
            print(f"  No prior {list_name} snapshot - {len(current)} taken as baseline.")
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(current, f)
        return evs

    events = diff_against(SDN_SNAPSHOT_PATH, sdn, "SDN")
    csl_events = diff_against(CSL_SNAPSHOT_PATH, csl, "Non-SDN")
    log = json.load(open(SDN_LOG_PATH, encoding="utf-8")) if os.path.exists(SDN_LOG_PATH) else []
    for e in log:                      # historical entries predate the tag; all were SDN
        e.setdefault("list", "SDN")
    log.extend(events)
    log.extend(csl_events)
    with open(SDN_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)

    # --- combined screening index ---
    # SAM.gov alone is ~4x the size of the other lists put together, so it
    # goes in its own file the browser fetches only after the fast index is in
    # hand and searchable. Same row shape in both.
    records = (
        list(ofac_records(sdn, sdn_alt, sdn_ctry, "SDN"))
        + list(ofac_records(csl, csl_alt, csl_ctry, "Non-SDN"))
        + bis_state + un + uk + eu + fincen311
    )
    index = build_index(records)
    with open(SDN_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f)

    # SAM.gov is fetched on scheduled / manual runs only (see fetch_sam). On a
    # push build `sam` is empty: keep the sdn_index_sam.json the last real fetch
    # left behind — restored from the workflow cache — and carry its entry count
    # forward, rather than overwriting the live list with an empty file.
    if sam:
        sam_index = build_index(sam)
        sam_entries = len(sam)
        with open(SDN_INDEX_SAM_PATH, "w", encoding="utf-8") as f:
            json.dump(sam_index, f)
        with open(SAM_META_PATH, "w", encoding="utf-8") as f:
            json.dump({"entries": sam_entries}, f)
    else:
        sam_index, sam_entries = [], 0
        try:
            with open(SDN_INDEX_SAM_PATH, encoding="utf-8") as f:
                sam_index = json.load(f)
        except (OSError, ValueError):
            sam_index = []
        try:
            with open(SAM_META_PATH, encoding="utf-8") as f:
                sam_entries = int(json.load(f).get("entries", 0))
        except (OSError, ValueError):
            sam_entries = 0
        if not os.path.exists(SDN_INDEX_SAM_PATH):
            with open(SDN_INDEX_SAM_PATH, "w", encoding="utf-8") as f:
                json.dump([], f)

    counts = {}
    for rec in records:
        counts[rec["source"]] = counts.get(rec["source"], 0) + 1
    if sam:
        for rec in sam:
            counts[rec["source"]] = counts.get(rec["source"], 0) + 1
    elif sam_entries:
        counts["SAM"] = sam_entries
    counts["total"] = sum(counts.values())
    counts["index_rows"] = len(index) + len(sam_index)
    with open(SDN_COUNTS_PATH, "w", encoding="utf-8") as f:
        json.dump(counts, f)

    write_change_feeds(log, today)

    def tally(evs):
        return tuple(sum(1 for e in evs if e["action"] == a)
                     for a in ("added", "removed", "modified"))

    added, removed, modified = tally(events)
    c_added, c_removed, c_modified = tally(csl_events)
    print(f"index: {len(index)} rows + {len(sam_index)} SAM rows "
          f"from {counts['total']} entries "
          f"({', '.join(f'{k} {v}' for k, v in sorted(counts.items()) if k not in ('total', 'index_rows'))}). "
          f"SDN today: {added} added, {removed} removed, {modified} modified. "
          f"Non-SDN today: {c_added} added, {c_removed} removed, {c_modified} modified.")


if __name__ == "__main__":
    main()
