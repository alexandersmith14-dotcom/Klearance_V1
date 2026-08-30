"""Generate a self-contained HTML dashboard from store.json.

    python dashboard.py            # writes dashboard.html
    python dashboard.py --open     # ...and opens it

No server, no dependencies, no network calls. Regenerate after each pipeline run.

Design notes:
  * Urgency and deadline proximity are shown as colour + text label. The status
    yellow sits below 3:1 contrast on a light surface, so colour alone would not
    be readable for everyone; the label is the mitigation, not decoration.
  * "Updates by agency" is one hue against a muted track (magnitude comparison),
    not a multi-colour categorical set — the agencies aren't competing series.
  * Dark mode is declared under both the OS media query and the data-theme
    scope, so a manual toggle wins in both directions.
"""
import argparse
import hashlib
import html
import json
# main() binds a local named `html` for the page string, which shadows the module
# there. Alias the escaper so it stays reachable inside main.
from html import escape as hesc
import os
import re
import webbrowser
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import fetcher
import regref

# Kaufman Rossin is Florida-based; the "Updated" stamp reads in the firm's
# own time zone rather than UTC. ZoneInfo (not a fixed UTC-4/-5 offset) so it
# tracks EST/EDT automatically across the DST boundary.
EASTERN = ZoneInfo("America/New_York")

# "Ask" is parked. Nothing is broken and nothing was deleted — set ASK_ENABLED
# back to True and the box returns exactly as it was.
#
# Parked 2026-07-21 on evidence. A bake-off of free models turned up the failure
# this feature cannot afford: the source text of 12 CFR 1002.9 carries only the
# subsection markers (a)(2)(i) and (a)(2)(ii), but the answering model cited
# (iii) through (vi) and attached one to each element of the notice, and the
# reconciler then in use (gemma-4-26b) restated all six as fact in its main list
# — a hallucination given citation-level precision. Two other free models
# quarantined and flagged it, so this is model quality, not a design fault.
#
# Scoping the box to the tracked updates removed that specific failure (no CFR
# text in, no subsections to invent) and measured well. It is parked anyway:
# model-written prose under a CRCM's name is a liability posture to take
# deliberately rather than by default, which is the same reasoning that already
# keeps RegAssistant out of this repo.
#
# Preserved and still working: the Worker and its keys, the reconciler order
# measured in the bake-off, corpus.json, ecfr_corpus.py, the in-browser BM25
# retrieval and the whole client path. Unparking is this one flag.
#
# ASK_INCLUDE_REGULATIONS stays False independently. Before setting it True,
# upgrade the ANSWERERS and re-run the bake-off — the fabrication came from an
# answerer reading CFR text, and a better reconciler catches rather than
# prevents it.
#
# The plain keyword search box is a different thing entirely — no model, no
# network — and is unaffected by either flag.
ASK_ENABLED = False
ASK_INCLUDE_REGULATIONS = False

# Where a reader lands when they click a source name. Human pages, deliberately
# NOT the URLs fetcher.py uses — most of those are raw RSS and would drop a
# reader into a wall of XML.
#
# Six agencies here publish through two feeds. The primary one links to the
# agency home page, which effectively never moves; the secondary one links to its
# specific index, because "FDIC FILs" pointing at fdic.gov would say nothing about
# what that feed actually is. Durability where it costs nothing, specificity where
# it earns its keep.
#
# Two notes for whoever revisits these. The OCC reorganised its site, so the old
# /news-issuances/... paths now soft-404 — they return 200 and redirect to a 404
# page, which a status check alone would pass. And consumerfinance.gov returns
# 403 to scripts while serving browsers normally, so a failed command-line check
# there is not evidence of a dead link; both CFPB links were confirmed in a real
# browser.
SOURCE_LINKS = {
    # Primary feed -> agency home page.
    "FDIC": "https://www.fdic.gov/",
    "OCC": "https://www.occ.gov/",
    "Federal Reserve": "https://www.federalreserve.gov/",
    "CFPB": "https://www.consumerfinance.gov/",
    "FinCEN": "https://www.fincen.gov/",
    "NCUA": "https://ncua.gov/",
    "OFAC": "https://ofac.treasury.gov/",
    "CSBS": "https://www.csbs.org/",
    # Secondary feed -> the specific listing it is named after.
    "FDIC FILs": "https://www.fdic.gov/news/financial-institution-letters",
    "OCC Bulletins": "https://www.occ.gov/news-events/newsroom/?nr=Bulletin",
    "Fed SR/CA Letters":
        "https://www.federalreserve.gov/supervisionreg/srletters/srletters.htm",
    "CFPB Rules": "https://www.consumerfinance.gov/rules-policy/final-rules/",
    "FinCEN Advisories": "https://www.fincen.gov/resources/advisoriesbulletinsfact-sheets",
    "NCUA Press": "https://ncua.gov/news/press-releases",
    # State regulators.
    "FL OFR Press": "https://flofr.gov/news/press-releases",
    "TX Dept of Banking": "https://www.dob.texas.gov/news-and-events/industry-notices",
}

# Every cite in regref.py is a part of title 12, given as "12 CFR 215",
# "CFPB 1002" or "Fed 228" — the trailing number is the part either way.
CFR_PART = re.compile(r"(\d{3,4})\s*$")


def ecfr_url(cfr):
    """eCFR link for a regref cite, or None if no part number is present."""
    m = CFR_PART.search(cfr)
    return f"https://www.ecfr.gov/current/title-12/part-{m.group(1)}" if m else None

STORE_PATH = "store.json"
OUT_PATH = "dashboard.html"
# Subscribable calendar feed of every upcoming deadline. Built here, deployed by
# the workflow, and refreshed each daily run — a reader subscribes once and new
# comment periods appear in their own calendar automatically, with reminders.
# Derived output, never committed (see .gitignore).
ICS_PATH = "deadlines.ics"

# Cumulative log of OFAC SDN list changes, written by sdn_monitor.py. Read-only
# here — this file's lifecycle (fetch, diff, snapshot) lives entirely in that
# script, not in the dashboard build.
SDN_LOG_PATH = "sdn_log.json"

# Read-only here too — only used to report the current full-list count next
# to the "search the full list" box. The actual per-entry data for that
# search comes from sdn_index.json (sdn_monitor.py SDN_INDEX_PATH), fetched
# lazily by the browser, not from this file.
SDN_SNAPSHOT_PATH = "sdn_snapshot.json"

# Absolute URL of the published site. Social scrapers require absolute URLs for
# og:image and og:url — a relative path silently produces no preview.
SITE_URL = "https://kaufman2699.github.io/Klearance_V1/"

# One permalink page per relevant update, so each plain-English summary gets
# its own indexable URL instead of living only inside a client-side "show
# more" list. Derived output, regenerated every build — never committed (see
# .gitignore), same treatment as deadlines.ics.
UPDATES_DIR = "updates"
SITEMAP_PATH = "sitemap.xml"

# Kaufman Rossin brand.
# Navy #003B6A and green #AED136 are taken from kaufmanrossin.com, along with
# its heading grey #3C3C3C and body ink #212529.
#
# The green is used ONLY as a solid accent block (the header bar, panel rules)
# with dark text on top - never for text, thin marks, or anything that carries
# meaning by colour. It measures 1.75:1 against white, well under the 3:1 floor,
# so as a data colour it would be invisible to a lot of readers. That is how the
# firm's own site uses it too.
#
# Navy is too dark to sit on a dark background, so dark mode uses a lighter step
# of the same hue (#4E9BD8), checked against the dark surface.
#
# The wordmark itself: traced from kaufmanrossin.com's own SVG, ink colours
# unchanged. Used in a shared constant rather than duplicated inline because it
# appears twice (header, contact card) and the path data is long.
KR_LOGO_SVG = """<svg class="krlogo" viewBox="0 0 351.4 67.22" role="img" aria-label="Kaufman Rossin">
<style>.cls-1{fill:#1e4c7e}.cls-2{fill:#aed136}.cls-3{fill:#828282}</style>
<path class="cls-1" d="M 244.27,33.47 A 12.83,12.83 0 0 1 231.2,20.36 13.11,13.11 0 1 1 257.4,20.29 13,13 0 0 1 244.27,33.47 Z m 0,-23.6400002 c -5.87,0 -10.06,4.6500002 -10.06,10.4600002 0,5.81 4.26,10.53 10.19,10.53 5.93,0 10,-4.6 10,-10.46 0,-5.86 -4.25,-10.5300002 -10.13,-10.5300002 z"/>
<path class="cls-1" d="m 220.86,33.22 -7.67,-10.41 h -4.56 v 10.41 h -2.91 V 7.4399998 h 8.48 c 5.66,0 9.31,3.0000002 9.31,7.6900002 0,4.3 -2.83,6.26 -7.2,7.14 l 8.32,10.95 z m -6.88,-23.1 h -5.35 v 10.1 h 5.31 c 3.86,0 6.62,-1.58 6.62,-4.93 0,-3.25 -2.43,-5.17 -6.58,-5.17 z"/>
<path class="cls-1" d="M 0.4,33.13 V 7.4799998 H 4.85 V 19.16 H 4.92 L 14.09,7.4799998 H 19.6 L 9.52,20.22 l 11,12.94 h -5.7 L 4.96,21.55 H 4.89 v 11.58 z"/>
<path class="cls-1" d="m 32.23,7.4999998 h 0.43 L 43.81,33.13 H 38.54 L 37.4,30.22 h -9.9 l -1.1,2.91 H 21.51 Z M 35.6,25.88 33.84,21.47 c -0.71,-1.79 -1.44,-4.55 -1.44,-4.55 0,0 -0.72,2.76 -1.43,4.55 l -1.76,4.41 z"/>
<path class="cls-1" d="M 46.58,23.94 V 7.7199998 h 5 V 23.64 c 0,3.7 1.54,5.12 4.87,5.12 3.33,0 4.81,-1.42 4.81,-5.12 V 7.7199998 h 5 V 23.94 c 0,6.43 -4.27,9.53 -9.83,9.53 -5.56,0 -9.85,-3.1 -9.85,-9.53 z"/>
<path class="cls-1" d="M 73.69,7.7199998 H 89.4 V 12.32 H 78.71 v 8.41 h 9.69 v 4.67 h -9.69 v 7.73 h -5 z"/>
<path class="cls-1" d="M 96.04,7.4599998 H 96.4 L 108.4,21.21 120.23,7.4599998 h 0.43 V 33.13 h -4.84 V 23.6 c 0,-1.71 0.18,-4.44 0.18,-4.44 a 43.79,43.79 0 0 1 -2.6,3.58 l -4.84,5.65 h -0.47 l -4.84,-5.65 c -1.14,-1.34 -2.61,-3.58 -2.61,-3.58 0,0 0.17,2.73 0.17,4.44 v 9.53 h -4.8 z"/>
<path class="cls-1" d="m 136.18,7.4999998 h 0.43 L 147.77,33.13 h -5.28 l -1.09,-2.91 h -9.9 l -1.15,2.87 h -4.84 z M 139.55,25.88 137.8,21.47 c -0.72,-1.79 -1.47,-4.55 -1.47,-4.55 a 47.17,47.17 0 0 1 -1.44,4.55 l -1.76,4.41 z"/>
<path class="cls-1" d="m 159.2,21.32 a 37,37 0 0 1 -2.91,-3.47 46.31,46.31 0 0 1 0.36,4.63 v 10.65 h -4.7 V 7.4999998 h 0.5 L 164.75,19.5 a 34.87,34.87 0 0 1 2.87,3.48 c 0,0 -0.32,-2.88 -0.32,-4.64 V 7.7199998 H 172 V 33.36 h -0.5 z"/>
<path class="cls-1" d="m 263.89,28.27 2.41,-1.21 a 7.47,7.47 0 0 0 7,4 c 3.18,0 5.68,-1.66 5.68,-4.39 0,-2.73 -1.73,-3.94 -5.15,-5.37 l -2.18,-0.95 c -3.9,-1.66 -6,-3.44 -6,-7 0,-3.5600002 3.1,-6.1700002 7.2,-6.1700002 A 8.18,8.18 0 0 1 280.25,11.04 l -2.33,1.37 a 5.63,5.63 0 0 0 -5.07,-2.8000002 c -2.69,0 -4.34,1.4700002 -4.34,3.6700002 0,2.2 1.28,3.29 4.34,4.61 l 2.17,0.95 c 4.55,1.89 6.88,4 6.88,7.87 0,4.24 -3.58,6.81 -8.49,6.81 -5.22,-0.03 -8.07,-2.52 -9.52,-5.25 z"/>
<path class="cls-1" d="m 288.87,28.27 2.41,-1.21 a 7.47,7.47 0 0 0 7,4 c 3.18,0 5.67,-1.66 5.67,-4.39 0,-2.73 -1.73,-3.94 -5.14,-5.37 l -2.18,-0.95 c -3.9,-1.66 -6,-3.44 -6,-7 0,-3.5600002 3.1,-6.1700002 7.2,-6.1700002 A 8.18,8.18 0 0 1 305.23,11.04 l -2.33,1.37 a 5.63,5.63 0 0 0 -5.07,-2.8000002 c -2.7,0 -4.34,1.4700002 -4.34,3.6700002 0,2.2 1.28,3.29 4.34,4.61 l 2.17,0.95 c 4.55,1.89 6.88,4 6.88,7.87 0,4.24 -3.58,6.81 -8.49,6.81 -5.22,-0.03 -8.07,-2.52 -9.52,-5.25 z"/>
<path class="cls-1" d="m 316.34,7.4599998 h 2.94 V 33.22 h -2.94 z"/>
<path class="cls-1" d="m 351.4,33.55 h -0.63 v 0 L 334.4,15.36 c -0.55,-0.63 -1.3,-1.61 -1.67,-2.1 0.07,0.6 0.19,1.77 0.19,2.56 V 33.29 H 330 V 7.0899998 h 0.63 v 0 L 346.96,25.26 c 0.56,0.63 1.31,1.62 1.68,2.1 -0.06,-0.58 -0.16,-1.77 -0.16,-2.56 V 7.3599998 h 2.92 z"/>
<rect class="cls-2" x="187.36" y="0" width="2.9100001" height="40.830002"/>
<path class="cls-3" d="m 7.4,47.15 a 7,7 0 0 1 5.17,2 l -1,1.43 a 6,6 0 0 0 -4.13,-1.71 5.42,5.42 0 0 0 -5.51,5.68 5.48,5.48 0 0 0 5.6,5.67 6.24,6.24 0 0 0 4.53,-2 l 0.87,1.49 a 7.56,7.56 0 0 1 -5.53,2.24 7.4,7.4 0 1 1 0,-14.8 z"/>
<path class="cls-3" d="m 16.4,47.48 h 1.82 v 1.38 c 0,0.62 -0.05,1.1 -0.05,1.1 h 0.05 a 5.13,5.13 0 0 1 4.89,-2.81 c 3.79,0 6.15,3 6.15,7.42 0,4.42 -2.67,7.38 -6.32,7.38 a 5.09,5.09 0 0 1 -4.61,-2.73 h -0.06 a 11.42,11.42 0 0 1 0.06,1.23 v 6.77 H 16.4 Z m 6.34,12.79 c 2.51,0 4.56,-2.11 4.56,-5.7 0,-3.59 -1.83,-5.68 -4.47,-5.68 -2.36,0 -4.58,1.69 -4.58,5.7 0.02,2.84 1.59,5.68 4.51,5.68 z"/>
<path class="cls-3" d="m 40.47,52.88 h 0.79 v -0.37 c 0,-2.72 -1.49,-3.65 -3.52,-3.65 a 6.89,6.89 0 0 0 -4,1.35 l -0.9,-1.46 a 8.14,8.14 0 0 1 5,-1.6 c 3.4,0 5.29,1.88 5.29,5.42 v 9.05 h -1.79 v -1.55 c 0,-0.7 0.05,-1.18 0.05,-1.18 h -0.05 a 5.1,5.1 0 0 1 -4.72,3.06 c -2.36,0 -4.81,-1.37 -4.81,-4.18 0,-4.78 6.21,-4.89 8.66,-4.89 z m -3.54,7.45 c 2.7,0 4.33,-2.81 4.33,-5.26 V 54.45 H 40.4 c -2.22,0 -6.66,0.09 -6.66,3.21 0.04,1.32 1.08,2.67 3.19,2.67 z"/>
<path class="cls-3" d="m 52.52,53.71 h 5.6 v -6.29 h 1.72 v 6.29 h 5.56 v 1.63 h -5.56 v 6.28 h -1.72 v -6.28 h -5.6 z"/>
<path class="cls-3" d="m 82.03,52.88 h 0.78 v -0.37 c 0,-2.72 -1.49,-3.65 -3.51,-3.65 a 6.89,6.89 0 0 0 -4,1.35 l -0.9,-1.46 a 8.14,8.14 0 0 1 5,-1.6 c 3.4,0 5.28,1.88 5.28,5.42 v 9.05 H 82.9 v -1.55 c 0,-0.7 0,-1.18 0,-1.18 v 0 a 5.1,5.1 0 0 1 -4.72,3.06 c -2.36,0 -4.81,-1.37 -4.81,-4.18 0.03,-4.78 6.21,-4.89 8.66,-4.89 z m -3.54,7.45 c 2.69,0 4.32,-2.81 4.32,-5.26 V 54.45 H 82 c -2.22,0 -6.66,0.09 -6.66,3.21 0,1.32 1.06,2.67 3.15,2.67 z"/>
<path class="cls-3" d="m 94.12,47.15 a 5,5 0 0 1 4.61,2.67 v 0 c 0,0 0,-0.48 0,-1.07 v -6.88 h 1.91 v 19.75 h -1.86 v -1.49 a 7.34,7.34 0 0 1 0.06,-1 h -0.06 a 5.11,5.11 0 0 1 -4.86,2.83 c -3.79,0 -6.15,-3 -6.15,-7.41 0,-4.41 2.63,-7.4 6.35,-7.4 z m 0.08,13.07 c 2.36,0 4.58,-1.68 4.58,-5.7 0,-2.87 -1.46,-5.68 -4.49,-5.68 -2.5,0 -4.55,2.11 -4.55,5.68 0,3.57 1.82,5.7 4.46,5.7 z"/>
<path class="cls-3" d="m 103.31,47.48 h 2 l 3.94,10.37 c 0.25,0.71 0.47,1.69 0.47,1.69 h 0.06 a 13.86,13.86 0 0 1 0.51,-1.69 l 3.9,-10.37 h 2 l -5.38,14.14 h -2.08 z"/>
<path class="cls-3" d="m 118.82,41.87 h 2 v 2.35 h -2 z m 0.06,5.61 h 1.91 v 14.14 h -1.91 z"/>
<path class="cls-3" d="m 125.28,58.53 a 6.09,6.09 0 0 0 4.22,1.69 c 1.51,0 2.84,-0.76 2.84,-2.25 0,-3.09 -7.62,-2.33 -7.62,-6.94 0,-2.53 2.28,-3.9 4.83,-3.9 a 6,6 0 0 1 4.36,1.54 l -0.87,1.46 a 5,5 0 0 0 -3.54,-1.29 c -1.44,0 -2.81,0.62 -2.81,2.19 0,3.12 7.61,2.28 7.61,6.94 0,2.31 -2,4 -4.83,4 a 7.21,7.21 0 0 1 -5.23,-2 z"/>
<path class="cls-3" d="m 144.28,47.15 a 7.4,7.4 0 1 1 0,14.8 7.4,7.4 0 1 1 0,-14.8 z m 0,13.09 a 5.57,5.57 0 0 0 5.5,-5.76 5.51,5.51 0 1 0 -11,0 5.57,5.57 0 0 0 5.5,5.74 z"/>
<path class="cls-3" d="m 154.88,47.48 h 1.88 v 2.51 c 0,0.61 -0.05,1.12 -0.05,1.12 h 0.05 c 0.68,-2.14 2.25,-3.77 4.47,-3.77 a 3.66,3.66 0 0 1 0.76,0.09 v 1.88 a 4.57,4.57 0 0 0 -0.7,-0.06 c -2,0 -3.49,1.58 -4.11,3.66 a 9.33,9.33 0 0 0 -0.39,2.75 v 6 h -1.91 z"/>
<path class="cls-3" d="m 164.77,58.53 a 6.09,6.09 0 0 0 4.22,1.69 c 1.51,0 2.83,-0.76 2.83,-2.25 0,-3.09 -7.61,-2.33 -7.61,-6.94 0,-2.53 2.28,-3.9 4.83,-3.9 a 6,6 0 0 1 4.36,1.56 l -0.87,1.46 a 5,5 0 0 0 -3.54,-1.29 c -1.44,0 -2.81,0.62 -2.81,2.19 0,3.12 7.61,2.28 7.61,6.94 0,2.31 -2,4 -4.83,4 a 7.21,7.21 0 0 1 -5.23,-2 z"/>
</svg>"""

# Off while the light-mode look is still being tuned — every screenshot and
# every "check the live site" otherwise has to be read against whichever
# theme the viewer happens to be in. The variables and the manual
# :root[data-theme="dark"] scope stay in the CSS either way; this only
# gates the automatic prefers-color-scheme switch. Flip back on when done.
DARK_MODE_ENABLED = False

