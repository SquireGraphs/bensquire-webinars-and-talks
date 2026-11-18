"""Export the real analysis of the sample decks as JSON for the demo app.

    python demo_data.py mtg_sonnet_union.rl variants_slim.jsonl.gz demo_data.json
"""
import gzip
import json
import re
import sys
from collections import defaultdict

import relate

from decks import load_deck, load_deck_text

SAMPLE_DECKS = ("decks/brago_blink.txt", "decks/teysa_aristocrats.txt", "decks/lotho.txt",
                "decks/vivi.txt", "decks/flubs.txt", "decks/nazgul.txt")
from engine import deck_subgraph, causal_edges, find_loops
from wins import find_wins
from suggest import suggest
from synergies import deck_synergies
from nonbo import check

READABLE = {"mana": "mana", "card_draw": "card draw", "damage_players": "damage to opponents",
            "lifegain": "life gain", "life_loss_opponents": "life loss for opponents", "counters": "+1/+1 counters",
            "creature": "creature bodies", "token": "tokens", "artifact": "artifacts (Treasure)", "removal": "removal",
            "landfall": "landfall", "untap": "untaps", "graveyard": "graveyard filling"}
INTERACTION = re.compile(r"counter target|destroy|exile target|exile all|return target nonland|draw|scry|search your library|"
                         r"phase out|can't be countered|look at the top|each player", re.I)


def pretty(text, card):
    t = (text or "").replace("~", card.split(" // ")[0])
    return t[:1].upper() + t[1:]


def outcomes(loop):
    events = [o for o in loop["outcome"] if o.startswith("infinite ")]
    words = set()
    for e in events:
        for ev in e[len("infinite "):].split("/"):
            words.add({"self_enters": "enters", "creature_enters": "enters", "self_leaves": "leaves",
                       "creature_leaves": "leaves", "self_dies": "dies", "creature_dies": "dies",
                       "enters_from_graveyard": "enters", "gain_life": "life gain", "opponent_loses_life": "life loss",
                       "draw": "draw", "token_created": "token"}.get(ev, ev.replace("_", " ")))
    res = [READABLE[o] for o in loop["outcome"] if o in READABLE]
    trig = ("infinite " + ", ".join(sorted(words)) + " triggers") if words else None
    return [x for x in ([trig] if trig else []) + res]


def helper_abilities(db, names):
    """What each helper card contributes: its abilities that change other abilities (Teysa Karlov doubling death
    triggers) first, then its other abilities; at most two per card."""
    out = []
    for n in names:
        # abilities it gives other cards (Mikaeus granting undying) and ones that change other abilities (Teysa)
        # are usually how a helper helps, so they come first
        rows = list(db.execute("""MATCH (c:Card {name: $n})-[h:HAS_ABILITY|GRANTS]->(a:Ability)
                                  OPTIONAL MATCH (a)-[r:AMPLIFIES|MODIFIES]->()
                                  RETURN a.text AS t, count(r) AS amp, type(h) = 'GRANTS' AS granted""", {"n": n}))
        seen, texts = set(), []
        for r in sorted(rows, key=lambda r: (-r["amp"], not r["granted"])):
            t = (r["t"] or "").replace("~", n.split(" // ")[0]).strip()
            if r["granted"] and len(t) < 25:                   # a bare keyword ("undying"): say who gets it
                t = f"Gives your other creatures {t}"
            key = t.lower()[:60]
            if t and key not in seen:
                seen.add(key); texts.append(t[:240])
        out.append({"card": n, "abilities": texts[:2]})
    return out


_TRACES = None


def _traces():
    global _TRACES
    if _TRACES is None:
        try:
            _TRACES = json.load(open("combo_traces.json", encoding="utf-8"))
        except (OSError, ValueError):
            _TRACES = {}
    return _TRACES


