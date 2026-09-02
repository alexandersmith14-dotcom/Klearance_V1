import base64
import feedparser
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser

import certifi

# Several agency sites (CSBS, OFAC) reject non-browser User-Agents with a 403,
# so present as a browser rather than as "RegWatch".
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# How many items to take from each source. None = take everything the source
# lists, which varies a lot: OFAC shows 10, FinCEN Advisories 76, NCUA Press 60.
# Taking everything gives deeper history and more slack between runs — a source
# only loses items once they scroll off its own listing page.
PER_SOURCE = None

# The Federal Register API needs a real number for per_page; it won't accept
# "everything". 100 is well past what these agencies publish in a year.
FEDREG_PER_PAGE = 100


def cap(items):
    """Apply PER_SOURCE if one is set."""
    return items if PER_SOURCE is None else items[:PER_SOURCE]

# ---------------------------------------------------------------- RSS sources

RSS_FEEDS = [
    {"agency": "FDIC", "url": "https://public.govdelivery.com/topics/USFDIC_26/feed.rss"},
    {"agency": "OCC", "url": "https://www.occ.gov/rss/occ_news.xml"},
    {"agency": "OCC Bulletins", "url": "https://www.occ.gov/rss/occ_bulletins.xml"},
    {"agency": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    # SR/CA letters — the Fed's supervisory guidance channel. Titles are bare
    # codes ("SR 26-3"); the real subject line is in the summary field.
    {"agency": "Fed SR/CA Letters", "url": "https://www.federalreserve.gov/feeds/bankinginfo-rss.xml"},
    # Enforcement actions — its own feed because they drop off press_all faster
    # than off here; measured at ~7 of 15 items not present in press_all.
    {"agency": "Fed Enforcement", "url": "https://www.federalreserve.gov/feeds/press_enforcement.xml"},
    {"agency": "CFPB", "url": "https://www.consumerfinance.gov/about-us/newsroom/feed/"},
    {"agency": "CFPB Rules", "url": "https://www.consumerfinance.gov/rules-policy/final-rules/feed/"},
]

# Dropped after measuring keep-rate — all three were pure classification cost:
#   SEC  (news/pressreleases.rss)     0 of 10 relevant
#   FTC  (feeds/press-release.xml)    0 of 10 relevant — antitrust, Made-in-USA
#                                     claims, tenant-screening FCRA; adjacent to
#                                     consumer protection but not to banks/fintechs
#   CFTC (PressRoom/PressReleases)    0 of 10 relevant — commodity swaps, margin
#                                     rules, agricultural advisory committees. Was
#                                     added specifically to chase crypto
#                                     derivatives; that slice never showed up.
# Not reachable: FFIEC and NYDFS return 403 to all server-side requests (edge bot
# protection, not a User-Agent issue). Both would need a headless browser.

# ------------------------------------------------- Federal Register API sources

FEDREG_AGENCIES = [
    {"agency": "FinCEN", "slug": "financial-crimes-enforcement-network"},
    {"agency": "NCUA", "slug": "national-credit-union-administration"},
]

# Ready to switch on, currently off to hold spend at zero. Adding these four
# gives their actual rulemaking (~1 year each) instead of the few weeks their RSS
# feeds carry — about $3.72 of one-off classification, then pennies a day.
FEDREG_AGENCIES_PENDING = [
    {"agency": "FDIC", "slug": "federal-deposit-insurance-corporation"},
    {"agency": "OCC", "slug": "comptroller-of-the-currency"},
    {"agency": "Federal Reserve", "slug": "federal-reserve-system"},
    {"agency": "CFPB", "slug": "consumer-financial-protection-bureau"},
]

# Historical depth, queried separately from the recent window.
#
# Restricted to rules and proposed rules on purpose. Across these six agencies
# since 2018 that is ~1,450 documents; without the type filter it is ~6,450, and
# the extra 5,000 are Notices — bank holding company applications, meeting
# notices, routine filings. The Federal Reserve alone drops from 3,193 to 362.
# Paying to classify those so the relevance filter can discard them is waste.
# Off by default. The backfill is a deliberate, one-off ~$15 spend, so it should
# never start because a scheduled job happened to run — flip this when you mean
# to do it.
ARCHIVE_ENABLED = False

ARCHIVE_SINCE = "2018-01-01"
ARCHIVE_TYPES = ["RULE", "PRORULE"]
ARCHIVE_PAGE_SIZE = 1000        # Federal Register API maximum
ARCHIVE_MAX_PAGES = 5           # guard against an unbounded crawl

# ------------------------------------------------------ Scraped HTML <table>s

HTML_TABLES = [
    {
        "agency": "FinCEN Advisories",
        "url": "https://www.fincen.gov/resources/advisoriesbulletinsfact-sheets",
        "base": "https://www.fincen.gov",
        "title_col": 0,
        "date_col": 1,
        "desc_col": 2,
    },
    {
        "agency": "NCUA Press",
        "url": "https://ncua.gov/news/press-releases",
        "base": "https://ncua.gov",
        "title_col": 0,
        "date_col": 2,
        "desc_col": 1,  # NCUA's middle column is a category, not a description
    },
]

# ------------------------------------------- Scraped Drupal "views-row" lists
# FDIC FILs are the FDIC's primary supervisory channel to community banks and
# never appear in its press-release feed. OFAC publishes no feed at all.
# link_filter keeps us on real content links and drops nav/sidebar noise.

VIEWS_ROW_PAGES = [
    {
        "agency": "FDIC FILs",
        "url": "https://www.fdic.gov/news/financial-institution-letters/index.html",
        "base": "https://www.fdic.gov",
        "link_filter": "/news/financial-institution-letters/",
    },
    {
        "agency": "OFAC",
        "url": "https://ofac.treasury.gov/recent-actions",
        "base": "https://ofac.treasury.gov",
        "link_filter": "/recent-actions/2",
    },
    {
        "agency": "CSBS",
        "url": "https://www.csbs.org/newsroom",
        "base": "https://www.csbs.org",
        "link_filter": "/newsroom/",
    },
    # FinCEN press releases, readouts and enforcement announcements. These carry
    # 311 actions, geographic targeting orders, joint interagency statements and
    # enforcement penalties that are never in the Federal Register or the
    # Advisories/Bulletins page - and without this a FinCEN-co-issued item is
    # only tagged with the other agencies that also published it. The list is
    # one Drupal view; each item is a <div class="fincen-news-article">.
    {
        "agency": "FinCEN News",
        "url": "https://www.fincen.gov/news/press-releases",
        "base": "https://www.fincen.gov",
        "row_class": "fincen-news-article",
        "link_filter": "/news/news-releases/",
    },
]

# ------------------------------------------------ Florida OFR (first state source)
# Florida's Office of Financial Regulation is the first state regulator added.
# Unlike NYDFS and California DFPI, it does NOT block automated access — a plain
# request returns 200 — so it is fetched directly, no email subscription needed.
#
# Only Press Releases. Its Industry Alerts page was measured too (25% keep-rate)
# but is mostly hurricane proclamations and license-renewal reminders; Press
# Releases carried the actual regulatory signal at 35% — state-level money
# services, crypto and check-casher enforcement that never appears in the federal
# feeds (e.g. the OFR agreement with BAM Trading / Binance.US). Its enforcement is
# search-only, not a browsable list, so it cannot be scraped and is left out.
#
# Each item is a <p class="h3"><a>Title</a></p> followed by an MM/DD/YYYY date and
# a summary paragraph; server-rendered, so no browser is required.

FLOFR_PAGES = [
    {"agency": "FL OFR Press", "url": "https://flofr.gov/news/press-releases",
     "base": "https://flofr.gov"},
]

# TX Dept of Banking Industry Notices. State-bank supervisory content — cyber
# alerts, ransomware self-assessment tools, FFIEC announcements, money-services
# (transmitter) legislation. Measured at 26% keep-rate, on par with Florida, and
# active through 2026. The page is a per-year accordion of
# <li>Month DD, YYYY: <a href="...pdf">Title</a></li> items. Fetched over the
# completed TLS chain (see _dob_context). Its Enforcement Orders and Supervisory
# Memoranda pages are higher-value still but need per-sector link following /
# have no listing dates — candidates for a later pass.
DOB_PAGES = [
    {"agency": "TX Dept of Banking",
     "url": "https://www.dob.texas.gov/news-and-events/industry-notices",
     "base": "https://www.dob.texas.gov"},
]

DOB_ITEM = re.compile(r"<li>\s*([A-Z][a-z]+\s+\d{1,2},\s*\d{4})\s*:\s*(.*?)</li>", re.S)

FLOFR_H3 = re.compile(r'<p[^>]*class="[^"]*\bh3\b[^"]*"[^>]*>(.*?)</p>', re.S | re.I)
FLOFR_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")

# --------------------------------- Texas Dept of Banking (incomplete TLS chain)
# www.dob.texas.gov serves a cert from SSL.com (a real, publicly-trusted CA) but
# OMITS the intermediate from its handshake. Browsers fetch the intermediate via
# the cert's AIA extension and verify fine; Python does not, so a plain fetch
# fails with "self-signed certificate in chain". We bundle the intermediate and
# complete the chain ourselves — this is PROPER verification against the SSL.com
# root in certifi, exactly what a browser does, NOT verification disabled. That
# distinction matters: the tool republishes what it fetches, so the connection's
# integrity has to be genuinely verified.
DOB_HOST = "dob.texas.gov"
DOB_INTERMEDIATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "certs-ssl-com-intermediate.pem")
# Where to re-fetch the intermediate if the bundled copy is ever missing or the
# CA rotates it. Safe over HTTP: the cert self-verifies by signature.
DOB_AIA = "http://cert.ssl.com/SSLcom-SubCA-SSL-RSA-4096-R1.cer"
_dob_ctx = None