CSS = """
:root{
  color-scheme:light;
  /* One definition of the page font, referenced by body AND the search input.
     The input needs it stated explicitly: Samsung Internet ignores font:inherit
     on form controls and imposes its own Samsung Sans, so the search box drew in
     a visibly rounder typeface than the rest of the page. An explicit family via
     this variable overrides that, and sharing the variable keeps the two in
     lockstep. museo-sans is the firm face when installed; the rest is the system
     fallback that actually renders. */
  --ui-font:museo-sans,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --page:#f4f4f4; --surface:#ffffff; --raised:#fafafa;
  --ink:#212529; --ink-2:#3c3c3c; --ink-muted:#6c757d;
  --rule:#e3e3e3; --border:rgba(0,0,0,.12);
  --brand:#003b6a; --brand-bg:#003b6a; --brand-bg-2:#001f3f; --brand-bg-light:#074c83; --accent:#aed136;
  --info:var(--brand-bg-light);
  --bar:#003b6a; --track:#e3e3e3;
  --crit:#c0392b; --warn:#9a6400; --ok:#2f7d32; --neutral:#3c3c3c;
  --chip:#f0f0f0;
  --on-accent:#212529;
  /* v1.5: card elevation. Two-layer shadow (tight + soft) reads as a real
     surface lift rather than a flat drop-shadow; the hover variant is the
     same shape scaled up, not a different one, so hover feels like the same
     card lifting further rather than a different effect kicking in. */
  --shadow-sm:0 1px 2px rgba(16,24,32,.05),0 1px 4px rgba(16,24,32,.07);
  --shadow-md:0 4px 10px rgba(16,24,32,.09),0 2px 4px rgba(16,24,32,.06);
}
""" + ("""
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    /* --brand-bg lighter than plain navy (#00294a read as almost the same
       black as --page:#101418 — the top band and footer nearly vanished).
       Not lightened all the way to --brand's #4e9bd8: that's tuned for
       small text/links, and would be a glaring wall of blue at full-bleed
       band size. This sits between the two. */
    --page:#101418; --surface:#161a1d; --raised:#1d2226;
    --ink:#f5f5f5; --ink-2:#c9cdd1; --ink-muted:#8b9298;
    --rule:#2a3035; --border:rgba(255,255,255,.12);
    --brand:#4e9bd8; --brand-bg:#123a63; --brand-bg-2:#0a2242; --brand-bg-light:#215383; --accent:#aed136;
    --info:var(--brand-bg-light);
    --bar:#4e9bd8; --track:#2a3035;
    --crit:#e66767; --warn:#eda100; --ok:#4caf50; --neutral:#c9cdd1;
    --chip:#232a2f;
    --on-accent:#101418;
    /* Dark surfaces need a darker, more opaque shadow to read at all against
       a near-black page — the light-mode rgba values would be invisible. */
    --shadow-sm:0 1px 2px rgba(0,0,0,.4),0 1px 4px rgba(0,0,0,.3);
    --shadow-md:0 4px 12px rgba(0,0,0,.5),0 2px 4px rgba(0,0,0,.35);
  }
}
""" if DARK_MODE_ENABLED else "") + """
:root[data-theme="dark"]{
  color-scheme:dark;
  --page:#101418; --surface:#161a1d; --raised:#1d2226;
  --ink:#f5f5f5; --ink-2:#c9cdd1; --ink-muted:#8b9298;
  --rule:#2a3035; --border:rgba(255,255,255,.12);
  --brand:#4e9bd8; --brand-bg:#123a63; --brand-bg-2:#0a2242; --brand-bg-light:#215383; --accent:#aed136;
    --info:var(--brand-bg-light);
  --bar:#4e9bd8; --track:#2a3035;
  --crit:#e66767; --warn:#eda100; --ok:#4caf50; --neutral:#c9cdd1;
  --chip:#232a2f;
  --on-accent:#101418;
  --shadow-sm:0 1px 2px rgba(0,0,0,.4),0 1px 4px rgba(0,0,0,.3);
  --shadow-md:0 4px 12px rgba(0,0,0,.5),0 2px 4px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
/* museo-sans is the firm's typeface, licensed through Adobe Fonts and not
   embeddable in a self-contained file, so it only resolves on a machine that has
   it installed locally. Everything after it is the fallback that actually runs.
   It used to be kaufmanrossin.com's chain — Calibri,Georgia,Verdana — and that
   was a real bug on phones: Calibri is a Windows font, so desktops got Calibri
   and looked right, while iOS has no Calibri and fell through to GEORGIA, a
   serif book face, for the whole dashboard. It rendered like a printed document,
   not a tool. Measured on a 390px viewport before changing it.
   The system stack below gives SF on iOS, Segoe UI on Windows, Roboto on
   Android — each platform's own interface face, which is what "native" looks
   like. Do not reintroduce Georgia or Verdana as fallbacks. */
/* var(--surface), not var(--page) — kaufmanrossin.com's own pages sit
   directly on white, not a grey backdrop with white cards floating on it.
   Panels/KPI tiles keep their own border (var(--border)) for definition, so
   they don't disappear now that they match the page behind them. --page
   stays defined and still used for recessed elements like input fields
   (e.g. .ask-row input) that want to read as sunken against a white card. */
body{margin:0;padding:0;background:var(--surface);color:var(--ink);
  font-family:var(--ui-font);font-size:14px;line-height:1.55;
  -webkit-text-size-adjust:100%;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:22px}
/* Full-bleed replica of kaufmanrossin.com's own two-band header: a navy
   utility strip (site-switcher tabs + Payment Portal/File Sharing/phone/
   Español) above a white nav bar (wordmark + primary nav). Every link here
   leaves Klearance for the real site, same target=_blank reasoning as the
   footer's nav — Klearance has none of these pages itself; this is brand
   chrome borrowed wholesale, not a Klearance-specific nav that happens to
   look similar. Colours/sizes measured off the live site, not eyeballed. */
.krtop{background:var(--brand-bg)}
/* Full-bleed, not capped at the page's 1240px column like every other band
   below it -- this thin utility strip reads as chrome pinned to the browser
   edges (matching the real site), not as part of the page's own content
   width. The white krheader row underneath keeps the 1240px cap since it
   carries the logo/nav that aligns with the rest of the page. */
.krtopwrap{padding:0 22px;
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap}
.krtop nav{display:flex;align-items:center}
.krtop a{display:inline-block;padding:6px 10px;font-size:14px;font-weight:500;
  color:#fff;text-decoration:none;white-space:nowrap}
.krtop a:hover{text-decoration:underline}
.krtop .sites a.active{background:var(--accent);color:var(--brand);border-radius:2px}
/* Dividers throughout .krtop, matching the real site's actual style measured
   off kaufmanrossin.com's computed styles -- a solid 1px LIME ::after
   (background:var(--accent)), not a translucent white border-left (the
   earlier, un-measured guess this replaces). The real site applies it to
   EVERY .util link uniformly, including the last (Español) -- its bar just
   lands on the strip's trailing edge with nothing past it to contrast
   against, so no :last-child exclusion is needed there. .sites only gets it
   after Wealth (2nd child), not before it: a line before Wealth would cut
   across the "CPAs and Advisors" active pill's own background instead of
   sitting on plain navy, which the real site's own layout never has to
   contend with (its first item isn't a filled pill). */
.krtop .sites a:nth-child(2),.krtop .util a{position:relative}
.krtop .sites a:nth-child(2)::after,.krtop .util a::after{content:'';position:absolute;
  top:50%;transform:translateY(-50%);right:0;width:1px;height:14px;background:var(--accent)}
.krtop .util a{font-size:12.5px}
/* Second band: plain flush white bar, no card/shadow — this is fixed brand
   chrome sitting outside .wrap, not a themed page element, so it stays
   white/navy in every theme same as the old single-band header did. */
header.krheader{background:#fff;border-bottom:1px solid var(--border)}
.krheaderwrap{max-width:1240px;margin:0 auto;padding:10px 22px 14px}
.krheader-toprow{display:flex;justify-content:flex-end}
.krheader-mainrow{display:flex;align-items:center;justify-content:space-between;
  gap:20px;flex-wrap:wrap;margin-top:6px}
.krheaderwrap .logowrap{flex:none}
.krheaderwrap .krlogo{width:160px}
.krheader nav{display:flex;align-items:center;gap:20px}
.krheader nav a{color:var(--brand);font-size:14.5px;font-weight:500;text-decoration:none}
.krheader nav a:hover{text-decoration:underline}
.krheader .krsearch{display:flex;align-items:center;border:1px solid var(--border);
  border-radius:6px;overflow:hidden}
.krheader .krsearch input{border:none;padding:4px 8px;font-size:12px;width:85px;
  color:var(--brand);background:#fff}
.krheader .krsearch input:focus{outline:none}
.krheader .krsearch button{border:none;background:transparent;color:var(--brand);
  padding:0 8px;height:26px;display:inline-flex;align-items:center;cursor:pointer}
.krheader .krsearch button:hover{background:var(--raised)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip:rect(0,0,0,0);white-space:nowrap;border:0}
/* Grey breadcrumb band — same colours as the real site's Bootstrap-derived
   breadcrumb: light grey pill, blue link, muted grey for the current page.
   Full-bleed here rather than the real site's narrower boxed version, since
   Klearance's page has no sidebar to make room for. */
.krcrumb{background:radial-gradient(ellipse at center,#f3f4f6 0%,#dde1e6 100%)}
.krcrumbwrap{display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:8px;max-width:1240px;margin:0 auto;padding:10px 22px;font-size:14px}
.krcrumb-path a{color:#007bff;text-decoration:none}
.krcrumb-path a:hover{text-decoration:underline}
.krcrumb-path span[aria-hidden]{color:#6c757d;margin:0 8px}
.krcrumb-path span:last-child{color:#6c757d}
.krcrumb-updated{color:#6c757d;font-size:12.5px}
.krcrumb-updated-mobile{display:none}
/* Page-specific title card — everything the real corporate header doesn't
   carry (Klearance's own name, audience, freshness stamp, share/export
   actions). Sits below the replicated chrome above, inside .wrap like the
   rest of the page content; no longer holds its own logo since the krheader
   band above already carries the wordmark once. */
.pagehead{display:flex;align-items:center;gap:16px;margin-bottom:14px;
  padding-bottom:14px;border-bottom:1px solid var(--border)}
.pagehead .t{flex:1}
/* var(--brand), not a fixed navy — the pagehead now lives inside .notice,
   which follows the theme-able --surface, so its text must follow theme too. */
h1{font-size:27px;margin:0;color:var(--brand);font-weight:800;letter-spacing:-.01em}
/* The Check Spike mark trails off the wordmark's last "i" instead of sitting
   beside it as a separate icon — display:flex + align-items:flex-end lands
   the mark on the text baseline the same way a trailing punctuation mark
   would sit. Sized relative to the h1's own font-size, not a fixed pixel
   value, so it stays in proportion if the wordmark size ever changes. */
h1.wordmark{display:flex;align-items:flex-end;gap:1px}
h1.wordmark svg{width:.85em;height:.72em;margin-bottom:.08em;flex:none}

/* Full-bleed hero band, same skeleton as the firm's RISK page: giant
   wordmark + lime rule + credit line on the left, body copy beside a lime
   divider on the right, Check Spike oversized and faint in the background.
   Plain full-width block, not a 100vw/translateX breakout — body carries no
   side padding here (see .krtop above), so a normal block already spans
   edge to edge without the scrollbar-width overflow that trick invites. */
.herowrap{position:relative;
  background:radial-gradient(ellipse at center,var(--brand-bg) 0%,var(--brand-bg-2) 100%);
  overflow:hidden;margin-bottom:14px}
/* v1.5 experiment: looped background video (Pexels, free license, no attribution
   required) instead of the flat gradient. The gradient above is left in place as
   the poster-less fallback — same color the video's overlay tints toward, so if
   the file fails to load or reduced-motion hides it, the band still reads as a
   deliberate navy panel, not a broken image. object-fit:cover crops rather than
   letterboxes since this is a decorative backdrop, not content that must stay
   uncropped. */
.hero-video{position:absolute;inset:0;width:100%;height:100%;
  object-fit:cover;z-index:0}
.hero-overlay{position:absolute;inset:0;z-index:0;
  background:radial-gradient(ellipse at center,rgba(0,59,106,.55) 0%,rgba(0,31,63,.88) 100%)}
@media (prefers-reduced-motion:reduce){.hero-video{display:none}}
.hero-bgmark{position:absolute;right:48px;bottom:-28px;pointer-events:none}
.hero-bgmark svg{display:block}
/* Draws the big background line in once on load, using the path's own
   length as stroke-dasharray/offset. */
.hero-bgmark path{stroke-dasharray:570;stroke-dashoffset:570;
  animation:heroDraw 2.4s cubic-bezier(.65,0,.35,1) forwards}
@keyframes heroDraw{to{stroke-dashoffset:0}}
/* Starts once the line finishes drawing, not before — pinging mid-draw would
   read as two unrelated animations instead of one sequenced moment. */
.hero-ping-sm{animation:heroPing 2.6s cubic-bezier(.25,.6,.4,1) 2.4s infinite}
/* Different cycle length than the small icon's ping on purpose — the ring
   travels far more pixel distance at this scale, so matching the raw
   duration made it read faster and twitchier. Tuned by eye, not a formula:
   small sped up a touch, this one slowed down further, aiming for both to
   feel like the same pace despite the size difference. */
.hero-ping{animation:heroPing 8.4s cubic-bezier(.25,.6,.4,1) 2.4s infinite}
@keyframes heroPing{
  0%{opacity:.5;transform:scale(1)}
  75%{opacity:0;transform:scale(2.4)}
  100%{opacity:0;transform:scale(2.4)}
}
@media (prefers-reduced-motion:reduce){
  .hero-bgmark path{animation:none;stroke-dashoffset:0}
  .hero-ping,.hero-ping-sm{animation:none;opacity:0}
}
.hero-inner{position:relative;z-index:1;max-width:1240px;margin:0 auto;
  padding:44px 22px;display:grid;grid-template-columns:1fr 1px 1fr;
  gap:44px;align-items:center}
.hero-divider{background:var(--accent);width:1px;height:100%;align-self:stretch}
/* Fits to content (the wordmark, its widest child) rather than stretching to
   fill the grid column — that's what lets .hero-rule below key off 100% and
   always match the wordmark's actual rendered width instead of guessing a
   fixed px value that's wrong at some size or other. */
.hero-titleblock{width:fit-content;display:flex;flex-direction:column;align-items:flex-end}
.hero-word{font-family:inherit;font-weight:800;font-size:clamp(46px,7.5vw,88px);
  color:#fff;margin:0;line-height:1;letter-spacing:-.02em;
  display:flex;align-items:flex-end;gap:0}
/* Pull-in was a fixed -64px, tuned against desktop's ~88px font-size. The
   wordmark's font-size is a clamp() that floors at 46px on narrow phones,
   so the icon shrinks but the pull stayed fixed -- on mobile it ate almost
   the entire icon width, reading as the icon and the K tangled together.
   em-based keeps the same visual gap-to-icon-size ratio at every size. */
.hero-word svg{width:1.35em;height:1.1em;margin:0 -.727em .1em 0;flex:none;overflow:visible}
/* K/R merge: the wordmark's two brand-color letters start pulled apart
   (K left, R right) and slide together on load, landing right as the
   page's own .herowrap fade-in finishes (.24s delay + .5s duration above)
   so the letters aren't sliding across an invisible band. A short
   text-shadow pulse fires on landing to sell "merge" as a distinct beat
   rather than just a slide-and-stop. Implicit 0% keyframe = the element's
   own pre-animation transform (set by .kr-k/.kr-r below), so krMerge only
   needs a `to` frame. */
.hero-word .kr{color:var(--accent);display:inline-block;opacity:0}
.hero-word .kr-k{position:relative;transform:translateX(-32px);
  animation:krMerge 1s cubic-bezier(.2,.8,.3,1) .8s forwards,
    krPulse .45s ease-out 1.8s forwards}
/* Echoes the KAUFMAN | ROSSIN credit line's own pipe divider (.hero-pipe)
   — grows in right as K/R land and form "KR" (1.8s, same moment as their
   pulse), holds through the pause, then shrinks back out as R continues
   into its real position (2.8s, same moment .hero-word-rest starts
   fading in). Positioned relative to .kr-k itself (not the whole word)
   so it doesn't need a measured absolute offset. */
/* margin-left/width were fixed 10px/4px, tuned against desktop's ~88px
   wordmark font-size -- same scale-mismatch bug as the icon pull-in above.
   At mobile's 46px floor that fixed footprint ate a much bigger share of
   --kr-gap below (which IS em-based), so the divider ran into R instead of
   sitting cleanly between the two letters. em-based keeps the same ratio
   to --kr-gap at every size. */
.kr-divider{position:absolute;left:100%;top:50%;margin-left:.114em;
  width:3px;height:1.3em;background:var(--accent);border-radius:2px;
  transform:translateY(-50%) scaleY(0);transform-origin:center;
  animation:krDividerIn .3s ease-out 1.8s forwards,
    krDividerOut .3s ease-in 2.8s forwards}
@keyframes krDividerIn{to{transform:translateY(-50%) scaleY(1)}}
@keyframes krDividerOut{to{transform:translateY(-50%) scaleY(0)}}
@media (prefers-reduced-motion:reduce){
  .kr-divider{animation:none;transform:translateY(-50%) scaleY(0)}
}
/* R doesn't just slide into its real spot — it stops next to K first
   (forming "KR" at the same moment K lands: 33% of this 3s run = .8s
   delay + 1s, matching krMerge's own timing above), holds there through
   66% (a full second), then continues on into its real position while
   .hero-word-rest (the "lea"/"ance" spans, still at opacity 0) fades in
   around it so R doesn't appear to slide across already-visible
   letters. --kr-gap is the horizontal distance from R's real position
   back to next to K, in em so it scales with the responsive wordmark
   size instead of a viewport-specific px guess — deliberately short of
   flush against K (K's own glyph has a diagonal leg that reads as
   touching R's stem at true zero gap) so "KR" reads as two distinct
   letters, not a collision. */
.hero-word .kr-r{--kr-gap:-1.15em;transform:translateX(32px);
  animation:krFormThenSettle 3s cubic-bezier(.4,0,.2,1) .8s forwards,
    krPulse .45s ease-out 1.8s forwards,
    krPulse .4s ease-out 3.8s forwards}
@keyframes krMerge{to{opacity:1;transform:translateX(0)}}
@keyframes krFormThenSettle{
  0%{opacity:0;transform:translateX(32px)}
  33%{opacity:1;transform:translateX(var(--kr-gap))}
  66%{opacity:1;transform:translateX(var(--kr-gap))}
  100%{opacity:1;transform:translateX(0)}
}
@keyframes krPulse{
  0%,100%{text-shadow:0 0 0 rgba(174,209,54,0)}
  50%{text-shadow:0 0 18px rgba(174,209,54,.9)}
}
.hero-word-rest{opacity:0;animation:heroRestIn .5s ease-out 2.8s forwards}
@keyframes heroRestIn{to{opacity:1}}
@media (prefers-reduced-motion:reduce){
  .hero-word .kr{animation:none;opacity:1;transform:none}
  .hero-word-rest{animation:none;opacity:1}
}
.hero-rule{width:100%;height:4px;background:var(--accent);margin:16px 0 10px}
.hero-sub{margin:0;font-size:12px;letter-spacing:.1em;
  text-transform:uppercase;color:#bcd3e6}
.hero-sub b{display:block;color:#fff;font-weight:800;font-size:15px;
  letter-spacing:0;text-transform:none;margin-top:3px}
.hero-pipe{color:var(--accent);font-weight:400}
.hero-copy{font-size:19px;line-height:1.55;font-weight:600;color:#fff;margin:0}
@media (max-width:820px){
  /* padding/gap tightened well below the desktop values (44px/44px) rather
     than inheriting them, let alone the old 48px override -- stacking to a
     single column already adds height the side-by-side desktop layout
     never has (title block + copy now stack instead of sharing one row),
     and the desktop gap value was doing double duty as horizontal spacing
     there; unchanged, it becomes 44px of pure vertical dead air between
     the stacked blocks on a phone. Top/bottom padding evened out to 20px/14px
     (ported from Mihari) -- an earlier pass cut bottom to 10px while leaving
     top at 28px, which traded the dead-space problem for a lopsided
     top-heavy look on a real device; this keeps both ends trim without
     either one reading as bigger than the other. herowrap's own
     margin-bottom also cut (14px to 6px) since that stacked with .wrap's
     mobile padding-top to widen the white gap before the "Read this
     first" notice card. */
  .hero-inner{grid-template-columns:1fr;padding:20px 20px 14px;gap:12px}
  .hero-divider{display:none}
  .hero-bgmark svg{width:340px;height:auto}
  .herowrap{margin-bottom:6px}
  /* .hero-rule's 16px/10px margin is fixed px, not tied to the wordmark's
     font-size (the rule sits outside .hero-word, so em there wouldn't
     track the wordmark anyway) -- it just reads as oversized once the
     wordmark itself has shrunk to its 46px mobile floor. Tightened to
     match, same reasoning as the other mobile paddings in this block. */
  .hero-rule{margin:8px 0 8px}
  /* Both em-scaled correctly now (proportional to desktop's approved
     look), but at mobile's small absolute sizes the same ratio still
     reads as cramped -- icon nearly touching K, divider nearly touching
     R. Relaxed further on mobile only: less negative icon pull-in opens
     air before the K, smaller divider margin-left pulls the bar back
     toward K and off of R. Desktop unchanged. */
  .hero-word svg{margin-right:-.65em;transform:translateY(-4px)}
  .kr-divider{margin-left:.05em}
  /* Was centered on mobile (floated with dead space either side, read as
     adrift rather than deliberate) -- shifted right instead, matching
     desktop's right-aligned-to-the-wordmark treatment rather than
     centering. Per Alexander. */
  .hero-sub{align-self:flex-end;text-align:right}
}

/* "by KAUFMAN | ROSSIN" credit line — same navy/lime pipe as the full-size
   KR wordmark, just set as small text under the product name rather than
   redrawing the logo mark at a tiny size. */
.krby{font-size:11px;color:var(--ink-muted);margin:1px 0 4px;letter-spacing:.02em}
.krbyname{color:var(--brand);font-weight:700}
.krbyname .pipe{color:var(--accent);font-weight:400;margin:0 3px}
button{font:inherit;font-size:13px;padding:8px 14px;color:var(--ink);
  background:var(--surface);border:1px solid var(--border);border-radius:8px;
  cursor:pointer;transition:background-color .12s ease,border-color .12s ease}
button:hover{background:var(--raised)}
.pagehead button{background:var(--accent);border-color:var(--accent);
  color:var(--on-accent);font-weight:700}
/* Solid navy + lime trim, same pairing as the footer's Subscribe button —
   a deliberate two-tone pill rather than the plain white default. Extra
   top margin so it reads as its own element below the last card, not
   crowded right up against it. Same sweep-fill trick as Subscribe: a
   200%-wide two-tone gradient sliding via background-position, not a
   plain background-color fade (which has no direction).
   Text stays white through the hover fill by request — every other use
   of the lime accent on this page keeps text dark on it for contrast
   (see the header button), so this is a deliberate one-off exception,
   not the pattern to copy elsewhere. */
#showmore,#dlmore{margin-top:16px;color:#fff;
  border:1px solid var(--brand);border-left:8px solid var(--accent);
  background:linear-gradient(to right,var(--accent) 50%,var(--brand) 50%);
  background-size:200% 100%;background-position:right bottom;
  transition:background-position .5s ease}
#showmore:hover,#dlmore:hover{background-position:left bottom}
/* .dl rows keep their divider on the last one (unlike .card, which drops
   its own) so this button doesn't sit flush against the final deadline's
   text — a bit more breathing room than the shared 16px above. */
#dlmore{margin-top:24px}
.pagehead button:hover{filter:brightness(1.06)}

/* Icon toolbar (share / install / more) — overrides the lime pagehead-button
   fill above: these are quiet icon buttons, not CTAs, and .icon-btn beats
   the plain-element "pagehead button" selector on specificity regardless of
   source order. #export keeps the lime fill; it lives in the dropdown, not
   directly in the toolbar row, so the generic rule still reaches it there.
   Theme-following colours, not fixed light ones — the pagehead now lives
   inside .notice's theme-able surface rather than a fixed-white card. */
.icon-toolbar{display:flex;align-items:center;gap:2px;flex:none;
  background:var(--raised);border:1px solid var(--border);border-radius:999px;padding:4px}
.icon-toolbar .icon-btn{width:36px;height:36px;padding:0;display:inline-flex;
  align-items:center;justify-content:center;border-radius:50%;border:none;
  background:transparent;color:var(--brand);font-weight:400;
  transition:background-color .12s ease}
.icon-toolbar .icon-btn:hover{background:var(--chip);filter:none}
.icon-btn-wrap{position:relative}
.more-menu{position:absolute;top:calc(100% + 8px);right:0;z-index:20;
  background:#fff;border:1px solid rgba(0,0,0,.12);border-radius:10px;
  box-shadow:0 4px 10px rgba(16,24,32,.15);padding:6px;min-width:140px}
.more-menu[hidden]{display:none}
.more-menu button{width:100%;text-align:left;color:#212529}
.icon-toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(6px);
  background:#212529;color:#fff;padding:8px 16px;border-radius:8px;font-size:13px;
  z-index:50;opacity:0;pointer-events:none;transition:opacity .15s ease,transform .15s ease}
.icon-toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* Notes/Tasks dialog — a single shared modal reused across every item rather
   than one per card, since there can be hundreds of cards on screen. Only
   its content and the data-url it's bound to change per open. Sits above
   the icon toast (z-index 50) since a save can fire while it's open. */
.item-dialog-backdrop{position:fixed;inset:0;background:rgba(10,14,18,.5);
  display:flex;align-items:center;justify-content:center;padding:20px;z-index:60}
.item-dialog-backdrop[hidden]{display:none}
.item-dialog{background:var(--surface);border-radius:12px;box-shadow:var(--shadow-md);
  width:100%;max-width:420px;max-height:min(560px,84vh);display:flex;flex-direction:column;
  overflow:hidden}
.idhead{display:flex;align-items:flex-start;gap:10px;padding:14px 16px;
  border-bottom:1px solid var(--border)}
.idhead .idttl{flex:1;font-size:13.5px;font-weight:700;color:var(--ink);line-height:1.4}
.idkind{display:block;font-size:11px;font-weight:700;letter-spacing:.06em;
  text-transform:uppercase;color:var(--brand);margin-bottom:2px}
#idClose{flex:none;width:26px;height:26px;padding:0;display:flex;align-items:center;
  justify-content:center;font-size:18px;line-height:1;border-radius:6px}
#idBody{padding:14px 16px;overflow-y:auto}
#idNoteText{width:100%;min-height:120px;resize:vertical;font:inherit;font-size:14px;
  padding:10px 12px;color:var(--ink);background:var(--surface);border:1px solid var(--border);
  border-radius:8px}
#idNoteText:focus{outline:2px solid var(--brand);outline-offset:1px;border-color:var(--brand)}
.idnotefoot{margin-top:8px;font-size:11.5px;color:var(--ink-muted)}
#idTaskForm{display:flex;gap:8px;margin-bottom:12px}
#idTaskInput{flex:1;font:inherit;font-size:14px;padding:8px 10px;color:var(--ink);
  background:var(--surface);border:1px solid var(--border);border-radius:8px}
#idTaskInput:focus{outline:2px solid var(--brand);outline-offset:1px;border-color:var(--brand)}
#idTaskForm button{padding:8px 12px}
#idTaskList{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px}
.idtask{display:flex;align-items:center;gap:9px;padding:7px 4px;border-radius:6px}
.idtask:hover{background:var(--raised)}
.idtask label{flex:1;display:flex;align-items:center;gap:9px;font-size:13.5px;
  color:var(--ink);cursor:pointer}
.idtask input[type=checkbox]{width:16px;height:16px;flex:none;accent-color:var(--brand)}
.idtask.done label{color:var(--ink-muted);text-decoration:line-through}
.idtaskdel{flex:none;width:24px;height:24px;padding:0;display:flex;align-items:center;
  justify-content:center;color:var(--ink-muted);border:none;background:none;border-radius:6px}
.idtaskdel:hover{color:var(--crit);background:var(--raised)}
.idempty{font-size:13px;color:var(--ink-muted);padding:6px 4px}

/* One-time onboarding callout, pointed at the first card's calendar/notes/
   tasks icons via JS (see initQuickStart) — position:fixed with inline
   left/top set from the target's actual getBoundingClientRect, not a fixed
   guess, so it tracks wherever that row really lands at the reader's width.
   --info is var(--brand-bg-light) — the same mid-tone step used in the
   hero/panel gradients, not a separate off-brand blue, so a transient
   system message still reads as part of the Klearance palette. */
.quickstart{position:fixed;z-index:70;width:280px;background:var(--info);color:#fff;
  border-radius:12px;box-shadow:var(--shadow-md);padding:14px 16px 12px}
.quickstart[hidden]{display:none}
.qs-tail{position:absolute;width:14px;height:14px;background:var(--info);
  transform:rotate(45deg);border-radius:3px}
.qs-above .qs-tail{bottom:-6px}
.qs-below .qs-tail{top:-6px}
.qs-head{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.qs-head svg{flex:none}
.qs-head b{flex:1;font-size:14px}
.qs-close{flex:none;width:22px;height:22px;padding:0;display:flex;align-items:center;
  justify-content:center;background:rgba(255,255,255,.16);border:none;color:#fff;
  border-radius:6px;font-size:16px;line-height:1}
.qs-close:hover{background:rgba(255,255,255,.28)}
.quickstart p{margin:0 0 12px;font-size:13.5px;line-height:1.5;color:#fff}
.qs-foot{display:flex;justify-content:flex-end}
.qs-foot button{background:rgba(0,0,0,.22);border:none;color:#fff;font-weight:700;
  padding:7px 14px;border-radius:8px;font-size:13px}
.qs-foot button:hover{background:rgba(0,0,0,.34)}
@media (max-width:640px){.quickstart{width:calc(100vw - 32px)}}

/* Public-facing notice. Deliberately at the top and in normal body size: a
   personal triage tool can put its caveats in the footer, a public one can't.
   Anyone landing here needs to know the summaries are generated before they
   read any of them. */
.notice{background:var(--surface);border:1px solid var(--border);
  border-left:4px solid var(--crit);border-radius:12px;padding:13px 16px;
  margin-bottom:18px;font-size:13px;color:var(--ink-2);text-align:justify;
  text-align-last:left;hyphens:auto;box-shadow:var(--shadow-sm)}
@keyframes noticeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.notice strong{color:var(--ink)}
/* Whole-page staggered entrance: each major band fades/lifts in on load,
   one after another, using the same noticeIn keyframe the notice tile
   already had rather than a second near-duplicate. Delays step by .08s so
   the cascade reads as one sequence, top to bottom, not simultaneous
   blocks popping in at once. `backwards` holds the 0%-frame state (opacity
   0) during the animation-delay so nothing flashes visible-then-hidden
   before its turn starts. Animated on .colmain/.colside individually, not
   .cols itself: an element with a transform-animation (this keyframe lifts
   via translateY) becomes a new containing block for its descendants for
   as long as the `animation` property is set, which silences .colside's
   own position:sticky if the animation sits on its ancestor instead. */
.krtop,header.krheader,.krcrumb,.herowrap,.notice,.kpis,.colmain,.colside{
  animation:noticeIn .5s cubic-bezier(.25,.6,.4,1) backwards}
.krtop{animation-delay:0s}
header.krheader{animation-delay:.08s}
.krcrumb{animation-delay:.16s}
.herowrap{animation-delay:.24s}
.notice{animation-delay:.32s}
.kpis{animation-delay:.4s}
.colmain,.colside{animation-delay:.48s}
@media (prefers-reduced-motion:reduce){
  .krtop,header.krheader,.krcrumb,.herowrap,.notice,.kpis,.colmain,.colside{animation:none}
}

.coverage{font-size:12.5px;color:var(--ink-2)}
.coverage summary{cursor:pointer;font-size:12.5px;color:var(--brand);
  font-weight:600;list-style:none;display:inline-block;
  padding-bottom:3px;border-bottom:2px solid var(--accent)}
.coverage summary::-webkit-details-marker{display:none}
.coverage summary::before{content:"▸ ";}
.coverage[open] summary::before{content:"▾ ";}
.coverage .body{padding-top:10px;line-height:1.6}
.coverage .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
  gap:6px 22px;margin-top:8px}
.rr-group{margin-top:16px}
.rr-group h3{font-size:13px;margin:0 0 2px;color:var(--ink)}
.rr-table{width:100%;border-collapse:collapse;margin-top:7px}
.rr-table th{text-align:left;font-size:11px;text-transform:uppercase;
  letter-spacing:.05em;color:var(--ink-muted);padding:5px 8px;
  border-bottom:1px solid var(--rule)}
.rr-table td{padding:6px 8px;border-bottom:1px solid var(--rule);
  font-size:12.5px;vertical-align:top}
.rr-letter{font-weight:700;color:var(--brand);white-space:nowrap;width:64px}
.rr-cfr{color:var(--ink-2);white-space:nowrap;font-variant-numeric:tabular-nums}
.rr-note{color:var(--ink-muted);font-size:11.5px;margin-top:3px}
.rr-foot{margin-top:14px;padding-left:18px}
.rr-foot li{margin-bottom:8px;font-size:12.5px}
.rr.hidden,.rr-group.hidden{display:none}
.coverage code{background:var(--chip);padding:1px 5px;border-radius:3px;font-size:11.5px}

/* "Ask the regulations" panel. Retrieval runs in the browser; only the model
   call goes to a Cloudflare Worker, which holds the API key server-side. */
.ask-panel{background:var(--surface);border:1px solid var(--border);
  border-left:4px solid var(--brand);border-radius:12px;padding:16px 18px;
  margin-bottom:18px;box-shadow:var(--shadow-sm)}
.ask-panel h2{font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;
  color:var(--brand);margin:0 0 6px;font-weight:700}
.ask-panel .sub{font-size:12.5px;color:var(--ink-2);margin:0 0 11px}
.ask-row{display:flex;gap:8px;flex-wrap:wrap}
.ask-row input{flex:1 1 380px;font:inherit;font-size:13px;padding:9px 12px;
  color:var(--ink);background:var(--page);border:1px solid var(--border);
  border-radius:8px}
.ask-row input:focus{outline:2px solid var(--brand);outline-offset:1px}
.ask-row button{font:inherit;font-size:13px;font-weight:700;padding:9px 18px;
  background:var(--brand);color:#fff;border:1px solid var(--brand);
  border-radius:8px;cursor:pointer}
.ask-row button:disabled{opacity:.55;cursor:default}
#askout{margin-top:13px;font-size:13.5px;line-height:1.6;color:var(--ink)}
#askout:empty{display:none}
#askout .ans{background:var(--raised);border:1px solid var(--border);
  border-radius:10px;padding:13px 15px}
#askout .cites{font-size:12px;color:var(--ink-muted);margin-top:9px;
  padding-top:8px;border-top:1px solid var(--rule)}
#askout h3{font-size:13px;margin:11px 0 5px;color:var(--ink)}
#askout ul{margin:5px 0 5px 20px;padding:0}
#askout li{margin-bottom:3px}
#askout .warn{color:var(--warn)}
.ask-note{font-size:11.5px;color:var(--ink-muted);margin-top:9px}
/* The individual model answers behind the reconciled one. Collapsed by default:
   the merged answer is what to read, these are for checking it. */
#askout .askraw{margin-top:9px}
#askout .askraw summary{font-size:12px;color:var(--ink-2);cursor:pointer;
  padding:5px 0;user-select:none}
#askout .askraw summary:hover{color:var(--brand)}
#askout .askraw .ans{margin-top:8px}
#askout .ans+.ans{margin-top:9px}
#askout .who{font-size:11px;font-weight:700;letter-spacing:.05em;
  text-transform:uppercase;color:var(--brand);margin-bottom:7px}
@media (max-width:640px), (hover:none) and (pointer:coarse) and (max-width:1024px){
  .ask-panel{padding:12px 13px;margin-bottom:14px}
  .ask-row input{flex:1 1 100%}
  .ask-row button{width:100%}
}
/* A real 2x2 for the four tiles — all the same size, not two stretched
   to fill a row — with Updates by agency as a tall panel to their right,
   explicitly placed across both rows. Explicit placement (not order/span
   on the tiles) is what makes the 2x2 fall out correctly: the four .kpi
   tiles are auto-placed in DOM order and simply skip the cell agency
   already occupies, landing as [Updates this week, Open comment periods]
   / [Enforcement actions, Effective this quarter] — Effective ends up
   under Open comment periods, not floating on its own. Grouped with the
   KPI tiles because both are quick-glance summary stats — it was
   previously buried in the sidebar below the deadlines list. */
.kpis{display:grid;grid-template-columns:1fr 1fr 2fr;grid-auto-rows:1fr;
  gap:12px;margin-bottom:18px}
.kpis .p-agencies{grid-column:3;grid-row:1 / span 2}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:14px 16px;box-shadow:var(--shadow-sm);overflow:hidden}
/* Same navy strip + lime underline as .panel>h2, on the tile's own label —
   negative margin exactly cancels .kpi's own padding so it reads as the
   tile's top edge, not a heading floating inside it. */
.kpi .l{margin:-14px -16px 12px;padding:8px 16px;font-size:11.5px;
  letter-spacing:.07em;text-transform:uppercase;font-weight:700;
  background:radial-gradient(ellipse at center,var(--brand-bg-light) 0%,var(--brand-bg) 100%);
  color:#fff;border-bottom:3px solid var(--accent)}
.kpi .v{font-size:32px;line-height:1.15;letter-spacing:-.02em;margin:6px 0 2px}
.kpi .n{font-size:12px;color:var(--ink-muted)}
.kpi .n.up{color:var(--crit)} .kpi .n.down{color:var(--ok)}
/* Long/short phrasings of a tile note; the phone block swaps them. */
.kpi .n-short{display:none}
/* Inert on desktop -- see kpi-brk's Python-side comment in kpis() for why
   this exists instead of trusting the browser to wrap consistently. */
.kpi-brk{display:none}
/* Clickable tiles (those with a non-zero count) filter the list on click. The
   hover lift is the same shadow shape scaled up (--shadow-md), not a
   different effect, so it reads as the same tile rising rather than
   something new switching on. Pressed state trades the lift for an inset
   ring instead — it should look seated, not floating, while active. */
.kpi[data-kpi]{cursor:pointer;user-select:none;text-align:left;
  transition:border-color .12s ease,box-shadow .12s ease,transform .12s ease}
.kpi[data-kpi]:hover:not([aria-pressed="true"]){border-color:var(--brand);
  box-shadow:var(--shadow-md);transform:translateY(-2px)}
.kpi[data-kpi]:focus-visible{outline:2px solid var(--brand);outline-offset:1px}
.kpi[aria-pressed="true"]{border-color:var(--accent);transform:none;
  box-shadow:inset 0 0 0 1px var(--accent)}

/* Two labelled groups. The pills used to be one undifferentiated row, which hid
   the fact that they answer different questions: agency pills filter by WHO
   published an item, topic pills by WHAT it is about. Same look, different
   mechanism, no way for a reader to tell. */
.pillgroup{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin-bottom:18px}
.pillgroup:last-of-type{margin-bottom:18px}
.clearfilters{display:block;background:none;border:none;color:var(--brand);
  font-size:12px;font-weight:700;text-decoration:underline;cursor:pointer;
  padding:0;margin:8px 0 12px}
.clearfilters:hover{color:var(--ink)}
.clearfilters[hidden]{display:none}
.grouplabel{font-size:11px;font-weight:700;letter-spacing:.06em;
  text-transform:uppercase;color:var(--ink-muted);width:104px;flex:none}
.grouplabel small{display:block;font-weight:400;letter-spacing:0;
  text-transform:none;font-size:11px;line-height:1.3;margin-top:1px}
.searchwrap{position:relative;flex:1 1 340px;max-width:520px}
/* appearance:none is not cosmetic here. A type="search" field keeps its native
   appearance by default, and a native search control renders in the OS's own
   control font rather than the page font — which is why the search box looked
   like a different typeface from everything around it. Resetting appearance
   makes it an ordinary text field that honours the font below. Size matches the
   body (14px) rather than the old 13px, so it no longer reads a notch smaller. */
/* Radius matches the tiles/notice/filters bar (8-10px), not a full pill. It was
   999px, which made the search box the only pill-shaped element on a page of
   soft rectangles — it read as a different kind of control from its neighbours
   even once the font matched. */
.searchwrap input{width:100%;appearance:none;-webkit-appearance:none;
  font-family:var(--ui-font);font-size:14px;padding:8px 30px 8px 12px;
  color:var(--ink);background:var(--surface);border:1px solid var(--border);
  border-radius:10px}
.searchwrap input:focus{outline:2px solid var(--brand);outline-offset:1px;
  border-color:var(--brand)}
.searchwrap input::-webkit-search-cancel-button{display:none}
.searchwrap input::placeholder{font-family:var(--ui-font);opacity:1;color:var(--ink-muted)}
#clearq{position:absolute;right:4px;top:50%;transform:translateY(-50%);
  border:none;background:transparent;color:var(--ink-muted);font-size:17px;
  line-height:1;padding:2px 8px;cursor:pointer;border-radius:50%}
#clearq:hover{color:var(--ink);background:var(--chip)}
.pills{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:18px}
/* Hard offset shadow (no blur, solid colour) rather than a soft drop shadow —
   a distinct depth cue for these specifically, since KPI tiles/panels already
   use the soft --shadow-sm/md language elsewhere. Hover pulls the pill away
   from its shadow (offset grows); a real click presses it flush into the
   shadow (offset to 0) for tactile feedback. Kept modest (3-5px) since pills
   sit only 7px apart — a bigger offset would visually collide with the next
   pill over. var(--ink) inverts per theme (dark offset on a light pill, light
   offset on a dark one), so no new colour token is needed. */
.pill{font-size:12.5px;padding:6px 13px;border-radius:999px;cursor:pointer;
  background:var(--surface);border:1px solid var(--border);color:var(--ink-2);
  box-shadow:3px 3px 0 0 var(--accent);
  transition:background-color .12s ease,border-color .12s ease,color .12s ease,
    box-shadow .12s ease,transform .12s ease}
.pill:hover{border-color:var(--brand);transform:translate(-1px,-1px);
  box-shadow:5px 5px 0 0 var(--accent)}
.pill:active{transform:translate(3px,3px);box-shadow:0 0 0 0 var(--accent)}
/* Selected pill uses navy, not the brand green: the green is a background
   accent and white text on it fails contrast badly. Shadow stays lime,
   matching the View toggle's active state — that control's shadow lives on
   its shared wrapper, not per-button, so every segment (pressed or not)
   shares the same lime offset. */
.pill[aria-pressed="true"]{
  background:radial-gradient(ellipse at center,var(--brand-bg-light) 0%,var(--brand) 100%);
  border-color:var(--brand);color:#fff;font-weight:700;box-shadow:3px 3px 0 0 var(--accent)}
.pill[aria-pressed="true"]:hover{box-shadow:5px 5px 0 0 var(--accent)}
.pill[aria-pressed="true"]:active{box-shadow:0 0 0 0 var(--accent)}

/* Relevance is a lens, not a gate — this switches between the filtered default
   and everything collected. */
.viewtoggle{display:inline-flex;border:1px solid var(--border);border-radius:999px;
  overflow:hidden;box-shadow:3px 3px 0 0 var(--accent)}
.viewtoggle button{border:none;border-radius:0;padding:6px 15px;font-size:12.5px;
  background:var(--surface);color:var(--ink-2);cursor:pointer}
.viewtoggle button[aria-pressed="true"]{
  background:radial-gradient(ellipse at center,var(--brand-bg-light) 0%,var(--brand) 100%);
  color:#fff;font-weight:700}
/* Set-aside items are dimmed AND labelled — dimming alone is not a readable
   signal, and in the everything view the reader must be able to tell which
   items met the criteria. */
.dropped{opacity:.78}
.badge.setaside{background:transparent;border:1px solid var(--border);
  color:var(--ink-muted);font-weight:400}

#filters summary{display:none}          /* desktop: always expanded, no control */
#filters>.pillgroup:last-of-type{margin-bottom:18px}
.cols{display:grid;grid-template-columns:1fr 400px;gap:18px;align-items:start}
/* align-items:start, not stretch: whichever of colmain/colside is naturally
   taller varies with the data (rich update cards vs compact deadline rows),
   and forcing a shared row height just relocates the mismatch into whatever
   sits last — an empty bordered box on quickcontact, a blank slab inside
   .p-updates, a gap inside the deadlines panel. matchColumnHeights() in the
   script is the actual fix: it measures both columns after every render and
   trims whichever is taller (cardLimit or dlLimit) down to match, so the
   columns end at roughly the same point with real content, not filler. */
@media (max-width:900px){.cols{grid-template-columns:1fr}}

.panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:16px 18px;box-shadow:var(--shadow-sm);overflow:hidden}
.panel+.panel{margin-top:18px}
/* v1.6: Kaufman Rossin's own risk-intelligence tools (TrakRI, StatRI, ScrubRI,
   GeoRI...) structure every panel with a solid navy header strip, not a card
   shadow with an underlined label — evidenced across six real product
   screenshots. The negative margin pulls the strip to the panel's edges and
   its own radius mirrors the panel's (minus the border width), so it reads as
   the card's own top edge rather than a heading floating in the padding.
   .panel>h2 covers the plain div panel (agencies); .foldable>summary covers
   the two <details> panels, where the clickable/foldable element is summary,
   not h2 — the header strip has to live on whichever element actually owns
   the panel's padding-box edge. */
.panel>h2,.foldable>summary{margin:-16px -18px 14px;padding:10px 18px;
  background:radial-gradient(ellipse at center,var(--brand-bg-light) 0%,var(--brand-bg) 100%);
  color:#fff;border-radius:11px 11px 0 0;
  border-bottom:3px solid var(--accent)}
.panel>h2{font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;font-weight:700}
/* Two panels are <details> so they can fold on a phone. Suppress the native
   disclosure triangle at every width — on desktop they are plain panels with no
   affordance, and script blocks the click that would otherwise collapse them. */
.foldable>summary{list-style:none;display:block}
.foldable>summary::-webkit-details-marker{display:none}
.foldable>summary h2{cursor:default;margin:0;font-size:11.5px;letter-spacing:.07em;
  text-transform:uppercase;font-weight:700;color:inherit}
.panel .note{font-size:12px;color:var(--ink-2);margin:0 0 12px}
/* Scope line under the deadlines heading. */
.dlscope{font-size:12px;color:var(--ink-muted);margin:0 0 10px;
  font-variant-numeric:tabular-nums}

.card{padding:14px 0;border-bottom:1px solid var(--rule)}
.card:first-of-type{padding-top:0}
.card:last-child{border-bottom:none;padding-bottom:0}
.card .top{display:flex;align-items:center;gap:8px;margin-bottom:7px;flex-wrap:wrap}
.badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;background:var(--chip);color:var(--ink-2)}
.badge.t-Final{color:#fff;background:var(--crit)}
.badge.t-Proposed{color:#fff;background:var(--warn)}
.badge.t-Guidance{color:#fff;background:var(--brand)}
.badge.t-Enforcement{color:#fff;background:var(--neutral)}
.p-sdn .sdnintro{font-size:12.5px;color:var(--ink-muted);margin:2px 0 10px}
.p-sdn .sdnlist{display:flex;flex-direction:column;gap:2px}
.sdnrow{display:flex;align-items:center;gap:10px;padding:6px 0;
  border-bottom:1px solid var(--rule);flex-wrap:wrap}
.sdnrow[hidden]{display:none}
.sdnrow .badge{flex:none}
.sdnname{font-size:13px;font-weight:600;flex:1 1 auto;min-width:180px}
.sdnmeta{font-size:12px;color:var(--ink-muted);white-space:nowrap}
.sdnempty,.sdnnote{font-size:12.5px;color:var(--ink-muted)}
.sdnsearch{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.sdnsearch input{flex:1 1 320px;max-width:420px;appearance:none;-webkit-appearance:none;
  font-family:var(--ui-font);font-size:14px;padding:8px 12px;
  color:var(--ink);background:var(--surface);border:1px solid var(--border);
  border-radius:10px}
.sdnsearch input:focus{outline:2px solid var(--brand);outline-offset:1px;
  border-color:var(--brand)}
.sdnsearch input::-webkit-search-cancel-button{display:none}
.sdncount{font-size:12.5px;color:var(--ink-muted)}
.p-sdn h3{font-size:13px;text-transform:uppercase;letter-spacing:.04em;
  color:var(--brand);font-weight:700;margin:8px 0 8px;padding-bottom:4px;
  border-bottom:2px solid var(--accent);display:inline-block}
.sdnfullsearch{margin-top:4px}
.sdnfullcount{font-weight:400;text-transform:none;letter-spacing:0}
.sdnhighlights{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px 28px}
.sdnhighlights .sdnmini h3{margin:0;display:inline-block}
.sdnmini summary{cursor:pointer;list-style:none}
.sdnmini summary::-webkit-details-marker{display:none}
.sdnmini summary::before{content:"▸ ";color:var(--ink-muted);font-size:12px}
.sdnmini[open] summary::before{content:"▾ "}
/* Long entity names (some run 60+ chars) wrapped inconsistently against
   short ones when name+meta shared a row with flex-wrap — some rows one
   line, some two, reading as cramped/uneven. Every row now always stacks
   name then meta, so the rhythm is consistent regardless of name length. */
.sdnhighlights .sdnrow{flex-direction:column;align-items:flex-start;
  gap:2px;padding:9px 0}
.sdnhighlights .sdnmeta{white-space:normal}
.sdnhighlights .sdnlist{gap:0}
@media (max-width:900px){.sdnhighlights{grid-template-columns:1fr 1fr}}
@media (max-width:480px){.sdnhighlights{grid-template-columns:1fr}}
.sdnactivity{margin-top:14px;padding-top:10px;border-top:1px solid var(--rule)}
.sdnactivity summary{cursor:pointer;font-size:13px;font-weight:600;
  color:var(--ink-2)}
.sdnactivity[open] summary{margin-bottom:4px}
.sdnsince{font-weight:400;color:var(--ink-muted);font-size:12px}
/* --- name-screening: modes, bulk, per-match detail, source badges --- */
.sdnmodes{display:flex;gap:4px;margin:2px 0 8px}
.sdnmode{font:inherit;font-size:12px;font-weight:600;padding:5px 11px;cursor:pointer;
  color:var(--ink-2);background:var(--surface);border:1px solid var(--border);
  border-radius:999px}
.sdnmode.is-on{color:#fff;background:var(--brand);border-color:var(--brand)}
.sdnbulk{display:flex;flex-direction:column;gap:8px;margin-bottom:10px;max-width:520px}
.sdnbulk textarea{font-family:var(--ui-font);font-size:13.5px;padding:8px 12px;
  color:var(--ink);background:var(--surface);border:1px solid var(--border);
  border-radius:10px;resize:vertical}
.sdnbulk textarea:focus{outline:2px solid var(--brand);outline-offset:1px;border-color:var(--brand)}
#sdnbulkgo{align-self:flex-start;font:inherit;font-size:13px;font-weight:600;
  padding:7px 16px;cursor:pointer;color:#fff;background:var(--brand);
  border:1px solid var(--brand);border-radius:10px}
.sdndisclaimer{font-size:12px;line-height:1.5;color:var(--ink-muted);
  margin:0 0 12px;max-width:600px}
.sdndisclaimer strong{color:var(--ink-2)}
.sdnfeeds{font-size:12px;color:var(--ink-muted);margin:12px 0 0}
.badge.sdnsrc{font-size:10px;letter-spacing:.03em;padding:1px 6px;
  background:var(--brand);color:#fff;vertical-align:middle}
.badge.sdnsrc.nonsdn{background:var(--neutral)}
.sdnalias{font-size:11.5px;color:var(--ink-muted);font-weight:400}
.sdndetail{flex-basis:100%;font-size:11.5px;color:var(--ink-muted);
  line-height:1.5;margin-top:2px}
.sdndetail a{color:var(--brand);white-space:nowrap}
.sdnbulkrow .sdnname{font-weight:600}
.sdnbulkrow.hit .sdnname::after{content:" ●";color:var(--crit)}
.sdnbulkrow .sdnmeta{white-space:normal}
.card .agency{font-size:12px;color:var(--ink-muted)}
.card h3{font-size:14.5px;margin:0 0 5px;font-weight:600;line-height:1.35;
  text-align:justify;text-align-last:left}
.card h3 a{color:var(--brand);text-decoration:none}
.card h3 a:hover{text-decoration:underline}
.card p{margin:0;font-size:13px;color:var(--ink-2);text-align:justify;
  text-align-last:left;hyphens:auto}
.card .meta{margin-top:7px;font-size:12px;color:var(--ink-muted);
  font-variant-numeric:tabular-nums}
.u{font-weight:600}
.u::before{content:"● "}
.u-High{color:var(--crit)} .u-Medium{color:var(--warn)} .u-Low{color:var(--ink-muted)}

.dl{display:block;padding:11px 0;border-bottom:1px solid var(--rule)}
/* Everything sits flush with the card edge (matching the update cards); the
   urgency dot is a bullet at the start of the agency line, aligned with the
   headline below it. */
.dl .agency{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--ink-muted);margin-bottom:2px}
.dl .agency .dot{flex:none;width:8px;height:8px;border-radius:50%}
.dl .ttl{font-size:13.5px;font-weight:600;line-height:1.35;
  text-align:justify;text-align-last:left}
.dl .ttl a{color:var(--ink);text-decoration:none}
.dl .ttl a:hover{text-decoration:underline}
.dl .when{font-size:12px;margin-top:3px;font-variant-numeric:tabular-nums}
.soon{color:var(--crit)} .mid{color:var(--warn)} .far{color:var(--ok)}
/* Per-deadline "add to calendar". Sits on the same row as the date rather than
   its own line — on a phone six of these each costing a line put ~140px back
   onto a panel we had just spent effort shrinking. Quiet until hovered so it
   doesn't compete with the deadline itself. */
.dlfoot{display:flex;align-items:center;justify-content:space-between;gap:10px;
  margin-top:3px}
.dlfoot .when{margin-top:0}
/* Inline audio player for an item's spoken plain-English summary. One shared
   <audio> element drives every bar (see the script). Colours are existing
   tokens, so dark mode is covered. */
.rp{display:flex;align-items:center;gap:8px;margin-top:8px;background:var(--raised);
  border:1px solid var(--border);border-radius:8px;padding:6px 9px}
.rp-btn{flex:none;width:28px;height:28px;border-radius:50%;border:none;padding:0;
  background:var(--brand);color:#fff;display:inline-flex;align-items:center;
  justify-content:center;cursor:pointer}
.rp-btn svg{width:11px;height:11px;fill:currentColor}
.rp-t{flex:none;font-size:10.5px;color:var(--ink-muted);
  font-variant-numeric:tabular-nums}
.rp-track{flex:1;min-width:70px;position:relative;height:5px;border-radius:3px;
  background:var(--rule);cursor:pointer}
.rp-fill{position:absolute;left:0;top:0;bottom:0;width:0;background:var(--brand);
  border-radius:3px}
/* Unscoped (was .dl .cal) — the same button now also appears on update cards
   in the main feed, not just the sidebar deadline list. */
.cal{flex:none;font:inherit;font-size:11.5px;color:var(--brand);
  background:none;border:1px solid transparent;border-radius:8px;
  padding:4px 7px;cursor:pointer;white-space:nowrap;
  display:inline-flex;align-items:center;justify-content:center;gap:4px;
  transition:border-color .12s ease}
.cal:hover{border-color:var(--border);text-decoration:underline}
/* Card version: meta text left, one or two (rare — an item can have both a
   comment deadline and an effective date) calendar buttons right. Wraps
   rather than truncating when both are present. */
.cardfoot{display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:4px 10px;margin-top:7px}
.cardfoot .meta{margin-top:0}
.cardfoot .cal{padding:2px 7px}
/* Groups calendar buttons (0-2, only when dated) with the always-present
   Notes/Tasks icons into one right-hand cluster, so cardfoot/dlfoot stay a
   clean two-group flex (meta left, actions right) instead of the three
   loose children space-between would otherwise spread unevenly. */
.actions{display:flex;align-items:center;gap:4px;flex-wrap:wrap}
.itemrow{display:flex;gap:2px}
/* Notes/Tasks icon buttons — same interaction language as .cal but icon-only
   (aria-label carries the name) since they sit on every item, dated or not,
   and a text label on two more buttons per card would crowd the list. The
   dot marks items that already have a saved note or an open task, so a
   reader scanning the feed can tell what they've already annotated without
   opening each one. */
.itembtn{position:relative;flex:none;display:inline-flex;align-items:center;
  justify-content:center;width:28px;height:28px;padding:0;color:var(--ink-muted);
  background:none;border:1px solid transparent;border-radius:8px;cursor:pointer;
  transition:border-color .12s ease,color .12s ease}
.itembtn:hover{border-color:var(--border);color:var(--brand)}
.itembtn .dot{position:absolute;top:3px;right:3px;width:7px;height:7px;
  border-radius:50%;background:var(--accent);display:none}
.itembtn.has .dot{display:block}

.agrow{display:grid;grid-template-columns:120px 1fr 74px;gap:7px 10px;align-items:center}
.agrow .n{font-size:12.5px;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meter{position:relative;height:11px;background:var(--track);border-radius:6px;overflow:hidden}
.meter>span{position:absolute;inset:0 auto 0 0;background:var(--bar);border-radius:6px}
.agrow .c{font-size:12px;color:var(--ink-2);font-variant-numeric:tabular-nums}
.empty{color:var(--ink-2);font-size:13px;padding:8px 0}

/* Small bordered author card — kaufmanrossin.com's own blog-post byline box
   (photo left, name/title right, tiny icon buttons underneath), not the
   bigger square-photo Key Contacts card. Ordinary surface card like .notice
   above it, sitting on the page itself rather than the navy footer below. */
.quickcontact{margin-top:22px;background:var(--surface);border:1px solid var(--accent);
  border-radius:12px;padding:22px 24px;display:flex;gap:18px;align-items:flex-start;
  box-shadow:var(--shadow-sm)}
.quickcontact .qc-photo img{display:block;width:88px;height:88px;border-radius:50%;
  object-fit:cover}
.quickcontact .qc-text{min-width:0}
.quickcontact .qc-name{display:block;font-size:15px;font-weight:700;color:var(--brand);
  text-decoration:none}
.quickcontact .qc-name:hover{text-decoration:underline}
.quickcontact .qc-title{font-size:13.5px;color:var(--ink-2);margin-top:3px;line-height:1.45}
.quickcontact .qc-icons{display:flex;gap:6px;margin-top:12px}
/* Fixed dark squares regardless of theme — matches kaufmanrossin.com's own
   icon buttons exactly, same reasoning as the header/logo staying fixed. */
.quickcontact .qc-icons a{display:inline-flex;align-items:center;justify-content:center;
  width:28px;height:28px;border-radius:6px;background:#212529;color:#fff;
  transition:filter .12s ease}
.quickcontact .qc-icons a:hover{filter:brightness(1.3)}

/* Footer band. Full-bleed navy, mirrors kaufmanrossin.com's own footer — this
   is the point of publishing the tool, so it gets real estate at the bottom,
   not a link buried in small print. It sits outside .wrap in the markup so
   the navy reaches both edges; .footwrap re-applies the same 1240px column so
   the content still lines up with everything above it. Fixed white/light text
   throughout, not var(--ink) — same reasoning as the header: brand chrome on
   a fixed navy background in every theme, not a theme-following surface. */
footer.sitefoot{margin-top:22px;background:var(--brand-bg)}
/* Same 1fr/400px split as .cols above, and the same 22px padding as .wrap
   — so the second column lands at exactly the same x as "Updates by
   agency" does in the main content, not an eyeballed approximation. */
.footwrap{max-width:1240px;margin:0 auto;padding:22px 22px 16px;
  display:grid;grid-template-columns:1fr 400px;gap:18px;align-items:start}
.footbrand{display:flex;flex-direction:column}
/* kaufmanrossin.com's own footer runs the wordmark flush white on navy, no
   plate behind it. The source SVG's ink is fixed (navy body, grey subtext),
   not theme-aware, so a plain recolour can't reach that — brightness(0)
   flattens the fill to black first, then invert(1) turns it pure white.
   Applied to .cls-1/.cls-3 only, not the whole logo: .cls-2 is the accent
   bar between KAUFMAN and ROSSIN, already var(--accent)'s lime by default
   in the source SVG, and stays lime rather than washing out to white. */
.footbrand .logowrap{display:inline-block}
.footbrand .krlogo{display:block;width:220px;height:auto}
.footbrand .krlogo .cls-1,.footbrand .krlogo .cls-3{filter:brightness(0) invert(1)}
/* kaufmanrossin.com's own footer wordmark drops the "cpa + advisors" line
   entirely — just the mark. .cls-3 is that subtext specifically (traced
   from the header version, which does want it), so hiding it here rather
   than touching the shared KR_LOGO_SVG constant. */
.footbrand .krlogo .cls-3{display:none}
/* Social row. Kept well clear of the logo, near the legal strip at the
   bottom — same placement kaufmanrossin.com's own footer uses, not
   crowded directly under the wordmark. */
.footsocialwrap{max-width:1240px;margin:0 auto;padding:0 22px 24px}
.footsocial{display:flex;gap:8px}
.footsocial .social-btn{display:inline-flex;align-items:center;justify-content:center;
  width:40px;height:40px;border-radius:50%;background:var(--accent);color:#00294a;
  transition:filter .12s ease}
.footsocial .social-btn svg{width:22px;height:22px}
.footsocial .social-btn:hover{filter:brightness(1.1)}
/* Locations / Quick Links / Subscribe — kaufmanrossin.com's own footer nav,
   pointed at the real pages on its site since Klearance has none of its own
   (no blog, no offices, no careers page). Every link here leaves the site,
   same reasoning as Full bio above: target=_blank, never orphan the reader
   mid-update-list. */
.footnav{display:flex;flex-wrap:wrap;gap:14px}
.footcol{display:flex;flex-direction:column;gap:6px;min-width:100px}
.footcol h3{font-size:12px;letter-spacing:.06em;text-transform:uppercase;
  color:#fff;margin:0 0 3px;font-weight:600}
/* Measured off kaufmanrossin.com's own footer: 14px, weight 300, pure
   white. The earlier 600-weight/off-white version was overcorrecting —
   what read as "bold" on their site is white-on-navy optical bleed, not
   actual font-weight; theirs is lighter than ours was, not heavier. */
.footcol a{font-size:8px;font-weight:700;color:#fff;text-decoration:none}
.footcol a:hover{text-decoration:underline}
.footsub p{font-size:13px;color:#c9d6e3;margin:0 0 10px}
/* Sweep, not fade. A plain background-color transition has no direction —
   it just crossfades everywhere at once. This is the standard trick: a
   two-stop gradient at 200% width, only half visible at a time, and
   sliding background-position across it reads as the fill sweeping in
   from the left. Text stays one dark colour throughout — it has enough
   contrast on both white and lime — so nothing needs to sync against it. */
.footsub a.btn{display:block;width:100%;box-sizing:border-box;text-align:center;
  font-size:13px;font-weight:400;padding:9px 16px;border-radius:0;
  text-decoration:none;color:#212529;
  border:1px solid #fff;border-left:8px solid var(--accent);
  background:linear-gradient(to right,var(--accent) 50%,#fff 50%);
  background-size:200% 100%;background-position:right bottom;
  transition:background-position .5s ease,border-color .5s ease}
.footsub a.btn:hover,.footsub a.btn:active{background-position:left bottom;border-color:var(--accent)}
/* Legal strip. The divider spans the full navy width; the text inside
   re-centers to the same 1240px column as .footwrap above it. Copied
   verbatim from kaufmanrossin.com's own footer, entities and all — this is
   the firm's standard boilerplate, not Klearance-specific text. */
.footlegalwrap{border-top:1px solid rgba(255,255,255,.15)}
.footlegal{max-width:1240px;margin:0 auto;padding:14px 22px 20px;
  font-size:11px;color:#8fa6bc;line-height:1.6}
.footlegal p{margin:0 0 8px}
.footlegal a{color:#9fb3c8;text-decoration:underline}
.footlegal a:hover{color:#fff}
/* Matches kaufmanrossin.com's own footer exactly: this one link is blue
   (#007bff), Legal Disclaimer/Privacy Policy next to it are plain white. */
.footlegal a.donotsell{color:#007bff}
.footlegal a.donotsell:hover{color:#3395ff}
.footbottom{display:flex;flex-wrap:wrap;justify-content:space-between;
  gap:6px 20px;margin-top:10px;padding-top:10px}

/* ===================================================================
   PHONE LAYOUT LIVES LAST, AND MUST STAY LAST.

   Media-query rules carry no extra specificity, so any base rule written
   below this block beats it on source order alone. It used to sit in the
   middle of the stylesheet with ~70 lines after it, and seven overrides
   were silently dead: .panel padding, .card h3 size and line-height,
   .card p size, .contact padding and gap. Phones were quietly served
   desktop spacing and nothing pointed at it - the page just looked a bit
   wrong. Add new base rules ABOVE this block, never below.
   =================================================================== */
/* Phone layout. The desktop proportions put 1,434px of header, counts and
   filters above the first actual update — nearly two full screens of scrolling
   before any content, on the device most LinkedIn traffic arrives from.
   Everything here buys that height back.

   Not width alone. A phone in landscape is around 800px wide, so a plain
   max-width:640px rule handed it the full desktop layout on a 375px-tall
   screen: updates above deadlines, the filter block fully expanded, nothing
   foldable, small tap targets. The pointer test catches a phone in either
   orientation; the width bound keeps large tablets and touch-capable laptops on
   the desktop layout, which is what suits them.

   MUST stay in sync with the MOBILE matchMedia in the script below. */
@media (max-width:640px), (hover:none) and (pointer:coarse) and (max-width:1024px){
  /* Type scale. The old one ran 11-12.5px for everything secondary, which on a
     phone reads as dense rather than compact — small type is the main thing that
     makes a page feel clunky rather than considered. Nothing here is larger than
     its desktop size; the phone just stops being punished. */
  .wrap{padding:12px}
  body{font-size:15px}

  /* The header was 152px — 18% of an iPhone screen — as a solid navy slab with
     a two-line title and a three-line subtitle. Two causes, both fixed here
     rather than by shrinking type:
       1. "Export CSV" took 102px of a 366px row, leaving the title 220px and
          forcing the wrap. The button is now hidden outright on phones —
          downloading a spreadsheet is a desktop action, and nobody opens a CSV
          on a handset to do work with it. That returns the whole row to the
          title. The button stays in the markup so its click handler always has
          an element; display:none also takes it out of the tab order, which is
          what we want when it is not offered.
       2. The audience and the timestamp ran together into three ragged lines.
          They are separate blocks now, and the stamp is dimmed — it is
          reference, not a headline.
     The accent rule also drops 4px to 3px: at phone width a 4px bar reads as a
     third element rather than a trim. */
  /* The replicated corporate chrome (utility strip + full nav) costs another
     ~166px on top of the budget above — worth it on desktop for the brand
     match, not on a phone. The utility strip drops outright; the nav bar
     keeps just the wordmark, same "downloading a spreadsheet is a desktop
     action" logic as #export just above. */
  .krtop{display:none}
  .krheaderwrap{padding:10px 16px}
  .krheaderwrap .krlogo{width:140px}
  .krheader-toprow{display:none}
  .krheader nav{display:none}
  /* NOT display:none -- the Share/Install/More icon buttons live inside
     .krcrumb (see the HTML comment above .icon-toolbar) and are meant to
     stay visible on a phone per that same comment ("mobile-first actions").
     A blanket hide here would silently take them out along with the rest
     of the row. */
  .pagehead{gap:18px;margin-bottom:14px;padding-bottom:14px;
    flex-direction:column;align-items:flex-start}
  h1{font-size:22px;line-height:1.2;margin:0 0 4px}
  .sub{font-size:12.5px;line-height:1.4}
  /* Export CSV re-enabled on mobile per Alexander -- it now lives behind the
     More (kebab) button instead of its own row-crowding button, so it no
     longer costs the title its width the way the old always-visible button
     did. Only the button's own inline placement was the phone problem;
     tucking it behind a menu keeps it reachable without that cost. */
  /* Full-format stamp was wrapping to its own left-aligned line below the
     breadcrumb path -- the row only has width for path + one more item, and
     "Updated [full date/time]" was winning that slot over the icons every
     time. Hiding it here lets path + icon-toolbar share the single row that
     justify-content: space-between on .krcrumbwrap already wants to give
     them -- icons land in the upper-right corner, same relative position as
     desktop. The short-format .krcrumb-updated-mobile below replaces it on
     its own deliberate full-width row instead of an accidental wrap. */
  .krcrumb-updated{display:none}
  /* align-items:center (set on .krcrumbwrap for the desktop single-line
     layout) vertically centers the path text inside the 36px-tall icon
     buttons' row height, leaving visible padding above and below it before
     the wrapped line below even starts. flex-end anchors the path to the
     row's bottom edge instead, so the gap down to .krcrumb-updated-mobile is
     just row-gap — tightened here since column-gap (path-to-icons spacing on
     the row above) doesn't need to match it. */
  .krcrumbwrap{align-items:flex-end;row-gap:4px}
  .krcrumb-updated-mobile{display:block;order:3;flex-basis:100%;
    color:#6c757d;font-size:12.5px;text-align:left}
  /* Stays at readable body size on purpose — see the .notice comment above; a
     public tool cannot put its caveats in the footer. Only the padding and the
     leading tighten here, and the deadline explanation moved into the coverage
     panel. The caveat itself is not shrunk to get the height down. */
  .notice{padding:12px 13px;font-size:13px;line-height:1.45;margin-bottom:14px}

  /* Two-up instead of stacked: four numbers in half the height. Row height
     must size to content (auto), not the desktop 1fr -- with the desktop
     value still active here, the grid forced every tile in a row to the
     same fixed height and .kpi's overflow:hidden clipped any label that
     needed a 3rd wrapped line mid-word ("UPDAT", "COMM PERIO"). The agency
     panel also needs its own explicit grid-column reset: without it, its
     desktop-only grid-column:3 placement (below) still applies here and
     grid auto-creates an implicit 3rd column to satisfy it, squeezing the
     two real 1fr columns down to whatever width is left over -- which is
     the second half of the same bug, not a separate one. */
  .kpis{grid-template-columns:1fr 1fr;grid-auto-rows:auto;gap:8px;margin-bottom:14px}
  .kpis .p-agencies{grid-column:1 / -1;grid-row:auto}
  /* A light shrink: tighter padding and a slightly smaller value number, to buy
     back a little height above the first update without making the tiles cramped
     — the big number stays the headline. */
  .kpi{padding:10px 12px;border-radius:12px;text-align:center}
  /* .kpi[data-kpi] (the clickable tiles -- non-zero counts, "Open comment
     periods" and "Effective this quarter" here) carries its own desktop
     text-align:left for cursor/hover styling, and its higher specificity
     (class+attribute beats plain class) wins over the .kpi rule above
     regardless of source order -- only the two clickable tiles stayed
     left-aligned while the other two centered correctly. Same-specificity
     override, scoped to mobile. */
  .kpi[data-kpi]{text-align:center}
  /* kpi-brk forces all four labels to the same explicit two-line shape (see
     its Python-side comment in kpis()) rather than leaving line count to
     each label's own text length and whatever font the device actually
     renders -- that was producing a real, on-device mismatch (two tiles
     wrapped, two didn't). Flex broke it (a flex item ignores its own
     display value, so display:block never forced anything, and the flex
     spec strips a whitespace-only text run next to another item, eating
     the space between words on short labels). table-cell fixed that but
     introduced a different bug: a lone table-cell sizes to its own content
     width, not the tile's full width, so the navy strip stopped reaching
     the tile's edges. Plain block needs neither trick: since every label
     is now guaranteed the same two lines, its natural content height is
     already equal across all four, so the existing symmetric padding
     alone centers it vertically -- no min-height, vertical-align, or
     special display value required, and block's default 100% width keeps
     the strip spanning the tile like it always did on desktop. */
  .kpi-brk{display:block}
  .kpi .l{margin:-10px -12px 10px;padding:6px 12px;text-align:center}
  .kpi .v{font-size:23px;margin:2px 0 1px}
  .kpi .l,.kpi .n{font-size:11.5px;line-height:1.3}
  /* Short phrasing so no tile note wraps: one wrapped note made the bottom row
     16px taller and left the tile beside it looking half empty. */
  .kpi .n-long{display:none}
  .kpi .n-short{display:inline}

  /* Label above the controls rather than beside them — the fixed 104px column
     was taking 31% of a 331px content width and pushing the source pills onto
     five rows. */
  .grouplabel{width:100%;flex:0 0 100%;margin-bottom:3px}
  .grouplabel small{display:inline;margin-left:6px}
  .pillgroup{gap:7px;margin-bottom:13px}
  .searchwrap{flex:1 1 100%;max-width:none}

  /* 16px is not a style choice. Safari zooms the whole page when you focus an
     input smaller than 16px, so tapping search used to lurch the layout and
     leave the reader pinched in. This is the fix for that, not a size bump. */
  #q{font-size:16px;padding-top:11px;padding-bottom:11px}

  .panel{padding:14px 15px}
  /* Must match .panel's own phone padding above, same reasoning as the
     desktop rule — the strip's negative margin cancels exactly the padding
     it sits inside, nothing more or less. */
  .panel>h2,.foldable>summary{margin:-14px -15px 14px;padding:9px 15px}
  .cols{gap:14px}
  .card h3{font-size:15px}
  .card p{font-size:13.5px}

  .rr-table td,.rr-table th{padding:4px 5px;font-size:11.5px}
  .rr-letter{width:46px}
  /* The desktop grid's fixed 400px second column would overflow a phone
     screen, so this reverts to a plain stacked flex column. */
  .footwrap{display:flex;flex-direction:column;padding:20px 16px 16px;gap:16px}
  /* Stacked, the divider reads better under the logo than as a stray line
     hanging off the wrapped row. */
  .footbrand{padding-bottom:14px;border-bottom:1px solid rgba(255,255,255,.15);
    width:100%}
  .footnav{width:100%;gap:18px 22px;padding-top:14px;
    border-top:1px solid rgba(255,255,255,.15)}
  .footsocialwrap{padding:0 16px}
  .footbottom{flex-direction:column;gap:8px}

  /* Stacked columns put deadlines eight screens down, below every update card,
     even though they are the most actionable thing on the page. display:contents
     lifts the panels out of their column wrappers so they can be ordered.
     Updates by agency isn't part of .cols any more — it moved into .kpis —
     so there's no order rule for it here. */
  .cols{display:flex;flex-direction:column;gap:14px}
  .colmain,.colside{display:contents}
  .p-deadlines{order:1}
  .p-updates{order:2}
  #alsofound{order:3}
  .quickcontact{order:4}

  /* Touch targets. Apple's guideline is 44px and these measured 33-35px, which
     is the difference between tapping a filter and aiming at one. min-height
     with centred content rather than more padding, so the pill rows do not grow
     taller than they need to. */
  .pill,.viewtoggle button{min-height:44px;display:inline-flex;align-items:center;
    justify-content:center;font-size:13px}
  .pill{padding:0 14px}
  .viewtoggle button{padding:0 16px}
  .viewtoggle{border-radius:14px}
  #showmore,#dlmore{min-height:44px}
  /* Inline on the date row, so it costs no extra line, but padded enough to be
     tappable. A full 44px here would grow every deadline row instead. */
  .dl .cal,.cardfoot .cal{min-height:34px;padding:0 10px;border-color:var(--border)}
  .itembtn{width:34px;height:34px;border-color:var(--border)}
  /* The real cause, found by measuring actual rendered rects rather than
     guessing: .dlfoot is flex with no flex-basis set on either child, so
     .actions (the icon group) was getting SHRUNK to whatever width was left
     over after .when's date text, not its own natural content width -- and
     that leftover width came in under what cal+notes+tasks actually need by
     less than a pixel, so the icon group wrapped internally (calendar alone
     on row 1, notes+tasks together on row 2) even though the icons
     themselves had room if given priority. flex-shrink:0 makes .actions
     keep its full natural width always; .when's date text already wraps to
     two lines cleanly (see its own multi-line date+day-count text), so it's
     the one that should absorb width pressure, not the icon group. The gap
     trims below still help -- less width for .actions to demand in the
     first place -- but weren't the actual fix on their own. */
  .dl .actions{gap:2px;flex-shrink:0}
  .dl .itemrow{gap:1px}
  .dl .cal{padding:0 7px}
  /* Same squeeze on the update card's footer: keep the icon cluster at its
     natural width so the date/severity text absorbs the pressure instead of
     the actions wrapping above the row. */
  .cardfoot .actions{flex-shrink:0}
  .cardfoot .meta{min-width:0}
  .quickcontact .qc-icons a{width:36px;height:36px}
  .card h3{line-height:1.45}
  .card h3 a{display:inline-block;padding:2px 0}
  #showmore,#dlmore{width:100%;margin-top:12px;padding:11px;font-weight:600;
    color:var(--brand);background:var(--surface)}

  /* Foldable panels. Only here, and only as an affordance the reader can use --
     both stay open on arrival. Collapsing the update list by default would show
     a visitor from LinkedIn a page of headings and nothing else. */
  .foldable>summary{cursor:pointer;list-style:none;display:block}
  .foldable>summary::-webkit-details-marker{display:none}
  /* Flex so the disclosure arrow can sit hard right. The card-count span floats
     right on desktop; inside a flex row that float is ignored and it simply
     lands between the title and the arrow, which reads correctly on a phone. */
  .foldable>summary h2{display:flex;align-items:center;gap:8px}
  /* Arrow inherits white from the navy strip now — var(--brand) here would be
     navy-on-navy, invisible. */
  .foldable>summary h2::after{content:"▸";margin-left:auto;font-size:13px;
    line-height:1;color:inherit}
  .foldable[open]>summary h2::after{content:"▾"}
  /* The strip's own bottom margin is the gap to the content below it; with the
     panel shut there is no content, so that margin becomes dead space between
     the strip and the panel's bottom edge. Zeroed on the summary itself, which
     is what carries the margin now (not h2 — see the base panel-header rule). */
  .foldable:not([open])>summary{margin-bottom:0}

  /* Collapse the filter block. 327px of pills sat above the first update on a
     phone; search stays OUTSIDE this <details> — now directly below the collapsed
     summary — because it is the control people reach for and must stay visible. */
  #filters summary{display:block;cursor:pointer;list-style:none;
    font-size:12.5px;font-weight:700;color:var(--brand);padding:9px 13px;
    background:var(--surface);border:1px solid var(--border);border-radius:8px;
    margin-bottom:12px}
  #filters summary::-webkit-details-marker{display:none}
  #filters summary::after{content:" ▸";}
  #filters[open] summary::after{content:" ▾";}
  #filters[open] summary{margin-bottom:10px}
}
"""

