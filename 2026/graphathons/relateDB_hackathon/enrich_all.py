"""Prepare LLM enrichment for the whole Commander card pool, skipping cards already done.

    python enrich_all.py mtg_live.rl --have extracted_sonnet.jsonl extracted_sonnet_lotho.jsonl
    python extract.py submit  --model haiku --requests extract_requests_all.jsonl --tag all
    python extract.py collect --model haiku --requests extract_requests_all.jsonl --tag all --wait
    bash build_all.sh <cards.json> <variants.json.gz> extracted_sonnet.jsonl,extracted_sonnet_lotho.jsonl,extracted_haiku_all.jsonl

Haiku is the choice here: with the rules and cleanup applied, it finds as many known combos as Sonnet.
Lands are included only when they have abilities beyond producing mana.
"""
import argparse
import json
import re

import relate

from enrich_deck import enriched_ids
from extract import card_prompt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("store")
    ap.add_argument("--have", nargs="*", default=[])
    ap.add_argument("--out", default="extract_requests_all.jsonl")
    a = ap.parse_args()
    db = relate.connect(a.store)
    done = enriched_ids(a.have)
    rows = list(db.execute("""MATCH (c:Card) WHERE 'commander' IN c.legalIn
        RETURN c.oracleId AS id, c.name AS name, c.typeLine AS typeLine, c.manaCost AS manaCost,
               c.oracleText AS oracleText, c.keywords AS keywords, c.types AS types"""))
    mana_only = re.compile(r"^(?:\({t}: add \{[wubrgc]\}\.\)|\{t}: add [^.]*\.|[^\n]*enters tapped\.)$", re.I)
    todo = [r for r in rows if r["id"] not in done and r["oracleText"]
            and not ("Land" in (r["types"] or []) and all(mana_only.match(l.strip()) for l in r["oracleText"].split("\n") if l.strip()))]
    with open(a.out, "w", encoding="utf-8") as f:
        for r in todo:
            f.write(json.dumps({"custom_id": r["id"], "card": r["name"], "prompt": card_prompt(r)}) + "\n")
    words = sum(len((r["oracleText"] or "").split()) for r in todo)
    print(f"Commander-legal cards: {len(rows):,}; already enriched: {len(rows) - len(todo) - sum(1 for r in rows if not r['oracleText']):,}; "
          f"to enrich: {len(todo):,} (~{words * 1.4 / 1e6:.1f}M card-text tokens)")
    print(f"estimated batch cost with Haiku 5.5: ~${len(todo) * (300 * 0.05 + 280 * 0.25) / 1e6:.2f} "
          f"(uncached ~300 input + ~280 output tokens per card, measured on the first 586)")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
