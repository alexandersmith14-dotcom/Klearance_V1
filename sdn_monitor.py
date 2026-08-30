"""Tracks OFAC sanctions lists — day-over-day changes to the Specially
Designated Nationals (SDN) list, plus a combined name-screening index that
covers SDN, SDN aliases, the OFAC Consolidated (non-SDN) list and its
aliases, with per-entry detail (remarks, country) for disambiguation.

Separate from the main regulatory-update tracker in fetcher.py/pipeline.py
because it's a different kind of data: huge name lists that churn by
individual additions/removals/edits rather than by agencies publishing
notices. No Claude classification here.

OFAC publishes delta files, but its own guidance (FAQ #90) says to do a
full-list refresh rather than trust deltas alone — so that's what this
does: download the whole list and diff by ent_num (OFAC's stable per-entry
ID) against yesterday's snapshot.

Run order: before dashboard.py, so the log/index/feeds are fresh when the
dashboard embeds and the workflow publishes them.
"""
import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape as xml_escape

import fetcher

BASE = "https://sanctionslistservice.ofac.treas.gov/api/download"
SDN_URL = f"{BASE}/SDN.CSV"
SDN_ALT_URL = f"{BASE}/ALT.CSV"
SDN_ADD_URL = f"{BASE}/ADD.CSV"
CONS_URL = f"{BASE}/CONS_PRIM.CSV"
CONS_ALT_URL = f"{BASE}/CONS_ALT.CSV"
CONS_ADD_URL = f"{BASE}/CONS_ADD.CSV"

# Committed — must persist ACROSS runs. GitHub Actions checks out a fresh
# repo every run; a gitignored snapshot would make every run treat itself
# as the baseline and never diff.
SDN_SNAPSHOT_PATH = "sdn_snapshot.json"
CSL_SNAPSHOT_PATH = "csl_snapshot.json"

# Cumulative log of SDN Added/Removed/Modified events. Committed — the audit
# trail is the thing with lasting value.
SDN_LOG_PATH = "sdn_log.json"

# Compact public search index over the CURRENT lists. Gitignored (fully
# derivable at build time); the workflow copies it into site/.
SDN_INDEX_PATH = "sdn_index.json"

# RSS + CSV of recent SDN changes, for periodic re-screening. Gitignored;
# copied into site/ by the workflow.
CHANGES_RSS_PATH = "sdn-changes.xml"
CHANGES_CSV_PATH = "sdn-changes.csv"
CHANGES_WINDOW_DAYS = 120

SITE_BASE = "https://kaufman2699.github.io/Klearance_V1"

# Column order in OFAC's primary CSVs (SDN.CSV and CONS_PRIM.CSV share it).
# No header row; columns are fixed.
FIELDS = ["ent_num", "name", "sdn_type", "program", "title", "call_sign",
          "vess_type", "tonnage", "grt", "vess_flag", "vess_owner", "remarks"]

REMARKS_CAP = 600  # keep detail useful without bloating the index


def _rows(text):
    reader = csv.reader(io.StringIO(text), skipinitialspace=True)
    for row in reader:
        # A trailing DOS EOF marker (0x1A) shows up as a bogus row; only real
        # entries have a numeric ent_num in column 0.
        if row and row[0].strip().isdigit():
            yield row


def fetch_primary(url):
    """Returns {ent_num: {field: value}} for a primary list (SDN or CONS)."""
    out = {}
    for row in _rows(fetcher.get(url, timeout=120)):
        row = (row + [""] * len(FIELDS))[:len(FIELDS)]
        rec = {FIELDS[i]: row[i].strip() for i in range(len(FIELDS))}
        ent_num = rec.pop("ent_num")
        for k, v in rec.items():
            if v == "-0-":            # OFAC's "no data" placeholder
                rec[k] = ""
        out[ent_num] = rec
    return out


def fetch_alt(url):
    """Returns {ent_num: [alt_name, ...]} from ALT.CSV / CONS_ALT.CSV.
    Columns: ent_num, alt_num, alt_type, alt_name, alt_remarks."""
    out = {}
    for row in _rows(fetcher.get(url, timeout=120)):
        if len(row) < 4:
            continue
        ent_num, name = row[0].strip(), row[3].strip()
        if name and name != "-0-":
            out.setdefault(ent_num, [])
            if name not in out[ent_num]:
                out[ent_num].append(name)
    return out


def fetch_add(url):
    """Returns {ent_num: country} from ADD.CSV / CONS_ADD.CSV — first
    non-empty country wins. Columns: ent_num, add_num, address,
    city_state_province_postal, country, add_remarks."""
    out = {}
    for row in _rows(fetcher.get(url, timeout=120)):
        if len(row) < 5:
            continue
        ent_num, country = row[0].strip(), row[4].strip()
        if country and country != "-0-" and ent_num not in out:
            out[ent_num] = country
    return out


def diff(previous, current, today):
    """SDN change events: Added / Removed / Modified."""
    events = []
    prev_ids, cur_ids = set(previous), set(current)
    for ent_num in sorted(cur_ids - prev_ids, key=int):
        r = current[ent_num]
        events.append({"date": today, "ent_num": ent_num, "action": "added",
                       "name": r["name"], "program": r["program"], "sdn_type": r["sdn_type"]})
    for ent_num in sorted(prev_ids - cur_ids, key=int):
        r = previous[ent_num]
        events.append({"date": today, "ent_num": ent_num, "action": "removed",
                       "name": r["name"], "program": r["program"], "sdn_type": r["sdn_type"]})
    for ent_num in sorted(prev_ids & cur_ids, key=int):
        if previous[ent_num] != current[ent_num]:
            r = current[ent_num]
            events.append({"date": today, "ent_num": ent_num, "action": "modified",
                           "name": r["name"], "program": r["program"], "sdn_type": r["sdn_type"]})
    return events


