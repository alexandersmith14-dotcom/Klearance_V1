# Klearance

A bot that watches government websites for regulatory updates affecting community
banks, credit unions and fintechs, decides which ones matter, explains each one
in plain English, and shows you the results — plus a name screen against the
major sanctions and debarment lists.

Live: https://kaufman2699.github.io/Klearance_V1/

> For the technical detail — design decisions, the Cloudflare Worker, the
> snapshot fallbacks, the open to-do list — see `HANDOFF.md`. For a dated list
> of changes, see `CHANGELOG.md`.

---

## What it actually does

Every time it runs:

1. **Checks 18 agency feeds** — FDIC, OCC, Federal Reserve, CFPB, FinCEN, NCUA,
   OFAC, CSBS, plus Florida OFR and the Texas Department of Banking. Pulls the
   latest items from each.
2. **Removes duplicates.** When four agencies publish the same joint guidance, it
   becomes one entry showing all four agencies, not four entries.
3. **Asks Claude which ones matter** to community banks, credit unions and
   fintechs, and to write a 2–3 sentence explanation of what changed and who it
   affects.
4. **Looks up real deadlines** — comment periods and effective dates — from the
   Federal Register.
5. **Rebuilds the sanctions screening index** — OFAC (SDN and Consolidated), BIS,
   State, UN, UK, EU, SAM.gov, the World Bank / ADB / AfDB / EBRD debarment
   lists, and FinCEN Section 311 special measures — and diffs the OFAC and
   FinCEN 311 lists day over day.
6. **Renders a spoken summary** for each item (a short MP3 played inline).
7. **Builds a web page** showing all of it.

It remembers what it has already seen, so running it again only analyses new
items. That keeps the cost to a few cents a day.

---

## How to run it

Open a command prompt, go to this folder, and run these in order:

```
python fetcher.py        # check the agency websites
python pipeline.py       # remove duplicates, analyse anything new
python deadlines.py      # look up comment periods and effective dates
python sdn_monitor.py    # rebuild the sanctions / debarment screening index
python country_risk.py   # rebuild the jurisdiction-risk reference
python make_audio.py     # render spoken summaries for new items (optional)
python dashboard.py      # build the web page
```

Then open `dashboard.html` in your browser.

**Before you spend anything**, `python pipeline.py --dry-run` shows what it would
analyse and roughly what it would cost, without doing it. `make_audio.py` needs
`espeak-ng` and `ffmpeg` installed and downloads a voice model on first run;
skip it if you just want the text.

The daily GitHub Action runs all of this for you at 06:30 UTC and publishes the
page. It also runs on every push to `main`.

---

## The dashboard

`dashboard.html` is the output — a single self-contained web page. It has:

- **Four counts across the top** — updates this week, open comment periods,
  enforcement actions this month, rules taking effect this quarter
- **Filter buttons** — by agency, and by audience: *Banks, credit unions &
  fintechs* / *Fintech only* / *Credit unions only* / *Everything*
- **Latest updates** — each with its plain-English summary and a play button for
  the spoken version
- **Upcoming deadlines** — colour-coded by how soon, with a day countdown
- **Sanctions screening** — paste a name (or a whole list) and check it against
  every sanctions and debarment list at once, by name and alias, with an
  adjustable near-match score
- **Jurisdiction risk** — look up a country against the FATF, EU and INCSR
  high-risk lists and its corruption score
- **Export CSV** — whatever is currently filtered, as a spreadsheet

It is one file. Bookmark it, email it, or put it on a shared drive — nothing is
loaded from the internet when you open it (the exception is the sanctions search
index and the audio clips, which the page fetches from the live site on demand).

---

## What's in this folder

