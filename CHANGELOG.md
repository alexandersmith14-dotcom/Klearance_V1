# Changelog

Notable changes to Klearance. Newest first. Dates are UTC.

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