def _dob_context():
    """SSL context that trusts the SSL.com root (via certifi) AND supplies the
    intermediate the server omits, so the chain verifies. Built once; auto-heals
    from the AIA URL if the bundled intermediate is missing."""
    global _dob_ctx
    if _dob_ctx is not None:
        return _dob_ctx
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        ctx.load_verify_locations(DOB_INTERMEDIATE)
    except (FileNotFoundError, ssl.SSLError):
        der = urllib.request.urlopen(DOB_AIA, timeout=30).read()
        pem = ("-----BEGIN CERTIFICATE-----\n"
               + base64.encodebytes(der).decode()
               + "-----END CERTIFICATE-----\n")
        ctx.load_verify_locations(cadata=pem)
    _dob_ctx = ctx
    return _dob_ctx

# ---------------------------------------------------------------- HTML helpers

TAG = re.compile(r"<[^>]+>")
LINK = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
TIME = re.compile(r'<time[^>]*datetime="([^"]{10})', re.I)
TEXTDATE = re.compile(r"([A-Z][a-z]{2,8})\s+(\d{1,2}),\s*(\d{4})")


def get(url, timeout=60, impersonate=False):
    """Fetch a URL as text.

    impersonate=True routes through curl_cffi with a real Chrome TLS/JA3
    fingerprint, which clears Cloudflare's passive bot check (AfDB, FFIEC).
    It does not solve the interactive JS challenge some sites use (IDB), and
    it is slower, so it stays opt-in per source.
    """
    if impersonate:
        from curl_cffi import requests as _cffi
        r = _cffi.get(url, impersonate="chrome124", timeout=timeout)
        r.raise_for_status()
        return r.text
    # dob.texas.gov needs the completed cert chain; everything else uses the
    # default context.
    ctx = _dob_context() if DOB_HOST in urllib.parse.urlsplit(url).netloc else None
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=timeout, context=ctx
    ) as r:
        return r.read().decode("utf-8", "ignore")