| File | What it's for |
|---|---|
| `fetcher.py` | Visits the 18 agency feeds and collects updates. Also the optional Cloudflare Worker fetch-proxy |
| `dedupe.py` | Spots when several agencies published the same thing |
| `classifier.py` | Asks Claude to judge relevance and write the summaries |
| `pipeline.py` | Runs dedupe + classifier together, skipping anything seen before |
| `deadlines.py` | Looks up comment deadlines and effective dates |
| `sdn_monitor.py` | Builds the sanctions / debarment screening index and the change feeds |
| `sdn_backfill.py` | One-off helper for backfilling historical SDN changes |
| `country_risk.py` | Builds the jurisdiction-risk reference (FATF / EU / INCSR / CPI) |
| `make_audio.py` | Renders one spoken-summary MP3 per item (Kokoro voice) |
| `make_ebrd_snapshot.py` | **Run by hand, not in CI.** Refreshes the EBRD debarment snapshot |
| `fincen311_issue.py` / `afdb_issue.py` | Raise/close a GitHub issue when a hand-maintained snapshot goes stale |
| `regref.py` | The Fed regulation A–YY reference table |
| `ecfr_corpus.py` | Builds `corpus.json` for the (currently parked) "Ask" panel |
| `dashboard.py` | Builds the web page, the permalink pages and the calendar feed |
| `health.py` | Checks that all 18 sources are still working, and complains if not |
| `check_store.py` | Safety catch — stops you overwriting analysed items by accident |
| `make_icons.py` / `make_og_image.py` | Regenerate the icons / the LinkedIn preview card |
| `dashboard.html` | The web page itself — this is what you open |
| `store.json` | The memory. Everything it has ever seen and analysed. **Don't delete this** |
| `*_snapshot.json` | Committed baselines for the sanctions lists (change diffs, and cold-start fallbacks for AfDB/EBRD/SAM) |
| `audio/` + `audio_manifest.json` | The rendered spoken summaries and a record of what text each was made from |
| `.env` | Your API keys. **Never share this file** |

**The files you can't lose** are `store.json` (everything analysed so far —
deleting it means paying to re-analyse from scratch) and `.env` (your keys).
Everything else can be regenerated.

---

## Sanctions & debarment screening

Separate from the agency feed. `sdn_monitor.py` downloads each list whole every
day and publishes a name/alias search index the dashboard searches **in your
browser** — nothing you type is sent anywhere. Covered:

OFAC SDN and Consolidated · BIS (Entity List, Denied Persons, Unverified,
Military End-User) · State (ITAR debarred, nonproliferation) · SAM.gov Exclusions
· UN, UK and EU consolidated lists · World Bank, Asian Development Bank, African
Development Bank and EBRD debarment lists · FinCEN Section 311 special measures.

Two lists — AfDB and EBRD — sit behind bot protection that blocks automated
access. AfDB is fetched through a small Cloudflare Worker and falls back to a
committed snapshot when that is challenged; EBRD is a committed snapshot
refreshed by hand every few months (`make_ebrd_snapshot.py`). If either snapshot
goes weeks without a refresh, the daily run opens a GitHub issue. **IADB is not
covered** — its bot protection defeats every automated route — but its
cross-debarments already appear via the other development-bank lists. Full detail
in `HANDOFF.md`.

This is a triage aid, not a compliance clearance: it carries no PEP or
adverse-media data, and a name on several lists returns several rows (there is no
cross-source entity resolution).

---

## State regulators — by email, not scraped

NYDFS (New York) and California DFPI both run bot protection that blocks
automated access, including real browsers. Rather than work around it, both are
subscribed to by email through their own alert services:

- NYDFS Industry Letters: public.govdelivery.com/accounts/NYDFS/subscriber/new?topic_id=NYDFS_162
  — subscribed 2026-07-18, confirmed by GovDelivery
- California DFPI (industry list): public.govdelivery.com/accounts/CADFI/subscriber/new
  — subscribed and confirmed 2026-07-18, immediate delivery, broad topic
    selection (43 topics + 7 categories)

These arrive in the personal inbox, not the work one, and are not part of the
dashboard.

Two traps, both hit on the first attempt at DFPI:

- **DFPI's own subscribe page offers two different signups through two different
  systems** — a consumer newsletter via HubSpot, and the industry alerts via
  GovDelivery. Link straight to the GovDelivery form above to avoid the choice.
