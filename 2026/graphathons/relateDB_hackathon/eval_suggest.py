"""Leave-one-out test for next-best-card: remove one piece of a combo the deck already has,
then see where each ranking puts the missing card.

    python eval_suggest.py mtg_live.rl variants_slim.jsonl.gz decks/*.txt
"""
import gzip
import json
import random
import statistics
import sys

import relate

from decks import load_deck, load_deck_text
from engine import find_loops
from suggest import suggest
from wins import find_wins


def ranks(rows, card, key):
    order = sorted(rows, key=lambda r: (-key(r), r["card"]))
    return next((i + 1 for i, r in enumerate(order) if r["card"] == card), None)


def main(store, variants_path, *paths, per_deck=12):
    db = relate.connect(store)
    variants = [json.loads(l) for l in gzip.open(variants_path, "rt")]
    results = {"Spellbook count only": [], "Spellbook, weighted": [], "graph only": [], "combined": []}
    for path in paths:
        text = open(path, encoding="utf-8").read()
        d = load_deck(db, path) if "Commander" in text else load_deck_text(db, text)
        deck = sorted(set(d["commanders"] + d["cards"]))
        ds = set(deck)
        found = find_loops(db, deck) + find_wins(db, deck)
        inside = [frozenset(u["card"]["name"] for u in v["uses"]) for v in variants
                  if not v["requires"] and {u["card"]["name"] for u in v["uses"]} <= ds]
        hit = [s for s in inside if any(set(l["cards"]) <= s or set(l["cards"]) >= s for l in found)]
        pieces = sorted({c for s in hit for c in s} - set(d["commanders"]))
        random.Random(7).shuffle(pieces)
        for p in pieces[:per_deck]:
            d2 = {**d, "cards": [c for c in d["cards"] if c != p]}
            rows, _ = suggest(db, d2, variants)
            grows, _ = suggest(db, d2, variants, graph_only=True, top_graph=40)
            results["Spellbook count only"].append(ranks(rows, p, lambda r: r["known"] + 0.01 * r["links"]))
            results["Spellbook, weighted"].append(ranks(rows, p, lambda r: r["knownScore"] + 0.01 * r["links"]))
            results["graph only"].append(ranks(grows, p, lambda r: r["graphScore"]))
            results["combined"].append(ranks(rows, p, lambda r: r["score"]))
            print(f"  {d['commanders'][0][:12]} minus {p}: known-only #{results['Spellbook count only'][-1]}, "
                  f"graph-only #{results['graph only'][-1]}, combined #{results['combined'][-1]}", flush=True)
    print(f"\n{len(results['combined'])} trials")
    for name, rs in results.items():
        got = [r for r in rs if r]
        at = lambda k: sum(1 for r in got if r <= k) / len(rs)
        print(f"  {name:22} top-1 {at(1):.0%}  top-5 {at(5):.0%}  top-10 {at(10):.0%}  median rank {statistics.median(got) if got else '-'}")


if __name__ == "__main__":
    main(*sys.argv[1:])