def text_of(html):
    return " ".join(unescape(TAG.sub(" ", html)).split())


class TableRowParser(HTMLParser):
    """Collects <tbody> rows from every <table> on a page.

    Each row is {"cells": [...], "url": first <a> href, "datetime": <time>}.
    Some FinCEN tables use <time>, others plain MM/DD/YYYY text, so capture both
    and let the caller prefer whichever exists.
    """

    def __init__(self):
        super().__init__()
        self.rows = []
        self.in_tbody = False
        self.in_cell = False
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tbody":
            self.in_tbody = True
        elif tag == "tr" and self.in_tbody:
            self.row = {"cells": [], "url": "", "datetime": ""}
        elif tag == "td" and self.row is not None:
            self.in_cell = True
            self.cell = []
        elif tag == "a" and self.in_cell and not self.row["url"]:
            self.row["url"] = a.get("href", "")
        elif tag == "time" and self.in_cell and not self.row["datetime"]:
            self.row["datetime"] = a.get("datetime", "")

    def handle_endtag(self, tag):
        if tag == "tbody":
            self.in_tbody = False
        elif tag == "td" and self.in_cell:
            self.row["cells"].append(" ".join("".join(self.cell).split()))
            self.in_cell = False
        elif tag == "tr" and self.row is not None:
            if self.row["cells"]:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