JS = r"""
const DATA = JSON.parse(document.getElementById('data').textContent);
const AUDIO_KEYS = new Set(JSON.parse((document.getElementById('audiokeys') || {}).textContent || '[]'));
// Feed names grouped under the agency that publishes them. Readers think "OCC",
// not "OCC versus OCC Bulletins".
const GROUPS = JSON.parse(document.getElementById('groups').textContent);
// feed name -> agency label, for the by-agency chart.
const FEED_TO_AGENCY = {};
GROUPS.forEach(([label, feeds]) => feeds.forEach(f => { FEED_TO_AGENCY[f] = label; }));
const TODAY = document.body.dataset.today;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const days = d => Math.round((new Date(d) - new Date(TODAY)) / 86400000);

// Deferred until Chrome decides installability criteria are met; may never
// fire at all (desktop Safari, already installed, unsupported browser) —
// installBtn falls back to a toast in that case rather than doing nothing
// silently. Registered up here, immediately, since the event can fire before
// any of the click handlers below are wired up.
let deferredInstallPrompt = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  deferredInstallPrompt = e;
});

const toastEl = $('#iconToast');
let toastTimer = null;
function showToast(msg) {
  if (!toastEl) return;
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2600);
}

$('#shareBtn').addEventListener('click', async () => {
  const shareData = {title: document.title, url: location.href};
  if (navigator.share) {
    try { await navigator.share(shareData); } catch (e) { /* user cancelled the sheet — not an error */ }
    return;
  }
  try {
    await navigator.clipboard.writeText(location.href);
    showToast('Link copied');
  } catch (e) {
    showToast('Copy failed — copy the address bar instead');
  }
});

$('#installBtn').addEventListener('click', async () => {
  if (deferredInstallPrompt) {
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    return;
  }
  showToast(window.matchMedia('(display-mode: standalone)').matches
    ? 'Already installed'
    : 'Use your browser menu to install or bookmark this page');
});

const moreBtn = $('#moreBtn'), moreMenu = $('#moreMenu');
function closeMoreMenu() {
  moreMenu.hidden = true;
  moreBtn.setAttribute('aria-expanded', 'false');
}
moreBtn.addEventListener('click', e => {
  e.stopPropagation();
  const opening = moreMenu.hidden;
  moreMenu.hidden = !opening;
  moreBtn.setAttribute('aria-expanded', String(opening));
});
document.addEventListener('click', e => {
  if (!moreMenu.hidden && !e.target.closest('.icon-btn-wrap')) closeMoreMenu();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !moreMenu.hidden) closeMoreMenu();
});
$('#export').addEventListener('click', closeMoreMenu);

let filter = {kind: 'all', value: ''};
let query = '';
// Relevance is a lens, not a gate. The profile it screens against is one
// person's view of what matters; a public audience does not share it. Default to
// the filtered view because that is the useful default, but everything the
// agencies published stays one click away.
let showAll = false;

// Search runs IN ADDITION to whichever pill is active, so "FinCEN" + "stablecoin"
// narrows rather than replacing the pill selection.
// Terms match at the START of a word, not anywhere inside one. Plain substring
// matching produced false hits that were hard to spot: searching "regulation gg"
// returned an item about Regulation O, because "gg" sits inside "trigger".
// Anchoring to a word boundary still allows prefixes, so "stablecoin" finds
// "stablecoins" and "reg" finds "regulation".
const rxCache = new Map();
function termRx(t) {
  if (!rxCache.has(t)) {
    const lit = t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    // Short terms must match a WHOLE word; longer ones may match a prefix.
    //
    // Regulation designators are single letters, and a prefix match on "d" hits
    // data, disparate, directors, delay — so "Regulation D" returned Reg B and
    // Reg O items. Prefix matching is still wanted for real words ("stablecoin"
    // should find "stablecoins"), so the rule is length-based rather than global.
    const rx = t.length <= 2 ? `\\b${lit}\\b` : `\\b${lit}`;
    rxCache.set(t, new RegExp(rx, 'i'));
  }
  return rxCache.get(t);
}

function matchesQuery(d) {
  if (!query) return true;
  const hay = (d.title + ' ' + d.why + ' ' + d.sources.join(' ') + ' ' +
               (d.tags || []).join(' ') + ' ' + (d.type || '')).toLowerCase();
  // Every whitespace-separated term must appear, so extra words narrow the
  // result instead of widening it the way an OR match would.
  return query.split(/\s+/).every(t => termRx(t).test(hay));
}

function rows() {
  return DATA.filter(d => {
    if (!showAll && !d.relevant) return false;
    if (!matchesQuery(d)) return false;
    // One agency, several feeds — the pill value is pipe separated so "OCC"
    // covers both the press feed and the bulletins.
    if (filter.kind === 'agency') {
      return filter.value.some(group => group.split('|').some(f => d.sources.includes(f)));
    }
    // Fintech uses the classifier's explicit judgment, not a word match.
    // Searching the text instead returns 63 items where the classifier finds 48,
    // agreeing on only 31: it misses 17 genuinely fintech items and adds 32 that
    // are not, because most summaries carry the phrase "community banks and
    // fintechs" regardless of subject. This is why it is a control, not a search.
    if (filter.kind === 'fintech') return d.fintech === true;
    // Credit unions likewise use the classifier's judgment, not the NCUA source:
    // NCUA publishes plenty that is not credit-union-specific, and interagency
    // credit-union items arrive under other agencies' names.
    if (filter.kind === 'credit_union') return d.credit_union === true;
    // KPI tiles: d.kpi holds the tile keys this item satisfies, tagged at build
    // time so the tile count and this list are the same computation.
    if (filter.kind === 'kpi') return (d.kpi || []).includes(filter.value);
    return true;
  });
}

// Items the relevance filter dropped, shown ONLY when searching. The filter is a
// judgment and it can be wrong for a specific reader — a bank in Tennessee wants
// the Tennessee disaster-relief notice even though it is not a broad regulatory
// change. Without this, search silently misses 280 items and looks like proof
// that nothing exists.
// The reg reference filters with the same search box, so "Regulation B" or
// "1002" surfaces the lookup row as well as the tracked items.
function renderRegRef() {
  const rows = document.querySelectorAll('#regref tr.rr');
  if (!rows.length) return;
  let shown = 0;
  rows.forEach(tr => {
    const hit = !query || query.split(/\s+/).every(t => termRx(t).test(tr.dataset.rr));
    tr.classList.toggle('hidden', !hit);
    if (hit) shown++;
  });
  document.querySelectorAll('#regref .rr-group').forEach(g => {
    g.classList.toggle('hidden', !g.querySelectorAll('tr.rr:not(.hidden)').length);
  });
  // Open the panel automatically when a search matches a regulation.
  const det = document.getElementById('regref');
  if (query && shown && shown < rows.length) det.open = true;
}

function renderFilteredOut() {
  const box = $('#alsofound');
  // Redundant when the full set is already on screen.
  if (!query || showAll) { box.innerHTML = ''; return; }
  const hits = DATA.filter(d => !d.relevant && matchesQuery(d)).slice(0, 15);
  if (!hits.length) { box.innerHTML = ''; return; }
  box.innerHTML = `
    <div class="panel" style="margin-top:18px">
      <h2>Also found — items the relevance filter set aside (${hits.length})</h2>
      <p class="note">These did not meet the community bank / fintech criteria, so
      they are not in the counts above. Shown because they match your search.</p>
      ${hits.map(d => `
        <div class="card dropped">
          <div class="top">
            <span class="badge">${esc(d.type || '—')}</span>
            <span class="agency">${esc(d.sources.join(' · '))}</span>
          </div>
          <h3><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a></h3>
          <p>${esc(d.why)}</p>
          <div class="cardfoot">
            <div class="meta">${esc(d.date)}</div>
            <div class="actions">${calButtons(d)}${itemActionButtons(d)}</div>
          </div>
        </div>`).join('')}
    </div>`;
}

// One initial card limit on every screen size, not just phones. At 25 the
// desktop page ran deep before the contact card and footer came into view;
// 8 matches what the phone was already doing, so there's one number to
// reason about instead of two. The rest are one click/tap away.
//
// MOBILE still drives ordering, folding and the filter block below —
// driven by a matchMedia listener rather than a one-off check at load: a
// load-time read is unreliable and would also strand a phone that rotates
// into landscape with the narrow layout.
// MUST match the phone media query in the stylesheet above. A phone in
// landscape is ~800px wide, so testing width alone treated it as a desktop and
// switched off the folding, the deadlines-first ordering and the collapsed
// filter block exactly when the 375px-tall screen needed them most.
const MOBILE = window.matchMedia(
  '(max-width:640px), (hover:none) and (pointer:coarse) and (max-width:1024px)');
let cardLimit = 8;
// Deadlines are ordered FIRST on a phone because they are the most actionable
// thing here — but uncapped that panel ran 1,389px, two thirds of everything
// above the first update card. Capping it keeps the ordering decision without
// making the reader scroll past 78 dates to reach an update. On desktop this
// is just a starting point: matchColumnHeights() below resets it to "show
// everything" on every render, then trims it back down only if the deadlines
// column actually ends up taller than the updates column.
let dlLimit = MOBILE.matches ? 6 : Infinity;
let userChoseLimit = false;      // never override an explicit "show more"
let userChoseDlLimit = false;    // same, for the deadlines panel
let userToggledFilters = false;  // or an explicit open/close
const MIN_COL_LIMIT = 3;         // never trim a column down to almost nothing

function renderCards(rs) {
  // Every matching row renders into the DOM regardless of cardLimit — only
  // display is capped (via the `hidden` attribute, same mechanism as a native
  // <details> accordion), not markup. A search crawler executing this script
  // sees the full text of every update; only human eyes past cardLimit need a
  // click to reveal it. Slicing rs itself before mapping used to mean 362 of
  // 370 plain-English summaries never existed in the DOM at all.
  $('#cards').innerHTML = rs.length ? rs.map((d, i) => {
    const short = (d.type || '').split(' ')[0];
    // In the "everything" view a set-aside item must be visibly marked, or the
    // reader cannot tell which items met the criteria and which did not.
    return `<div class="card${d.relevant ? '' : ' dropped'}"${i >= cardLimit ? ' hidden' : ''}>
      <div class="top">
        <span class="badge t-${esc(short)}">${esc(d.type || '—')}</span>
        <span class="agency">${esc(d.sources.join(' · '))}</span>
        ${d.relevant ? '' : '<span class="badge setaside">set aside by filter</span>'}
      </div>
      <h3><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a></h3>
      <p>${esc(d.why)}</p>
      ${playerBar(d)}
      <div class="cardfoot">
        <div class="meta">${esc(d.date)} · <span class="u u-${esc(d.urgency)}">${esc(d.urgency)}</span></div>
        <div class="actions">${d.slug ? `<a class="cal" href="updates/${esc(d.slug)}.html">Details</a>` : ''}${calButtons(d)}${itemActionButtons(d)}</div>
      </div>
    </div>`;
  }).join('') : '<div class="empty">No updates match this filter.</div>';
  $('#cardcount').textContent = `${rs.length} update${rs.length === 1 ? '' : 's'}`;
  const more = $('#showmore');
  if (more) {
    const hidden = rs.length - Math.min(rs.length, cardLimit);
    more.hidden = hidden <= 0;
    more.textContent = `Show ${hidden} more update${hidden === 1 ? '' : 's'}`;
  }
}

// Deadlines carried by a set of rows. Used twice: once for what is on screen,
// once for the unfiltered total behind the "N of M" line.
function deadlineItems(rs) {
  const items = [];
  rs.forEach(d => {
    if (d.comments_close_on && d.comments_close_on >= TODAY)
      items.push({d, when: d.comments_close_on, what: 'Comments close'});
    if (d.effective_on && d.effective_on >= TODAY)
      items.push({d, when: d.effective_on, what: 'Takes effect'});
  });
  return items;
}

// Same two dates deadlineItems checks, but rendered inline on the update card
// itself rather than only in the sidebar — a reader scanning the feed can add
// a comment deadline or effective date to their calendar without hunting for
// the matching sidebar entry. Markup and data-* attributes match .dl .cal
// exactly so the one delegated click handler below covers both.
const CAL_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>';

// Base URL for the per-item audio clips (one MP3 per store key, produced by the
// clip script). Default assumes an `audio/` folder next to this page; override
// with window.KLEARANCE_AUDIO_BASE. An item with no key, or whose clip has not
// been generated yet, simply shows no bar - its row is unchanged.
const AUDIO_BASE = (window.KLEARANCE_AUDIO_BASE || 'audio').replace(/\/+$/, '');
const RP_PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
const RP_PAUSE = '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';
function playerBar(d) {
  // Show a bar only where a clip actually exists (AUDIO_KEYS is written at
  // build time from ./audio). Covers set-aside items too once their clips
  // are generated; anything without one is left untouched.
  if (!d.key || !AUDIO_KEYS.has(d.key)) return '';
  return '<div class="rp" data-src="' + esc(AUDIO_BASE) + '/' + esc(d.key) + '.mp3">'
    + '<button class="rp-btn" type="button" aria-label="Play summary">' + RP_PLAY + '</button>'
    + '<span class="rp-t rp-cur">0:00</span>'
    + '<div class="rp-track"><div class="rp-fill"></div></div>'
    + '<span class="rp-t rp-dur">0:00</span>'
    + '</div>';
}
function calButtons(d) {
  const btns = [];
  if (d.comments_close_on && d.comments_close_on >= TODAY)
    btns.push(['Comments close', d.comments_close_on]);
  if (d.effective_on && d.effective_on >= TODAY)
    btns.push(['Takes effect', d.effective_on]);
  return btns.map(([label, when]) => `<button class="cal" data-t="${esc(d.title)}"
    data-w="${esc(when)}" data-l="${esc(label)}" data-u="${esc(d.url)}"
    aria-label="Add ${esc(label.toLowerCase())} ${esc(when)} to your calendar">${CAL_ICON}</button>`).join('');
}

// Notes/Tasks icon buttons, present on every item (dated or not) — unlike
// calButtons above, which only renders when a comment/effective date exists.
// Storage is per-browser localStorage keyed by the item's URL: there's no
// login on this site, so a stable per-device key is the only option, and
// it keeps these completely private (nothing here leaves the browser).
const NOTES_KEY = 'klearanceNotes';
const TASKS_KEY = 'klearanceTasks';
function loadNotes() { try { return JSON.parse(localStorage.getItem(NOTES_KEY)) || {}; } catch { return {}; } }
function saveNotes(o) { localStorage.setItem(NOTES_KEY, JSON.stringify(o)); }
function loadTasks() { try { return JSON.parse(localStorage.getItem(TASKS_KEY)) || {}; } catch { return {}; } }
function saveTasks(o) { localStorage.setItem(TASKS_KEY, JSON.stringify(o)); }
const NOTE_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h8l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M14 3v5h5"/><path d="M8 13h8M8 17h5"/></svg>';
const TASK_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6.5l1.2 1.2L7.5 5.4"/><path d="M4 12.5l1.2 1.2 2.3-2.3"/><path d="M4 18.5l1.2 1.2 2.3-2.3"/></svg>';
function itemActionButtons(d) {
  const hasNote = !!(loadNotes()[d.url] || '').trim();
  const hasTask = (loadTasks()[d.url] || []).some(t => !t.done);
  return `<div class="itemrow">
    <button class="itembtn${hasNote ? ' has' : ''}" data-mode="notes" data-t="${esc(d.title)}"
      data-u="${esc(d.url)}" aria-label="Notes">${NOTE_ICON}<span class="dot"></span></button>
    <button class="itembtn${hasTask ? ' has' : ''}" data-mode="tasks" data-t="${esc(d.title)}"
      data-u="${esc(d.url)}" aria-label="Tasks">${TASK_ICON}<span class="dot"></span></button>
  </div>`;
}

// Human name for whatever is narrowing the page right now. Read off the pressed
// control rather than mapped from filter.kind, so it cannot drift from the label
// the reader actually clicked.
function scopeLabel() {
  const parts = [];
  if (filter.kind === 'agency') {
    const labels = Array.from(document.querySelectorAll('#sourceGroup .pill[aria-pressed="true"]'))
      .map(p => p.textContent.trim());
    if (labels.length) parts.push(labels.join(', '));
  } else if (filter.kind !== 'all') {
    const el = $('.kpi[aria-pressed="true"] .l') || $('.pill[aria-pressed="true"]');
    if (el) parts.push(el.textContent.trim());
  }
  if (query) parts.push('“' + query + '”');
  return parts.join(' · ');
}

// One-event .ics for the per-deadline "Add to calendar" button. Mirrors the
// server-built feed (build_ics in dashboard.py) — same all-day event, same two
// alarms — so a single deadline and the whole feed behave identically. RFC 5545:
// CRLF endings, escaped text, folded lines.
function icsEsc(t) {
  return String(t).replace(/\\/g, '\\\\').replace(/\n/g, '\\n')
    .replace(/,/g, '\\,').replace(/;/g, '\\;');
}
function icsFold(line) {            // <=75 octets, continuation lines start with a space
  const enc = new TextEncoder();
  let out = '', cur = '';
  for (const ch of line) {
    if (enc.encode(cur + ch).length > 74) { out += (out ? '\r\n' : '') + cur; cur = ' ' + ch; }
    else cur += ch;
  }
  return out + (out ? '\r\n' : '') + cur;
}
function ymdPlus1(ymd) {
  const d = new Date(ymd + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10).replace(/-/g, '');
}
function eventIcs(title, when, label, url) {
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+/, '');
  let h = 0; const key = when + label + title;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) | 0;
  const uid = 'regwatch-' + when.replace(/-/g, '') + '-' + (h >>> 0).toString(36);
  return [
    'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Klearance//Regulatory deadlines//EN',
    'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
    'BEGIN:VEVENT',
    'UID:' + uid + '@regwatch',
    'DTSTAMP:' + stamp,
    'DTSTART;VALUE=DATE:' + when.replace(/-/g, ''),
    'DTEND;VALUE=DATE:' + ymdPlus1(when),
    icsFold('SUMMARY:' + icsEsc(label + ': ' + title)),
    icsFold('DESCRIPTION:' + icsEsc(label + '. Open the source before acting: ' + url)),
    icsFold('URL:' + url),
    'BEGIN:VALARM', 'ACTION:DISPLAY', 'DESCRIPTION:Klearance deadline in 7 days', 'TRIGGER:-P7D', 'END:VALARM',
    'BEGIN:VALARM', 'ACTION:DISPLAY', 'DESCRIPTION:Klearance deadline tomorrow', 'TRIGGER:-P1D', 'END:VALARM',
    'END:VEVENT', 'END:VCALENDAR',
  ].join('\r\n') + '\r\n';
}
function downloadText(name, text, mime) {
  const blob = new Blob([text], { type: mime });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function renderDeadlines(rs) {
  const items = deadlineItems(rs);
  items.sort((a, b) => a.when.localeCompare(b.when));
  // Sorted soonest-first, so a cap hides the least urgent — never the ones about
  // to close.
  const shown = items.slice(0, dlLimit);
  $('#deadlines').innerHTML = shown.length ? shown.map(({d, when, what}) => {
    const n = days(when);
    const cls = n <= 14 ? 'soon' : n <= 45 ? 'mid' : 'far';
    const col = cls === 'soon' ? 'var(--crit)' : cls === 'mid' ? 'var(--warn)' : 'var(--ok)';
    return `<div class="dl">
      <div class="body">
        <div class="agency"><span class="dot" style="background:${col}"></span>${esc(d.sources.join(' · '))}</div>
        <div class="ttl"><a href="${esc(d.fr_url || d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a></div>
        <div class="dlfoot">
          <div class="when ${cls}">${esc(what)} ${esc(when)} · ${n} day${n === 1 ? '' : 's'}</div>
          <div class="actions">
            <button class="cal" data-t="${esc(d.title)}" data-w="${esc(when)}"
              data-l="${esc(what)}" data-u="${esc(d.fr_url || d.url)}"
              aria-label="Add this deadline to your calendar">${CAL_ICON}</button>
            ${itemActionButtons(d)}
          </div>
        </div>
        ${playerBar(d)}
      </div></div>`;
  }).join('')
  : '<div class="empty">No dated deadlines in this view. Dates come from matched Federal Register documents; items without a match show none.</div>';

  const dlMore = $('#dlmore');
  if (dlMore) {
    const hidden = items.length - shown.length;
    dlMore.hidden = hidden <= 0;
    dlMore.textContent = `Show ${hidden} more deadline${hidden === 1 ? '' : 's'}`;
  }

  // "N of M · what you clicked". M is every upcoming deadline in the current
  // view, so the reader can see the filter bit even when the rows look familiar.
  const scope = $('#dlscope');
  const total = deadlineItems(DATA.filter(d => showAll || d.relevant)).length;
  if (scope) {
    const what = scopeLabel();
    scope.textContent = what
      ? `${items.length} of ${total} · ${what}`
      : `All upcoming · ${total}`;
  }
  const dlCount = $('#dlcount');
  if (dlCount) dlCount.textContent = `${total} deadline${total === 1 ? '' : 's'}`;
}

function renderAgencies(rs) {
  const agencyCount = $('#agencycount');
  if (agencyCount) agencyCount.textContent = `${rs.length} update${rs.length === 1 ? '' : 's'}`;
  const c = {};
  // Counted by agency, matching the Source pills. Counting raw feeds instead
  // listed "FDIC" and "FDIC FILs" as if they were separate regulators, and an
  // interagency item counted once per feed rather than once per agency.
  rs.forEach(d => {
    const seen = new Set();
    d.sources.forEach(s => {
      const label = FEED_TO_AGENCY[s] || s;
      if (!seen.has(label)) { seen.add(label); c[label] = (c[label] || 0) + 1; }
    });
  });
  const e = Object.entries(c).sort((a, b) => b[1] - a[1]).slice(0, 10);
  const max = e.length ? e[0][1] : 1;
  $('#agencies').innerHTML = e.length ? e.map(([n, v]) =>
    `<div class="n" title="${esc(n)}">${esc(n)}</div>
     <div class="meter"><span style="width:${Math.round(v / max * 100)}%"></span></div>
     <div class="c">${v} update${v === 1 ? '' : 's'}</div>`).join('')
    : '<div class="empty">—</div>';
}

// Rich update cards (full paragraphs) and compact deadline rows almost never
// reach the same natural height at the same item count, and which side is
// taller flips with the data — a heavy deadline week can outrun 8 update
// cards just as easily as the other way round. Rather than stretch one to
// match (every attempt at that just relocated the same dead space into
// whatever absorbed it — an empty bordered box on quickcontact, a blank slab
// inside .p-updates, a gap inside the deadlines panel), measure both columns
// after they render and trim whichever is taller down to match, via the same
// cardLimit/dlLimit + "Show more" mechanism a reader's own click already
// uses. Every pass starts from each column's own natural baseline (never the
// previous pass's trimmed value) so a later filter change that leaves more
// room can grow a previously-trimmed column back instead of only ever
// shrinking it further.
function matchColumnHeights(rs) {
  if (!userChoseLimit) cardLimit = 8;
  if (!userChoseDlLimit) dlLimit = MOBILE.matches ? 6 : (deadlineItems(rs).length || MIN_COL_LIMIT);
  renderCards(rs); renderDeadlines(rs);
  const colmain = $('.colmain'), colside = $('.colside');
  // The visible bottom edge a reader actually sees is the last bordered box
  // in each column (quickcontact; the deadlines panel) — not the plain,
  // borderless wrapper div around it. Padding the wrapper changes nothing
  // visible: whatever comes after .cols already starts at the row's full
  // height regardless, so that padding was inert. Padding the box itself
  // moves its border down to actually close the gap.
  const mainBox = $('.quickcontact') || colmain;
  const sideBox = $('.p-deadlines') || colside;
  if (mainBox) mainBox.style.paddingBottom = '';
  if (sideBox) sideBox.style.paddingBottom = '';
  if (MOBILE.matches) return;              // stacked on a phone, nothing to balance
  if (!colmain || !colside) return;
  if (!(userChoseLimit && userChoseDlLimit)) {
    let guard = 0;
    let diff = colmain.offsetHeight - colside.offsetHeight;
    // Trim one item off the taller side at a time, but never past the
    // closest point: an item is itself tens of pixels tall, so the last
    // trim can overshoot from "a bit too tall" to "now a bit too short" the
    // other way. Comparing abs(diff) before/after and undoing a step that
    // made it worse lands on whichever side of zero is actually closer.
    while (guard++ < 60 && Math.abs(diff) >= 12) {
      if (diff > 0 && !userChoseLimit && cardLimit > MIN_COL_LIMIT) {
        cardLimit--; renderCards(rs);
      } else if (diff < 0 && !userChoseDlLimit && dlLimit > MIN_COL_LIMIT) {
        dlLimit--; renderDeadlines(rs);
      } else break;                          // the taller side is at its floor or the reader's explicit choice
      const newDiff = colmain.offsetHeight - colside.offsetHeight;
      if (Math.abs(newDiff) > Math.abs(diff)) {
        if (diff > 0) { cardLimit++; renderCards(rs); } else { dlLimit++; renderDeadlines(rs); }
        break;
      }
      diff = newDiff;
    }
  }
  // Item-count trimming only gets within one item's height of matching (a
  // deadline row and an update card are different heights, so the closest
  // whole-item crossing can still be tens of pixels off). Close the exact
  // remainder by padding the shorter side's own visible box — this actually
  // moves its border down to meet the taller column, rather than adding
  // space nobody sees: whatever follows .cols already starts at the taller
  // column's height regardless of what the shorter column's own box looks
  // like, so padding a plain wrapper never did anything visible.
  //
  // Applying that padding changes the page's total height, which can toggle
  // whether a vertical scrollbar is present — that changes the viewport's
  // available width, which reflows text and shifts heights again, so the
  // first padding value is only a first guess. Re-measuring afterward and
  // adding the residual on top (not recomputing from scratch, which would
  // just clear the padding and rediscover the same pre-scrollbar guess
  // every time) converges on the actual post-reflow width in a couple of
  // passes.
  let box = null, pad = 0;
  for (let i = 0; i < 5; i++) {
    const d = colmain.offsetHeight - colside.offsetHeight;
    if (Math.abs(d) < 1) break;
    if (box === null) box = d > 0 ? sideBox : mainBox;
    if (!box) break;
    pad += (box === sideBox ? d : -d);
    if (pad < 0) pad = 0;
    box.style.paddingBottom = pad + 'px';
  }
}

function render() {
  const rs = rows();
  matchColumnHeights(rs);
  renderAgencies(rs); renderFilteredOut(); renderRegRef();
  const cf = $('#clearFilters');
  if (cf) cf.hidden = filter.kind === 'all';
}

// Counts on the buttons, so the size of each lens is visible before clicking
// rather than inferred afterwards. Computed from the data, never hardcoded.
function labelViews() {
  const rel = DATA.filter(d => d.relevant).length;
  const fin = DATA.filter(d => d.relevant && d.fintech === true).length;
  const cu = DATA.filter(d => d.relevant && d.credit_union === true).length;
  $('#viewRelevant').textContent = `Banks, credit unions & fintechs (${rel})`;
  $('#viewAll').textContent = `Everything (${DATA.length})`;
  const f = $('[data-kind="fintech"]');
  if (f) f.textContent = `Fintech only (${fin})`;
  const c = $('[data-kind="credit_union"]');
  if (c) c.textContent = `Credit unions only (${cu})`;
}

function setView(all) {
  showAll = all;
  $('#viewAll').setAttribute('aria-pressed', String(all));
  $('#viewRelevant').setAttribute('aria-pressed', String(!all));
  $('#viewnote').textContent = '';
  render();
}
$('#viewAll').addEventListener('click', () => setView(true));
$('#viewRelevant').addEventListener('click', () => setView(false));

const searchBox = $('#q');
searchBox.addEventListener('input', () => {
  query = searchBox.value.trim().toLowerCase();
  $('#clearq').hidden = !query;
  render();
});
$('#clearq').addEventListener('click', () => {
  searchBox.value = ''; query = ''; $('#clearq').hidden = true;
  searchBox.focus(); render();
});
// Escape clears the box — expected in a search field, and quicker than
// selecting the text to delete it.
searchBox.addEventListener('keydown', e => {
  if (e.key === 'Escape' && searchBox.value) { $('#clearq').click(); }
});

// One active filter at a time across pills AND kpi tiles, so selecting either
// clears the other. Source's All pill is the exception: it means "no agency
// restriction," which is also true whenever some other dimension (Fintech
// only, a KPI tile) is driving the view, so it stays pressed rather than
// going blank alongside everything else.
function clearFilterUI() {
  document.querySelectorAll('.pill, .kpi[data-kpi]')
    .forEach(x => x.setAttribute('aria-pressed', 'false'));
  const all = $('.pill[data-kind="all"]');
  if (all) all.setAttribute('aria-pressed', 'true');
  $('#viewnote').textContent = '';
}

document.querySelectorAll('.pill').forEach(p => {
  if (p.closest('#sourceGroup')) return; // Source pills: multi-select, handled below.
  p.addEventListener('click', () => {
    const already = p.getAttribute('aria-pressed') === 'true';
    clearFilterUI();
    if (already) {
      filter = {kind: 'all', value: ''};
    } else {
      p.setAttribute('aria-pressed', 'true');
      filter = {kind: p.dataset.kind, value: p.dataset.value || ''};
    }
    render();
  });
});

// Clear filters: resets Source/Fintech/Credit-union/KPI selections back to
// "all", same as clicking the Source group's All pill, from one visible
// control instead of relying on the reader to notice that pill does it.
const clearFiltersBtn = $('#clearFilters');
if (clearFiltersBtn) {
  clearFiltersBtn.addEventListener('click', () => {
    clearFilterUI();
    filter = {kind: 'all', value: ''};
    render();
  });
}

// Source pills multi-select: pick several agencies, results combine (OR).
// Still a single filter dimension overall — picking a KPI tile or Fintech/
// Credit-union pill clears the Source selections, same as before.
const sourceGroup = $('#sourceGroup');
if (sourceGroup) {
  const agencyPills = () => Array.from(sourceGroup.querySelectorAll('.pill[data-kind="agency"]'));
  const allPill = sourceGroup.querySelector('.pill[data-kind="all"]');

  function applySourceFilter() {
    const chosen = agencyPills().filter(p => p.getAttribute('aria-pressed') === 'true')
      .map(p => p.dataset.value);
    if (chosen.length) {
      filter = {kind: 'agency', value: chosen};
      allPill.setAttribute('aria-pressed', 'false');
    } else {
      filter = {kind: 'all', value: ''};
      allPill.setAttribute('aria-pressed', 'true');
    }
    render();
  }

  allPill.addEventListener('click', () => {
    clearFilterUI();
    filter = {kind: 'all', value: ''};
    render();
  });

  agencyPills().forEach(p => p.addEventListener('click', () => {
    document.querySelectorAll('.kpi[data-kpi], .pill[data-kind="fintech"], .pill[data-kind="credit_union"]')
      .forEach(x => x.setAttribute('aria-pressed', 'false'));
    $('#viewnote').textContent = '';
    const pressed = p.getAttribute('aria-pressed') === 'true';
    p.setAttribute('aria-pressed', String(!pressed));
    applySourceFilter();
  }));
}

// KPI tiles filter the list to exactly what they count. Clicking the active tile
// again clears back to all. KPI items are all relevant, so also drop out of the
// "Everything" view for a consistent picture.
document.querySelectorAll('.kpi[data-kpi]').forEach(k => {
  const activate = () => {
    const key = k.dataset.kpi;
    const already = filter.kind === 'kpi' && filter.value === key;
    clearFilterUI();
    if (already) {
      filter = {kind: 'all', value: ''};
    } else {
      filter = {kind: 'kpi', value: key};
      k.setAttribute('aria-pressed', 'true');
      $('#viewnote').textContent = 'Showing: ' + k.querySelector('.l').textContent;
      showAll = false;
      $('#viewAll').setAttribute('aria-pressed', 'false');
      $('#viewRelevant').setAttribute('aria-pressed', 'true');
    }
    render();
  };
  k.addEventListener('click', activate);
  k.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
  });
});

$('#export').addEventListener('click', () => {
  const rs = rows();
  const head = ['date','title','sources','type','urgency','comments_close_on','effective_on','url','summary'];
  const cell = v => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const csv = [head.join(',')].concat(rs.map(d => [
    d.date, d.title, d.sources.join('; '), d.type, d.urgency,
    d.comments_close_on || '', d.effective_on || '', d.url, d.why
  ].map(cell).join(','))).join('\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv'}));
  a.download = `klearance-${TODAY}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
});

// Open in the markup, collapsed here only on a phone. Written this way round so
// a script failure leaves the filters visible rather than hiding them entirely.
const showMoreBtn = document.getElementById('showmore');
if (showMoreBtn) showMoreBtn.addEventListener('click', () => {
  cardLimit = Infinity;
  userChoseLimit = true;
  render();
  showMoreBtn.hidden = true;
});

const dlMoreBtn = document.getElementById('dlmore');
if (dlMoreBtn) dlMoreBtn.addEventListener('click', () => {
  dlLimit = Infinity;
  userChoseDlLimit = true;
  render();
  dlMoreBtn.hidden = true;
});

// Full-list SDN search — looks up a name against the CURRENT ~19k-entry list,
// not just recent changes. sdn_index.json isn't fetched until the reader
// actually types something, so a page load that never touches this box never
// pays for it. Fetched once and cached in memory for the rest of the session.
// Full-list name screening across SDN + Consolidated (non-SDN), primary
// names and aliases. Index row:
//   [ent_num, primary_name, type, program, source, is_alias, remarks, country, alias_name]
// sdn_index.json (~12 MB, gzipped on the wire) is fetched only on first use.
const sdnListQ = document.getElementById('sdnlistq');
if (sdnListQ) {
  const results = document.getElementById('sdnlistresults');
  const countEl = document.getElementById('sdnlistcount');
  const bulkQ = document.getElementById('sdnbulkq');
  const bulkGo = document.getElementById('sdnbulkgo');
  const RESULT_CAP = 60;
  let index = null, indexPromise = null, norm = null;

  // Fold accents, drop punctuation, collapse whitespace. "Muḩammad" -> "muhammad".
  const foldName = s => (s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
  const nameOf = r => r[5] ? r[8] : r[1];        // alias rows screen on the alias

  function ensureNorm() {
    if (index && !norm) norm = index.map(r => foldName(nameOf(r)));
  }
  function matchRows(query) {
    ensureNorm();
    const qt = foldName(query).split(' ').filter(Boolean);
    if (!qt.length) return [];
    const seen = new Map();                       // source:ent_num -> best hit
    for (let i = 0; i < index.length; i++) {
      const n = norm[i];
      if (!qt.every(t => n.includes(t))) continue;
      const r = index[i], key = r[4] + ':' + r[0], isAlias = !!r[5];
      const prev = seen.get(key);
      if (!prev || (prev.isAlias && !isAlias)) {
        seen.set(key, { row: r, isAlias, matched: nameOf(r) });
      }
    }
    return [...seen.values()];
  }
  function detail(r) {
    const bits = [];
    if (r[7]) bits.push('Country: ' + esc(r[7]));
    if (r[6]) bits.push(esc(r[6]));
    const link = 'https://sanctionssearch.ofac.treas.gov/Details.aspx?id=' + encodeURIComponent(r[0]);
    return `<div class="sdndetail">${bits.join(' &middot; ')}`
      + (bits.length ? ' &middot; ' : '')
      + `<a href="${link}" target="_blank" rel="noopener">View on OFAC &#8599;</a></div>`;
  }
  function rowHtml(m) {
    const r = m.row;
    const src = r[4] === 'SDN'
      ? '<span class="badge sdnsrc">SDN</span>'
      : '<span class="badge sdnsrc nonsdn">Non-SDN</span>';
    const aka = m.isAlias
      ? ` <span class="sdnalias">alias &mdash; listed as &ldquo;${esc(r[1])}&rdquo;</span>` : '';
    return `<div class="sdnrow">
      <div class="sdnname">${src} ${esc(m.matched || '(unnamed entry)')}${aka}</div>
      <div class="sdnmeta">${esc(r[3] || '—')}${r[2] ? ' · ' + esc(r[2]) : ''}</div>
      ${detail(r)}
    </div>`;
  }
  function renderOne(query) {
    if (!query) { results.innerHTML = ''; countEl.textContent = ''; return; }
    const m = matchRows(query), shown = m.slice(0, RESULT_CAP);
    results.innerHTML = shown.length ? shown.map(rowHtml).join('')
      : '<p class="sdnempty">No match on the current OFAC SDN or Consolidated lists.</p>';
    countEl.textContent = m.length > RESULT_CAP
      ? `first ${RESULT_CAP} of ${m.length} matches`
      : `${m.length} match${m.length === 1 ? '' : 'es'}`;
  }
  function loadIndex() {
    if (index) return Promise.resolve(index);
    if (!indexPromise) {
      countEl.textContent = 'loading lists…';
      indexPromise = fetch('sdn_index.json').then(r => r.json())
        .then(d => { index = d; return d; })
        .catch(() => { countEl.textContent = 'Could not load the lists — try again.'; indexPromise = null; });
    }
    return indexPromise;
  }

  sdnListQ.addEventListener('input', () => {
    const q = sdnListQ.value.trim();
    if (!q) { renderOne(''); return; }
    if (index) { renderOne(q); return; }
    loadIndex().then(() => { if (index) renderOne(sdnListQ.value.trim()); });
  });

  if (bulkGo) bulkGo.addEventListener('click', () => {
    const names = (bulkQ.value || '').split('\n').map(s => s.trim()).filter(Boolean).slice(0, 300);
    if (!names.length) { results.innerHTML = ''; countEl.textContent = ''; return; }
    countEl.textContent = 'screening…';
    loadIndex().then(() => {
      if (!index) return;
      let hits = 0;
      results.innerHTML = names.map(name => {
        const m = matchRows(name);
        if (m.length) hits++;
        const summary = m.length
          ? m.slice(0, 3).map(x => esc(x.matched) + (x.isAlias ? ' (alias)' : '') + ' &mdash; ' + esc(x.row[3] || '—')).join('; ')
            + (m.length > 3 ? ` +${m.length - 3} more` : '')
          : 'no match';
        return `<div class="sdnrow sdnbulkrow${m.length ? ' hit' : ''}">
          <div class="sdnname">${esc(name)}</div>
          <div class="sdnmeta">${summary}</div>
        </div>`;
      }).join('');
      countEl.textContent = `${hits} of ${names.length} name${names.length === 1 ? '' : 's'} hit the lists`;
    });
  });

  document.querySelectorAll('.sdnmode').forEach(btn => btn.addEventListener('click', () => {
    document.querySelectorAll('.sdnmode').forEach(b => {
      const on = b === btn;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    document.querySelectorAll('.sdnfullsearch [data-pane]').forEach(p => {
      p.hidden = p.dataset.pane !== btn.dataset.mode;
    });
    results.innerHTML = ''; countEl.textContent = '';
  }));
}

// "Add to calendar" — delegated on document rather than any one list, since
// .cal buttons now render in three places that each re-render independently
// (sidebar deadlines, update cards, "also found" cards) and re-binding a
// listener to regenerated markup in three spots is just this, done three
// times. Builds a one-event .ics and downloads it; the phone's calendar app
// opens it, the desktop's saves it.
document.addEventListener('click', e => {
  const b = e.target.closest('.cal');
  if (!b) return;
  const ics = eventIcs(b.dataset.t, b.dataset.w, b.dataset.l, b.dataset.u);
  const name = (b.dataset.l + '-' + b.dataset.w).replace(/[^a-z0-9]+/gi, '-')
    .toLowerCase().replace(/^-|-$/g, '') + '.ics';
  downloadText(name, ics, 'text/calendar');
});

// ---------------------------------------------- Inline audio player
// One <audio> for the whole page. A clip can appear in two bars at once
// (an update card and its matching sidebar deadline), so state is keyed by
// src and every matching bar is repainted together. Delegated on document,
// same reasoning as the .cal handler above.
const rpAudio = new Audio();
rpAudio.preload = 'none';
let rpSrc = null;
function rpFmt(s) { s = Math.max(0, s | 0); return (s / 60 | 0) + ':' + String(s % 60).padStart(2, '0'); }
function rpBars(src) { return document.querySelectorAll('.rp[data-src="' + src + '"]'); }
function rpPaint() {
  if (!rpSrc) return;
  const pct = rpAudio.duration ? rpAudio.currentTime / rpAudio.duration * 100 : 0;
  rpBars(rpSrc).forEach(bar => {
    bar.querySelector('.rp-btn').innerHTML = rpAudio.paused ? RP_PLAY : RP_PAUSE;
    bar.querySelector('.rp-fill').style.width = pct + '%';
    bar.querySelector('.rp-cur').textContent = rpFmt(rpAudio.currentTime);
    if (rpAudio.duration) bar.querySelector('.rp-dur').textContent = rpFmt(rpAudio.duration);
  });
}
function rpClear(src) {
  rpBars(src).forEach(bar => {
    bar.querySelector('.rp-btn').innerHTML = RP_PLAY;
    bar.querySelector('.rp-fill').style.width = '0';
    bar.querySelector('.rp-cur').textContent = '0:00';
  });
}
['timeupdate', 'play', 'pause', 'loadedmetadata'].forEach(ev => rpAudio.addEventListener(ev, rpPaint));
rpAudio.addEventListener('ended', () => { if (rpSrc) rpClear(rpSrc); });
document.addEventListener('click', e => {
  const bar = e.target.closest('.rp');
  if (!bar) return;
  const src = bar.dataset.src;
  if (rpSrc === src) {
    if (e.target.closest('.rp-track') && rpAudio.duration) {
      const t = bar.querySelector('.rp-track').getBoundingClientRect();
      rpAudio.currentTime = Math.min(1, Math.max(0, (e.clientX - t.left) / t.width)) * rpAudio.duration;
    } else {
      rpAudio.paused ? rpAudio.play() : rpAudio.pause();
    }
    return;
  }
  if (rpSrc) rpClear(rpSrc);
  rpSrc = src;
  rpAudio.src = src;
  rpAudio.play().catch(() => {});
});

// ---------------------------------------------------------- Notes & Tasks
// One shared dialog reused across every item, rather than one per card —
// there can be hundreds of cards rendered at once. Only its content and the
// URL it's bound to change per open. Closing it re-runs render() so any
// card whose note/task state just changed picks up its dot immediately.
const itemDialog = $('#itemDialog');
const idBody = $('#idBody');
const idTitleEl = $('#idTitle');
const idKindEl = $('#idKind');
let idCurrent = null;
let idSaveTimer = null;

function openItemDialog(mode, title, url) {
  idCurrent = { mode, title, url };
  idKindEl.textContent = mode === 'notes' ? 'Note' : 'Tasks';
  idTitleEl.textContent = title;
  renderItemDialogBody();
  itemDialog.hidden = false;
  const focusEl = idBody.querySelector('textarea, input');
  if (focusEl) focusEl.focus();
}
function closeItemDialog() {
  if (itemDialog.hidden) return;
  itemDialog.hidden = true;
  idCurrent = null;
  render();
}
function renderItemDialogBody() {
  if (!idCurrent) return;
  const { mode, url } = idCurrent;
  if (mode === 'notes') {
    const val = loadNotes()[url] || '';
    idBody.innerHTML = `<textarea id="idNoteText"
      placeholder="Private note — only saved in this browser.">${esc(val)}</textarea>
      <div class="idnotefoot">Saved automatically. Only visible on this device.</div>`;
    $('#idNoteText').addEventListener('input', e => {
      clearTimeout(idSaveTimer);
      const v = e.target.value;
      idSaveTimer = setTimeout(() => {
        const n = loadNotes();
        if (v.trim()) n[url] = v; else delete n[url];
        saveNotes(n);
      }, 400);
    });
  } else {
    const list = loadTasks()[url] || [];
    idBody.innerHTML = `
      <form id="idTaskForm">
        <input id="idTaskInput" type="text" placeholder="Add a task" autocomplete="off">
        <button type="submit">Add</button>
      </form>
      <ul id="idTaskList">${list.length ? list.map(t => `
        <li class="idtask${t.done ? ' done' : ''}" data-id="${esc(t.id)}">
          <label><input type="checkbox" ${t.done ? 'checked' : ''}>${esc(t.text)}</label>
          <button type="button" class="idtaskdel" aria-label="Delete task">&times;</button>
        </li>`).join('') : '<li class="idempty">No tasks yet.</li>'}</ul>`;
    $('#idTaskForm').addEventListener('submit', e => {
      e.preventDefault();
      const input = $('#idTaskInput');
      const text = input.value.trim();
      if (!text) return;
      const t = loadTasks();
      const arr = t[url] || (t[url] = []);
      arr.push({ id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6), text, done: false });
      saveTasks(t);
      renderItemDialogBody();
    });
    idBody.querySelectorAll('.idtask').forEach(li => {
      const id = li.dataset.id;
      li.querySelector('input[type=checkbox]').addEventListener('change', e => {
        const t = loadTasks();
        const task = (t[url] || []).find(x => x.id === id);
        if (task) { task.done = e.target.checked; saveTasks(t); }
        li.classList.toggle('done', e.target.checked);
      });
      li.querySelector('.idtaskdel').addEventListener('click', () => {
        const t = loadTasks();
        t[url] = (t[url] || []).filter(x => x.id !== id);
        saveTasks(t);
        renderItemDialogBody();
      });
    });
  }
}
document.addEventListener('click', e => {
  const openBtn = e.target.closest('.itembtn');
  if (openBtn) { openItemDialog(openBtn.dataset.mode, openBtn.dataset.t, openBtn.dataset.u); return; }
  if (e.target.id === 'idClose' || e.target === itemDialog) closeItemDialog();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !itemDialog.hidden) closeItemDialog();
});


const filtersEl = document.getElementById('filters');
if (filtersEl) filtersEl.addEventListener('toggle', () => { userToggledFilters = true; });

// Foldable panels collapse on a phone only. On desktop the heading is still a
// <summary>, so a click would fold the main update list -- block it there rather
// than leave a control that does something the desktop layout never intends.
const foldables = Array.from(document.querySelectorAll('.foldable'));
foldables.forEach(el => {
  const sum = el.querySelector('summary');
  if (sum) sum.addEventListener('click', e => { if (!MOBILE.matches) e.preventDefault(); });
});

function applyViewport() {
  if (!userChoseLimit) cardLimit = 8;
  if (!userChoseDlLimit) dlLimit = MOBILE.matches ? 6 : Infinity;
  // Re-open anything the reader folded on a phone before turning to landscape or
  // widening the window; a collapsed panel on desktop has no visible way back.
  if (!MOBILE.matches) foldables.forEach(el => { el.open = true; });
  if (filtersEl && !userToggledFilters) {
    // Assigning .open fires 'toggle', which would set the user flag — suppress it.
    const wanted = !MOBILE.matches;
    if (filtersEl.open !== wanted) {
      filtersEl.open = wanted;
      userToggledFilters = false;
    }
  }
  render();
}
MOBILE.addEventListener('change', applyViewport);
applyViewport();

// matchColumnHeights()'s padding correction is exact only for the width it
// was computed at — text reflows continuously as the window resizes, not
// just at the mobile breakpoint MOBILE above watches, so a plain resize
// within "desktop" (dragging the window narrower, docking it to half the
// screen) leaves the last-computed padding stale and visibly wrong at the
// new width. Debounced with setTimeout, not requestAnimationFrame: rAF only
// fires while the tab is visible/foregrounded, and a resize can still land
// after the tab loses focus (e.g. a window manager snapping it to a side).
let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(render, 150);
});

labelViews();
setView(false);

// ------------------------------------------------------------- Quick start
// Two-step onboarding tour, shown once per browser: step 1 points at the
// calendar/notes/tasks icons on the first card, step 2 at the share/
// bookmark/more toolbar. One shared callout element is reused and re-pointed
// between steps rather than two separate elements, same "one shared
// component, re-targeted" pattern as the Notes/Tasks dialog above.
// Positioned by measuring the real target via getBoundingClientRect rather
// than a hardcoded coordinate, so it lands correctly at whatever width/zoom
// the reader is on, and repositions live if they resize or scroll before
// dismissing.
const QS_KEY = 'klearanceQuickStartSeen';
const QS_STEPS = [
  { sel: '.icon-toolbar',
    text: 'Share this page, save/install it for quick access, or export the tracked updates to a spreadsheet from here.' },
  { sel: '#filters',
    text: 'Narrow what you see: switch scope with View, or combine agencies with Source — pick several to see them together.' },
  { sel: '#searchGroup',
    text: 'Search by keyword — works together with View and Source, not instead of them.' },
  // Absent when ASK_ENABLED is off in dashboard.py -- showStep skips a
  // missing target and moves on rather than ending the tour early.
  { sel: '.ask-panel',
    text: 'Ask a plain-English question about the tracked updates and get a sourced answer, reconciled across three models — research to verify, not compliance advice.' },
  // Points at #deadlines rather than #cards -- Upcoming Deadlines renders
  // above Latest Updates on the page, so the callout should land on the
  // section a reader actually meets first.
  { sel: '#deadlines .dl .actions',
    text: 'Every update has three quick actions: add a deadline to your calendar, jot a private note, or track a task — right from the list.' },
  // Skipped automatically on any row whose audio clip has not been generated
  // yet (no .rp element to point at), same as the gated Ask step above.
  { sel: '#deadlines .dl .rp',
    text: 'Press play to hear the item’s plain-English summary read aloud — a local voice, nothing added or interpreted.' },
  // Sidebar panel, below the deadlines. Skipped automatically if SDN
  // monitoring is off (no .p-sdn element), same as the gated Ask step.
  { sel: '.p-sdn',
    text: 'Separate from the agency feed: this tracks additions and removals on the OFAC SDN list, and lets you screen a name against the full current list right here.' },
];
function initQuickStart() {
  // ?quickstart=1 replays the tour on demand -- lets it be checked on a real
  // phone without digging into dev tools to clear localStorage by hand.
  if (/[?&]quickstart=1(&|$)/.test(location.search)) localStorage.removeItem(QS_KEY);
  if (localStorage.getItem(QS_KEY)) return;
  const qs = $('#quickstart');
  const qsText = qs && qs.querySelector('p');
  const qsBtn = $('#qsDismiss');
  if (!qs || !qsText) return;
  let step = 0;
  function place(target) {
    const r = target.getBoundingClientRect();
    const margin = 12;
    const above = r.top > qs.offsetHeight + margin + 20;
    let left = r.left + r.width / 2 - qs.offsetWidth / 2;
    left = Math.max(10, Math.min(left, window.innerWidth - qs.offsetWidth - 10));
    qs.style.left = left + 'px';
    qs.style.top = (above ? r.top - qs.offsetHeight - margin : r.bottom + margin) + 'px';
    qs.classList.toggle('qs-above', above);
    qs.classList.toggle('qs-below', !above);
    const tailLeft = Math.max(16, Math.min(r.left + r.width / 2 - left - 7, qs.offsetWidth - 30));
    qs.querySelector('.qs-tail').style.left = tailLeft + 'px';
  }
  let currentTarget = null;
  const reposition = () => { if (!qs.hidden && currentTarget) place(currentTarget); };
  function showStep(i) {
    if (i >= QS_STEPS.length) { finish(); return; }
    const s = QS_STEPS[i];
    const target = document.querySelector(s.sel);
    // A gated feature (e.g. Ask, off via ASK_ENABLED) has no element to
    // point at -- skip it rather than ending the whole tour early.
    if (!target) { showStep(i + 1); return; }
    const hasMore = QS_STEPS.slice(i + 1).some(x => document.querySelector(x.sel));
    step = i;
    currentTarget = target;
    qsText.textContent = s.text;
    qsBtn.textContent = hasMore ? 'Next' : 'Got it';
    qs.hidden = false;
    // Scroll the next target into view on advance -- later steps (filters,
    // search, the deadline/update actions) can sit well below the fold, and
    // without this the callout just repositions itself onto a spot the
    // reader has to go hunting for. Skipped when already fully visible so
    // step 1 (up near the top already) doesn't jump for no reason. The
    // existing scroll listener keeps the callout tracking the target for
    // the rest of the smooth-scroll animation.
    const r = target.getBoundingClientRect();
    if (r.top < 0 || r.bottom > window.innerHeight) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    place(target);
  }
  function finish() {
    qs.hidden = true;
    localStorage.setItem(QS_KEY, '1');
    window.removeEventListener('resize', reposition);
    window.removeEventListener('scroll', reposition);
  }
  function advance() {
    if (step < QS_STEPS.length - 1) showStep(step + 1); else finish();
  }
  window.addEventListener('resize', reposition);
  window.addEventListener('scroll', reposition, { passive: true });
  $('#qsClose').addEventListener('click', finish);
  qsBtn.addEventListener('click', advance);
  showStep(0);
}
initQuickStart();

// ------------------------------------------------------- Ask the tracked updates
// Retrieval happens HERE, in the browser: the page already holds every tracked
// update. Only the model call leaves, to a Cloudflare Worker that holds the API
// key server-side — so the key is never in this page, and searching costs
// nothing. corpus.json (the actual CFR text) is loaded only when the panel says
// data-regs="1"; see ASK_INCLUDE_REGULATIONS in dashboard.py for why it is off.
const ASK_ENDPOINT = 'https://regwatch-ask.alexandersmith14.workers.dev';

const STOP = new Set(('the a an and or of to in for on is are be as by with that this it at from '
  + 'any all no not may must shall will can under per each').split(' '));
const tok = s => (String(s).toLowerCase().match(/[a-z0-9]+/g) || [])
  .filter(w => w.length > 1 && !STOP.has(w));

let ASK_INDEX = null;

// BM25 (Okapi) over regs + tracked updates. Same ranking as the local tool.
function buildAskIndex(regs) {
  const passages = [];
  regs.forEach(s => passages.push({
    kind: 'regulation', label: s.citation, title: s.heading,
    stamp: 'as of ' + s.as_of,
    text: s.reg_name + ' ' + s.heading + ' ' + s.text,
  }));
  DATA.filter(d => d.relevant).forEach(d => passages.push({
    kind: 'update', label: (d.sources || []).join(', '), title: d.title,
    stamp: 'dated ' + d.date,
    text: d.title + ' ' + (d.why || '') + ' ' + (d.tags || []).join(' '),
  }));
  const docs = passages.map(p => tok(p.text));
  const N = docs.length, avgdl = docs.reduce((a, d) => a + d.length, 0) / (N || 1);
  const df = {};
  docs.forEach(d => new Set(d).forEach(t => { df[t] = (df[t] || 0) + 1; }));
  const idf = {};
  for (const t in df) idf[t] = Math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5));
  const tf = docs.map(d => { const c = {}; d.forEach(t => c[t] = (c[t] || 0) + 1); return c; });
  return { passages, docs, idf, tf, avgdl, k1: 1.5, b: 0.75 };
}

function askSearch(q, k) {
  const ix = ASK_INDEX, terms = tok(q), scored = [];
  for (let i = 0; i < ix.passages.length; i++) {
    const c = ix.tf[i], dl = ix.docs[i].length;
    let s = 0;
    for (const t of terms) {
      if (!c[t]) continue;
      s += (ix.idf[t] || 0) * (c[t] * (ix.k1 + 1)) /
           (c[t] + ix.k1 * (1 - ix.b + ix.b * dl / ix.avgdl));
    }
    if (s > 0) scored.push([s, i]);
  }
  scored.sort((a, b) => b[0] - a[0]);
  return scored.slice(0, k).map(([, i]) => ix.passages[i]);
}

// The models answer in markdown; render the small subset they actually use.
function askMd(t) {
  return t.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))
    .replace(/^#{1,6}\s*(.+)$/gm, '<h3>$1</h3>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^\s*[-*]\s+(.+)$/gm, '<li>$1</li>')
    .replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>')
    .replace(/\n{2,}/g, '<br><br>');
}

(function initAsk() {
  const q = $('#askq'), go = $('#askgo'), out = $('#askout');
  if (!q || !go) return;
  const say = html => { out.innerHTML = html; };

  // No model picker. Every question goes to every model the Worker has a key
  // for, and the Worker reconciles them into one answer. "Best available" asked
  // the reader to choose between three names they have no way to rank.

  // corpus.json is fetched only when the panel asks for regulations. With it off
  // the index is the tracked updates alone, so there is no CFR text in front of
  // the models and no subsections for them to invent.
  const WANT_REGS = document.querySelector('.ask-panel')?.dataset.regs === '1';

  async function ensureIndex() {
    if (ASK_INDEX) return true;
    if (!WANT_REGS) { ASK_INDEX = buildAskIndex([]); return true; }
    try {
      const r = await fetch('corpus.json');
      ASK_INDEX = buildAskIndex(r.ok ? await r.json() : []);
    } catch (e) {
      ASK_INDEX = buildAskIndex([]);   // updates-only still answers usefully
    }
    return true;
  }

  async function ask() {
    const question = q.value.trim();
    if (!question) return;
    go.disabled = true;
    say('<div class="ans">Searching the tracked updates&hellip;</div>');
    try {
      await ensureIndex();
      // 12 is what the free tiers accept; more makes Groq return 413.
      const passages = askSearch(question, 12);
      if (!passages.length) {
        say('<div class="ans">Nothing in the tracked updates matches that. '
          + 'Try different wording, or search the list below.</div>');
        return;
      }
      say('<div class="ans">Asking the models and comparing their answers&hellip;</div>');
      const res = await fetch(ASK_ENDPOINT, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({question, passages}),
      });
      const d = await res.json();
      if (d.error) { say('<div class="ans warn">' + esc(d.error) + '</div>'); return; }

      const all = d.answers || [];
      const good = all.filter(a => a.text && a.text.trim());
      if (!good.length) {
        // Every model failed — usually free-tier quota. Say which and why.
        say('<div class="ans warn">No model could answer just now.<br>'
          + all.map(a => esc(a.provider) + ': ' + esc(a.error || 'no answer')).join('<br>')
          + '</div>');
        return;
      }

      // One answer to read. The reconcile pass is instructed to state the
      // models' disagreements inside the text, so a single block is not hiding
      // a split — but the raw answers stay one click away, because "they
      // agreed" is a claim the reader should be able to check.
      const main = d.merged ? d.merged.text : good[0].text;
      let note;
      if (d.merged) {
        note = 'Reconciled from ' + good.length + ' models'
             + (good.length < (d.asked || good.length)
                 ? ' (' + (d.asked - good.length) + ' unavailable)' : '')
             + (d.merged.independent
                 ? ' by a separate model.'
                 : ' by one of them — no independent reconciler configured.');
      } else {
        note = good.length + ' of ' + (d.asked || good.length)
             + ' models answered, so there was nothing to compare.';
      }

      let raw = '';
      if (good.length > 1) {
        raw = '<details class="askraw"><summary>See the ' + good.length
            + ' separate answers</summary>'
            + good.map(a => '<div class="ans"><div class="who">' + esc(a.provider)
                + ' &middot; ' + esc(a.model) + '</div>' + askMd(a.text) + '</div>').join('')
            + '</details>';
      }

      const cites = [...new Set(passages.map(p => p.label))].join(' &middot; ');
      say('<div class="ans">' + askMd(main) + '</div>' + raw
        + '<div class="cites">' + note + '<br>Grounded in: ' + cites + '</div>');
    } catch (e) {
      say('<div class="ans warn">The assistant is unavailable right now. '
        + 'Everything below still works.</div>');
    } finally {
      go.disabled = false;
    }
  }
  go.addEventListener('click', ask);
  q.addEventListener('keydown', e => { if (e.key === 'Enter') ask(); });
})();
"""

