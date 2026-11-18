"""Score graph versions side by side on the sample decks, regression checks and random decks.

    python compare.py variants_slim.jsonl.gz mtg.rl mtg_haiku_union.rl mtg_haiku_replace.rl
"""
import gzip
import json
import random
import subprocess
import sys

import relate

from decks import load_deck
from engine import find_loops
from wins import find_wins
from nonbo import check


def last_line(cmd):
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip().splitlines()
    return out[-1] if out else "?"


def score(store, variants):
    # Check scripts first: Relate lets one process hold the file at a time.
    r = {"loop checks": last_line(["python3", "engine.py", store]),
         "nonbo checks": last_line(["python3", "nonbo.py", store])}
    db = relate.connect(store)
    for path in ["decks/brago_blink.txt", "decks/teysa_aristocrats.txt"]:
        d = load_deck(db, path)
        deck = sorted(set(d["commanders"] + d["cards"]))
        ds = set(deck)
        loops = find_loops(db, deck) + find_wins(db, deck)
        inside = [frozenset(u["card"]["name"] for u in v["uses"]) for v in variants
                  if not v["requires"] and {u["card"]["name"] for u in v["uses"]} <= ds]
        matches = lambda l, s: set(l["cards"]) <= s or set(l["cards"]) >= s
        confirmed = [l for l in loops if any(matches(l, s) for s in inside)]
        found = {s for s in inside if any(matches(l, s) for l in loops)}
        flags = [f for f in check(db, deck, d["commanders"][0]) if f["verdict"] == "nonbo"]
        r[d["commanders"][0]] = {"loops": len(loops), "confirmed": len(confirmed), "spellbook": len(inside),
                                 "found": found, "nonbo flags": len(flags),
                                 "unconfirmed": [l["cards"] for l in loops if l not in confirmed]}
    pool = [x["n"] for x in db.execute(
        "MATCH (c:Card) WHERE 'commander' IN c.legalIn AND NOT 'Land' IN c.types RETURN c.name AS n")]
    random.seed(3)
    r["random"] = sum(bool(find_loops(db, random.sample(pool, 62))) for _ in range(30))
    db.close()
    return r


def main(variants_path, *stores):
    """Score each store once (cached in compare_<store>.json), then print them side by side."""
    import os
    report = {}
    for s in stores:
        cache = f"compare_{os.path.basename(s)}.json"
        if os.path.exists(cache):
            r = json.load(open(cache))
            for cmd in ("Brago, King Eternal", "Teysa Karlov"):
                r[cmd]["found"] = {frozenset(x) for x in r[cmd]["found"]}
        else:
            variants = [json.loads(l) for l in gzip.open(variants_path, "rt")]
            r = score(s, variants)
            dump = json.loads(json.dumps(r, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o)))
            for cmd in ("Brago, King Eternal", "Teysa Karlov"):
                dump[cmd]["found"] = [sorted(x) for x in r[cmd]["found"]]
            json.dump(dump, open(cache, "w"))
        report[s] = r
    base = report[stores[0]]
    for store, r in report.items():
        print(f"\n=== {store}")
        print(f"   regression: {r['loop checks']}; {r['nonbo checks']}; random decks with a loop: {r['random']}/30")
        for cmd in ("Brago, King Eternal", "Teysa Karlov"):
            x = r[cmd]
            print(f"   {cmd}: Spellbook combos found {len(x['found'])}/{x['spellbook']}; loops {x['loops']} "
                  f"({x['confirmed']} confirmed, {x['loops'] - x['confirmed']} unconfirmed); nonbo flags {x['nonbo flags']}")
            if store != stores[0]:
                for s in sorted(x["found"] - base[cmd]["found"], key=len):
                    print("      + newly found:", " + ".join(sorted(s)))
                for s in sorted(base[cmd]["found"] - x["found"], key=len):
                    print("      - lost:", " + ".join(sorted(s)))


if __name__ == "__main__":
    main(*sys.argv[1:])
