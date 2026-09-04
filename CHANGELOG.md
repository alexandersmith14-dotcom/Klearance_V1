# Changelog

Notable changes to Klearance. Newest first. Dates are UTC. Daily automated
"Update tracker data" commits are omitted. Entries before 2026-09-03 are
reconstructed from git history and grouped by theme rather than listed commit
by commit.

## 2026-09-03

### Sanctions screening — AfDB debarment list

- **Cloudflare Worker fetch-proxy for afdb.org.** `worker/worker.js` gained a
  `GET /proxy?url=` route (host allowlist + `PROXY_KEY` secret); `fetcher.get(url,
  via_worker=True)` routes through it. Deployed to
  `regwatch-ask.alexandersmith14.workers.dev` with
  `compatibility_flags = ["global_fetch_strictly_public"]` — without that flag a
  Worker subrequest to the Cloudflare-fronted afdb.org fails with error 1042.
- **Committed-snapshot fallback.** afdb.org serves an interactive Cloudflare
  challenge ("Just a moment…") that the proxy clears only intermittently, so
  `fetch_afdb()` retries four times then falls back to `afdb_snapshot.json`
  (cold-start pattern, same as `sdn_index_sam.json`). Every live success rewrites
  the snapshot and `afdb_snapshot_meta.json`. 1,363 rows: ~55 AfDB-own
  debarments plus cross-debarments from the other MDBs.
- **Freshness check.** `fetch_afdb()` writes `afdb_stale.json` each run;
  `afdb_issue.py` (workflow step, scheduled/manual only) keeps one
  `afdb-freshness` GitHub issue in sync — opened when the snapshot has gone
  `AFDB_STALE_DAYS` (21) without a live refresh, closed automatically on the next
  live success. Same shape as the FinCEN 311 freshness check.

### Sanctions screening — EBRD Ineligible Entities

- **New source, snapshot-only** (`source` tag `EBRD`). `www.ebrd.com`
  fingerprint-gates `POST /bin/ebrd_dxp/filterlistservlet` (plain POST → 403/500;
  impersonated Chrome → JSON), so there is no live path from CI.
- `make_ebrd_snapshot.py` — standalone, `curl_cffi` from a residential IP, walks
  all ~107 pages and writes `ebrd_snapshot.json` (+ `ebrd_snapshot_meta.json`).
  Kept out of `requirements.txt` on purpose; CI never runs it.
- `sdn_monitor.fetch_ebrd()` loads the snapshot and warns past `EBRD_STALE_DAYS`
  (120). 1,259 records: **131 EBRD-own primary debarments** plus 1,128
  cross-debarments already carried by the World Bank / ADB / AfDB feeds.
- `dashboard.py` coverage text names EBRD; fixed a duplicated
  "cross-debarments)" in the screening-panel intro.
