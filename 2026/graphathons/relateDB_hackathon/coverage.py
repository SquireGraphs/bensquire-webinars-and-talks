"""Coverage benchmark: how many Commander Spellbook combos can the graph trace step by step?

    python coverage.py mtg_live.rl variants_slim.jsonl.gz            # the fixed 300-combo sample
    python coverage.py mtg_live.rl variants_slim.jsonl.gz --show 30  # also list misses

A fixed random sample (seed 5) of combos with 2-4 named cards, no generic pieces, all
Commander-legal. "Traced" means the graph finds a loop or one-shot win using only those cards.
"""
import argparse
import gzip
import json
import random
import re
import time
from collections import Counter

import relate

from engine import find_loops
from wins import find_wins

CATS = [("counters", r"\+1/\+1 counter|-1/-1 counter|proliferate|undying|persist"),
        ("untap", r"\buntap\b"), ("library", r"\bmill\b|from the top of your library|library into your graveyard"),
        ("storm/cast triggers", r"\bstorm\b|whenever you cast"), ("cast from graveyard/exile", r"cast [^.]*from (?:your graveyard|exile)|flashback|escape"),
        ("copies", r"token that's a copy|copy of target|copy target activated|copy target triggered"),
        ("cost reduction", r"costs? \{\d\} less|cost \{1\} less|costs? less"), ("extra turns/combat", r"extra turn|additional combat"),
        ("flicker", r"exile [^.]*return [^.]*to the battlefield")]


def sample(db, variants_path, n=300, seed=5):
    legal = {r["n"]: (r["t"] or "").lower() for r in db.execute(
        "MATCH (c:Card) WHERE 'commander' IN c.legalIn RETURN c.name AS n, c.oracleText AS t")}
    pool = []
    for line in gzip.open(variants_path, "rt"):
        v = json.loads(line)
        names = [u["card"]["name"] for u in v["uses"]]
        if not v["requires"] and 2 <= len(names) <= 4 and all(c in legal for c in names):
            pool.append((sorted(names), [f["feature"]["name"] for f in v["produces"]][:3]))
    random.seed(seed)
    return random.sample(pool, n), legal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("store")
    ap.add_argument("variants")
    ap.add_argument("--show", type=int, default=0)
    ap.add_argument("--category", default=None)
    a = ap.parse_args()
    db = relate.connect(a.store)
    combos, legal = sample(db, a.variants)
    t0, traced, misses, cats = time.time(), 0, [], Counter()
    for cards, feats in combos:
        loops = find_loops(db, cards, budget_s=10) + find_wins(db, cards)
        if any(set(l["cards"]) <= set(cards) for l in loops):
            traced += 1
        else:
            text = " ".join(legal[c] for c in cards)
            hit = [k for k, rx in CATS if re.search(rx, text)] or ["other"]
            cats.update(hit)
            misses.append((cards, feats, hit))
    print(f"traced {traced} of {len(combos)} ({100 * traced / len(combos):.1f}%) in {time.time() - t0:.0f}s")
    print("mechanics in the misses:", ", ".join(f"{k} {n}" for k, n in cats.most_common()))
    shown = [m for m in misses if not a.category or a.category in m[2]][:a.show]
    for cards, feats, hit in shown:
        print(f"  {' + '.join(cards)}  ->  {', '.join(feats)}  [{', '.join(hit)}]")


if __name__ == "__main__":
    main()
