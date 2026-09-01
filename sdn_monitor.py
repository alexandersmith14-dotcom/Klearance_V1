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


def diff(previous, current, today):
    events = []
    prev, cur = set(previous), set(current)
    for num in sorted(cur - prev, key=int):
        r = current[num]
        events.append({"date": today, "ent_num": num, "action": "added",
                       "name": r["name"], "program": r["program"], "sdn_type": r["sdn_type"]})
    for num in sorted(prev - cur, key=int):
        r = previous[num]
        events.append({"date": today, "ent_num": num, "action": "removed",
                       "name": r["name"], "program": r["program"], "sdn_type": r["sdn_type"]})
    for num in sorted(prev & cur, key=int):
        if previous[num] != current[num]:
            r = current[num]
            events.append({"date": today, "ent_num": num, "action": "modified",
                           "name": r["name"], "program": r["program"], "sdn_type": r["sdn_type"]})
    return events


def write_change_feeds(log, today):
    cutoff = (datetime.fromisoformat(today) - timedelta(days=CHANGES_WINDOW_DAYS)).date().isoformat()
    recent = sorted((e for e in log if e["date"] >= cutoff),
                    key=lambda e: (e["date"], e["ent_num"]), reverse=True)
    with open(CHANGES_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "action", "ent_num", "name", "program", "sdn_type"])
        for e in recent:
            w.writerow([e["date"], e["action"], e["ent_num"], e["name"],
                        e["program"], e.get("sdn_type", "")])

    def item(e):
        link = f'https://sanctionssearch.ofac.treas.gov/Details.aspx?id={e["ent_num"]}'
        try:
            dt = datetime.fromisoformat(e["date"]).replace(tzinfo=timezone.utc)
        except ValueError:
            dt = datetime.now(timezone.utc)
        return (f"    <item>\n"
                f"      <title>{xml_escape(e['action'].title() + ': ' + (e['name'] or '(unnamed)'))}</title>\n"
                f"      <description>{xml_escape(e['action'].title() + ' on the OFAC SDN list. Program: ' + (e['program'] or 'n/a') + '.')}</description>\n"
                f"      <link>{xml_escape(link)}</link>\n"
                f"      <guid isPermaLink=\"false\">sdn-{e['date']}-{e['ent_num']}-{e['action']}</guid>\n"
                f"      <pubDate>{format_datetime(dt)}</pubDate>\n"
                f"    </item>")

    xml = (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<rss version=\"2.0\"><channel>\n"
           f"    <title>Klearance - OFAC SDN list changes</title>\n"
           f"    <link>{SITE_BASE}/</link>\n"
           f"    <description>Daily additions, removals and modifications on the OFAC "
           f"Specially Designated Nationals list.</description>\n"
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
    sam = safe("SAM.gov Exclusions", fetch_sam)

    # --- SDN diff + audit log (unchanged) ---
    if os.path.exists(SDN_SNAPSHOT_PATH):
        with open(SDN_SNAPSHOT_PATH, encoding="utf-8") as f:
            events = diff(json.load(f), sdn, today)
    else:
        events = []
        print(f"  No prior SDN snapshot - {len(sdn)} taken as baseline.")
    with open(SDN_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(sdn, f)
    with open(CSL_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(csl, f)
    log = json.load(open(SDN_LOG_PATH, encoding="utf-8")) if os.path.exists(SDN_LOG_PATH) else []
    log.extend(events)
    with open(SDN_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)

    # --- combined screening index ---
    # SAM.gov alone is ~4x the size of the other seven lists put together, so it
    # goes in its own file the browser fetches only after the fast index is in
    # hand and searchable. Same row shape in both.
    records = (
        list(ofac_records(sdn, sdn_alt, sdn_ctry, "SDN"))
        + list(ofac_records(csl, csl_alt, csl_ctry, "Non-SDN"))
        + bis_state + un + uk + eu
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

    added = sum(1 for e in events if e["action"] == "added")
    removed = sum(1 for e in events if e["action"] == "removed")
    modified = sum(1 for e in events if e["action"] == "modified")
    print(f"index: {len(index)} rows + {len(sam_index)} SAM rows "
          f"from {counts['total']} entries "
          f"({', '.join(f'{k} {v}' for k, v in sorted(counts.items()) if k not in ('total', 'index_rows'))}). "
          f"SDN today: {added} added, {removed} removed, {modified} modified.")


if __name__ == "__main__":
    main()