def parse_table_date(row, date_col):
    """Return (sort_key, display). Undated rows sort last."""
    raw = row["datetime"][:10] if row["datetime"] else ""
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d"), raw
        except ValueError:
            pass
    text = row["cells"][date_col] if date_col < len(row["cells"]) else ""
    try:
        dt = datetime.strptime(text, "%m/%d/%Y")
        return dt, dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    # FinCEN dates its evergreen reference material — fact sheets, infographics,
    # the SAR Bulletins — to the month only ("09/2007"). Without this they fell
    # through as the literal string "12/2020", which is not a date any consumer
    # of this field can sort, and earlier still as "unknown", which sorted ABOVE
    # 2026 items and put 1990s fact sheets at the top of the dashboard.
    # Reported as YYYY-MM, not YYYY-MM-01: we know the month, not the day, and
    # inventing a day would be asserting precision the source never gave.
    try:
        dt = datetime.strptime(text, "%m/%Y")
        return dt, dt.strftime("%Y-%m")
    except ValueError:
        return datetime.min, text or "unknown"


def parse_chunk_date(chunk):
    """Date from a views-row chunk: <time datetime> first, else 'July 17, 2026'."""
    m = TIME.search(chunk)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d"), m.group(1)
        except ValueError:
            pass
    m = TEXTDATE.search(text_of(chunk))
    if m:
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                dt = datetime.strptime(" ".join(m.groups()), fmt)
                return dt, dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return datetime.min, "unknown"


# ------------------------------------------------------------------- fetchers


def fetch_rss(source):
    # feedparser sends its own User-Agent by default, which FTC rejects with a 403.
    parsed = feedparser.parse(source["url"], agent=UA["User-Agent"])
    status = parsed.get("status")
    if status and status >= 400:
        raise RuntimeError(f"HTTP {status}")
    if not parsed.entries:
        raise RuntimeError("feed returned 0 entries")
    return [
        {
            "agency": source["agency"],
            "title": e.get("title", ""),
            "url": e.get("link", ""),
            "date": e.get("published", ""),
            # Some feeds (notably FDIC's GovDelivery) ship the entire HTML email
            # template as the summary — tens of KB of boilerplate per item. Strip
            # tags and cap it so the classifier sees content, not markup.
            "summary": text_of(e.get("summary", ""))[:1500],
            "source_type": "rss",
        }
        for e in cap(parsed.entries)
    ]


