"""One-time backfill: seed sdn_log.json with real historical SDN changes from
before daily diffing existed (see the commit that fixed sdn_snapshot.json
persistence — everything before that never actually diffed against anything).

The raw SDN.CSV has no per-entry date, so this can't come from the same
pipeline as sdn_monitor.py. It comes from a different OFAC source instead:
Federal Register "Notice of OFAC Sanctions Action(s)" documents, which are
dated, structured, and — importantly — state the REAL action date, which is
often months earlier than the notice's publication date (OFAC can batch-
publish). Confirmed against a real example while building this: a notice
published 2026-08-06 stated "This action was issued on April 23, 2026."

Not run as part of the daily workflow. Run by hand:

    python sdn_backfill.py --review     # print candidates, write nothing
    python sdn_backfill.py --commit     # append reviewed candidates to sdn_log.json

Always run --review first and read the output. Notice formatting varies
enough across documents (a removal notice's entity/aircraft table stripped to
garbage on the first one inspected while building this) that a bad parse is a
real risk, and a wrong name in a compliance tool's change log is worse than a
missing one.
"""
import argparse
import json
import re
import urllib.parse
import urllib.request

FR_API = "https://www.federalregister.gov/api/v1/documents.json"
UA = {"User-Agent": "RegWatch/1.0"}
SDN_LOG_PATH = "sdn_log.json"

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

ACTION_DATE_RE = re.compile(r"issued on ([A-Z][a-z]+) (\d{1,2}), (\d{4})")
ITEM_START_RE = re.compile(r"(?:^|\s)(\d{1,3})\.\s+(?=[A-Z“\"])")
BRACKET_RE = re.compile(r"\[([A-Z][A-Z0-9\-]*)\]")


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                 timeout=30) as r:
        return r.read().decode("utf-8", "ignore")


def strip_html(html):
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&mdash;", "-").replace("&ldquo;", '"')
            .replace("&rdquo;", '"').replace("&amp;", "&")
            .replace("&#8203;", ""))
    return re.sub(r"\s+", " ", text).strip()


def top_level_split(text, sep):
    """Split on `sep` only outside parentheses — a plain .split(sep) breaks on
    entries like 'NAME (a.k.a. X; a.k.a. Y), City' where the separator also
    appears inside an alias list."""
    depth, parts, cur = 0, [], []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def extract_name(entry_text):
    # Prefer cutting right before the alias list — that's the cleanest
    # boundary and matches the real SDN.CSV name field almost exactly
    # (confirmed against AL-SHABBANI's actual CSV record while building
    # this). Falls back to the first top-level semicolon, then a hard
    # truncation as a last resort so nothing ever renders as a wall of text.
    aka = entry_text.find("(a.k.a")
    if aka != -1:
        return entry_text[:aka].strip().rstrip(",")
    parts = top_level_split(entry_text, ";")
    if parts and parts[0].strip():
        return parts[0].strip()
    return entry_text[:80].strip()


def extract_entries(text):
    """Yields (name, program) for each numbered list item in the notice
    body. Program is every bracket tag found in that item, joined — Federal
    Register entries often carry more than one (e.g. [SDGT] [ILLICIT-DRUGS-
    EO14059]), unlike the CSV's single Program field."""
    starts = [m.start() for m in ITEM_START_RE.finditer(text)]
    starts.append(len(text))
    for i in range(len(starts) - 1):
        chunk = text[starts[i]:starts[i + 1]]
        tags = BRACKET_RE.findall(chunk)
        if not tags:
            continue  # not an SDN entry item — narrative text, footer, etc.
        name = extract_name(ITEM_START_RE.sub("", chunk, count=1))
        if len(name) < 3 or len(name) > 200:
            continue  # parse clearly went wrong; skip rather than guess
        # "Modified" entries print the OLD record then "-TO-" then the NEW
        # one in full, so the same tag appears twice in one chunk — dedupe
        # while keeping order, rather than showing "[SDGT SDGT]".
        yield name, " ".join(dict.fromkeys(tags))


