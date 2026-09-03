"""Keep one GitHub issue in sync with afdb_stale.json.

sdn_monitor.fetch_afdb() writes afdb_stale.json each run. afdb.org sits behind
an interactive Cloudflare challenge the Worker proxy clears only intermittently;
on a blocked run the committed afdb_snapshot.json is used instead. That is fine
for a while - the debarment list moves slowly - but a snapshot that has not been
refreshed from a live fetch in AFDB_STALE_DAYS means the challenge has been
winning for weeks and someone should look.

Run from the workflow with `gh` authenticated via GH_TOKEN:
  - not stale  -> close the standing issue if open
  - stale      -> create the issue, or edit its body in place

Matched by the `afdb-freshness` label. Never fails the build (exit 0).
Same shape as fincen311_issue.py.
"""
import json
import re
import subprocess
import sys

LABEL = "afdb-freshness"
TITLE = "AfDB debarment snapshot is stale"
STALE_PATH = "afdb_stale.json"
ASSIGNEE = "kaufman2699"


def gh(*args, check=True):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def _issue_number(text):
    m = re.search(r"/issues/(\d+)", text or "")
    return m.group(1) if m else None


def find_issue():
    r = gh("issue", "list", "--label", LABEL, "--state", "open",
           "--json", "number", "--limit", "5", check=False)
    if r.returncode != 0:
        print("gh issue list failed:", r.stderr.strip())
        return None
    items = json.loads(r.stdout or "[]")
    return items[0]["number"] if items else None


def ensure_label():
    gh("label", "create", LABEL, "--color", "FBCA04",
       "--description", "Auto: afdb_snapshot.json has not had a live refresh in weeks",
       check=False)


def build_body(data):
    age = data.get("age_days")
    age_txt = "unknown" if age is None else f"{age} days"
    return "\n".join([
        f"_Checked {data.get('generated', '?')} by the daily pipeline._",
        "",
        f"`afdb_snapshot.json` was last refreshed from a live fetch on "
        f"**{data.get('fetched_at') or 'an unknown date'}** ({age_txt} ago); the "
        f"threshold is {data.get('threshold_days', '?')} days. Screening still "
        f"uses the snapshot, so coverage is intact but ageing.",
        "",
        f"afdb.org ({data.get('source_url', '')}) serves an interactive "
        "Cloudflare challenge that the Worker fetch-proxy clears only some of the "
        "time. Weeks of failures means it is now mostly losing.",
        "",
        "**To fix (any one):**",
        "- Run `sdn_monitor.fetch_afdb()` from a residential IP and commit the "
        "refreshed `afdb_snapshot.json` + `afdb_snapshot_meta.json`.",
        "- Check whether the Worker still reaches afdb.org "
        "(`curl -H \"X-Proxy-Key: ...\" \"$PROXY_WORKER_URL/proxy?url=<afdb url>\"`).",
        "- If afdb.org has hardened for good, move it behind a real scraping "
        "proxy (Zyte/ScrapingBee) or retire the source (cross-debarments are "
        "already covered by World Bank + ADB).",
        "",
        "_Updated by the pipeline each day; closes automatically once a live "
        "fetch succeeds._",
    ])


def main():
    try:
        data = json.load(open(STALE_PATH, encoding="utf-8"))
    except FileNotFoundError:
        print(f"{STALE_PATH} not found - fetch_afdb likely did not run; "
              "leaving any issue untouched.")
        return
    except (OSError, ValueError) as e:
        print(f"could not read {STALE_PATH}: {e}")
        return

    stale = bool(data.get("stale"))
    number = find_issue()

    if not stale:
        if number:
            gh("issue", "close", str(number), "--comment",
               "A live AfDB fetch succeeded; the snapshot is current again. "
               "Closing automatically.", check=False)
            print(f"closed #{number} - snapshot fresh")
        else:
            print("not stale, no open issue - ok")
        return

    ensure_label()
    body = build_body(data)
    with open("_afdb_issue_body.md", "w", encoding="utf-8") as f:
        f.write(body)

    if number:
        r = gh("issue", "edit", str(number), "--body-file", "_afdb_issue_body.md",
               check=False)
        print(f"updated #{number}" if r.returncode == 0
              else f"edit failed: {r.stderr.strip()}")
    else:
        r = gh("issue", "create", "--title", TITLE, "--label", LABEL,
               "--body-file", "_afdb_issue_body.md", check=False)
        print(r.stdout.strip() if r.returncode == 0
              else f"create failed: {r.stderr.strip()}")
        number = _issue_number(r.stdout) if r.returncode == 0 else None

    if number:
        a = gh("issue", "edit", str(number), "--add-assignee", ASSIGNEE, check=False)
        if a.returncode != 0:
            print(f"could not assign {ASSIGNEE}: {a.stderr.strip()}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the pipeline over a notification
        print(f"afdb_issue: {type(e).__name__}: {e}")
    sys.exit(0)
