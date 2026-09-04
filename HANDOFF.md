# Klearance — handoff

Paste this into a new chat, or point Claude at this file.

**Project:** `C:\Users\alexa\Klearance_V1`
**Live:** https://kaufman2699.github.io/Klearance_V1/
**Repo:** https://github.com/kaufman2699/Klearance_V1 (public)

> Formerly "RegWatch" / "RegWatch_V2". Renamed to Klearance and the repo moved to
> the `kaufman2699` org during the firm migration. Older commit messages, the
> `worker/` deploy name (`regwatch-ask`) and some code comments still say
> RegWatch — that is cosmetic, not a second project.

---

## What it is

A regulatory tracker for community banks, credit unions and fintechs, built by
Alexander Smith (CRCM, CFE — Risk Advisory Services, Kaufman Rossin) as a free
public tool for business development. Firm approval obtained.

It watches 18 federal and state agency feeds daily, deduplicates interagency
republication, uses Claude to judge relevance and write plain-English summaries,
matches Federal Register records for real comment deadlines, screens names
against a stack of sanctions and debarment lists, and publishes one
self-contained HTML page.

**State: finished and running.** Nothing is half-built.

    800 events tracked · 477 relevant · 74 fintech-specific · 103 credit-union

---

## How it runs

GitHub Actions, `.github/workflows/update.yml`:
- **06:30 UTC daily**, on push to main, or manually from the Actions tab
- Steps, in order: `fetcher.py` → `pipeline.py` → `deadlines.py` →
  `sdn_monitor.py` → `country_risk.py` → `make_audio.py` → `dashboard.py` →
  `health.py` → freshness-issue steps → commit → assemble `site/` → deploy Pages
- Secrets: `ANTHROPIC_API_KEY` (classifier), `SAM_API_KEY` (SAM.gov exclusions),
  `WORKER_PROXY_KEY` (AfDB fetch proxy); repo Variable `PROXY_WORKER_URL`
- Cost: a few cents a day; nothing on days with no new items
- `watchdog.yml` is a weekly empty commit so the schedule is not disabled by
  GitHub's 60-day-inactivity rule (a scheduled run does not itself count).

Locally the same scripts run in that order. `.env` holds the keys and is
gitignored.

---

## Files

| File | Role |
|---|---|
| `fetcher.py` | 18 agency feeds: RSS, Federal Register API, scraped tables, scraped Drupal lists. Also the optional Cloudflare Worker fetch-proxy (`get(url, via_worker=True)`) |
| `dedupe.py` | Cross-agency clustering; key is sha1(normalized title + date) |
| `classifier.py` | Relevance + fintech + credit-union judgment + summaries. `classify()` is importable |
| `pipeline.py` | Dedupe + classify only what's new. `--dry-run`, `--backfill FIELD`, `--refresh-dates` |
| `deadlines.py` | Matches Federal Register records for comment / effective dates |
| `sdn_monitor.py` | Sanctions + debarment screening index (`sdn_index.json` + `sdn_index_sam.json`), day-over-day diff of SDN / Non-SDN / FinCEN 311 into `sdn_log.json` and the change feeds |
| `sdn_backfill.py` | One-off historical SDN backfill helper |
| `country_risk.py` | Jurisdiction-risk reference (FATF, EU, INCSR + corruption score) → `country_risk.json` |
| `regref.py` | Fed regulation A–YY lookup table |
| `ecfr_corpus.py` | Builds `corpus.json` (Regs B, E, DD) for the parked "Ask" panel |
| `make_audio.py` | Renders one Kokoro `af_heart` MP3 per store key for the inline player; `audio_manifest.json` tracks text hashes |
| `make_ebrd_snapshot.py` | **Run by hand, not in CI.** Regenerates `ebrd_snapshot.json` via `curl_cffi` from a residential IP |
| `fincen311_issue.py` | Keeps one GitHub issue in sync with `fincen311_stale.json` |
| `afdb_issue.py` | Keeps one GitHub issue in sync with `afdb_stale.json` |
| `dashboard.py` | Builds `dashboard.html`, the permalink pages and the calendar feed |
| `health.py` | Per-source health check. `--report-only` never exits non-zero |
| `check_store.py` | Blocks a push that would delete events. Run by `.githooks/pre-push` |
| `make_og_image.py` / `make_icons.py` | Regenerate the LinkedIn card / favicons + manifest |
| `store.json` | **The memory — 800 analysed events. Do not delete.** |

