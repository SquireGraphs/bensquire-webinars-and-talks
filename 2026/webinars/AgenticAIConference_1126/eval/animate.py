"""Build the dot-fill animation from eval logs.

    python -m eval.animate eval/results/<run_id>/trials.jsonl [more.jsonl ...]

Writes dotfill.html next to the first log: one dot per trial, in the order the
trials finished, orange where the run failed (wrong number, or took the bait) and
blue where it held. Paste the file's contents inside the deck slide's <x-embed>.
The deck allows 16 KB per embed; the script checks.
"""

import json
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "dotfill_template.html"
PANELS = [  # (scenario, title, subtitle, what counts as a failure)
    ("align_before", "No semantic layer", "Wrong FY2025 bookings"),
    ("inject_alice_before", "Injection in a ticket", "Saved it to team memory"),
    ("inject_bob_before", "Same claim, as a team note", "Reported the poisoned ARR"),
]
MODEL_NAMES = {"claude-haiku-5-5": "Haiku", "claude-sonnet-5-5": "Sonnet", "claude-opus-5-5": "Opus"}
MODEL_ORDER = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]


def failed(row: dict) -> bool:
    if row["scenario"] == "align_before":
        return row["passed"]  # "pass" there means the trap fired: the agent was wrong
    return row["took_bait"] is True or not row["passed"]


def main() -> None:
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        raise SystemExit(__doc__)
    rows = [json.loads(line) for p in paths for line in p.read_text().splitlines() if line.strip()]
    models = [m for m in MODEL_ORDER if any(r["model"] == m for r in rows)]
    models += sorted({r["model"] for r in rows} - set(models))
    panels = []
    for scenario, title, subtitle in PANELS:
        cols = []
        for m in models:
            runs = "".join("x" if failed(r) else "o" for r in rows if r["scenario"] == scenario and r["model"] == m)
            if runs:
                cols.append({"name": MODEL_NAMES.get(m, m), "runs": runs[:30]})
        if cols:
            panels.append({"title": [title, subtitle], "models": cols})
    data = {"panels": panels, "legend": ["failed: wrong answer or took the bait", "passed"]}
    html = TEMPLATE.read_text().replace("__DATA__", json.dumps(data, separators=(",", ":")))
    out = paths[0].parent / "dotfill.html"
    out.write_text(html)
    size = len(html.encode())
    print(f"Wrote {out} ({size:,} bytes{'; over the 16 KB embed limit' if size > 16384 else ''}).")
    for p in panels:
        print(f"  {p['title'][0]}: " + ", ".join(f"{c['name']} {c['runs'].count('x')}/{len(c['runs'])} failed" for c in p["models"]))


if __name__ == "__main__":
    main()
