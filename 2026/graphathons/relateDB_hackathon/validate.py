"""Measure how many Commander Spellbook combos the ability graph finds, and why it misses.

    curl -LO https://json.commanderspellbook.com/variants.json.gz
    python slim_variants.py variants.json.gz variants_slim.jsonl.gz   # 735 MB -> 3 MB, streamed
    python validate.py mtg.rl variants_slim.jsonl.gz --sample 2000

For each sampled variant (a specific set of cards that makes a combo), the cards
are run through engine.find_loops as if they were a deck. A variant counts as
found when the engine reports a loop using at least two of its cards.

Variants that also "require" generic templates (e.g. "a creature with an ETB")
are scored separately: their full card list isn't known, so a miss there is
expected more often.
"""
import argparse
import csv
import gzip
import json
import random
import re
from collections import Counter

import relate

from engine import causal_edges, deck_subgraph, find_loops

# Map Spellbook feature names onto the engine's outcome vocabulary.
OUTCOME_MAP = [
    ("mana", re.compile(r"\bmana\b", re.I)),
    ("damage_players", re.compile(r"damage|life loss|lose the game|win the game|lifeloss", re.I)),
    ("card_draw", re.compile(r"\bdraw", re.I)),
    ("counters", re.compile(r"\+1/\+1 counter", re.I)),
    ("lifegain", re.compile(r"lifegain|gain life", re.I)),
    ("token", re.compile(r"token", re.I)),
    ("etb", re.compile(r"\bETB\b|enters", re.I)),
    ("death", re.compile(r"death|dies|sacrifice", re.I)),
]
# Words in the combo cards' rules text that hint at a mechanic the model lacks.
GAP_HINTS = {
    "untap": r"\buntap\b", "copy": r"\bcop(?:y|ies)\b", "cast/storm": r"\bcast\b|\bstorm\b",
    "bounce/return to hand": r"return [^.]* to (?:its owner's|your) hand",
    "tap ability": r"\{t\}:", "+1/+1 counters": r"\+1/\+1 counter", "-1/-1 counters": r"-1/-1 counter",
    "proliferate": r"proliferate", "graveyard": r"graveyard", "cost reduction": r"costs? \{?\d?\}? ?less",
    "extra turn/phase": r"extra (?:turn|combat)|additional combat", "life total": r"life total",
}


def load_variants(path):
    """Slim JSONL from slim_variants.py (one variant per line), or a small full export."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        if ".jsonl" in str(path):
            return [json.loads(line) for line in f]
        return json.load(f)["variants"]


def variant_cards(v):
    return [u["card"]["name"] for u in v.get("uses", [])]


def features(v):
    return [p["feature"]["name"] for p in v.get("produces", [])]


def expected_outcomes(v):
    names = " | ".join(features(v))
    return {tok for tok, rx in OUTCOME_MAP if rx.search(names)}


def connected(g):
    """Do the variant's cards interact at all (any cause-effect edge between two cards)?"""
    A = g["abilities"]
    return any(A[a]["card"] != A[b]["card"] for a, bs in causal_edges(g).items() for b in bs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("store")
    ap.add_argument("variants")
    ap.add_argument("--sample", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--misses", default="misses.csv")
    args = ap.parse_args()

    db = relate.connect(args.store)
    known = {r["n"] for r in db.execute("MATCH (c:Card) RETURN c.name AS n")}
    variants = load_variants(args.variants)
    usable = [v for v in variants
              if (v.get("legalities") or {}).get("commander")
              and len(variant_cards(v)) >= 2
              and all(n in known for n in variant_cards(v))]
    print(f"variants: {len(variants)}, commander-legal with >=2 known cards: {len(usable)}")
    random.Random(args.seed).shuffle(usable)
    sample = usable[:args.sample]

    stats = Counter()
    gap_hits, gap_miss = Counter(), Counter()
    feat_hit, feat_miss = Counter(), Counter()
    outcome_agree = Counter()
    miss_rows = []
    for v in sample:
        cards = variant_cards(v)
        group = "template" if v.get("requires") else "specific"
        loops = [l for l in find_loops(db, cards) if len(set(l["cards"]) & set(cards)) >= 2]
        g = deck_subgraph(db, cards)
        found = bool(loops)
        stats[(group, "found" if found else ("connected" if connected(g) else "unconnected"))] += 1
        text = " ".join((a["text"] or "") for a in g["abilities"].values())
        hints = {h for h, rx in GAP_HINTS.items() if re.search(rx, text)}
        (gap_hits if found else gap_miss).update(hints)
        (feat_hit if found else feat_miss).update(features(v))
        if found:
            exp = expected_outcomes(v)
            got = set().union(*(set(l["outcome"]) for l in loops))
            got |= {"etb" for o in got if "enters" in o} | {"death" for o in got if "dies" in o}
            outcome_agree["checked"] += bool(exp)
            outcome_agree["agree"] += bool(exp & got)
        else:
            miss_rows.append({"id": v["id"], "group": group, "cards": " + ".join(cards),
                              "produces": " | ".join(features(v)), "hints": ", ".join(sorted(hints)),
                              "popularity": v.get("popularity")})

    print(f"\nsample: {len(sample)} variants")
    for group in ("specific", "template"):
        tot = sum(n for (g, _), n in stats.items() if g == group)
        if not tot:
            continue
        f, c, u = (stats[(group, k)] for k in ("found", "connected", "unconnected"))
        print(f"  {group:<9} n={tot:<5} found {f/tot:6.1%}   interacting but no loop {c/tot:6.1%}   no interaction {u/tot:6.1%}")
    if outcome_agree["checked"]:
        print(f"\noutcome agreement on found combos: {outcome_agree['agree']}/{outcome_agree['checked']}"
              f" ({outcome_agree['agree']/outcome_agree['checked']:.1%})")

    n_miss = len(miss_rows)
    n_hit = len(sample) - n_miss
    print("\nmechanics in the cards' text (share of misses vs share of finds):")
    for h in sorted(GAP_HINTS, key=lambda h: -gap_miss[h]):
        print(f"  {h:<22} misses {gap_miss[h]/max(n_miss,1):6.1%}   finds {gap_hits[h]/max(n_hit,1):6.1%}")
    print("\nmost common Spellbook results among misses:")
    for name, n in feat_miss.most_common(12):
        print(f"  {n:>5}  {name}")
    with open(args.misses, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(miss_rows[0].keys()) if miss_rows else ["id"])
        w.writeheader()
        w.writerows(miss_rows)
    print(f"\nwrote {n_miss} misses to {args.misses}")


if __name__ == "__main__":
    main()