---

## Sanctions & debarment screening

`sdn_monitor.py` builds a name/alias search index published alongside the
dashboard and searched entirely in the browser. Sources, and how each is
fetched:

| Source | tag | ~rows | fetch |
|---|---|---|---|
| OFAC SDN | `SDN` | 19,300 | OFAC `SDN.CSV` |
| OFAC Consolidated (non-SDN) | `Non-SDN` | 480 | OFAC CSV |
| BIS + State (Entity List, DPL, Unverified, MEU, ITAR, nonprolif.) | `BIS` / `State` | 6,300 | Trade.gov CSL |
| UN Consolidated | `UN` | 1,000 | UN XML |
| UK OFSI ConList | `UK` | 5,100 | OFSI CSV |
| EU FSF | `EU` | 6,200 | EU XML |
| SAM.gov Exclusions | `SAM` | 168,000 | SAM API (scheduled runs only — low quota; cold-start `sdn_index_sam.json` on push builds) |
| World Bank debarment | `World Bank` | 1,515 | JSON API |
| Asian Development Bank | `ADB` | 1,495 | paginated JSON |
| African Development Bank | `AfDB` | 1,363 | Worker fetch-proxy + committed `afdb_snapshot.json` fallback (see below) |
| EBRD Ineligible Entities | `EBRD` | 1,259 | **snapshot only** — `ebrd_snapshot.json`, regenerated by hand (see below) |
| FinCEN Section 311 / 9714 special measures | `FinCEN 311` | 51 | scraped fincen.gov table + hand-mined `fincen311_details.csv` |

### AfDB — Worker fetch-proxy + snapshot

- `worker/worker.js` has a `GET /proxy?url=` route (host allowlist + `PROXY_KEY`
  secret). Deployed at `regwatch-ask.alexandersmith14.workers.dev` (Cloudflare
  account `alexandersmith14@gmail.com`) with
  `compatibility_flags = ["global_fetch_strictly_public"]` — without that flag a
  Worker subrequest to Cloudflare-fronted afdb.org fails with **error 1042**.
- The Worker `PROXY_KEY` secret must equal the `WORKER_PROXY_KEY` GitHub repo
  secret. `PROXY_WORKER_URL` is a repo Variable.
- afdb.org serves an **interactive** Cloudflare challenge ("Just a moment…") that
  the proxy clears only intermittently, so `fetch_afdb()` retries 4× then falls
  back to committed `afdb_snapshot.json` (+ `afdb_snapshot_meta.json` date). Every
  live success rewrites both. `afdb_issue.py` opens a GitHub issue if the
  fallback goes `AFDB_STALE_DAYS` (21) without a live refresh.

### EBRD — manual snapshot

- `www.ebrd.com` fingerprint-gates `POST /bin/ebrd_dxp/filterlistservlet` — a
  plain POST gets 403/500, an impersonated-Chrome POST gets JSON. **No live path
  from CI.**
- `make_ebrd_snapshot.py` (standalone, `curl_cffi`, residential IP; kept OUT of
  `requirements.txt`) walks ~107 pages → `ebrd_snapshot.json` +
  `ebrd_snapshot_meta.json`. `fetch_ebrd()` just loads it and warns past
  `EBRD_STALE_DAYS` (120). Re-run every few months and commit.