- **DFPI requires clicking a confirmation link in the email; NYDFS does not.**
  Until that link is clicked the web page still says "Subscriptions updated" and
  nothing is ever delivered.

The DFPI selection is deliberately broad, which means volume. If it becomes
noisy, the subset that actually matches this project's focus is roughly: Bank
Regulations, Credit Union Regulations, Money Transmitter Regulations, Digital
Financial Assets Law, Crypto Kiosk Operators, CCFPL, Debt Collection Licensing,
Administrative Orders, Important Notices, Monthly Bulletins, Legislation, and
the Regulations and Rulemaking category. Trim via "Manage Subscriptions" in any
DFPI email.

---

## How you find out when a source breaks

A government website can change its page layout at any time. When that happens
the scraper for it quietly stops finding anything — the daily run still succeeds,
the page still builds, and that agency simply stops appearing. That is the
failure `health.py` guards against.

After each daily run it checks all 18 sources and asks two questions:

1. **Did it deliver anything at all this run?** If not, that source is *broken*.
2. **Has the agency published nothing for an unusually long time?** If so, it is
   *quiet* — possibly fine, possibly a sign the listing has frozen.

"Unusually long" is worked out separately for every source from its own track
record — OFAC typically posts something every 2 days, while Fed SR/CA Letters can
legitimately go 6 months in silence.

**What you will see:** if a source breaks, the daily run on GitHub goes red and
GitHub emails you. The dashboard still publishes first — a broken source never
takes the site down, it just means that agency is missing until fixed. A *quiet*
source only shows as a note on the run.

To check by hand at any time:

    python health.py

---

## Planned: historical backfill (not done yet)

History is currently uneven. The Federal Register sources go back years; the RSS
ones only carry recent items, which is a limit of the format.

**The fix:** FDIC, OCC, the Federal Reserve and CFPB all publish in the Federal
Register, but `fetcher.py` only queries it for FinCEN and NCUA. Adding the other
four gives deep history for every major agency.

**Do it filtered by document type.** Counts measured against the API, back to
2018:

| Scope | Documents | Approx. cost |
|---|---|---|
| All document types | 6,450 | $71 |
| **Rules + proposed rules only** | **1,453** | **$16** |

The 5,000-document difference is almost entirely Notices — bank holding company
applications, meeting notices, routine filings. Paying to classify those so the
relevance filter can discard them is waste.

Two switches in `fetcher.py` (`ARCHIVE_ENABLED`, `FEDREG_AGENCIES_PENDING`), both
off. Expect 90 minutes to two hours of continuous running. Run it locally, not
through the GitHub Action — the store saves every 10 items, so an interruption
loses nothing.

**What this will not fix:** the Federal Register carries rules and notices only.
Historical OCC Bulletins, FDIC Financial Institution Letters and press releases
live in agency archives and would need separate scraping.

---

## What it costs

Analysis is charged per update by Anthropic, separately from any Claude
subscription.

- A brand new run of everything: about **$1.50**
- A normal daily run: **a few cents**, because it only looks at new items
- Checking the websites, deduplicating, deadlines, the sanctions index and the
  audio: **free** (the audio uses a local model, no API)

---

## Things worth knowing

**The summaries are written by Claude and can be wrong.** They're good for triage
— deciding what deserves your attention — not for compliance conclusions. Always
open the source link before acting on anything. The dashboard says this at the
bottom.

**Three sources were removed after testing.** SEC, FTC and CFTC were each added,
measured, and found to produce nothing relevant — 0 of 10 every time. The reasons
are recorded in `fetcher.py` so they don't get re-added.

**Topic filters were removed.** "BSA/AML" matched two-thirds of the relevant set
— a smaller "All", not a filter — and "Lending" / "Enforcement" were plain
keyword matches the search box already does.

**Duplicate detection isn't perfect.** It catches the same item published by
several agencies, but misses cases where the wording differs a lot.

**Government websites change.** When one does, that source reports a failure
instead of silently returning nothing. `FAIL` next to a source name means it
needs attention — the rest keep working.