# Feeds grouped under the agency that publishes them. Several agencies use more
# than one channel, and the split is an artefact of how we fetch, not something a
# reader cares about — nobody thinks "FDIC versus FDIC FILs", they think "FDIC".
#
# This also fixes a real gap. The pills used to be the top nine feeds by volume,
# which meant you could filter to "Fed SR/CA Letters" but not to "Federal
# Reserve", and to "OCC Bulletins" but not "OCC" — the two most recognisable
# banking regulators looked missing, and 16 relevant items sat behind no pill at
# all. Grouped, eight pills reach 100% of items with no truncation.
AGENCY_GROUPS = [
    ("FDIC", ["FDIC", "FDIC FILs"]),
    ("OCC", ["OCC", "OCC Bulletins"]),
    ("Federal Reserve", ["Federal Reserve", "Fed SR/CA Letters"]),
    ("CFPB", ["CFPB", "CFPB Rules"]),
    ("FinCEN", ["FinCEN", "FinCEN Advisories"]),
    ("NCUA", ["NCUA", "NCUA Press"]),
    ("OFAC", ["OFAC"]),
    ("CSBS", ["CSBS"]),
    # State regulators. Labelled by state so the pill reads as the state, not the
    # feed name — a reader filters by "Florida", not "FL OFR Press".
    ("Florida", ["FL OFR Press"]),
    ("Texas", ["TX Dept of Banking"]),
]