FEDREG_FIELDS = ["title", "publication_date", "type", "html_url", "abstract"]
FEDREG_API = "https://www.federalregister.gov/api/v1/documents.json"


def _fedreg_items(source, results):
    return [
        {
            "agency": source["agency"],
            "title": d.get("title", ""),
            "url": d.get("html_url", ""),
            "date": d.get("publication_date", ""),
            "summary": d.get("abstract") or "",
            "doc_type": d.get("type", ""),
            "source_type": "federal_register",
        }
        for d in results
    ]


def fetch_fedreg(source):
    """Recent window: every document type, newest first.

    Keeps notices, information-collection renewals and geographic targeting
    orders in view for current activity — those matter day to day even though
    they are not worth buying eight years of.
    """
    query = {
        "conditions[agencies][]": source["slug"],
        "order": "newest",
        "per_page": str(FEDREG_PER_PAGE),
        "fields[]": FEDREG_FIELDS,
    }
    url = FEDREG_API + "?" + urllib.parse.urlencode(query, doseq=True)
    data = json.loads(get(url, timeout=45))
    if not data.get("results"):
        raise RuntimeError("API returned 0 results")
    return _fedreg_items(source, data["results"])


def fetch_fedreg_archive(source):
    """Historical depth: rules and proposed rules back to ARCHIVE_SINCE.

    Paginated — a single request caps out well below the eight-year volume for
    the larger agencies, and silently taking only the first page would look like
    success while quietly truncating the record.
    """
    results, page = [], 1
    while page <= ARCHIVE_MAX_PAGES:
        query = {
            "conditions[agencies][]": source["slug"],
            "conditions[publication_date][gte]": ARCHIVE_SINCE,
            "conditions[type][]": ARCHIVE_TYPES,
            "order": "newest",
            "per_page": str(ARCHIVE_PAGE_SIZE),
            "page": str(page),
            "fields[]": FEDREG_FIELDS,
        }
        url = FEDREG_API + "?" + urllib.parse.urlencode(query, doseq=True)
        data = json.loads(get(url, timeout=90))
        batch = data.get("results") or []
        results.extend(batch)
        if len(batch) < ARCHIVE_PAGE_SIZE:
            break
        page += 1

    if not results:
        raise RuntimeError("archive query returned 0 results")
    return _fedreg_items(source, results)


def fetch_table(source):
    parser = TableRowParser()
    parser.feed(get(source["url"]))
    if not parser.rows:
        raise RuntimeError("no table rows found (page layout may have changed)")

    items = []
    for row in parser.rows:
        cells = row["cells"]
        if source["title_col"] >= len(cells):
            continue
        title = cells[source["title_col"]].replace("PDF Only", "").strip()
        # Spanish editions duplicate an English item we already have.
        if title.endswith(" Spanish"):
            continue
        sort_key, date_str = parse_table_date(row, source["date_col"])
        desc = cells[source["desc_col"]] if source["desc_col"] < len(cells) else ""
        items.append(
            (
                sort_key,
                {
                    "agency": source["agency"],
                    "title": title,
                    "url": urllib.parse.urljoin(source["base"], row["url"]),
                    "date": date_str,
                    "summary": desc,
                    "source_type": "scraped_table",
                },
            )
        )
    if not items:
        raise RuntimeError("rows found but none had a usable title column")
    items.sort(key=lambda p: p[0], reverse=True)
    return cap([x for _, x in items])


