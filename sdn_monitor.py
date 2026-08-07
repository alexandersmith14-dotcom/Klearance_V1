"""Tracks day-over-day changes to OFAC's Specially Designated Nationals (SDN)
list — separate from the main regulatory-update tracker in fetcher.py/pipeline.py
because it's a different kind of data: one huge list (~17k entries) that churns
by individual name additions/removals/edits rather than by agencies publishing
notices. No Claude classification here — a name, program tag ("CUBA",
"SDGT", "RUSSIA-EO14024") and action type are self-explanatory the way a
50-page proposed rule is not.

OFAC does publish delta files, but its own guidance (FAQ #90) says database
administrators should do a full-list refresh rather than trust deltas alone —
so that's what this does: download the whole list, diff by ent_num (OFAC's
own stable per-entry ID) against yesterday's snapshot.

Run order: before dashboard.py, so SDN_LOG_PATH is fresh when the dashboard
embeds it.
"""
import csv
import io
import json
import os
from datetime import datetime, timezone

import fetcher

SDN_URL = "https://sanctionslistservice.ofac.treas.gov/api/download/SDN.CSV"

# Committed, not gitignored — this has to persist ACROSS runs, and GitHub
# Actions checks out a fresh repo every run with nothing carried over except
# what's in git. A gitignored snapshot here would mean every single daily run
# sees no prior snapshot and treats itself as the baseline forever, silently
# never diffing anything. (That happened — see the commit that fixed it.)
SDN_SNAPSHOT_PATH = "sdn_snapshot.json"

# Cumulative log of Added/Removed/Modified events, one entry per change.
# Committed — this is the thing with lasting value, an audit trail.
SDN_LOG_PATH = "sdn_log.json"

# Compact, public, current-list search index — see the comment where it's
# written for why it's separate from SDN_SNAPSHOT_PATH. Gitignored, not
# committed: unlike the snapshot, this doesn't need to persist across runs —
# it's fully derivable from `current` at build time, same as sitemap.xml. The
# workflow's "Assemble the published site" step copies it into site/.
SDN_INDEX_PATH = "sdn_index.json"

# Position of each field in OFAC's SDN.CSV — no header row, columns are fixed.
FIELDS = ["ent_num", "name", "sdn_type", "program", "title", "call_sign",
          "vess_type", "tonnage", "grt", "vess_flag", "vess_owner", "remarks"]


def fetch_sdn():
    """Returns {ent_num: {field: value}} for the current published SDN list."""
    text = fetcher.get(SDN_URL, timeout=120)
    reader = csv.reader(io.StringIO(text), skipinitialspace=True)
    rows = {}
    for row in reader:
        # A trailing DOS EOF marker (0x1A) shows up as its own bogus "row" at
        # the end of the file — not whitespace, so a plain truthiness/strip
        # check lets it through. Only real entries have a numeric ent_num.
        if not row or not row[0].strip().isdigit():
            continue
        row = (row + [""] * len(FIELDS))[:len(FIELDS)]
        rec = {FIELDS[i]: row[i].strip() for i in range(len(FIELDS))}
        ent_num = rec.pop("ent_num")
        # OFAC's own "no data" placeholder — normalise so it never
        # masquerades as a real field value.
        for k, v in rec.items():
            if v == "-0-":
                rec[k] = ""
        rows[ent_num] = rec
    return rows


def diff(previous, current, today):
    """Returns a list of change events: Added / Removed / Modified."""
    events = []
    prev_ids, cur_ids = set(previous), set(current)

    for ent_num in sorted(cur_ids - prev_ids, key=int):
        rec = current[ent_num]
        events.append({"date": today, "ent_num": ent_num, "action": "added",
                        "name": rec["name"], "program": rec["program"],
                        "sdn_type": rec["sdn_type"]})

    for ent_num in sorted(prev_ids - cur_ids, key=int):
        rec = previous[ent_num]
        events.append({"date": today, "ent_num": ent_num, "action": "removed",
                        "name": rec["name"], "program": rec["program"],
                        "sdn_type": rec["sdn_type"]})

    for ent_num in sorted(prev_ids & cur_ids, key=int):
        if previous[ent_num] != current[ent_num]:
            events.append({"date": today, "ent_num": ent_num, "action": "modified",
                            "name": current[ent_num]["name"],
                            "program": current[ent_num]["program"],
                            "sdn_type": current[ent_num]["sdn_type"]})

    return events


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    current = fetch_sdn()

    if os.path.exists(SDN_SNAPSHOT_PATH):
        with open(SDN_SNAPSHOT_PATH, encoding="utf-8") as f:
            previous = json.load(f)
        events = diff(previous, current, today)
    else:
        # First-ever run — every entry would show as "Added" against an empty
        # snapshot, which is noise, not news. Establish the baseline instead.
        events = []
        print(f"No prior snapshot — treating today's {len(current)} entries "
              f"as the baseline. Changes will start showing from tomorrow's run.")

    with open(SDN_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f)

    # A separate, compact public file for searching the CURRENT full list (as
    # opposed to SDN_LOG_PATH, which only covers day-over-day changes). Array-
    # of-arrays rather than the snapshot's full per-entry dicts, and drops the
    # bulky vessel/tonnage/remarks fields nothing here uses — name + program +
    # type is what a reader searches by. Lazy-loaded by the dashboard on first
    # use, not embedded in the page itself, so it can't bloat initial page
    # weight the way the update cards used to before that got fixed.
    index = [[num, r["name"], r["sdn_type"], r["program"]]
             for num, r in current.items()]
    with open(SDN_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f)

    # Always write the log, even with zero new events — the workflow's commit
    # step does an unconditional `git add sdn_log.json`, which needs the file
    # to exist from run one (a quiet first day is real data too, not nothing).
    log = []
    if os.path.exists(SDN_LOG_PATH):
        with open(SDN_LOG_PATH, encoding="utf-8") as f:
            log = json.load(f)
    log.extend(events)
    with open(SDN_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)

    added = sum(1 for e in events if e["action"] == "added")
    removed = sum(1 for e in events if e["action"] == "removed")
    modified = sum(1 for e in events if e["action"] == "modified")
    print(f"SDN list: {len(current)} entries — "
          f"{added} added, {removed} removed, {modified} modified today")


if __name__ == "__main__":
    main()