def build_rows(store):
    rows = []
    for r in store.values():
        rows.append({
            "key": r.get("key", ""),
            "title": r.get("title", ""), "url": r.get("url", ""),
            "fr_url": r.get("fr_url"), "date": r.get("date", ""),
            "sources": r.get("sources", []), "type": r.get("update_type", ""),
            "urgency": r.get("urgency", "Low"), "relevant": bool(r.get("relevant")),
            "why": r.get("plain_english", ""), "tags": r.get("tags", []),
            "fintech": bool(r.get("fintech_specific")),
            "credit_union": bool(r.get("credit_union")),
            "comments_close_on": r.get("comments_close_on"),
            "effective_on": r.get("effective_on"),
        })
    rows.sort(key=lambda d: (d["date"] or "0000"), reverse=True)
    return rows


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def assign_slugs(rows):
    """Give every relevant row a stable, unique slug for its permalink page.
    Set-aside items get none — thin, filtered-out content isn't worth its own
    indexable page, and skipping them keeps the sitemap free of noise."""
    seen = set()
    for d in rows:
        if not d["relevant"]:
            d["slug"] = None
            continue
        base = _SLUG_STRIP.sub("-", (d["title"] or "").lower()).strip("-")[:80] or "update"
        slug = f"{d['date'] or '0000-00-00'}-{base}"
        if slug in seen:
            n = 2
            while f"{slug}-{n}" in seen:
                n += 1
            slug = f"{slug}-{n}"
        seen.add(slug)
        d["slug"] = slug


