"""Keep one GitHub issue in sync with fincen311_stale.json.

sdn_monitor.fetch_fincen311() writes fincen311_stale.json each run: the FinCEN
Section 311 measures whose hand-mined enrichment in fincen311_details.csv has
fallen behind the FinCEN overview table (a rule newer than mined_date, or a
measure never mined), plus any orphan CSV measures with no table row.

This script, run from the workflow with `gh` authenticated via GH_TOKEN:
  - nothing stale  -> close the standing issue if open
  - something stale -> create the issue, or edit its body in place

The issue is found by the `fincen311-freshness` label so the match is exact.
Never fails the build: any error is logged and swallowed (exit 0).
"""
import json
import re
import subprocess
import sys

LABEL = "fincen311-freshness"
TITLE = "FinCEN 311 measures need re-mining"
STALE_PATH = "fincen311_stale.json"
ASSIGNEE = "kaufman2699"


def gh(*args, check=True):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def _issue_number(text):
    """Issue number from `gh issue create` output (it prints the issue URL)."""
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
    gh("label", "create", LABEL, "--color", "BFD4F2",
       "--description", "Auto: FinCEN 311 details.csv has fallen behind the FinCEN table",
       check=False)  # no-op if it already exists


def build_body(data):
    measures = data.get("measures", [])
    orphans = data.get("orphans", [])
    table = data.get("table_url", "")
    out = [
        f"_Checked {data.get('generated', '?')} by the daily pipeline. "
        f"Source: {table}_",
        "",
        "`fincen311_details.csv` carries facts hand-mined from FinCEN's rulemaking "
        "documents (addresses, former names, named sub-entities). The rows below "
        "have drifted from the current FinCEN table.",
        "",
    ]
    if measures:
        out += ["### Measures to (re-)mine", "",
                "| Measure | Status | Why | Latest doc | Last mined |",
                "|---|---|---|---|---|"]
        for m in measures:
            out.append(
                f"| {m['name']} | {m['status']} | {m['reason']} | "
                f"{m.get('latest_doc_date') or '—'} | {m.get('mined_date') or '—'} |"
            )
        out += ["",
                f"**To fix:** open the [FinCEN 311 table]({table}), follow the "
                "measure's current rule / NPRM, mine the addresses, aliases and "
                "named sub-entities into `fincen311_details.csv`, set the `self` "
                "row's `mined_date` to that document's date, and commit.", ""]
    if orphans:
        out += ["### Orphan rows (in the CSV, not in the table)", "",
                "These `measure` slugs no longer match any FinCEN table row - "
                "FinCEN renamed or removed the measure, so the join key needs to "
                "follow (or the rows should be dropped):", "",
                *[f"- `{o}`" for o in orphans], ""]
    out.append("_This issue is updated by the pipeline each day and closes "
               "automatically when nothing is stale._")
    return "\n".join(out)


def main():
    try:
        data = json.load(open(STALE_PATH, encoding="utf-8"))
    except FileNotFoundError:
        print(f"{STALE_PATH} not found - fetch_fincen311 likely did not run; "
              "leaving any issue untouched.")
        return
    except (OSError, ValueError) as e:
        print(f"could not read {STALE_PATH}: {e}")
        return

    stale = bool(data.get("measures") or data.get("orphans"))
    number = find_issue()

    if not stale:
        if number:
            gh("issue", "close", str(number), "--comment",
               "All FinCEN 311 measures are current with the FinCEN table. "
               "Closing automatically.", check=False)
            print(f"closed #{number} - nothing stale")
        else:
            print("nothing stale, no open issue - ok")
        return

    ensure_label()
    body = build_body(data)
    with open("_fincen311_issue_body.md", "w", encoding="utf-8") as f:
        f.write(body)

    if number:
        r = gh("issue", "edit", str(number), "--body-file",
               "_fincen311_issue_body.md", check=False)
        print(f"updated #{number}" if r.returncode == 0
              else f"edit failed: {r.stderr.strip()}")
    else:
        r = gh("issue", "create", "--title", TITLE, "--label", LABEL,
               "--body-file", "_fincen311_issue_body.md", check=False)
        print(r.stdout.strip() if r.returncode == 0
              else f"create failed: {r.stderr.strip()}")
        number = _issue_number(r.stdout) if r.returncode == 0 else None

    # Assign the repo owner so the issue notifies them regardless of watch
    # settings. Separate from create/edit: a bad assignee (not a collaborator,
    # or an org account) must not sink the whole step.
    if number:
        a = gh("issue", "edit", str(number), "--add-assignee", ASSIGNEE, check=False)
        if a.returncode != 0:
            print(f"could not assign {ASSIGNEE}: {a.stderr.strip()}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the pipeline over a notification
        print(f"fincen311_issue: {type(e).__name__}: {e}")
    sys.exit(0)
