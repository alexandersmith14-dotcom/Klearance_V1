#!/usr/bin/env python3
"""Regenerate ebrd_snapshot.json from the EBRD Ineligible Entities list.

RUN THIS BY HAND, from a residential connection - NOT in CI. www.ebrd.com
gates its /bin/ebrd_dxp/filterlistservlet endpoint on the client's TLS/JA3
fingerprint: a plain urllib/requests POST gets a 403 or a 500, an impersonated
Chrome gets the JSON. curl_cffi is therefore a dependency of THIS script only
and is deliberately kept out of requirements.txt - sdn_monitor.fetch_ebrd()
just reads the committed snapshot.

    pip install curl_cffi
    python make_ebrd_snapshot.py            # writes ebrd_snapshot.json + _meta.json
    git add ebrd_snapshot.json ebrd_snapshot_meta.json && git commit

The list is ~1,280 rows, ~90% cross-debarments already carried by the World
Bank / ADB / AfDB feeds; EBRD's own ~130 primary debarments are the reason to
keep it. It moves slowly - a refresh every few months is plenty.
"""
import json
import re
import sys
import time
from datetime import datetime, timezone

SERVLET = "https://www.ebrd.com/bin/ebrd_dxp/filterlistservlet"
REFERER = ("https://www.ebrd.com/home/who-we-are/strategies-governance-compliance/"
           "ebrd-sanctions-system/ineligible-entities.html")
PARENT_PATH = "/content/dam/ebrd_dxp/content-fragments/occo/ineligible-entities"
SNAPSHOT_PATH = "ebrd_snapshot.json"
META_PATH = "ebrd_snapshot_meta.json"
REMARKS_CAP = 600


def _body(page):
    fields = {
        "cardType": "iecard", "currentPage": str(page), "parentPath": PARENT_PATH,
        "sortBy": "newest-first", "IsLoggedIn": "false", "isAlumni": "false",
        "isBeeps": "false", "countryFilters": "", "endDate": "", "eventSort": "",
        "filters": "", "noticeTypeFilters": "", "pageTypeFilters": "",
        "searchKey": "", "sectorFilters": "", "startDate": "", "statusFilters": "",
        "topicFilters": "",
    }
    return "&".join(f"{k}={v}" for k, v in fields.items())


def _record(it):
    name = (it.get("title") or "").strip()
    ntype = (it.get("projectNoticeType") or "").strip().lower()
    origin = (it.get("originatingInstitution") or "").strip()
    primary = origin == "" or re.search(r"primary", origin, re.I) is not None
    program = "EBRD " + ("debarred" if primary else "cross-debarred")
    remarks = "; ".join(x for x in [
        program,
        f"practice: {it['prohibitedPractice']}" if it.get("prohibitedPractice") else "",
        f"from {it['ineligibleFromDate']}" if it.get("ineligibleFromDate") else "",
        f"to {it['ineligibleUntilDate']}" if it.get("ineligibleUntilDate") else "",
        f"EBRD notice effective {it['dateNoticeEffectiveAtEBRD']}"
        if it.get("dateNoticeEffectiveAtEBRD") else "",
        origin if (origin and not primary) else "",
        f"addr: {it['address']}" if it.get("address") else "",
    ] if x)
    return {
        "key": "ebrd" + re.sub(r"[^a-z0-9]+", "",
                               (name + (it.get("ineligibleFromDate") or "")).lower())[:48],
        "name": name,
        "type": {"individual": "individual", "firm": "entity"}.get(ntype, ""),
        "program": program,
        "source": "EBRD",
        "remarks": remarks[:REMARKS_CAP],
        "country": (it.get("nationality") or "").strip(),
        "aliases": [],
    }


def fetch_page(sess, page):
    for attempt in range(4):
        r = sess.post(SERVLET, data=_body(page),
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Referer": REFERER},
                      impersonate="chrome124", timeout=40)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                pass
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"EBRD page {page}: {r.status_code} after retries")


def main():
    try:
        from curl_cffi import requests as creq
    except ImportError:
        sys.exit("need curl_cffi: pip install curl_cffi  (kept out of requirements.txt on purpose)")

    sess = creq.Session()
    first = fetch_page(sess, 1)
    total = first["resultCount"][0]["resultCount"]
    per_page = len(first["searchResult"]) or 12
    pages = -(-total // per_page)
    print(f"EBRD: {total} rows over {pages} pages")

    raw = list(first["searchResult"])
    for p in range(2, pages + 1):
        raw.extend(fetch_page(sess, p)["searchResult"])
        if p % 20 == 0:
            print(f"  ...{p}/{pages}")
        time.sleep(0.15)

    if len(raw) < total * 0.98:
        sys.exit(f"only got {len(raw)}/{total} rows - aborting, not overwriting the snapshot")

    records, seen = [], set()
    for it in raw:
        rec = _record(it)
        if not rec["name"] or rec["key"] in seen:
            continue
        seen.add(rec["key"])
        records.append(rec)

    primaries = sum(1 for r in records if r["program"] == "EBRD debarred")
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump({"refreshed_at": datetime.now(timezone.utc).date().isoformat(),
                   "rows": len(records), "primary_debarments": primaries}, f, indent=1)
    print(f"wrote {SNAPSHOT_PATH}: {len(records)} records ({primaries} EBRD-primary)")


if __name__ == "__main__":
    main()