def deck_report(db, d, variants):
    """d = a loaded deck (decks.load_deck or decks.load_deck_text)."""
    cmd = d["commanders"][0]
    deck = sorted(set(d["commanders"] + d["cards"]))
    ds = set(deck)
    cards = {r["name"]: r for r in db.execute("""MATCH (c:Card) WHERE c.name IN $n
        RETURN c.name AS name, c.typeLine AS typeLine, c.manaCost AS manaCost, c.oracleText AS text,
               c.colorIdentity AS ci, c.types AS types, c.imageUrl AS image""", {"n": deck})}
    nonland = [c for c in deck if c in cards and "Land" not in (cards[c]["types"] or [])]

    # Spellbook combos fully inside the deck, and the loops the graph finds
    inside, spellbook_ids = [], {}
    for v in variants:
        names = frozenset(u["card"]["name"] for u in v["uses"])
        if not v["requires"] and names <= ds:
            inside.append((names, [f["feature"]["name"] for f in v["produces"]]))
            spellbook_ids.setdefault(names, v.get("id"))
    search = {}
    from engine import saved_models
    taught = [set(m["loop"]["cards"]) for m in saved_models() if m.get("loop")]
    by_you = lambda l: any(set(l["cards"]) <= s or set(l["cards"]) >= s for s in taught)
    loops = find_loops(db, deck, info=search) + find_wins(db, deck)
    matches = lambda l, s: set(l["cards"]) <= s or set(l["cards"]) >= s
    # Known combos no loop explains: try engines that repeat once each turn (Brago + Archaeomancer + Cyclonic Rift)
    from engine import find_turn_engines
    untraced = [s for s, _ in inside if not any(matches(l, s) for l in loops)]
    loops += find_turn_engines(db, untraced)
    combos = []
    for l in loops:
        sb = next((feats for s, feats in inside if matches(l, s)), None)
        everything = outcomes(l) if l.get("kind") not in ("win", "turn") else [l.get("label") or "repeats each turn"]
        triggers = [o for o in everything if o.endswith(" triggers")]
        primary = triggers + (["infinite mana"] if l["net"].get("mana", 0) > 0 else []) + ([l["label"]] if l.get("kind") == "win" else [])
        primary += ["infinite " + READABLE[k] for k in ("damage_players", "life_loss_opponents", "lifegain", "card_draw")
                    if k in l["net"]]
        primary = list(dict.fromkeys(primary))
        mine = sb is None and by_you(l)                    # a loop you taught it, by drawing the combo
        combos.append({"cards": l["cards"], "kind": l.get("kind", "loop"), "confirmed": sb is not None or mine, "spellbook": sb or [],
                       **({"confirmedBy": "your model"} if mine else {}),
                       "net": {k: v for k, v in l["net"].items() if v}, "outcomes": primary,
                       "alsoEnables": [o for o in everything if o not in primary and "infinite " + o not in primary
                                       and o not in ("creature bodies", "untaps", "tokens", "graveyard filling")],
                       "helpers": l.get("helpers", []),
                       "helperAbilities": helper_abilities(db, l.get("helpers", [])),
                       **({"payload": l["payload"]} if l.get("payload") else {}),
                       "path": [{"card": p["card"], "kind": p["kind"], "text": pretty(p["text"], p["card"]),
                                 **({"states": p["states"]} if p.get("states") else {})} for p in l["path"]]})
    missed = [{"cards": sorted(s), "spellbook": feats, "spellbookId": spellbook_ids.get(s)} for s, feats in inside
              if not any(matches(l, s) for l in loops)]
    # Known combos the graph can't trace yet are still combos: list them, marked as such.
    combos += [{"cards": m["cards"], "kind": "known", "confirmed": True, "spellbook": m["spellbook"], "spellbookId": m.get("spellbookId"), "net": {},
                "outcomes": [f.lower() for f in m["spellbook"][:3]], "alsoEnables": [], "helpers": [], "path": []}
               for m in missed]
    for c in combos:                                      # every confirmed combo links to its Spellbook entry
        if c.get("confirmed") and not c.get("spellbookId"):
            key = frozenset(c["cards"])
            c["spellbookId"] = spellbook_ids.get(key) or next((i for s, i in spellbook_ids.items() if s <= key or s >= key), None)
    # State traces for known combos (trace_combos.py): starting state, steps and lap-by-lap zones and counts
    traces = _traces()
    for c in combos:
        t = traces.get(c.get("spellbookId"))
        if t:
            c["trace"] = {k: t[k] for k in ("kind", "verdict", "steps", "start", "zones", "table")}
    order = {"loop": 0, "win": 0, "known": 1}
    combos.sort(key=lambda c: (not c["confirmed"], order.get(c.get("kind", "loop"), 0), len(c["cards"]), c["cards"]))

    # Findings
    # "Exile wording" is rules trivia (the Fiend Hunter trick), not a conflict: keep it off the Nonbos tab.
    findings = [f for f in check(db, deck, cmd) if f["verdict"] in ("nonbo", "note") and f["rule"] != "exile wording"]
    grouped = defaultdict(list)
    for f in findings:
        if f["rule"] == "an effect stops the event":
            grouped[f["cards"][0] if "Containment Priest" not in f["cards"] else "Containment Priest"].append(f)
    nonbos = []
    for src, fs in grouped.items():
        affected = sorted({c for f in fs for c in f["cards"]} - {src})
        # Prefer the rule's own wording (lower-case verb phrase) over the LLM's free-text reason.
        reasons = [r["r"] for r in db.execute("""MATCH (c:Card {name: $n})-[:HAS_ABILITY]->(:Ability)-[p:PREVENTS]->()
                                                 RETURN p.reason AS r""", {"n": src}) if r["r"]]
        # The rule's own wording starts with a verb ("exiles ...", "stops ..."); the LLM's is free text.
        reason = next((r for r in reasons if re.match(r"^[a-z]+s ", r)), reasons[0] if reasons else "stops this from happening")
        nonbos.append({"verdict": "nonbo", "source": src, "affected": affected, "rule": fs[0]["rule"],
                       "why": f"{src} {reason.rstrip('.')}."})
    nonbos += [{"verdict": f["verdict"], "source": f["cards"][0], "affected": f["cards"][1:], "rule": f["rule"],
                "why": f["why"]} for f in findings if f["rule"] != "an effect stops the event"]

    # Links between cards: hubs and unlinked cards
    g = deck_subgraph(db, nonland)
    E = causal_edges(g)
    A = g["abilities"]
    links = defaultdict(set)
    for a, bs in E.items():
        for b in bs:
            if A[a]["card"] != A[b]["card"]:
                links[A[a]["card"]].add(A[b]["card"])
                links[A[b]["card"]].add(A[a]["card"])
    in_combo = {c for x in combos if x["confirmed"] for c in x["cards"]}
    flagged = {x["source"] for x in nonbos} | {c for x in nonbos if x["verdict"] == "nonbo" for c in x["affected"]}
    missed_cards = {c for m in missed for c in m["cards"]}
    status = {}
    for c in nonland:
        if c in in_combo:
            status[c] = "combo"
        elif c in flagged:
            status[c] = "flagged"
        elif not links[c]:
            status[c] = "gap" if c in missed_cards else ("support" if INTERACTION.search(cards[c]["text"] or "") else "unlinked")
        else:
            status[c] = "linked"
    hubs = sorted(nonland, key=lambda c: -len(links[c]))[:6]

    # One card away
    pool = {r["n"] for r in db.execute("""MATCH (c:Card) WHERE 'commander' IN c.legalIn
        AND all(x IN c.colorIdentity WHERE x IN $ci) RETURN c.name AS n""", {"ci": d["identity"]})}
    away = defaultdict(list)
    for v in variants:
        names = {u["card"]["name"] for u in v["uses"]}
        miss = names - ds
        if len(miss) == 1 and len(names) >= 2 and not v["requires"]:
            m = next(iter(miss))
            if m in pool:
                away[m].append({"with": sorted(names & ds), "makes": [f["feature"]["name"] for f in v["produces"]][:3]})
    # Every card one away (not just the top few), with its text, image and Scryfall page.
    top = sorted(away, key=lambda m: (-len(away[m]), m))
    info = {r["n"]: r for r in db.execute("""MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS n, c.colorIdentity AS ci,
        c.typeLine AS type, c.manaCost AS cost, c.oracleText AS text, c.imageUrl AS image, c.oracleId AS id""", {"n": top})}
    one_away = []
    for m in top:
        x = info.get(m, {})
        examples, seen_ex = [], set()
        for ex in away[m]:
            k = tuple(ex["with"])
            if k not in seen_ex:
                seen_ex.add(k)
                examples.append(ex)
            if len(examples) == 3:
                break
        one_away.append({"card": m, "count": len(away[m]), "ci": x.get("ci", []), "example": examples[0], "examples": examples,
                         "type": x.get("type"), "cost": x.get("cost"), "text": x.get("text"), "image": x.get("image"),
                         "scryfall": f"https://scryfall.com/search?q=oracleid%3A{x['id']}" if x.get("id") else None})

    # Next best cards (combined score: known combos, new graph loops, links, conflicts)
    ranked, _ = suggest(db, d, variants)
    top = ranked[:30]
    sinfo = {r["n"]: r for r in db.execute("""MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS n, c.colorIdentity AS ci,
        c.typeLine AS type, c.manaCost AS cost, c.oracleText AS text, c.imageUrl AS image, c.oracleId AS id""",
        {"n": [r["card"] for r in top]})}
    suggestions = [{**{k: r[k] for k in ("card", "score", "known", "twoCard", "new", "newLoops", "links", "nonbo", "warnings", "examples")},
                    "ci": sinfo.get(r["card"], {}).get("ci", []), "type": sinfo.get(r["card"], {}).get("type"),
                    "cost": sinfo.get(r["card"], {}).get("cost"), "text": sinfo.get(r["card"], {}).get("text"),
                    "image": sinfo.get(r["card"], {}).get("image"),
                    "scryfall": f"https://scryfall.com/search?q=oracleid%3A{sinfo[r['card']]['id']}" if r["card"] in sinfo else None}
                   for r in top]

    return {"commander": cmd, "identity": d["identity"], "spells": len(nonland), "lands": 100 - len(nonland),
            "suggestions": suggestions,
            "synergies": deck_synergies(db, deck),
            "search": {k: search.get(k) for k in ("stoppedEarly", "cycles", "checked", "seconds")},
            "problems": d.get("problems", []),
            "combos": combos, "missed": missed, "nonbos": nonbos, "hubs": [{"card": h, "links": len(links[h])} for h in hubs],
            "oneAway": one_away, "oneAwayTotal": len(away),
            "cards": [{"name": c, "type": cards[c]["typeLine"], "cost": cards[c]["manaCost"], "text": cards[c]["text"],
                       "image": cards[c].get("image"), "status": status[c], "links": len(links[c])} for c in nonland]}


def main(store, variants_path, out):
    variants = [json.loads(l) for l in gzip.open(variants_path, "rt")]
    db = relate.connect(store)
    def load_any(p):
        text = open(p, encoding="utf-8").read()
        return load_deck(db, p) if re.search(r"^commander\s*$", text, re.I | re.M) else load_deck_text(db, text)
    data = {"decks": [deck_report(db, load_any(p), variants) for p in SAMPLE_DECKS],
            "validation": json.load(open("validation.json"))}
    json.dump(data, open(out, "w"), ensure_ascii=False)
    for dk in data["decks"]:
        print(f"{dk['commander']}: {len(dk['combos'])} combos ({sum(c['confirmed'] for c in dk['combos'])} confirmed), "
              f"{len(dk['missed'])} missed, {len(dk['nonbos'])} findings, one-away {dk['oneAwayTotal']}, "
              f"statuses {dict(__import__('collections').Counter(c['status'] for c in dk['cards']))}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