- **IADB** remains uncovered — full Cloudflare Turnstile ("Verify you are
  human"), not reachable from CI, a Worker, or TLS impersonation. Its
  cross-debarments already flow through the WB / ADB / AfDB / EBRD feeds.

### Spoken-summary audio clips

- **Clip generation wired into the daily run.** The 773 committed clips were a
  one-time bulk render with no regeneration path, so every item added since
  2026-08-28 had no inline player.
- `make_audio.py` renders a Kokoro `af_heart` clip (same voice as the backlog)
  for each store item with a plain-English summary, re-renders one whose summary
  text changed, and prunes a clip whose item has left the store.
  `audio_manifest.json` tracks the text hash each clip was rendered from;
  existing clips with no manifest entry are trusted as-is. `AUDIO_MAX` caps
  renders per run (default 60), newest first.
- Workflow: `espeak-ng` + `ffmpeg` apt install (misaki's G2P needs the first,
  the runner image ships neither), a Hugging Face model cache, and a non-fatal
  "Render summary audio" step before the dashboard build. A broken Kokoro
  install degrades to "fewer players", never a failed publish. `audio/` and the
  manifest join the daily data commit.

### Docs

- Added this `CHANGELOG.md`.
- Refreshed `HANDOFF.md`, which had gone stale at 2026-08-01 — wrong project
  name, repo, paths and URL, and none of the sanctions-screening stack, the
  audio pipeline or the firm migration.

## 2026-09-01 – 09-02

### Sanctions screening — MDB debarment and FinCEN 311

- **FinCEN Section 311 / 9714 special measures** added as a screening source
  (`FinCEN 311`). `fetch_fincen311()` scrapes the only machine-readable form —
  the fincen.gov overview table — and joins `fincen311_details.csv`, hand-mined
  from the linked rulemaking PDFs (addresses, former names, named sub-entities).
  Rescinded measures are de-emphasised in results rather than dropped.
- **FinCEN 311 freshness check.** `fetch_fincen311()` writes
  `fincen311_stale.json` when the table shows a rule newer than a measure's
  `mined_date` (or a measure never mined); `fincen311_issue.py` keeps one
  labelled GitHub issue in sync, assigned to the repo owner. Detection is daily
  and automatic; re-mining stays a manual PR.
- **World Bank** (`World Bank`, ~1,515) and **Asian Development Bank** (`ADB`,
  ~1,495) debarment lists added, both via JSON APIs. Both carry cross-debarments
  from all MDBs.
- **AfDB** first attempt (`curl_cffi` past Cloudflare) was added then reverted —
  it cleared from a residential IP but not from the GitHub Actions runner.
- **Non-SDN (OFAC Consolidated) changes** now tracked day over day in
  `sdn_log.json` and the `sdn-changes` RSS/CSV feeds, alongside SDN.
- **Multi-list hits** surfaced on results ("on N lists") — by design, since
  there is no cross-source entity resolution.
- **Read-aloud ("Listen") button** on single-name and bulk screening results,
  using the browser's `speechSynthesis` (device voice, no files).
- **Adjustable minimum name score** on screening, defaulting to 95.

### Feeds

- Added **Fed Enforcement** and **FinCEN News** (press releases) as agency
  sources; added a coverage-watch check to `health.py`.

### Homepage copy and chrome

- Hero copy rewrite; advisory-team CTA in the hero; a full-width advisory band
  between the feed and the screening tools; weekly-roundup opt-in block; author
  card with a contact line. Homepage title / OG / H1 rewritten (SEO block 6).
- Product name settled as **Klearance** (after a stint spelled "KleaRance").

## 2026-08

### Sanctions & jurisdiction screening (new capability)

- **OFAC SDN list change monitor** (`sdn_monitor.py`) — daily diff of the SDN
  list against a committed snapshot, with a full-list name/alias search and
  recent-additions/removals highlights published alongside the dashboard.
  `sdn_log.json` backfilled with real historical changes; `sdn_backfill.py`
  added, lookback widened to 150 notices.
- **Multi-list screening** — BIS, State, UN, UK and EU consolidated lists added
  (`e33a7b0`), then **SAM.gov Exclusions** (~168k rows, fetched on
  scheduled/manual runs only with a committed cold-start index). Aliases,
  non-SDN list, near-match scoring, per-result detail and bulk paste-a-list.
  Whole-word matching (a substring match made "gg" hit "trigger").
- **Jurisdiction-risk panel** — FATF call-for-action and grey lists, the EU
  high-risk third-country list, INCSR jurisdictions and a CPI corruption score,
  as a country lookup. All groups collapsible.
- Screening and jurisdiction panels moved below the agency feed; quick-start
  tour steps added for them.

### Audio

- **Inline audio player** on every update and deadline (`ee6911b`); 773
  Kokoro-rendered summary clips committed so CI can serve them, and published in
  the Pages build. (Regeneration was not automated until 2026-09-03.)

### Branding and chrome

- Rebrand churn: ClearReg → Mihari → KleaRance → **Klearance**, on the Mihari
  codebase, with the "Check Spike" wordmark mark and a KAUFMAN | ROSSIN-style
  pipe divider. Repo renamed along the way (…→ `Mihari_V1` → `kaufman2699`).
- Full **kaufmanrossin.com-style chrome** — two-band navy header with search,
  full-bleed navy top band and footer, KR-style footer (headshot author card,
  Locations / Quick Links / Subscribe, real legal text), lime accent
  conventions, site-switcher divider.
- **K|R monogram app icon** (favicon, home-screen icons, `site.webmanifest`)
  matching KR's real installed-app icon.
- Automatic dark mode switched off while the light look is tuned.
- Extensive mobile layout fixes: KPI tile wrapping and alignment, icon-toolbar
  wrapping, hero spacing, column height-matching in JS, landscape breakpoint.

### Pipeline and ops

- **Persistent audit log** and store reconciliation added to the health check.
- **GitHub-issue alert on workflow failure**; push-retry in the update workflow
  to survive the race with the bot's own commit.
- Core on-page **SEO** fixes: H1, canonical, JSON-LD, per-update permalink
  pages, static internal links for crawlers, Search Console verification file.
- `anthropic` pinned below 2.x.

## 2026-07

Initial build (project created 2026-07-18 as "RegWatch").

- Daily pipeline: `fetcher.py` (14 federal sources — RSS, Federal Register API,
  scraped tables and Drupal lists) → `dedupe.py` (cross-agency clustering) →
  `classifier.py` (Claude relevance + fintech judgment + plain-English
  summaries) → `deadlines.py` (Federal Register comment/effective dates) →
  `dashboard.py` (one self-contained HTML page) → GitHub Pages.
- **Per-source health check** (`health.py`) with per-source quiet thresholds
  derived from each source's own publication history; only BROKEN fails the run.
- **Store push-guard** (`check_store.py` + `.githooks/pre-push`) — blocks a push
  that would delete events, since the Action also commits `store.json`.
- SEC / FTC / CFTC trialled, measured at 0/10 relevant, dropped.
- **Credit-union audience expansion**; **Florida OFR** and **Texas Dept of
  Banking** added as the first state sources (measured keep-rate first). Texas
  cert-chain completion (`_dob_context`) rather than disabled verification.
- Relevance reframed as a lens ("Banks, credit unions & fintechs / Fintech only
  / Credit unions only / Everything") rather than a gate; topic pills removed.
- Click-to-filter KPI tiles; foldable panels; tab / bookmark / home-screen
  icons; the Fed regulation A–YY reference table (`regref.py`).
- **`watchdog.yml`** — a weekly empty commit so GitHub's 60-day-inactivity rule
  never disables the schedule.
- **"Ask"** feature built and measured (three models answer, a fourth
  reconciles; retrieval in the browser, model call via a Cloudflare Worker),
  then **parked** behind `ASK_ENABLED = False` after a free model fabricated CFR
  subsections — model-written prose under a CRCM's name is a deliberate call,
  not a default. The Worker stays deployed (it also serves the AfDB `/proxy`
  route).