def fetch_views_rows(source):
    """Scrape a Drupal listing page that renders results as repeated row blocks.

    Chunks the page on row-class boundaries and takes the longest matching link
    as the title. Chunks with no matching link are dropped, which conveniently
    also discards nested-row fragments and sidebar blocks. The row class is
    "views-row" by default; a page that wraps each result in its own class
    (FinCEN's news list uses "fincen-news-article") sets "row_class".
    """
    html = get(source["url"])
    row_class = source.get("row_class", "views-row")
    # Require the class token to end here (quote or space), so "fincen-news-article"
    # does not also match its BEM children "fincen-news-article__image" etc. and
    # chop each item into fragments.
    marks = [m.start() for m in re.finditer(
        r'class="[^"]*' + re.escape(row_class) + r'(?=[\s"])', html)]
    if not marks:
        raise RuntimeError(f"no {row_class} blocks found (page layout may have changed)")

    filt = source.get("link_filter")
    items, seen = [], set()
    for chunk in (html[a:b] for a, b in zip(marks, marks[1:] + [len(html)])):
        links = [
            (h, text_of(t))
            for h, t in LINK.findall(chunk)
            if text_of(t) and (filt is None or filt in h)
        ]
        if not links:
            continue
        href, title = max(links, key=lambda p: len(p[1]))
        url = urllib.parse.urljoin(source["base"], href)
        if url in seen:
            continue
        seen.add(url)
        sort_key, date_str = parse_chunk_date(chunk)
        # Chunks start mid-attribute (inside the opening tag), so drop everything
        # up to that tag's closing '>' before extracting text — otherwise the
        # attribute fragment survives tag-stripping and pollutes the summary.
        body = chunk[chunk.find(">") + 1 :] if ">" in chunk else chunk
        # Same problem at the tail: the chunk ends wherever the next views-row
        # begins, which can be mid-tag. Drop any unterminated trailing tag.
        body = re.sub(r"<[^>]*$", "", body)
        summary = text_of(body).replace(title, "", 1).strip()[:300]
        items.append(
            (
                sort_key,
                {
                    "agency": source["agency"],
                    "title": title,
                    "url": url,
                    "date": date_str,
                    "summary": summary,
                    "source_type": "scraped_list",
                },
            )
        )
    if not items:
        raise RuntimeError(f"{row_class} blocks found but none matched link_filter")
    items.sort(key=lambda p: p[0], reverse=True)
    return cap([x for _, x in items])


def fetch_flofr(source):
    """Scrape a Florida OFR listing page (h3-title + MM/DD/YYYY + summary blocks).

    Split the page on each <p class="h3">; everything up to the next one is a
    single item. The date is the first MM/DD/YYYY after the title, and the
    summary is the text following it.
    """
    html = get(source["url"])
    marks = [m.start() for m in FLOFR_H3.finditer(html)]
    if not marks:
        raise RuntimeError("no h3 items found (page layout may have changed)")

    items = []
    for a, b in zip(marks, marks[1:] + [len(html)]):
        chunk = html[a:b]
        h3 = FLOFR_H3.search(chunk)
        link = LINK.search(h3.group(1)) if h3 else None
        if not link:
            continue
        title = text_of(link.group(2))
        if not title:
            continue
        rest = chunk[h3.end():]
        m = FLOFR_DATE.search(rest)
        if m:
            try:
                dt = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                date_str = dt.strftime("%Y-%m-%d")
            except ValueError:
                dt, date_str = datetime.min, "unknown"
            summary = text_of(rest[m.end():])[:300]
        else:
            dt, date_str, summary = datetime.min, "unknown", text_of(rest)[:300]

        items.append((dt, {
            "agency": source["agency"],
            "title": title,
            "url": urllib.parse.urljoin(source["base"], link.group(1)),
            "date": date_str,
            "summary": summary,
            "source_type": "scraped_list",
        }))
    if not items:
        raise RuntimeError("h3 blocks found but none had a usable title")
    items.sort(key=lambda p: p[0], reverse=True)
    return cap([x for _, x in items])