UPDATE_PAGE_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  max-width:760px;margin:0 auto;padding:32px 20px;color:#212529;line-height:1.6;background:#fff}
a{color:#003b6a}
.badge{display:inline-block;background:#f0f0f0;color:#3c3c3c;font-size:12px;
  font-weight:700;text-transform:uppercase;letter-spacing:.04em;padding:3px 9px;
  border-radius:3px;margin:0 6px 0 0}
.meta{color:#6c757d;font-size:13px;margin:12px 0 24px}
.back{display:inline-block;margin-bottom:20px;font-size:13px}
footer{margin-top:40px;padding-top:16px;border-top:1px solid #e3e3e3;
  font-size:12px;color:#6c757d}
"""


def build_update_page(d, related=()):
    """A standalone, permalinked page for one regulatory update — the plain-
    English summary that otherwise only exists inside the dashboard's
    client-side JSON. Kept deliberately light (no app JS/CSS) since its only
    job is to be a crawlable, shareable landing page for this one topic.

    `related` is a handful of other updates from the same agency, passed in
    by write_update_pages(). Linking them here gives each permalink page a
    few internal inlinks besides the "back to tracker" link, and lets a
    crawler reach one topic's whole history without going back through the
    homepage's JS-rendered list each time."""
    title = d["title"] or "Regulatory update"
    agency = " · ".join(d["sources"]) or "Federal regulator"
    desc = (d["why"] or "")[:300]
    related_html = (
        "<h2>Related updates</h2><ul>" + "".join(
            f'<li><a href="{hesc(r["slug"])}.html">{hesc(r["title"] or "Regulatory update")}</a>'
            f' <span class="meta">{hesc(r["date"] or "")}</span></li>'
            for r in related
        ) + "</ul>"
    ) if related else ""
    page_url = f"{SITE_URL}{UPDATES_DIR}/{d['slug']}.html"
    dates = []
    if d.get("comments_close_on"):
        dates.append(f"Comments close {hesc(d['comments_close_on'])}")
    if d.get("effective_on"):
        dates.append(f"Takes effect {hesc(d['effective_on'])}")
    meta_line = " · ".join([hesc(d["date"] or "")] + dates)
    jsonld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": d["why"] or "",
        "datePublished": d["date"] or "",
        "url": page_url,
        "publisher": {"@type": "Organization", "name": "Kaufman Rossin",
                       "url": "https://kaufmanrossin.com/"},
        "about": agency,
    }
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{hesc(title)} — KleaRance</title>
<meta name="description" content="{hesc(desc)}">
<link rel="canonical" href="{page_url}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Klearance">
<meta property="og:title" content="{hesc(title)}">
<meta property="og:description" content="{hesc(desc)}">
<meta property="og:url" content="{page_url}">
<meta property="og:image" content="{SITE_URL}og-image.png">
<script type="application/ld+json">{json.dumps(jsonld)}</script>
<style>{UPDATE_PAGE_CSS}</style></head>
<body>
<a class="back" href="../index.html">&larr; Back to the KleaRance tracker</a><br>
<span class="badge">{hesc(d["type"] or "Update")}</span><span class="badge">{hesc(agency)}</span>
<h1>{hesc(title)}</h1>
<p class="meta">{meta_line}</p>
<p>{hesc(d["why"] or "")}</p>
<p><a href="{hesc(d["url"])}" target="_blank" rel="noopener">Read the original source at {hesc(agency)} &rarr;</a></p>
{related_html}
<footer>Tracked by <a href="../index.html">KleaRance</a>, Kaufman Rossin's regulatory
  intelligence platform. Not legal or compliance advice.</footer>
</body></html>"""


def write_update_pages(rows):
    """Writes one permalink HTML page per relevant row into UPDATES_DIR.
    Returns the list of (page_url, lastmod) pairs for the sitemap."""
    os.makedirs(UPDATES_DIR, exist_ok=True)
    slugged = [d for d in rows if d["slug"]]

    # Grouped by the same agency key build_update_page() shows as a badge, so
    # "related" always matches what's on screen. rows is already date-sorted
    # (build_rows), so each group stays newest-first without re-sorting here.
    by_agency = {}
    for d in slugged:
        by_agency.setdefault(" · ".join(d["sources"]), []).append(d)

    entries = []
    for d in slugged:
        siblings = by_agency[" · ".join(d["sources"])]
        related = [r for r in siblings if r is not d][:4]
        with open(os.path.join(UPDATES_DIR, f"{d['slug']}.html"), "w",
                   encoding="utf-8") as f:
            f.write(build_update_page(d, related))
        entries.append((f"{SITE_URL}{UPDATES_DIR}/{d['slug']}.html",
                         d["date"] or ""))
    return entries


def build_sitemap(update_entries, today):
    urls = [(SITE_URL, str(today))] + update_entries
    body = "".join(
        f"  <url><loc>{hesc(u)}</loc><lastmod>{hesc(lm or str(today))}</lastmod></url>\n"
        for u, lm in urls
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{body}</urlset>\n")


def kpis(rows, today):
    """Headline numbers, and tag each item with the tiles it belongs to.

    Each relevant row gets r["kpi"] = the list of tile keys it satisfies, and the
    tile counts are derived from those tags — so a tile's number and the list you
    get by clicking it are computed once and cannot drift apart. Non-relevant rows
    get an empty list, which also makes the click filter relevant-only for free.
    """
    def within(d, lo, hi):
        return bool(d) and str(lo) <= d <= str(hi)

    wk_start = today - timedelta(days=7)
    prev_start = today - timedelta(days=14)
    month_start = today.replace(day=1)
    q_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    q_end = date(today.year + (q_start.month + 3 > 12), ((q_start.month + 2) % 12) + 1, 28)
    soon_end = today + timedelta(days=30)

    for r in rows:
        r["kpi"] = []
    rel = [r for r in rows if r["relevant"]]

    last_wk = 0
    for r in rel:
        if within(r["date"], wk_start, today):
            r["kpi"].append("week")
        if within(r["date"], prev_start, wk_start):
            last_wk += 1
        if r["comments_close_on"] and r["comments_close_on"] >= str(today):
            r["kpi"].append("comments")
        if r["type"] == "Enforcement Action" and within(r["date"], month_start, today):
            r["kpi"].append("enforcement")
        if r["effective_on"] and str(q_start) <= r["effective_on"] <= str(q_end):
            r["kpi"].append("effective")

    def count(key):
        return sum(1 for r in rel if key in r["kpi"])

    this_wk = count("week")
    soon = sum(1 for r in rel if "comments" in r["kpi"]
               and r["comments_close_on"] <= str(soon_end))
    delta = this_wk - last_wk
    dn = "up" if delta > 0 else "down" if delta < 0 else ""
    dtxt = f"{'+' if delta > 0 else ''}{delta} vs last week" if delta else "same as last week"
    # (label, value, note, delta-class, tile key)
    # kpi-brk is an empty span, invisible and inert on desktop, that becomes
    # display:block at phone width -- a deliberate, explicit break point
    # rather than leaving it to the browser's own text-wrap, which depends
    # on the exact font actually loaded on the device and rendered
    # inconsistently between this build environment and a real phone (two
    # of four labels wrapped on-device here, none did in local testing).
    # Forcing all four to the same two-line shape sidesteps that mismatch
    # entirely instead of chasing it font by font.
    brk = '<span class="kpi-brk"></span>'
    return [
        ("Updates " + brk + "this week", this_wk, dtxt, dn, "week"),
        ("Open comment " + brk + "periods", count("comments"), f"{soon} closing within 30 days", "", "comments"),
        ("Enforcement " + brk + "actions", count("enforcement"), "This month", "", "enforcement"),
        # Two phrasings of one fact. At phone width the long form wraps to a
        # second line, which makes the whole bottom row 16px taller and leaves
        # the Enforcement tile beside it visibly empty — grid rows match heights,
        # so one wrapping note dents the tile next to it. Both are built from the
        # same q_end, so they cannot drift; CSS picks which is shown.
        ("Effective " + brk + "this quarter", count("effective"),
         f'<span class="n-long">Rules taking effect by {q_end.strftime("%b %Y")}</span>'
         f'<span class="n-short">By {q_end.strftime("%b %Y")}</span>', "", "effective"),
    ]


def _cfr_cell(cfr):
    url = ecfr_url(cfr)
    if not url:
        return html.escape(cfr)
    return (f'<a href="{url}" target="_blank" rel="noopener">'
            f'{html.escape(cfr)}</a>')


def regref_panel():
    """Federal Reserve regulation letter lookup, collapsed by default.

    Sits beside the tracker so a reader who hits "Regulation B" in an item can
    see what it covers without leaving the page. Rendered as static HTML and
    filtered client-side by the same search box as everything else.
    """
    blocks = []
    for group, desc, entries in regref.GROUPS:
        rows = "".join(
            # Searchable text includes the spoken forms — "regulation d" and
            # "reg d" — not just the bare letter, so the way people actually
            # type it finds the row.
            f'<tr class="rr" data-rr="{html.escape(" ".join([letter, "regulation " + letter, "reg " + letter, subject, cfr]).lower(), quote=True)}">'
            f'<td class="rr-letter">{html.escape(letter)}</td>'
            f'<td>{subject}'
            + (f'<div class="rr-note">{note}</div>' if note else "")
            # The cite links to the part on eCFR. All 47 were checked to resolve.
            # It stays a lookup aid, not a citation source — the caveat above the
            # table still stands, and eCFR is the current text, not a point-in-time
            # version, so anything being cited should be confirmed there directly.
            + f'</td><td class="rr-cfr">{_cfr_cell(cfr)}</td></tr>'
            for letter, subject, cfr, note in entries
        )
        blocks.append(
            f'<div class="rr-group"><h3>{html.escape(group)}</h3>'
            f'<p class="note">{html.escape(desc)}</p>'
            f'<table class="rr-table"><thead><tr><th>Reg</th><th>Subject</th>'
            f'<th>CFR</th></tr></thead><tbody>{rows}</tbody></table></div>'
        )

    # Omit the list entirely when there are no footnotes, rather than emitting an
    # empty <ul> that renders as stray padding.
    notes = (
        f'<ul class="rr-foot">{"".join(f"<li>{n}</li>" for n in regref.FOOTNOTES)}</ul>'
        if regref.FOOTNOTES else ""
    )
    return (
        '<details class="coverage" id="regref"><summary>'
        'Federal Reserve regulation reference (A&ndash;YY)</summary>'
        '<div class="body">'
        '<p>What each regulation letter covers, and where it now lives in the CFR. '
        'This is a lookup aid, not a citation source — confirm anything you intend '
        'to cite against the '
        '<a href="https://www.federalreserve.gov/supervisionreg/reglisting.htm" '
        'target="_blank" rel="noopener">Federal Reserve\'s own regulation listing</a>. '
        'Reserved and never-finalised letters are omitted.</p>'
        f'{"".join(blocks)}'
        f"{notes}"
        "</div></details>"
    )


def coverage_panel(store):
    """Build the 'what this covers' panel from the store itself.

    Generated rather than written by hand so it can't drift from reality — if a
    source breaks or is dropped, the panel stops claiming we track it. A public
    tool that silently under-reports is worse than no tool, because absence reads
    as "nothing happened".
    """
    # Live sources come from fetcher.py, not from the store. The store keeps
    # records from feeds that were trialled and dropped — SEC, FTC and CFTC were
    # each measured at 0 of 10 relevant and removed — and reading it alone made
    # this panel claim to track FTC and CFTC while the paragraph directly below
    # said it did not. A public page contradicting itself about its own coverage
    # is worse than one that says less.
    active = {s["agency"] for s, _ in fetcher.SOURCES}

    per = {}
    for r in store.values():
        for a in r.get("sources", []):
            if a in active and str(r.get("date", "")).startswith("20"):
                per.setdefault(a, []).append(r["date"])

    rows = []
    for agency in sorted(per):
        d = sorted(per[agency])
        url = SOURCE_LINKS.get(agency)
        name = html.escape(agency)
        # Linked to the agency's own listing page so a reader can check the
        # source rather than take this page's word for it.
        label = (f'<a href="{url}" target="_blank" rel="noopener">{name}</a>'
                 if url else name)
        rows.append(
            f'<div><strong>{label}</strong> — '
            f'{len(d)} items, {d[0]} to {d[-1]}</div>'
        )

    # Which states are tracked is read from the live source list (agency names
    # beginning with a 2-letter state code) so the Tracked line stays correct as
    # states are added or removed, rather than drifting against a hard-coded list.
    # We no longer enumerate what is NOT tracked: naming specific non-tracked
    # agencies caused a real error (they read as tracked) and invited "but you
    # follow NYDFS by email" confusion, when the email route never feeds this page.
    # The Tracked list below says what is in; one honest "not complete" line does
    # the rest. See the "Not a complete record" note further down.
    STATE_NAMES = {"FL": "Florida", "TX": "Texas"}
    tracked_states = sorted({
        STATE_NAMES.get(a.split()[0], a.split()[0])
        for a in active if a[:2] in STATE_NAMES and a[2:3] == " "
    })
    joined = (" and ".join([", ".join(tracked_states[:-1]), tracked_states[-1]])
              if len(tracked_states) > 2
              else " and ".join(tracked_states)) if tracked_states else ""

    tracked_intro = (
        'the US federal banking and financial-crime agencies listed below'
        + (f', plus the {joined} state financial regulators' if tracked_states else '')
        + '. History depth varies by source — some publish archives going back '
        'years, others only their most recent items.')
    return (
        '<details class="coverage"><summary>What this covers, and what it does not'
        '</summary><div class="body">'
        f'<p><strong>Tracked:</strong> {tracked_intro}</p>'
        f'<div class="grid">{"".join(rows)}</div>'
        # Moved here from the always-visible notice, which ran to six lines on a
        # phone. The instruction a reader must act on ("open the source") stays
        # up top; this is the explanation of how deadlines are derived, which is
        # reference and belongs with the other scope caveats.
        '<p style="margin-top:12px"><strong>Deadlines:</strong> shown only where a '
        'Federal Register record could be matched, and taken from that record\'s '
        'structured fields. An item showing no deadline has no match — that does '
        'not mean no deadline exists.</p>'
        # A separate mechanism from everything above — full-list diff against
        # OFAC's own SDN.CSV, not the classified/relevance-filtered feed — so
        # it needs its own line here or its coverage is invisible next to the
        # agency list above, same principle as the rest of this panel.
        '<p><strong>OFAC SDN list:</strong> tracked separately from the agency '
        'feed above — the full <a href="https://sanctionslist.ofac.treas.gov/Home/SdnList" '
        'target="_blank" rel="noopener">Specially Designated Nationals list</a> is '
        'downloaded and diffed against the previous day\'s list, not classified or '
        'filtered for relevance the way the rest of this page is.</p>'
        # One honest scope line instead of an enumerated "not tracked" list. The
        # enumeration named specific agencies (which read as tracked) and kept
        # inviting the question of why NYDFS — followed only by personal email
        # alert — wasn't here. What matters for liability is simply that a reader
        # must not assume completeness; absence on this page is not evidence that
        # nothing happened.
        '<p><strong>Not a complete record.</strong> Klearance covers the agencies '
        'listed above, and only what they post on those listing pages. It is not a '
        'substitute for monitoring every regulator you answer to &mdash; confirm '
        'anything material against the source.</p>'
        '<p><strong>Relevance:</strong> items are screened against a profile of US '
        'community banks (under ~$10B assets), federally-insured credit unions, and '
        'fintechs — BaaS and sponsor-bank arrangements, prepaid and FBO accounts, '
        'consumer lending and credit risk, BSA/AML, NCUA and share-insurance '
        'matters, and internal audit. Items outside that scope are collected but '
        'filtered out, so this is not a complete record of everything these '
        'agencies publish.</p>'
        "</div></details>"
    )


def sdn_panel(today):
    """OFAC SDN list changes, written by sdn_monitor.py into SDN_LOG_PATH.
    Server-rendered, not JS-templated like the main card feed — volume here
    is naturally small, so there's no pagination-vs-crawlability tradeoff to
    make; everything just renders.
    """
    if not os.path.exists(SDN_LOG_PATH):
        return ""
    with open(SDN_LOG_PATH, encoding="utf-8") as f:
        log = json.load(f)
    since_label = f"records back to {hesc(min(e['date'] for e in log))}" if log else "no records yet"

    # Three parallel lists rather than one combined feed — a reader wants
    # "who's newly sanctioned" or "who got delisted" or "what changed on an
    # existing entry", not a mixed chronological stream they have to scan
    # for the type they care about. (An earlier combined "Recent changes"
    # list — everything interleaved, its own search box — turned out to be
    # almost entirely redundant with these three, per Alexander.) Pulled
    # from the WHOLE log, not a day-windowed slice — a slow month for
    # additions shouldn't leave this empty just because the 5 most recent
    # ones happened 40 days ago.
    def _mini_list(title, action, empty_label):
        matching = sorted((e for e in log if e["action"] == action),
                           key=lambda e: e["date"], reverse=True)
        items = matching[:5]
        if not items:
            return (f'<details class="sdnmini"><summary><h3>{title}</h3></summary>'
                    f'<p class="sdnempty">{empty_label}</p></details>')
        rows = '<div class="sdnlist">' + "".join(
            f'<div class="sdnrow">'
            f'<span class="sdnname">{hesc(e["name"] or "(unnamed entry)")}</span>'
            f'<span class="sdnmeta">{hesc(e["program"] or "—")} · {hesc(e["date"])}</span>'
            f'</div>'
            for e in items
        ) + '</div>'
        # Fewer than 5 reads as cut off / broken unless it's clear that's
        # the true total, not a display limit — same principle as the
        # "Not a complete record" line elsewhere on this page: absence
        # or a short list must not be mistaken for missing data.
        note = (f'<p class="sdnnote">All {len(matching)} recorded.</p>'
                if len(matching) < 5 else '')
        return (f'<details class="sdnmini"><summary><h3>{title}</h3></summary>'
                f'{rows}{note}</details>')

    highlights = (
        '<div class="sdnhighlights">'
        + _mini_list("Most recent additions", "added", "No additions recorded yet.")
        + _mini_list("Most recent removals", "removed", "No removals recorded yet.")
        + _mini_list("Most recent modifications", "modified", "No modifications recorded yet.")
        + '</div>'
    )

    total = 0
    for path in (SDN_SNAPSHOT_PATH, "csl_snapshot.json"):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                total += len(json.load(f))
    total_label = f'{total:,} entries' if total else 'the full lists'

    # Separate from the changes log above: this looks up any name against the
    # CURRENT full SDN list (~19k entries), not just what changed recently.
    # Backed by sdn_index.json, fetched lazily on first use rather than
    # embedded in the page — a 19k-entry list doesn't belong in every page
    # load, only in the hands of someone actually searching.
    full_search = (
        '<div class="sdnfullsearch">'
        f'<h3>Screen a name <span class="sdnfullcount">(SDN + Non-SDN, {total_label})</span></h3>'
        '<div class="sdnmodes" role="tablist">'
        '<button type="button" class="sdnmode is-on" data-mode="one" aria-pressed="true">One name</button>'
        '<button type="button" class="sdnmode" data-mode="list" aria-pressed="false">Paste a list</button>'
        '</div>'
        '<div class="sdnsearch" data-pane="one">'
        '<input id="sdnlistq" type="search" autocomplete="off" '
        'placeholder="Name or alias — spelling and word order are matched loosely…" '
        'aria-label="Screen a name against the OFAC lists">'
        '<span id="sdnlistcount" class="sdncount"></span>'
        '</div>'
        '<div class="sdnbulk" data-pane="list" hidden>'
        '<textarea id="sdnbulkq" rows="6" '
        'placeholder="One name per line — customers, counterparties, beneficial owners…" '
        'aria-label="Paste names to screen, one per line"></textarea>'
        '<button type="button" id="sdnbulkgo">Screen list</button>'
        '</div>'
        '<p class="sdndisclaimer">A no-match here is <strong>not a compliance '
        'clearance.</strong> It searches the current OFAC SDN and Consolidated '
        '(non-SDN) lists by name and alias with loose spelling and word-order '
        'matching; it does not cover every sanctions or watch list, and the lists '
        'change daily. Confirm anything material against '
        '<a href="https://sanctionssearch.ofac.treas.gov/" target="_blank" '
        'rel="noopener">OFAC Sanctions Search</a>.</p>'
        '<div id="sdnlistresults" class="sdnlist sdnfullresults"></div>'
        '<p class="sdnfeeds">Recent SDN list changes, for periodic re-screening: '
        '<a href="sdn-changes.xml">RSS</a> &middot; <a href="sdn-changes.csv">CSV</a></p>'
        '</div>'
    )

    # Led with the full-list search — that's the one thing most readers
    # actually want here. Everything else (day-over-day changes, recent
    # additions/removals) is real and stays, but tucked into a closed
    # sub-section rather than competing with it up front; the panel was
    # reading as cluttered with all of it open by default.
    return (
        '<details class="panel p-sdn foldable" open>'
        f'<summary><h2>OFAC SDN list <span style="float:right;'
        f'text-transform:none;letter-spacing:0">{total_label}</span></h2></summary>'
        '<p class="sdnintro">Screen a name against OFAC\'s Specially Designated '
        'Nationals list and its Consolidated (non-SDN) list — primary names and '
        'known aliases, matched loosely on spelling and word order. Not '
        'classified or summarized; the name, program tag and entry detail '
        'speak for themselves.</p>'
        f'{full_search}'
        '<details class="sdnactivity">'
        f'<summary>Recent list activity <span class="sdnsince">({since_label})</span></summary>'
        f'{highlights}'
        '</details>'
        '</details>'
    )


# ----------------------------------------------------------- calendar feed (.ics)
# One VEVENT per upcoming deadline, each an all-day event carrying two DISPLAY
# alarms (7 days and 1 day before). The same event structure is built client-side
# for the per-item "Add to calendar" button; keep the two in step if either
# changes. RFC 5545: CRLF line endings, text escaping, and 75-octet line folding.

def _ics_escape(text):
    return (str(text).replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def _ics_fold(line):
    # Fold to <=75 octets with a leading space on continuations. Measured in
    # UTF-8 bytes, and a multi-byte char is never split across a fold.
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 74:
            out.append(cur)
            cur = b" " + b
        else:
            cur += b
    out.append(cur)
    return b"\r\n".join(out).decode("utf-8")


def _ics_events(rows, today):
    """(uid, yyyymmdd, label, title, url) for every upcoming deadline, soonest first.

    Deduplicated by UID. The store deliberately keeps same-agency items separate
    (merging them once collapsed three distinct OFAC actions), so one regulatory
    action can appear twice with an identical title, date and URL. On the page
    that is two rows; in a calendar it is one deadline, and emitting two VEVENTs
    under one UID is ambiguous — parsers may keep either or drop one silently.
    """
    events, seen = [], set()
    for r in rows:
        if not r["relevant"]:
            continue
        for field, label in (("comments_close_on", "Comments close"),
                             ("effective_on", "Takes effect")):
            d = r.get(field)
            # Full YYYY-MM-DD only; a month-only date can't be a calendar day.
            if not d or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) or d < str(today):
                continue
            url = r.get("fr_url") or r.get("url") or SITE_URL
            # URL is in the hash so two genuinely different items that happen to
            # share a title and date get distinct UIDs — a shared UID makes a
            # calendar treat them as one event and silently drop the other.
            uid = "regwatch-" + hashlib.sha1(
                (d + field + r["title"] + url).encode("utf-8")).hexdigest()[:16]
            if uid in seen:
                continue
            seen.add(uid)
            events.append((uid, d.replace("-", ""), label, r["title"], url))
    events.sort(key=lambda e: e[1])
    return events


def build_ics(rows, today):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//Klearance//Regulatory deadlines//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:Klearance regulatory deadlines",
        "X-WR-CALDESC:Comment-period and effective-date deadlines tracked by Klearance.",
        "REFRESH-INTERVAL;VALUE=DURATION:P1D", "X-PUBLISHED-TTL:P1D",
    ]
    for uid, ymd, label, title, url in _ics_events(rows, today):
        y, m, day = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:])
        end = (date(y, m, day) + timedelta(days=1)).strftime("%Y%m%d")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@regwatch",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{ymd}",
            f"DTEND;VALUE=DATE:{end}",
            f"SUMMARY:{_ics_escape(label + ': ' + title)}",
            f"DESCRIPTION:{_ics_escape(label + '. Open the source before acting: ' + url)}",
            f"URL:{url}",
            "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Klearance deadline in 7 days",
            "TRIGGER:-P7D", "END:VALARM",
            "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Klearance deadline tomorrow",
            "TRIGGER:-P1D", "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    # Fold every line, headers included — one over-length header is still a spec
    # violation some parsers reject. Folding a short line is a no-op.
    return "\r\n".join(_ics_fold(l) for l in lines) + "\r\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    with open(STORE_PATH, encoding="utf-8") as f:
        store = json.load(f)

    rows = build_rows(store)
    assign_slugs(rows)
    today = datetime.now(timezone.utc).date()

    # Which items have a spoken summary clip. Populated by the audio pipeline
    # into ./audio/<key>.mp3; drives whether playerBar renders a bar, so an
    # item without a clip (not yet generated, or none wanted) is untouched.
    _audio_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio")
    audio_keys = sorted(
        n[:-4] for n in os.listdir(_audio_dir) if n.endswith(".mp3")
    ) if os.path.isdir(_audio_dir) else []

    # Busiest agency first, so the ordering carries information rather than being
    # alphabetical by accident. Empty groups are dropped — a pill that returns
    # nothing is worse than no pill.
    relevant_rows = [d for d in rows if d["relevant"]]
    group_counts = [
        (label, feeds, sum(1 for d in relevant_rows
                           if any(f in d["sources"] for f in feeds)))
        for label, feeds in AGENCY_GROUPS
    ]
    group_counts = sorted((g for g in group_counts if g[2]),
                          key=lambda g: -g[2])

    source_pills = (
        '<button class="pill" data-kind="all" aria-pressed="true">All</button>'
        + "".join(f'<button class="pill" data-kind="agency" '
                  f'data-value="{hesc("|".join(feeds), quote=True)}" '
                  f'aria-pressed="false">{hesc(label)}</button>'
                  for label, feeds, _ in group_counts)
    )

    # Live counts in the share description, so the preview reflects reality
    # rather than a number that quietly goes stale.
    share_desc = (
        f"{sum(1 for d in rows if d['relevant'])} regulatory updates affecting "
        f"community banks, credit unions and fintechs, tracked across US federal "
        f"regulators and Florida's OFR. Plain-English summaries, comment "
        f"deadlines and effective dates. Updated daily."
    )

    coverage_html = coverage_panel(store)
    regref_html = regref_panel()
    sdn_html = sdn_panel(today)

    # Static seed for #cards: real <a href> markup for every relevant update,
    # present in the HTML this function returns — not only after
    # $('#cards').innerHTML = ... runs client-side (script.js, ~line 1660).
    # That JS call replaces this on load, so it stays the single source of
    # truth for the interactive list; this only makes sure a crawler reading
    # the raw HTML (no JS executed) still finds a real link to every
    # permalink page, instead of an empty div.
    static_cards_html = "".join(
        f'<div class="card"><h3><a href="{UPDATES_DIR}/{hesc(d["slug"])}.html">'
        f'{hesc(d["title"] or "Regulatory update")}</a></h3>'
        f'<p class="meta">{hesc(" · ".join(d["sources"]))} &middot; {hesc(d["date"] or "")}</p></div>'
        for d in relevant_rows if d["slug"]
    )


    # Tiles are clickable when they count something — clicking filters the list to
    # exactly those items. A zero tile is left inert (nothing to show).
    kpi_html = "".join(
        (f'<div class="kpi" data-kpi="{key}" role="button" tabindex="0" '
         f'aria-pressed="false">' if val else '<div class="kpi">')
        + f'<div class="l">{lbl}</div><div class="v">{val}</div>'
          f'<div class="n {cls}">{note}</div></div>'
        for lbl, val, note, cls, key in kpis(rows, today)
    )

    # Gated on ASK_ENABLED — see the note at the top of this file. When off the
    # panel is simply absent; initAsk() finds no #askq and returns, so no
    # corpus.json fetch and no call to the Worker.
    # data-regs is read by initAsk() to decide whether to load corpus.json, so
    # the scope of the feature is set here in Python, not duplicated in the JS.
    ask_html = f"""
<!-- Ask sits after the numbers and the filters, not before them. The tiles and
     the search box are what most readers came for and they cost nothing; the
     question box is the slow, optional thing. -->
<div class="ask-panel" data-regs="{1 if ASK_INCLUDE_REGULATIONS else 0}">
  <h2>Ask the tracked updates</h2>
  <p class="sub">Answers are drawn from the {'regulation text and the ' if ASK_INCLUDE_REGULATIONS else ''}updates
    tracked on this page, with sources named. Every question goes to three
    separate models and their answers are reconciled into one, with any
    disagreement between them stated in the answer. This is research to verify
    against the source &mdash; not legal or compliance advice.</p>
  <div class="ask-row">
    <input id="askq" autocomplete="off" maxlength="400"
           placeholder="e.g. what has FinCEN said about beneficial ownership?"
           aria-label="Ask a question about the tracked updates">
    <button id="askgo" type="button">Ask</button>
  </div>
  <div id="askout" aria-live="polite"></div>
  <div class="ask-note">Covers the updates tracked below{', and Regulation B, E and DD' if ASK_INCLUDE_REGULATIONS else ' &mdash; not regulation text'}.
    Answers can be wrong or incomplete &mdash; always open the source.</div>
</div>
""" if ASK_ENABLED else ""

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Regulatory update tracker — community banks, credit unions &amp; fintechs</title>
<meta name="description" content="{share_desc}">
<link rel="canonical" href="{SITE_URL}">
<!-- Tab, bookmark and home-screen icons. Generated by make_icons.py; the paths
     are relative because the site is served from a /regwatch/ subpath, not a
     domain root. Anything absolute here 404s.
     ?v=4 cache-busts every icon URL, manifest included -- Android's "Install
     app" flow mints a WebAPK via a Google server that caches the icon
     server-side once fetched, so a real icon-art change can outlive normal
     browser cache-clearing. Bump this number (and ICON_VERSION in
     make_icons.py, which stamps the same query onto the manifest's own
     icon srcs) any time the icon artwork changes again. -->
<link rel="icon" href="favicon.ico?v=4" sizes="any">
<link rel="icon" type="image/png" href="icon-32.png?v=4" sizes="32x32">
<link rel="icon" type="image/png" href="icon-16.png?v=4" sizes="16x16">
<link rel="apple-touch-icon" href="apple-touch-icon.png?v=4">
<link rel="manifest" href="site.webmanifest?v=4">
<meta name="theme-color" content="#003b6a">
<!-- Open Graph / Twitter card. Social scrapers cannot render the page, so the
     preview is driven entirely by these tags plus a real image file. Without
     them LinkedIn shows a bare URL with no title, description or image. -->
<meta property="og:type" content="website">
<meta property="og:site_name" content="Klearance">
<meta property="og:title" content="Regulatory update tracker — community banks, credit unions &amp; fintechs">
<meta property="og:description" content="{share_desc}">
<meta property="og:url" content="{SITE_URL}">
<meta property="og:image" content="{SITE_URL}og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="Regulatory update tracker for community banks, credit unions and fintechs">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Regulatory update tracker — community banks, credit unions &amp; fintechs">
<meta name="twitter:description" content="{share_desc}">
<meta name="twitter:image" content="{SITE_URL}og-image.png">
<script type="application/ld+json">{{
  "@context": "https://schema.org",
  "@type": "WebApplication",
  "name": "KleaRance",
  "url": "{SITE_URL}",
  "description": {json.dumps(share_desc)},
  "applicationCategory": "BusinessApplication",
  "operatingSystem": "Any",
  "isAccessibleForFree": true,
  "publisher": {{
    "@type": "Organization",
    "name": "Kaufman Rossin",
    "url": "https://kaufmanrossin.com/"
  }}
}}</script>
<style>{CSS}</style></head>
<body data-today="{today}">
<!-- Replica of kaufmanrossin.com's own two-band header, full-bleed outside
     .wrap so it spans edge to edge like the real one. Every link leaves
     Klearance for the real site — see the CSS comment above .krtop for why
     that's the right call rather than trying to invent Klearance equivalents
     of pages it doesn't have. -->
<div class="krtop">
  <div class="krtopwrap">
    <nav class="sites" aria-label="Kaufman Rossin sites">
      <a href="https://kaufmanrossin.com/" class="active" target="_blank" rel="noopener">CPAs and Advisors</a>
      <a href="https://kaufmanrossinwealth.com/" target="_blank" rel="noopener">Wealth</a>
      <a href="https://kaufmanrossinais.com/" target="_blank" rel="noopener">Fund Administration</a>
    </nav>
    <nav class="util" aria-label="Kaufman Rossin utility links">
      <a href="https://kaufmanrossin.com/" target="_blank" rel="noopener">Home</a>
      <a href="https://kaufmanrossin.com/kaufman-rossin-payment-portal/" target="_blank" rel="noopener">Payment Portal</a>
      <a href="https://kaufmanrossin.com/file-sharing/" target="_blank" rel="noopener">File Sharing</a>
      <a href="tel:888.680.5726">888.680.5726</a>
      <a href="https://es.kaufmanrossin.com/" target="_blank" rel="noopener">Espa&ntilde;ol</a>
    </nav>
  </div>
</div>
<header class="krheader">
  <div class="krheaderwrap">
    <!-- Two rows, not one: the real site sits its search box alone in a
         thin top row, right-aligned, with the logo+nav row below it — not
         inline with the nav links the way it first went in here. -->
    <div class="krheader-toprow">
      <form class="krsearch" role="search" action="https://kaufmanrossin.com/search" method="get" target="_blank">
        <label for="krsearch-input" class="sr-only">Search kaufmanrossin.com</label>
        <input id="krsearch-input" type="search" name="s" placeholder="Search">
        <button type="submit" aria-label="Search kaufmanrossin.com">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
        </button>
      </form>
    </div>
    <div class="krheader-mainrow">
      <div class="logowrap">{KR_LOGO_SVG}</div>
      <nav aria-label="Kaufman Rossin main navigation">
        <a href="https://kaufmanrossin.com/industries/" target="_blank" rel="noopener">Who We Serve</a>
        <a href="https://kaufmanrossin.com/services/" target="_blank" rel="noopener">What We Do</a>
        <a href="https://kaufmanrossin.com/resources/" target="_blank" rel="noopener">Our Ideas</a>
        <a href="https://kaufmanrossin.com/who-we-are/" target="_blank" rel="noopener">Get to Know Us</a>
        <a href="https://kaufmanrossin.com/careers/" target="_blank" rel="noopener">Careers</a>
        <a href="https://kaufmanrossin.com/contact-us/" target="_blank" rel="noopener">Contact Us</a>
      </nav>
    </div>
  </div>
</header>
<!-- Grey breadcrumb band, same as every subpage on kaufmanrossin.com runs
     directly under its header — "Home" is a real external link (leaves for
     the real site, same as everything else in the replicated chrome above),
     the current page is plain text, matching the real site's link/muted-text
     colour split exactly. -->
<div class="krcrumb">
  <div class="krcrumbwrap">
    <span class="krcrumb-path">
      <a href="https://kaufmanrossin.com/" target="_blank" rel="noopener">Home</a>
      <span aria-hidden="true">/</span>
      <span>Klearance</span>
    </span>
    <!-- Freshness stamp lives here now, not crowding the wordmark above —
         same fact, just relocated out of the title block. Sits as its own
         flex item (not grouped with the toolbar) so space-between spreads
         all three items across the row instead of bunching Updated and the
         buttons together on one side. -->
    <span class="krcrumb-updated">Updated
      {datetime.now(EASTERN).strftime('%B %-d, %Y %H:%M %Z') if os.name != 'nt'
       else datetime.now(EASTERN).strftime('%B %d, %Y %H:%M %Z')}</span>
    <!-- Mobile-only counterpart, hidden on desktop. Full-format above was
         dropped entirely on phones because "Updated [full date/time]" won the
         path row's one remaining slot over the icon toolbar every time and
         forced an ugly disconnected wrap. This one gets its own full-width
         row below the breadcrumb instead (see the mobile media query), so
         it's short enough to fit that row without repeating the wrap
         problem. -->
    <span class="krcrumb-updated-mobile">Updated
      {datetime.now(EASTERN).strftime('%b %-d, %H:%M %Z') if os.name != 'nt'
       else datetime.now(EASTERN).strftime('%b %d, %H:%M %Z')}</span>
    <!-- Share and install are mobile-first actions, so unlike the old lone
         Export CSV button they stay visible on a phone; see the media query.
         Export CSV moves into the overflow menu — it's still desktop-only,
         same reasoning as before, just relocated. Moved up here from the
         title card per Alexander, so they sit with the page-level "Updated"
         fact rather than crowding the wordmark; pushed to the row's far
         right edge (its own flex item) per his follow-up, not bunched
         against the Updated timestamp. -->
    <div class="icon-toolbar">
      <button id="shareBtn" class="icon-btn" type="button" aria-label="Share this page" title="Share">
        <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15V4"/><path d="M8 8l4-4 4 4"/><path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6"/></svg>
      </button>
      <button id="installBtn" class="icon-btn" type="button" aria-label="Install or bookmark this app" title="Install app">
        <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5h12a.5.5 0 0 1 .5.5v16l-6.5-4-6.5 4v-16a.5.5 0 0 1 .5-.5z"/></svg>
      </button>
      <div class="icon-btn-wrap">
        <button id="moreBtn" class="icon-btn" type="button" aria-label="More options" aria-haspopup="true" aria-expanded="false" title="More">
          <svg viewBox="0 0 24 24" width="19" height="19"><circle cx="5" cy="12" r="1.8" fill="currentColor"/><circle cx="12" cy="12" r="1.8" fill="currentColor"/><circle cx="19" cy="12" r="1.8" fill="currentColor"/></svg>
        </button>
        <div id="moreMenu" class="more-menu" hidden>
          <!-- Kept in the markup, not built conditionally, so #export's click
               handler always has its element regardless of menu state. -->
          <button id="export" type="button">Export CSV</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Hero band, same skeleton as the firm's RISK page (see the CSS comment
     above .herowrap): a brand moment before the functional dashboard starts,
     not part of .wrap so it can run full-bleed. -->
<div class="herowrap">
  <!-- v1.5 experiment: replaces the Check Spike background mark with a looped
       boardroom clip (Pexels, free license) + navy overlay for text contrast.
       See the .hero-video CSS comment for the fallback/reduced-motion story. -->
  <video class="hero-video" autoplay muted loop playsinline aria-hidden="true">
    <source src="assets/video/hero-boardroom.mp4" type="video/mp4">
  </video>
  <div class="hero-overlay" aria-hidden="true"></div>
  <div class="hero-inner">
    <div class="hero-titleblock">
      <h1 class="hero-word"><svg viewBox="0 0 40 34" aria-hidden="true">
          <path d="M2,22 L9,29 L17,10 L22,10 L25,4 L28,10 L38,10" fill="none"
            stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
          <circle class="hero-ping-sm" cx="25" cy="4" r="2.6" fill="none" stroke="var(--accent)"
            stroke-width="1.2" opacity="0"/>
          <circle cx="25" cy="4" r="2.6" fill="var(--accent)"/>
        </svg><span class="kr kr-k">K<span class="kr-divider" aria-hidden="true"></span></span><span class="hero-word-rest">lea</span><span class="kr kr-r">R</span><span class="hero-word-rest">ance</span><span class="sr-only"> — Regulatory Update Tracker for Community Banks, Credit Unions &amp; Fintechs</span></h1>
      <div class="hero-rule"></div>
      <p class="hero-sub"><b>by KAUFMAN <span class="hero-pipe">|</span> ROSSIN</b></p>
    </div>
    <div class="hero-divider"></div>
    <p class="hero-copy">Compliance teams don't need more alerts — they need
      clarity. KleaRance, Kaufman Rossin's regulatory intelligence platform,
      transforms the constant stream of rule changes into measurable,
      manageable insight your team can act on immediately.</p>
  </div>
</div>

<div class="wrap">

<div id="iconToast" class="icon-toast" role="status" aria-live="polite"></div>

<!-- Notes/Tasks dialog, shared across every item -- see the CSS comment
     above .item-dialog-backdrop for why this is one element, not one per
     card. Content and data-url swap in via renderItemDialogBody(). -->
<div id="itemDialog" class="item-dialog-backdrop" hidden>
  <div class="item-dialog" role="dialog" aria-modal="true" aria-labelledby="idTitle">
    <div class="idhead">
      <div class="idttl"><span class="idkind" id="idKind"></span><span id="idTitle"></span></div>
      <button id="idClose" type="button" aria-label="Close">&times;</button>
    </div>
    <div id="idBody"></div>
  </div>
</div>

<!-- One-time onboarding callout -- see initQuickStart() for how it finds and
     points at the first card's icon row. Shown once per browser, remembered
     via localStorage. -->
<div id="quickstart" class="quickstart" role="dialog" aria-label="Quick start" hidden>
  <div class="qs-tail"></div>
  <div class="qs-head">
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.6.55 1 1.32 1 2.17V16h6v-.33c0-.85.4-1.62 1-2.17A6 6 0 0 0 12 3z"/></svg>
    <b>Quick start</b>
    <button id="qsClose" class="qs-close" type="button" aria-label="Dismiss">&times;</button>
  </div>
  <p>Every update has three quick actions: add a deadline to your calendar, jot a private note, or track a task — right from the list.</p>
  <div class="qs-foot"><button id="qsDismiss" type="button">Got it</button></div>
</div>

<!-- The visible caveat is now the instruction only: what the summaries are, and
     what to do about it. How deadlines are derived moved into "What this covers"
     with the other scope caveats — it explains rather than instructs, and it was
     costing two of six lines on a phone. Nothing was deleted.
     Title + icon toolbar now live inside this same tile (merged with the old
     standalone .pagehead card, per Alexander — one tile, not two). -->
<div class="notice">
  <strong>Read this first.</strong> The summaries are based on agency listings.
  Always open the source document before acting on anything here.
  <div style="margin-top:9px">{coverage_html}</div>
  <div style="margin-top:6px">{regref_html}</div>
</div>

<div class="kpis">{kpi_html}
  <!-- Sits inside .kpis, not the sidebar — grouped with the KPI tiles since
       both are quick-glance summary stats, per Alexander. It takes tile3/4's
       old row-1 spot; those two tiles reflow to their own row below via
       nth-child ordering in the stylesheet, not by reordering the markup. -->
  <div class="panel p-agencies">
    <h2>Updates by agency <span style="float:right;text-transform:none;letter-spacing:0"
        id="agencycount"></span></h2>
    <div class="agrow" id="agencies"></div>
  </div>
</div>

<!-- Moved up from the bottom of the page, per Alexander — an empty-state
     panel ("No SDN list changes recorded") buried below 370 cards and the
     footer read as broken/missing rather than as a quiet day. -->
<div style="margin:18px 0">{sdn_html}</div>

<!-- Filters & view sits above Search now (was the other way round). On a phone
     this block collapses to its summary, so Search still lands directly under a
     single "Filters & view ▸" line and stays the first live control. -->
<details id="filters" open>
  <summary>Filters &amp; view</summary>
  <button id="clearFilters" type="button" class="clearfilters" hidden>Clear filters</button>
  <div class="pillgroup">
    <div class="grouplabel">View<small>how much to show</small></div>
    <div class="viewtoggle">
      <!-- "Relevant only" was self-referential: relevant to whom? These say who
           the page is for. Counts are filled in by script so they cannot go
           stale against the data. Everything comes first in the markup now,
           not because it's the default (it isn't — see setView below) but
           per Alexander's request on ordering. -->
      <button id="viewAll" aria-pressed="false">Everything</button>
      <button id="viewRelevant" aria-pressed="true">Banks, credit unions &amp; fintechs</button>
    </div>
    <!-- Fintech and Credit unions sit with the view toggle, not with Source,
         because they are the same kind of control: lenses on the classifier's
         judgment rather than keywords, and the two filters the search box cannot
         reproduce (a text match on "fintech"/"credit union" is far noisier). -->
    <button class="pill" data-kind="fintech" aria-pressed="false">Fintech only</button>
    <button class="pill" data-kind="credit_union" aria-pressed="false">Credit unions only</button>
    <span class="count" id="viewnote"></span>
  </div>
  <div class="pillgroup" id="sourceGroup">
    <div class="grouplabel">Source<small>who published it &middot; pick several to combine</small></div>
    {source_pills}
  </div>
</details>
<div class="pillgroup" id="searchGroup">
  <div class="grouplabel">Search<small>any word</small></div>
  <div class="searchwrap">
    <input id="q" type="search" autocomplete="off"
           placeholder="e.g. stablecoin, Regulation B, comment period…"
           aria-label="Search updates">
    <button id="clearq" type="button" hidden aria-label="Clear search">&times;</button>
  </div>
</div>

{ask_html}
<div class="cols">
  <div class="colmain">
    <!-- <details> rather than <div> so these fold on a phone, same mechanism as
         the filter block. Left open in the markup: a script failure must leave
         the content readable, never collapse the page to a row of headings. -->
    <details class="panel p-updates foldable" open>
      <summary><h2>Latest updates <span style="float:right;text-transform:none;letter-spacing:0"
          id="cardcount"></span></h2></summary>
      <div id="cards">{static_cards_html}</div>
      <button id="showmore" type="button" hidden>Show more updates</button>
    </details>
    <div id="alsofound"></div>
    <!-- Lives in .colmain, not after .cols, deliberately. .cols is a grid
         whose row stretches to the taller column; with the update list
         capped at 8 cards, colmain is now routinely shorter than colside's
         deadlines+agencies stack, and anything sitting after .cols closes
         had to wait out that whole row — a slab of empty page below the
         "Show more" button before this card even appeared. Placing it
         inside colmain means it follows the update list immediately,
         regardless of how tall the sidebar is. -->
    <div class="quickcontact">
      <div class="qc-photo">
        <img src="alexander-smith.png" alt="Alexander Smith, CRCM, CFE" width="72" height="72" loading="lazy">
      </div>
      <div class="qc-text">
        <a class="qc-name" href="https://kaufmanrossin.com/professionals/alexander-smith/" target="_blank" rel="noopener">Alexander Smith, CRCM, CFE</a>
        <div class="qc-title">Risk Advisory Services Senior Manager at Kaufman Rossin, one of the Top 50 CPA and advisory firms in the U.S.</div>
        <div class="qc-icons">
          <a href="mailto:asmith@kaufmanrossin.com?subject=Klearance%20regulatory%20tracker" aria-label="Email Alexander Smith">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg>
          </a>
          <a href="https://www.linkedin.com/in/alexandersmith14/" target="_blank" rel="noopener" aria-label="Alexander Smith on LinkedIn">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M4.98 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM3 9h4v12H3zm7 0h3.8v1.9h.05c.53-1 1.83-2.05 3.77-2.05 4.03 0 4.78 2.65 4.78 6.1V21h-4v-5.5c0-1.3-.02-3-1.83-3-1.83 0-2.11 1.43-2.11 2.9V21h-4z"/></svg>
          </a>
        </div>
      </div>
    </div>
  </div>
  <div class="colside">
    <details class="panel p-deadlines foldable" open>
      <summary><h2>Upcoming deadlines <span style="float:right;text-transform:none;letter-spacing:0"
          id="dlcount"></span></h2></summary>
      <!-- Says what the panel is currently scoped to. Without it the heading
           read "Upcoming deadlines" whatever was selected, and two of the KPI
           tiles are defined BY having a deadline — so filtering to them returns
           every deadline of that type and looks like the click did nothing. -->
      <div id="dlscope" class="dlscope"></div>
      <!-- No subscribe row. The feed still exists and still refreshes daily
           (deadlines.ics, see ICS_PATH) — it is simply not advertised here.
           Subscribing needed explaining, the copy-and-paste step was friction,
           and the calendar app does the rest anyway; "+ Calendar" on the
           deadline you actually care about is the direct action. -->
      <div id="deadlines"></div>
      <!-- Hidden in the markup and revealed by script only when something is
           actually capped, so a script failure leaves every deadline visible
           rather than a button that does nothing. -->
      <button id="dlmore" type="button" hidden>Show more deadlines</button>
    </details>
  </div>
</div>

</div>

<footer class="sitefoot">
  <div class="footwrap">
    <div class="footbrand">
      <div class="logowrap">{KR_LOGO_SVG}</div>
    </div>
    <div class="footnav">
      <div class="footcol">
        <h3>Locations</h3>
        <a href="https://kaufmanrossin.com/contact-us/miami/" target="_blank" rel="noopener">Miami</a>
        <a href="https://kaufmanrossin.com/contact-us/ft-lauderdale/" target="_blank" rel="noopener">Fort Lauderdale</a>
        <a href="https://kaufmanrossin.com/contact-us/boca-raton/" target="_blank" rel="noopener">Boca Raton</a>
        <a href="https://kaufmanrossin.com/contact-us/palm-beach/" target="_blank" rel="noopener">Palm Beach</a>
        <a href="https://kaufmanrossin.com/contact-us/new-york/" target="_blank" rel="noopener">New York</a>
        <a href="https://kaufmanrossin.com/contact-us/bangalore/" target="_blank" rel="noopener">Bangalore</a>
        <a href="https://kaufmanrossin.com/contact-us/gurgaon/" target="_blank" rel="noopener">Gurgaon</a>
        <a href="https://kaufmanrossin.com/contact-us/ivory-coast/" target="_blank" rel="noopener">Ivory Coast</a>
      </div>
      <div class="footcol">
        <h3>Quick Links</h3>
        <a href="https://kaufmanrossin.com/blog/" target="_blank" rel="noopener">Blog</a>
        <a href="https://kaufmanrossin.com/news/" target="_blank" rel="noopener">News</a>
        <a href="https://kaufmanrossin.com/resources/" target="_blank" rel="noopener">Resources</a>
        <a href="https://kaufmanrossin.com/professionals/" target="_blank" rel="noopener">Our Professionals</a>
        <a href="https://kaufmanrossin.com/preference-center/" target="_blank" rel="noopener">Subscription Center</a>
        <a href="https://www.linkedin.com/careersite/kaufmanrossin" target="_blank" rel="noopener">Careers</a>
        <a href="https://kaufmanrossin.com/events" target="_blank" rel="noopener">Events</a>
        <a href="https://kaufmanrossin.com/photos" target="_blank" rel="noopener">Photo Gallery</a>
      </div>
      <div class="footcol footsub">
        <h3>Subscribe</h3>
        <p>Get the latest news.</p>
        <a class="btn" href="https://kaufmanrossin.com/subscribe/" target="_blank" rel="noopener">Subscribe</a>
      </div>
    </div>
  </div>
  <div class="footsocialwrap">
    <div class="footsocial">
      <a class="social-btn" href="https://www.facebook.com/KaufmanRossin"
         target="_blank" rel="noopener" aria-label="Kaufman Rossin on Facebook">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M13.5 21v-8h2.7l.4-3.1h-3.1V8c0-.9.25-1.5 1.55-1.5H16.7V3.7C16.4 3.66 15.4 3.57 14.2 3.57c-2.5 0-4.2 1.52-4.2 4.3V9.9H7.3V13h2.7v8z"/></svg>
      </a>
      <a class="social-btn" href="https://www.linkedin.com/company/kaufman-rossin-&-co"
         target="_blank" rel="noopener" aria-label="Kaufman Rossin on LinkedIn">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M4.98 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM3 9h4v12H3zm7 0h3.8v1.9h.05c.53-1 1.83-2.05 3.77-2.05 4.03 0 4.78 2.65 4.78 6.1V21h-4v-5.5c0-1.3-.02-3-1.83-3-1.83 0-2.11 1.43-2.11 2.9V21h-4z"/></svg>
      </a>
      <a class="social-btn" href="https://www.youtube.com/user/KaufmanRossin"
         target="_blank" rel="noopener" aria-label="Kaufman Rossin on YouTube">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M21.6 7.6a2.7 2.7 0 0 0-1.9-1.9C18 5.2 12 5.2 12 5.2s-6 0-7.7.5a2.7 2.7 0 0 0-1.9 1.9A28 28 0 0 0 2 12a28 28 0 0 0 .4 4.4 2.7 2.7 0 0 0 1.9 1.9c1.7.5 7.7.5 7.7.5s6 0 7.7-.5a2.7 2.7 0 0 0 1.9-1.9A28 28 0 0 0 22 12a28 28 0 0 0-.4-4.4zM10 15.3V8.7L15.8 12z"/></svg>
      </a>
      <a class="social-btn" href="https://www.instagram.com/kaufmanrossin/"
         target="_blank" rel="noopener" aria-label="Kaufman Rossin on Instagram">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3.5" y="3.5" width="17" height="17" rx="4.5"/><circle cx="12" cy="12" r="4"/><circle cx="17.2" cy="6.8" r="1" fill="currentColor" stroke="none"/></svg>
      </a>
    </div>
  </div>
  <div class="footlegalwrap">
    <div class="footlegal">
      <p>The Kaufman Rossin Group consists of Kaufman Rossin CPAs and Advisors, a professional
        association providing accounting and advisory services; its wholly owned subsidiaries
        Kaufman Rossin Wealth, LLC, an Investment Adviser; Kaufman Rossin Insurance Services, an
        insurance solutions provider; Kaufman Rossin Registries, LLC, a registered agent; and
        Kaufman Rossin Professional Services Private Limited, an India-based professional
        services provider; as well as its affiliated entities, Kaufman Rossin Alternative
        Investment Services, LLC, a full-service fund administration provider, and Mary Street
        Capital, an investment banking affiliate.</p>
      <p><a class="donotsell" href="https://kaufmanrossin.com/website-privacy-policy/" target="_blank" rel="noopener">Do Not Sell or Share My Personal Information</a></p>
      <div class="footbottom">
        <div class="links">&copy; 2025 Kaufman, Rossin &amp; Co., A Professional Association, All Rights Reserved
          &nbsp;|&nbsp; <a href="https://kaufmanrossin.com/legal-disclaimer/" target="_blank" rel="noopener">Legal Disclaimer</a>
          &nbsp;|&nbsp; <a href="https://kaufmanrossin.com/website-privacy-policy/" target="_blank" rel="noopener">Privacy Policy</a></div>
        <div class="praxity">Kaufman Rossin is proud to be a member of Praxity</div>
      </div>
    </div>
  </div>
</footer>
<script type="application/json" id="data">{json.dumps(rows)}</script>
<script type="application/json" id="groups">{json.dumps(AGENCY_GROUPS)}</script>
<script type="application/json" id="audiokeys">{json.dumps(audio_keys)}</script>
<script>{JS}</script>
<script>
// Chrome dropped the hard requirement for a service worker to install from the
// menu (108 mobile / 112 desktop), but its algorithm for firing the automatic
// install prompt still weighs having a fetch handler. Without any worker at
// all, new visitors were never getting offered install. sw.js does nothing —
// no caching — so it can't ever serve yesterday's regulatory data as if it
// were current.
if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js');
</script>
</body></html>"""

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    update_entries = write_update_pages(rows)
    with open(SITEMAP_PATH, "w", encoding="utf-8") as f:
        f.write(build_sitemap(update_entries, today))
    print(f"Wrote {len(update_entries)} update pages to {UPDATES_DIR}/ "
          f"and {SITEMAP_PATH}")

    # The subscribable feed. Written with CRLF already embedded, so newline="" to
    # stop the platform translating them again.
    ics = build_ics(rows, today)
    with open(ICS_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(ics)

    print(f"Wrote {OUT_PATH} ({os.path.getsize(OUT_PATH)/1024:.0f} KB) — "
          f"{sum(1 for r in rows if r['relevant'])} relevant of {len(rows)} events")
    print(f"Wrote {ICS_PATH} ({ics.count('BEGIN:VEVENT')} deadlines)")
    if args.open:
        webbrowser.open("file://" + os.path.abspath(OUT_PATH))


if __name__ == "__main__":
    main()
