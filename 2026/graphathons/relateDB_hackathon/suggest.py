"""Next best card: rank cards to add to a deck, with reasons.

Signals per candidate (all within the commander's colours, Commander-legal, not already in the deck):
  known     Commander Spellbook combos it completes with cards already in the deck
  new       loops the graph finds that need this card (combos no database lists)
  links     cause-and-effect links to the deck (it makes what the deck uses, or triggers on what it does)
  nonbo     rules conflicts with the deck (penalty)
score = known score + 4*new + 0.5*links - 6*nonbo   (graph-only score: same without the known score)
known score = 3 * (1 + log2(combos completed)), + 4 if one of them is a two-card combo: the 30th
variant of the same loop adds little, and a card that combos with one card you run is the best kind.
"""
import math
import time
from collections import defaultdict

from enrich_subset import legal_in_identity, signatures
from engine import find_loops_through
from nonbo import check

_POOL_CACHE = {}


def deck_profile(db, names):
    """What the deck's abilities fire, trigger on, make and spend."""
    prof = defaultdict(set)
    for r in db.execute("""MATCH (c:Card)-[:HAS_ABILITY|GRANTS]->(a:Ability)-[x]->(t)
                           WHERE c.name IN $n AND type(x) IN ['RETRIGGERS','TRIGGERED_BY','PRODUCES','COSTS']
                           RETURN type(x) AS rel, t.name AS t""", {"n": list(names)}):
        prof[r["rel"]].add(r["t"])
    return prof


def links(prof_card, prof_deck):
    common = lambda a, b: len((prof_card[a] & prof_deck[b]) - {"mana"}) + 0.3 * ("mana" in prof_card[a] & prof_deck[b])
    return (common("TRIGGERED_BY", "RETRIGGERS") + common("RETRIGGERS", "TRIGGERED_BY")
            + common("PRODUCES", "COSTS") + common("COSTS", "PRODUCES"))


def suggest(db, d, variants, top_graph=20, neighbours=120, with_graph=True, graph_only=False):
    t0 = time.time()
    deck = sorted(set(d["commanders"] + d["cards"]))
    ds = set(deck)
    key = tuple(sorted(d["identity"]))
    if key not in _POOL_CACHE:
        pool = legal_in_identity(db, d["identity"])
        _POOL_CACHE[key] = (pool, signatures(db, list(pool)))
    pool, sigs = _POOL_CACHE[key]
    # known combos one card away
    known, examples, two_card = defaultdict(int), defaultdict(list), set()
    for v in variants:
        if v["requires"]:
            continue
        cards = {u["card"]["name"] for u in v["uses"]}
        missing = cards - ds
        if len(missing) == 1 and len(cards) >= 2:
            m = next(iter(missing))
            if m in pool:
                known[m] += 1
                if len(cards) == 2:
                    two_card.add(m)
                if len(examples[m]) < 3:
                    examples[m].append(sorted(cards & ds))
    # functional neighbours: share ability patterns with the deck
    want = set().union(*[sigs.get(c, set()) for c in deck if c in sigs]) if sigs else set()
    neigh = sorted((c for c in pool if c not in ds and sigs.get(c, set()) & want),
                   key=lambda c: (-len(sigs[c] & want), pool[c]["rank"] or 10**6))[:neighbours]
    cands = sorted(set(known) | set(neigh))
    prof_deck = deck_profile(db, deck)
    rows = []
    for c in cands:
        rows.append({"card": c, "known": known.get(c, 0), "twoCard": c in two_card, "examples": examples.get(c, []),
                     "links": round(links(deck_profile(db, [c]), prof_deck), 1), "new": 0, "newLoops": [], "nonbo": 0, "warnings": []})
    # graph_only: pick which cards get the (slower) loop search without using Spellbook at all
    for r in rows:
        r["knownScore"] = round(3 * (1 + math.log2(r["known"])) + (4 if r["twoCard"] else 0), 1) if r["known"] else 0
    pre = sorted(rows, key=lambda r: -(0.5 * r["links"] if graph_only else r["knownScore"] + 0.5 * r["links"]))
    # The loop search runs on the top candidates, plus every two-card combo piece (a card that combos
    # with one card you run deserves a look even if it has few other links).
    chosen = pre[:top_graph] + [r for r in pre[top_graph:] if r.get("twoCard") and not graph_only]
    for r in chosen if with_graph else []:
        loops = find_loops_through(db, deck, r["card"], limit=5, balance=False)
        r["new"] = len(loops)
        r["newLoops"] = [l["cards"] for l in loops[:3]]
        fs = [f for f in check(db, deck + [r["card"]], d["commanders"][0]) if f["verdict"] == "nonbo" and r["card"] in f["cards"]]
        r["nonbo"], r["warnings"] = len(fs), [f["why"] for f in fs[:2]]
    for r in rows:
        r["graphScore"] = round(4 * min(r["new"], 5) + 0.5 * r["links"] - 6 * r["nonbo"], 1)
        r["score"] = round(r["knownScore"] + r["graphScore"], 1)
    rows.sort(key=lambda r: (-r["score"], r["card"]))
    return rows, round(time.time() - t0, 1)