def build_index(sdn, sdn_alt, sdn_country, csl, csl_alt, csl_country):
    """One flat search index across both lists. Row shape:
    [ent_num, primary_name, sdn_type, program, source, is_alias, remarks, country, alias_name]
    - source: "SDN" | "Non-SDN"
    - is_alias: 0 for the primary-name row, 1 for an a.k.a. row (same ent_num)
    - alias_name: "" on a primary row; the a.k.a. string on an alias row
    """
    rows = []

    def add_list(primary, alts, countries, source):
        for num, r in primary.items():
            remarks = (r.get("remarks") or "")[:REMARKS_CAP]
            country = countries.get(num, "")
            rows.append([num, r["name"], r["sdn_type"], r["program"], source, 0, remarks, country, ""])
            for alt_name in alts.get(num, []):
                rows.append([num, r["name"], r["sdn_type"], r["program"], source, 1, remarks, country, alt_name])

    add_list(sdn, sdn_alt, sdn_country, "SDN")
    add_list(csl, csl_alt, csl_country, "Non-SDN")
    return rows


def write_change_feeds(log, today):
    cutoff = (datetime.fromisoformat(today) - timedelta(days=CHANGES_WINDOW_DAYS)).date().isoformat()
    recent = [e for e in log if e["date"] >= cutoff]
    recent.sort(key=lambda e: (e["date"], e["ent_num"]), reverse=True)

    # CSV
    with open(CHANGES_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "action", "ent_num", "name", "program", "sdn_type"])
        for e in recent:
            w.writerow([e["date"], e["action"], e["ent_num"], e["name"],
                        e["program"], e.get("sdn_type", "")])

    # RSS 2.0 — one item per change, newest first.
    def item(e):
        title = f'{e["action"].title()}: {e["name"] or "(unnamed entry)"}'
        desc = f'{e["action"].title()} on the OFAC SDN list. Program: {e["program"] or "n/a"}.'
        link = f'https://sanctionssearch.ofac.treas.gov/Details.aspx?id={e["ent_num"]}'
        try:
            dt = datetime.fromisoformat(e["date"]).replace(tzinfo=timezone.utc)
        except ValueError:
            dt = datetime.now(timezone.utc)
        return (f"    <item>\n"
                f"      <title>{xml_escape(title)}</title>\n"
                f"      <description>{xml_escape(desc)}</description>\n"
                f"      <link>{xml_escape(link)}</link>\n"
                f"      <guid isPermaLink=\"false\">sdn-{e['date']}-{e['ent_num']}-{e['action']}</guid>\n"
                f"      <pubDate>{format_datetime(dt)}</pubDate>\n"
                f"    </item>")

    now = format_datetime(datetime.now(timezone.utc))
    xml = (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
           f"<rss version=\"2.0\"><channel>\n"
           f"    <title>Klearance — OFAC SDN list changes</title>\n"
           f"    <link>{SITE_BASE}/</link>\n"
           f"    <description>Daily additions, removals and modifications on the "
           f"OFAC Specially Designated Nationals list.</description>\n"
           f"    <language>en-us</language>\n"
           f"    <lastBuildDate>{now}</lastBuildDate>\n"
           + "\n".join(item(e) for e in recent[:200])
           + "\n</channel></rss>\n")
    with open(CHANGES_RSS_PATH, "w", encoding="utf-8") as f:
        f.write(xml)


def main():
    today = datetime.now(timezone.utc).date().isoformat()

    sdn = fetch_primary(SDN_URL)
    sdn_alt = fetch_alt(SDN_ALT_URL)
    sdn_country = fetch_add(SDN_ADD_URL)
    csl = fetch_primary(CONS_URL)
    csl_alt = fetch_alt(CONS_ALT_URL)
    csl_country = fetch_add(CONS_ADD_URL)

    # --- SDN diff + audit log (unchanged behaviour) ---
    if os.path.exists(SDN_SNAPSHOT_PATH):
        with open(SDN_SNAPSHOT_PATH, encoding="utf-8") as f:
            previous = json.load(f)
        events = diff(previous, sdn, today)
    else:
        events = []
        print(f"No prior SDN snapshot — {len(sdn)} entries taken as the baseline. "
              f"Changes show from tomorrow's run.")

    with open(SDN_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(sdn, f)
    with open(CSL_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(csl, f)

    log = []
    if os.path.exists(SDN_LOG_PATH):
        with open(SDN_LOG_PATH, encoding="utf-8") as f:
            log = json.load(f)
    log.extend(events)
    with open(SDN_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)

    # --- combined screening index ---
    index = build_index(sdn, sdn_alt, sdn_country, csl, csl_alt, csl_country)
    with open(SDN_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f)

    # --- recent-changes RSS + CSV ---
    write_change_feeds(log, today)

    added = sum(1 for e in events if e["action"] == "added")
    removed = sum(1 for e in events if e["action"] == "removed")
    modified = sum(1 for e in events if e["action"] == "modified")
    print(f"SDN {len(sdn)} + Non-SDN {len(csl)} entries; index rows {len(index)} "
          f"(incl. {sum(1 for r in index if r[5])} aliases). "
          f"Today: {added} added, {removed} removed, {modified} modified.")


if __name__ == "__main__":
    main()