- ~131 of 1,259 rows are EBRD-own primary debarments; the rest are
  cross-debarments already carried by the World Bank / ADB / AfDB feeds.

### IADB — not covered

Full Cloudflare **Turnstile** ("Verify you are human"). Not reachable from CI, a
Worker, curl, or TLS impersonation. Its cross-debarments already flow through
the World Bank / ADB / AfDB / EBRD feeds. Options if ever wanted: a commercial
anti-bot proxy (Zyte / ScrapingBee), or a manual quarterly CSV → snapshot.

### FinCEN 311 freshness

`fetch_fincen311()` writes `fincen311_stale.json` when the FinCEN table shows a
rule newer than the hand-mined `mined_date` (or a measure never mined).
`fincen311_issue.py` keeps one labelled GitHub issue in sync. Detection is
automatic and daily; **re-mining the PDFs into `fincen311_details.csv` stays a
manual PR.**

---

## Spoken-summary audio

`make_audio.py` renders a Kokoro `af_heart` MP3 per store key with a
plain-English summary; `dashboard.py` scans `./audio` at build time to decide
which items get an inline player (`AUDIO_KEYS`).

- `audio_manifest.json` records the text hash each clip was rendered from, so a
  summary edit is re-rendered without touching the rest. Existing clips with no
  manifest entry are trusted as-is.
- `AUDIO_MAX` (default 60) caps renders per run, newest first — a mass summary
  change can't blow the CI budget.