def fetch_dob(source):
    """TX Dept of Banking Industry Notices: <li>Date: <a>Title</a></li> items in
    a per-year accordion. Fetched over the completed TLS chain."""
    html = get(source["url"])
    items = []
    for m in DOB_ITEM.finditer(html):
        date_raw, body = m.group(1), m.group(2)
        link = LINK.search(body)
        if not link:
            continue
        title = text_of(link.group(2))
        if not title:
            continue
        try:
            dt = datetime.strptime(date_raw.replace(",", ""), "%B %d %Y")
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            dt, date_str = datetime.min, "unknown"
        items.append((dt, {
            "agency": source["agency"],
            "title": title,
            "url": urllib.parse.urljoin(source["base"], link.group(1)),
            "date": date_str,
            "summary": "",
            "source_type": "scraped_list",
        }))
    if not items:
        raise RuntimeError("no <li> date-items found (page layout may have changed)")
    items.sort(key=lambda p: p[0], reverse=True)
    return cap([x for _, x in items])


# ----------------------------------------------------------------------- main

SOURCES = (
    [(s, fetch_rss) for s in RSS_FEEDS]
    + [(s, fetch_fedreg) for s in FEDREG_AGENCIES]
    # Historical backfill — DISABLED. Set ARCHIVE_ENABLED = True to turn on.
    # Adds ~1,450 rules and proposed rules back to 2018 across the six agencies,
    # which is ~1,400 new events and roughly $15 of one-off classification.
    # Everything after that stays a few cents a day, as now.
    + ([({**s}, fetch_fedreg_archive) for s in FEDREG_AGENCIES] if ARCHIVE_ENABLED else [])
    + [(s, fetch_table) for s in HTML_TABLES]
    + [(s, fetch_views_rows) for s in VIEWS_ROW_PAGES]
    + [(s, fetch_flofr) for s in FLOFR_PAGES]
    + [(s, fetch_dob) for s in DOB_PAGES]
)

def fetch_with_retry(fetch, source, attempts=5, pause=5):
    # health.py marks a source BROKEN on a single failed run, no averaging like
    # the QUIET threshold gets, so one dropped connection reads the same as a
    # genuinely dead source. In one day: CFPB Rules hit IncompleteRead twice,
    # OCC returned 0 entries, and TX Dept of Banking failed DNS resolution —
    # three different sources, three different fetch mechanisms (feedparser,
    # RSS, a custom-SSL urllib scrape), three different error shapes, but curl
    # fetched all three clean immediately after. That pattern is the runner's
    # network, not the sources, so the retry belongs at the call site shared
    # by every fetcher rather than duplicated inside each one.
    #
    # 3 attempts / 3s still let OCC through as BROKEN in 2 of 12 runs (2026-08-18)
    # — bumped to 5 attempts with exponential backoff (5s, 10s, 20s, 40s) so a
    # longer transient block has room to clear before the run gives up.
    for attempt in range(attempts):
        try:
            return fetch(source)
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(pause * (2 ** attempt))


def main():
    results, failures = [], []
    counts = {}

    for source, fetch in SOURCES:
        try:
            items = fetch_with_retry(fetch, source)
            results.extend(items)
            counts[source["agency"]] = counts.get(source["agency"], 0) + len(items)
            print(f"  OK    {source['agency']:20} {len(items)} items")
        except Exception as e:
            failures.append((source["agency"], str(e)))
            print(f"  FAIL  {source['agency']:20} {e}")

    with open("updates.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    # Record what happened for health.py. Without this the failures below are
    # printed into a log nobody reads and then lost — the run still exits 0, so
    # a source can 403 every day for a month and the Action stays green.
    with open("fetch_report.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "counts": counts,
                "failures": [{"agency": a, "error": e} for a, e in failures],
            },
            f,
            indent=2,
        )

    print(
        f"\n{len(results)} updates from {len(SOURCES) - len(failures)} of "
        f"{len(SOURCES)} sources -> updates.json"
    )

    if failures:
        print(f"\nWARNING: {len(failures)} source(s) failed:")
        for agency, err in failures:
            print(f"  - {agency}: {err}")


# Guarded so other modules (browser_fetcher) can reuse TableRowParser / text_of
# without triggering a full fetch on import.
if __name__ == "__main__":
    main()
