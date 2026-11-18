"""Choose which cards to enrich (LLM extraction) for the demo, based on sample decks.

    python enrich_subset.py mtg.rl variants_slim.jsonl.gz decks/*.txt --neighbors 100

Tiers, each card tagged with why it's in the subset:
  deck        every card in a sample deck (what the app analyses)
  one-away    completes a known Spellbook combo with what's already in the deck,
              within the commander's colours (what the app suggests)
  neighbor    same ability pattern as a deck card (same trigger, same output),
              ranked by popularity (what the app offers as alternatives)
Also reports which deck combos the current regex graph misses: the before/after test set.
"""
import argparse
import csv
import gzip
import json
from collections import defaultdict

import relate

from decks import load_deck
from engine import find_loops


def legal_in_identity(db, identity):
    return {r["n"]: r for r in db.execute("""
        MATCH (c:Card) WHERE 'commander' IN c.legalIn AND NOT 'Land' IN c.types
          AND all(x IN c.colorIdentity WHERE x IN $ci)
        RETURN c.name AS n, c.edhrecRank AS rank""", {"ci": identity})}


def signatures(db, names):
    """Ability patterns per card: (kind, trigger events, produced resources, costs)."""
    rows = db.execute("""
        MATCH (c:Card)-[:HAS_ABILITY]->(a:Ability) WHERE c.name IN $n
        OPTIONAL MATCH (a)-[:TRIGGERED_BY]->(e:Event)
        OPTIONAL MATCH (a)-[:PRODUCES]->(p:Resource)
        OPTIONAL MATCH (a)-[:COSTS]->(k:Resource)
        RETURN c.name AS card, [l IN labels(a) WHERE l <> 'Ability'][0] AS kind,
               collect(DISTINCT e.name) AS ev, collect(DISTINCT p.name) AS prod, collect(DISTINCT k.name) AS cost""",
        {"n": names})
    sig = defaultdict(set)
    for r in rows:
        if r["ev"] or r["prod"]:
            sig[r["card"]].add((r["kind"], tuple(sorted(r["ev"])), tuple(sorted(r["prod"])), tuple(sorted(r["cost"]))))
    return sig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("store")
    ap.add_argument("variants")
    ap.add_argument("decks", nargs="+")
    ap.add_argument("--neighbors", type=int, default=100)
    ap.add_argument("--out", default="enrich_subset.csv")
    args = ap.parse_args()

    db = relate.connect(args.store)
    variants = [json.loads(l) for l in gzip.open(args.variants, "rt")]
    picked = defaultdict(set)               # card -> reasons

    for path in args.decks:
        d = load_deck(db, path)
        name = d["commanders"][0]
        deck = sorted(set(d["commanders"] + d["cards"]))
        ds = set(deck)
        pool = legal_in_identity(db, d["identity"])
        spells = [c for c in deck if c in pool or c in d["commanders"]]
        for c in spells:
            picked[c].add(f"deck:{name}")

        # One card away: Spellbook combos missing exactly one in-colour card.
        away = defaultdict(int)
        inside = []
        for v in variants:
            cards = {u["card"]["name"] for u in v["uses"]}
            missing = cards - ds
            if not missing and not v["requires"]:
                inside.append(cards)
            elif len(missing) == 1 and len(cards) >= 2:
                m = next(iter(missing))
                if m in pool:
                    away[m] += 1
        for m in away:
            picked[m].add(f"one-away:{name}")

        # Functional neighbours: share an ability pattern with a deck card.
        deck_sig = signatures(db, spells)
        wanted = set().union(*deck_sig.values()) if deck_sig else set()
        cand = [c for c in pool if c not in ds]
        cand_sig = signatures(db, cand)
        scored = sorted(((len(s & wanted), -(pool[c]["rank"] or 10**6), c) for c, s in cand_sig.items() if s & wanted),
                        reverse=True)
        for _, _, c in scored[:args.neighbors]:
            picked[c].add(f"neighbor:{name}")

        # Before/after test set: combos inside the deck the current graph misses.
        loops = find_loops(db, deck)
        missed = [s for s in inside if not any(set(l["cards"]) <= s or set(l["cards"]) >= s for l in loops)]
        missed_cards = set().union(*missed) if missed else set()
        print(f"\n{name} ({''.join(d['identity'])}): {len(spells)} deck cards, {len(away)} one-away cards "
              f"(top: {', '.join(sorted(away, key=away.get, reverse=True)[:5])}), "
              f"{min(len(scored), args.neighbors)} neighbours")
        print(f"   Spellbook combos in deck: {len(inside)}; missed by the regex graph: {len(missed)}, "
              f"involving {len(missed_cards)} cards: {', '.join(sorted(missed_cards))}")
        for c in missed_cards:
            picked[c].add(f"missed-combo:{name}")

    tiers = defaultdict(int)
    for reasons in picked.values():
        top = min(reasons, key=lambda r: ["deck", "missed-combo", "one-away", "neighbor"].index(r.split(":")[0]))
        tiers[top.split(":")[0]] += 1
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["card", "reasons"])
        for c in sorted(picked):
            w.writerow([c, "; ".join(sorted(picked[c]))])
    n = len(picked)
    # ~150 input + ~400 output tokens per card; Haiku 5.5 batch $0.05/$0.25, Sonnet 5.5 batch $1/$5 per MTok
    haiku = n * (150 * 0.05 + 400 * 0.25) / 1e6
    sonnet = n * (150 * 1 + 400 * 5) / 1e6
    print(f"\nsubset: {n} unique cards  (by first reason: {dict(tiers)})")
    print(f"estimated extraction cost: Haiku 5.5 batch ~${haiku:.2f}, Sonnet 5.5 batch ~${sonnet:.2f} (before reasoning tokens)")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