- The workflow `apt-get install`s **espeak-ng** (misaki's G2P) **and ffmpeg**
  (WAV→MP3 — the runner image ships neither) and caches the HF model. The
  "Render summary audio" step is **non-fatal**: a broken Kokoro install degrades
  to "fewer players", never a failed publish — same rule as the agency sources.
- `audio/` (~800 MP3s, ~125 MB) is committed and grows ~1 MB/day; pruning only
  offsets items aging out of `store.json`.
- The browser `speechSynthesis` "Listen" button on screening results is a
  separate, older feature — device voice, no files.

---

## Decisions worth not re-litigating

- **Dedupe only merges across different agencies.** Same-agency merging collapsed
  three distinct OFAC Iran actions and two different CFPB ECOA rules.
- **Store key includes the date.** Title alone collapsed 18 "Sunshine Act
  Meetings" into one and silently dropped the whole beneficial-ownership sequence.
- **SEC, FTC and CFTC were added, measured at 0/10 relevant, and dropped.**
  Reasons are recorded in `fetcher.py`. Don't re-add without measuring.
- **NYDFS and California DFPI are not scraped.** Both block automated access
  including real browsers. Alexander subscribed to their email alerts instead —
  the sanctioned route. Do not add stealth/anti-detection.
- **State regulators are added one at a time, measured first.** Florida OFR
  (Press Releases, 30%) and Texas Dept of Banking (Industry Notices, 33%) are in.
  Texas CU Dept (dormant), OCCC (payday/pawn noise) and TX Savings & Mortgage
  (mortgage-originator admin) were each examined and rejected. Same discipline as
  the SEC/FTC/CFTC federal trials.
- **The Texas cert handling is chain-completion, NOT disabled verification.**
  www.dob.texas.gov serves a real SSL.com cert but omits the intermediate;
  browsers fetch it via the cert's AIA extension, Python doesn't, so a plain
  fetch fails "self-signed certificate in chain". `fetcher._dob_context` bundles
  the intermediate (`certs-ssl-com-intermediate.pem`, expires 2031) and verifies
  against the SSL.com root in certifi — proper verification. Do NOT "fix" a future
  cert error here by disabling verification: the tool republishes what it fetches,
  so an unverified connection means serving content whose integrity isn't checked.
  If the bundled cert expires, refresh it from the AIA URL in fetcher.py.
- **Search matches at word starts; terms ≤2 chars must match a whole word.**
  Two bugs came from being looser: "gg" matched "trigger", "regulation d" matched
  "data"/"disparate". Don't relax this.
- **Relevance is a lens, not a gate.** The Relevant/Everything toggle exists
  because one profile can't serve a public audience. The buttons are labelled
  **Banks, credit unions & fintechs / Fintech only / Credit unions only /
  Everything** with live counts; "Relevant only" was self-referential — relevant
  to whom?
- **Do not split the view into "Community banks" vs "Fintechs".** Measured, not
  assumed: a `bank_specific` judgment was added to the classifier and sampled
  over 25 relevant items for $0.27. **24 of 25 came back true (96%)**, and of the
  4 fintech items every one was *also* bank-specific — "fintech only" was empty.
  The two are not independent axes; fintech is a subset of bank. The field was
  removed again rather than kept unused.
- **Topic pills were removed.** BSA/AML matched 151 of 228 relevant items (66%),
  which is a smaller "All" rather than a filter; Lending and Enforcement were
  plain keyword matches the search box already does. Fintech survived because it
  reads the classifier's judgment.
- **The reg reference table carries no commentary**, and unverified entries (AA,
  JJ, SS, UU, ZZ) were removed rather than published with a caveat.
- **Source health is judged from `fetch_report.json`, not from `store.json`.**
  The obvious approach — per-source `last_seen` in the store — is wrong. A
  record's `sources` list is cumulative and never pruned, so an interagency item
  that FDIC keeps republishing keeps a dead FDIC FILs scraper looking alive
  forever. Fault-injection caught this; it silently defeated the whole check.
- **Quiet thresholds are per-source, derived from each source's own history**
  (2 × its 90th-percentile publication gap, floored at 21 days). Any fixed
  threshold nags or misses. Verified to flag nothing on a healthy day.
- **Only BROKEN fails the run; QUIET does not.** A daily automated check that
  cries wolf is one you learn to ignore.
- **"Phone" is not a width.** A phone in landscape is ~800px wide, so the
  original `max-width:640px` rule handed it the full desktop layout. The query is
  now `(max-width:640px), (hover:none) and (pointer:coarse) and (max-width:1024px)`.
  It lives in two places — the stylesheet and the `MOBILE` matchMedia — and they
  must stay identical. No amount of resizing a desktop browser reproduces the
  bug, because a desktop reports `pointer:fine` and never takes the branch.
- **Month-only dates stay month-only.** FinCEN dates reference material
  "09/2007". Stored as `2007-09`, not `2007-09-01` — the day is unknown and
  inventing one asserts precision the source never gave.
- **`store.json` has two authors, and git cannot merge it.** The Action
  classifies and commits too, so a local copy taken before a bot commit silently
  deletes whatever the bot added. `check_store.py` blocks such a push and names
  the events at risk. It runs from `.githooks/pre-push`, enabled once per clone:

      git config core.hooksPath .githooks

  Deliberate shrinks: `STORE_ALLOW_SHRINK=1 git push`. **Always rebase onto
  `origin/main` before committing `store.json`.** The daily "Commit refreshed
  data" step also commits `sdn_snapshot.json`, `csl_snapshot.json`,
  `fincen311_snapshot.json`, `afdb_snapshot.json` + `_meta`, `audio/`,
  `audio_manifest.json` and `audit_log.jsonl` — expect merge races there too.
- **`--refresh-dates` matches on title**, so it only touches titles appearing
  exactly once in the current fetch, and only records whose date is unparseable.
  Without both limits it re-keys recurring notices onto one key and destroys
  every occurrence but one. Always `--dry-run` this first and read the list.
- **Sanctions sources are pulled direct from each primary source, NOT via the
  OpenSanctions feed.** Their data is CC-BY-NC and Klearance is a commercial BD
  tool; their crawler code + per-source metadata is MIT and fine as documentation
  of where each list lives.
- **A name on several lists returns several rows** — there is no cross-source
  entity resolution. The "On N lists" callout on results is by design, not a bug
  to fix.
- **AfDB / EBRD fingerprint-and-challenge blocks are not "fixed" by disabling
  checks.** The proxy + snapshot fallback and the manual snapshot are the
  sanctioned routes. Do not add CAPTCHA-solving or stealth automation.

---

## Parked, ready to go

**"Ask"** — built, measured, working, switched off at `ASK_ENABLED` in
`dashboard.py`. Three models answer, a fourth reconciles. Unparking is one flag;
the Worker (`worker/`, deployed at `regwatch-ask.alexandersmith14.workers.dev`)
and its keys stay deployed. It also now carries the `/proxy` fetch-proxy route
the AfDB source depends on, so **the Worker must stay deployed whether or not Ask
is on.** Before Ask goes back on a public page: add the KV rate limit
(`worker/README.md`) — one question is four provider calls — and decide whether
model-written prose belongs under a CRCM's name. Full detail near the bottom of
this file and in `worker/README.md`.

**Historical backfill** — ~1,450 rules and proposed rules back to 2018 across six
agencies, ~$16 one-off. Two switches in `fetcher.py`, both off
(`ARCHIVE_ENABLED`, `FEDREG_AGENCIES_PENDING`). Restricted to `RULE` / `PRORULE`
— without that filter it is 6,450 documents and $71. Run locally, 90–120 min.

---

## Working agreements

- **Ask before any API spend.** Use `pipeline.py --dry-run` to price it first.
- **Ask before any Cloudflare spend or Worker redeploy.**
- **Plain language.** Alexander is an audit professional, not a developer.
- **Verify, don't assert.** He has caught real search bugs; check claims against
  the data before stating them.
- **Answer briefly.** Direct answer first.
- **Confirm the GitHub / Cloudflare account and public-vs-private before
  creating anything.** Never run `git config` on his behalf.
- **Keep this file and `CHANGELOG.md` current as work lands** — not just a
  changelog line.

---

## To-do / open items

Nothing here is broken — these are the next things to do.

1. **README.md is stale** — still says "14 government sources" / "RegWatch" in
   places, predates the sanctions screening, audio, and the Klearance rename.
   Same refresh this file just had.
2. **More sanctions sources**, highest value for US BSA/AML, direct-from-source,
   one at a time, measure first:
   - OFAC Civil Penalties & Enforcement Information (~800 names back to 2003, 23
     per-year HTML pages) — label "enforcement history, not a designation".
   - BIS export-violations (`us_bis_export`, `us_bis_mieu`).
   - Allied sanctions — Australia DFAT + Canada Consolidated are confirmed real
     gaps; then Switzerland SECO, Japan MoF/METI. UK FCDO list is broader than
     the OFSI ConList currently pulled — consider switching.
   See `project_klearance_sanctions_sources` in Claude's memory for the full plan
   and the ~193-source target.
3. **Refresh cadence for the manual snapshots** — `make_ebrd_snapshot.py` every
   few months from a residential IP; AfDB self-heals whenever a live fetch gets
   through, and `afdb_issue.py` will nag if it doesn't for 21 days.
4. **Relevance tuning** — does the keep judgment match Alexander's eye? Never
   reviewed against it; costs nothing.
5. **Historical backfill** — ~$16 one-off, see "Parked" above.
6. **Texas DOB phase 2** — its Enforcement Orders and Supervisory Memoranda are
   higher-value than the Industry Notices already in. Enforcement Orders need
   per-sector link following; Supervisory Memoranda carry no listing dates.
7. **More states** — method is proven (probe reachability → find listing pages →
   measure keep-rate → decide). FL and TX are in.
8. **Repo size** — `audio/` grows ~1 MB/day, uncapped. Revisit if it becomes a
   problem (Git LFS, or a shallow-history rewrite).

---

## "Ask" — PARKED, and the Worker it shares with the AfDB proxy

**Not on the page.** `ASK_ENABLED = False` in `dashboard.py`. Flipping that one
flag brings the box back exactly as described below. Nothing was deleted.

Parked deliberately, not because it failed: scoped to the tracked updates it
worked well and measured well. But model-written prose under a CRCM's name is a
liability posture to take on purpose rather than by default. Revisit with models
worth citing.

**The plain keyword search box is a different thing and is unaffected** — no
model, no network, still covers every tracked update.

    browser                          Cloudflare Worker      3 models answer
    searches the tracked       ──▶   adds the keys,    ──▶  in parallel, then a
    updates (BM25, free)             fans out               4th reconciles them

- **It no longer answers from CFR text, deliberately** (`ASK_INCLUDE_REGULATIONS`
  = `False`). A free model (`gpt-oss-20b`) fabricated CFR subsections
  `(iii)`–`(vi)` of 12 CFR 1002.9 from source text that contains only `(i)` and
  `(ii)`, and the then-reconciler restated them as fact. With no CFR text in
  front of the models there are no subsections to invent. The prompts were also
  hardened (forbid citing/subdividing any CFR marker not verbatim in the
  sources; treat a citation only one answer makes as unconfirmed).
- **Retrieval runs in the browser.** GitHub Pages cannot hold an API key, so the
  page searches locally (BM25 over the tracked updates + `corpus.json`) and only
  the model call leaves. Search is free at any traffic level.
- **`corpus.json`** is built by `ecfr_corpus.py` from the free eCFR API (Regs B,
  E, DD). Still published; nothing loads it while the flag is off.
- **The Worker** lives in `worker/` — deployed at
  `regwatch-ask.alexandersmith14.workers.dev`, endpoint hardcoded in
  `dashboard.py` as `ASK_ENDPOINT`. It now also serves the `GET /proxy?url=`
  fetch-proxy route the AfDB source uses, so it must stay deployed regardless of
  Ask. Deploy notes in `worker/README.md`; `npx wrangler` needs
  `wrangler login` (interactive) or `CLOUDFLARE_API_TOKEN`.
- **One question is four provider calls.** Groq, Gemini and OpenRouter each
  answer from the same passages; a fourth model reads only those three answers
  (not the sources) and writes the single answer shown — it can only narrow, not
  invent. Reconciler order (ultra → super → gemma) is measured, not taste;
  gemma is last-resort because it restated invented citations as fact where the
  nemotrons quarantined them.
- **Free-provider search is recorded in `worker.js`; don't repeat it.** Cerebras
  returns 402 on every model (free tier grants no inference). OpenAI has no free
  API tier. DeepSeek has no free tier and is a Chinese-jurisdiction API (a
  firm-policy question first). The familiar OpenRouter `:free` slugs now 404.
- **Add the KV rate limit (`worker/README.md`) before Ask goes public** — two
  bake-off runs alone exhausted Groq's free quota. Quota exhaustion is the
  expected failure and the box says so; the rest of the dashboard is unaffected.
- **The keys are Worker secrets**, never in the repo or page. `GROQ_API_KEY` is
  whitespace-sensitive in the name — a trailing space once made the Worker report
  "backend not configured". Never commit `worker/.wrangler/` (holds the
  Cloudflare account id + a personal-email account name).

---

## Something else reads store.json

A separate, private compliance-research assistant reads this project's
`store.json` (relevant items only, read-only) as its "what changed recently"
corpus, alongside regulation text from the free eCFR API.

- **`store.json`'s shape is a small contract.** It reads `title`,
  `plain_english`, `tags`, `relevant`, `sources`, `date` and `url`. Renaming or
  dropping those fields breaks it. Adding fields is safe.
- **It is deliberately NOT part of Klearance.** Klearance stays the public,
  self-running tracker with no server and no chatbot; a public Q&A bot would add
  hosting, cost and a materially higher liability posture. Keep them separate.