# A single notice commonly covers more than one action — confirmed on a real
# document (2026-15785) that both removed one person AND added two others in
# the same filing. Classifying per-notice from the SUMMARY line mislabeled
# every entry with whichever action the summary happened to mention first.
# These three lead-in sentences mark where each action's own entry list
# starts, so entries get classified by which section they actually fall in.
SECTION_TRIGGERS = [
    ("removed", re.compile(r"are unblocked under the relevant sanctions? authorit")),
    ("modified", re.compile(r"continue to be blocked under the relevant sanctions? authorit")),
    ("added", re.compile(r"(?<!continue to )are blocked under the relevant sanctions? authorit")),
]


def section_entries(text):
    """Yields (action, name, program) by splitting the body at each action's
    lead-in sentence and only extracting entries from within that section."""
    hits = []
    for action, pattern in SECTION_TRIGGERS:
        for m in pattern.finditer(text):
            hits.append((m.start(), action))
    hits.sort()
    if not hits:
        return
    hits.append((len(text), None))
    for i in range(len(hits) - 1):
        start, action = hits[i]
        end = hits[i + 1][0]
        for name, program in extract_entries(text[start:end]):
            yield action, name, program


def action_date(text, fallback):
    m = ACTION_DATE_RE.search(text)
    if not m:
        return fallback
    month, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    return f"{year:04d}-{MONTHS[month]:02d}-{day:02d}"


def fetch_candidates(lookback_docs=20):
    # Built by hand rather than urlencode() — the API needs several repeated
    # fields[]= keys, which urlencode's dict form can't express.
    qs = (
        "conditions%5Bagencies%5D%5B%5D=foreign-assets-control-office"
        "&conditions%5Bterm%5D=" + urllib.parse.quote("Notice of OFAC Sanctions Action")
        + f"&per_page={lookback_docs}&order=newest"
        "&fields%5B%5D=title&fields%5B%5D=publication_date"
        "&fields%5B%5D=document_number&fields%5B%5D=body_html_url"
    )
    index = json.loads(get(f"{FR_API}?{qs}"))

    events = []
    for doc in index["results"]:
        try:
            html = get(doc["body_html_url"])
        except Exception as e:
            print(f"  skip {doc['document_number']}: fetch failed ({e})")
            continue
        text = strip_html(html)
        date = action_date(text, doc["publication_date"])
        entries = list(section_entries(text))
        if not entries:
            print(f"  skip {doc['document_number']} ({doc['publication_date']}): "
                  f"no parseable entries found")
            continue
        counts = {}
        for action, name, program in entries:
            events.append({
                "date": date, "ent_num": f"fr-{doc['document_number']}",
                "action": action, "name": name, "program": program,
                "sdn_type": "",
            })
            counts[action] = counts.get(action, 0) + 1
        summary = ", ".join(f"{n} {a}" for a, n in counts.items())
        print(f"  {doc['document_number']} ({doc['publication_date']}, "
              f"action date {date}): {summary}")
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                     help="append to sdn_log.json (default: review only, write nothing)")
    ap.add_argument("--lookback", type=int, default=20,
                     help="how many recent Federal Register OFAC notices to scan")
    args = ap.parse_args()

    print(f"Scanning the {args.lookback} most recent OFAC Federal Register notices...")
    events = fetch_candidates(args.lookback)

    print(f"\n{len(events)} candidate events extracted:\n")
    for e in sorted(events, key=lambda e: e["date"], reverse=True):
        print(f'  {e["date"]}  {e["action"]:8s}  {e["name"][:60]:60s}  [{e["program"]}]')

    if not args.commit:
        print("\n--review only, nothing written. Re-run with --commit to append these "
              "to sdn_log.json once they look right.")
        return

    log = []
    with open(SDN_LOG_PATH, encoding="utf-8") as f:
        log = json.load(f)
    existing = {(e["date"], e["ent_num"]) for e in log}
    new = [e for e in events if (e["date"], e["ent_num"]) not in existing]
    log.extend(new)
    with open(SDN_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)
    print(f"\nAppended {len(new)} new events to {SDN_LOG_PATH} "
          f"({len(events) - len(new)} already present, skipped).")


if __name__ == "__main__":
    main()
