"""Prepare LLM enrichment for one deck and its relevant neighbours, skipping cards already done.

    python enrich_deck.py mtg_live.rl variants_slim.jsonl.gz decks/lotho.txt \
        --have extracted_sonnet.jsonl --name lotho
    python extract.py submit  --model sonnet --requests extract_requests_lotho.jsonl --tag lotho
    python extract.py collect --model sonnet --requests extract_requests_lotho.jsonl --tag lotho --wait
    # then rebuild with every saved enrichment:
    bash build_all.sh <cards.json> <variants.json.gz> extracted_sonnet.jsonl,extracted_sonnet_lotho.jsonl

Neighbours, each with a reason:
  deck        the deck's own nonland cards and commander
  one-away    completes a known Commander Spellbook combo with cards already in the deck,
              within the commander's colours (what the app suggests)
  neighbor    shares an ability pattern with a deck card (same trigger, same output),
              ranked by popularity (alternatives)
"""
import argparse
import csv
import gzip
import json
from collections import defaultdict

import relate

from decks import load_deck_text
from enrich_subset import legal_in_identity, signatures
from extract import card_prompt


def enriched_ids(paths):
    done = set()
    for p in paths:
        for line in open(p, encoding="utf-8"):
            row = json.loads(line)
            if row.get("abilities"):
                done.add(row["oracleId"])
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("store")
    ap.add_argument("variants")
    ap.add_argument("deck")
    ap.add_argument("--commander", default=None)
    ap.add_argument("--have", nargs="*", default=[], help="extraction files already done")
    ap.add_argument("--neighbors", type=int, default=100)
    ap.add_argument("--name", default="deck")
    a = ap.parse_args()

    db = relate.connect(a.store)
    d = load_deck_text(db, open(a.deck, encoding="utf-8").read(), a.commander)
    if not d["commanders"]:
        raise SystemExit("No commander found: add a Commander line or pass --commander.")
    for p in d["problems"]:
        print("  problem:", p)
    cmd = " + ".join(d["commanders"])
    deck = sorted(set(d["commanders"] + d["cards"]))
    ds = set(deck)
    pool = legal_in_identity(db, d["identity"])
    reasons = defaultdict(set)

    spells = [c for c in deck if c in pool or c in d["commanders"]]
    for c in spells:
        reasons[c].add("deck")

    away = defaultdict(int)
    for line in gzip.open(a.variants, "rt"):
        v = json.loads(line)
        cards = {u["card"]["name"] for u in v["uses"]}
        missing = cards - ds
        if len(missing) == 1 and len(cards) >= 2 and not v["requires"]:
            m = next(iter(missing))
            if m in pool:
                away[m] += 1
    for m in away:
        reasons[m].add("one-away")

    deck_sig = signatures(db, spells)
    wanted = set().union(*deck_sig.values()) if deck_sig else set()
    cand_sig = signatures(db, [c for c in pool if c not in ds])
    scored = sorted(((len(s & wanted), -(pool[c]["rank"] or 10**6), c) for c, s in cand_sig.items() if s & wanted), reverse=True)
    for _, _, c in scored[:a.neighbors]:
        reasons[c].add("neighbor")

    info = {r["name"]: r for r in db.execute("""MATCH (c:Card) WHERE c.name IN $n
        RETURN c.oracleId AS id, c.name AS name, c.typeLine AS typeLine, c.manaCost AS manaCost,
               c.oracleText AS oracleText, c.keywords AS keywords""", {"n": list(reasons)})}
    done = enriched_ids(a.have)
    todo = [info[c] for c in sorted(reasons) if c in info and info[c]["id"] not in done]

    with open(f"enrich_{a.name}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["card", "reasons", "already enriched"])
        for c in sorted(reasons):
            w.writerow([c, "; ".join(sorted(reasons[c])), c in info and info[c]["id"] in done])
    req = f"extract_requests_{a.name}.jsonl"
    with open(req, "w", encoding="utf-8") as f:
        for c in todo:
            f.write(json.dumps({"custom_id": c["id"], "card": c["name"], "prompt": card_prompt(c)}) + "\n")

    tiers = defaultdict(int)
    for c, rs in reasons.items():
        tiers[min(rs, key=["deck", "one-away", "neighbor"].index)] += 1
    print(f"{cmd} ({''.join(d['identity'])}): {len(reasons)} relevant cards {dict(tiers)}")
    print(f"already enriched: {len(reasons) - len(todo)}; to enrich now: {len(todo)}")
    n = len(todo)
    print(f"estimated cost: Haiku 5.5 batch ~${n * (150 * 0.05 + 400 * 0.25) / 1e6:.2f}, "
          f"Sonnet 5.5 batch ~${n * (150 * 1 + 400 * 5) / 1e6:.2f} (before reasoning tokens)")
    print(f"wrote enrich_{a.name}.csv and {req}")


if __name__ == "__main__":
    main()
