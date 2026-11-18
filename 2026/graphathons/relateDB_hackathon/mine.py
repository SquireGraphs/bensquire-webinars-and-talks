"""Mine mechanics: ask Claude to propose a rule for each combo the graph can't trace, then verify it.

    export ANTHROPIC_API_KEY=...
    python mine.py mtg_live.rl variants_slim.jsonl.gz --deck decks/teysa_aristocrats.txt   # a deck's misses
    python mine.py mtg_live.rl variants_slim.jsonl.gz --benchmark 20                      # 20 benchmark misses

A proposed rule (the same format as the Teach a mechanic tab) is ACCEPTED only if:
  1. with it, the graph traces the combo it was proposed for;
  2. every must-not-loop control in checks.py still doesn't loop;
  3. a fixed set of 60 random decks gains no new loops.
Accepted rules go to mined_rules.json for you to review; nothing is applied until you copy a rule into
custom_rules.json (or add it in the Teach tab). Rejected ones are kept with the reason, to learn from.
"""
import argparse
import gzip
import json
import os
import random
import re
import sys

import relate

import engine
from engine import find_loops
from wins import find_wins

SCHEMA = """You describe a Magic: The Gathering mechanic as a rule for a cause-and-effect graph.
A rule gives every card whose rules text matches some wording one extra ability:
  name: short name
  match: the wording to look for, in plain lowercase words (a regular expression is allowed). "this card" also
         matches the card's own name. It must be general: it should match every card with this mechanic, not one card.
  needs: optional creature type the deck must also contain (e.g. "Zombie" for "as long as you control a Zombie")
  kind: "Activated" (you choose to use it), "Triggered" (happens after an event) or "Static"
  trigger: for Triggered, a list with ONE event from: self_dies, self_leaves, self_enters, creature_dies, creature_enters,
           creature_leaves, cast_spell, gain_life, opponent_loses_life, draw, token_created, land_enters
  costs: list of {"resource": R, "amount": N or "mana value"}; R from: tap, mana, creature, artifact, card_draw,
         counters (remove +1/+1 counters, e.g. Walking Ballista), lifegain (pay life)
  produces: list of {"resource": R, "amount": N or "mana value"}; R from: mana, creature, token, artifact, card_draw,
            lifegain, life_loss_opponents, damage_players, untap, return_to_hand, counters
  causes: list of {"event": E, "scope": S}; E from the trigger events above; S is "self" (this card), "other"
          (another card), "copy" (a token copy of another card) or "any"
Only describe what the card text actually says. Examples:
{"name": "Cast from graveyard", "match": "you may cast this card from your graveyard", "needs": "Zombie", "kind": "Triggered",
 "trigger": ["self_dies"], "costs": [{"resource": "mana", "amount": "mana value"}], "produces": [{"resource": "creature", "amount": 1}],
 "causes": [{"event": "self_enters", "scope": "self"}, {"event": "creature_enters", "scope": "any"}]}
{"name": "Token copies", "match": "create a token that's a copy of target", "kind": "Activated", "trigger": [],
 "costs": [{"resource": "tap"}], "produces": [{"resource": "creature", "amount": 1}],
 "causes": [{"event": "self_enters", "scope": "copy"}, {"event": "creature_enters", "scope": "any"}]}"""

TOLERANCE = 0          # extra random-deck loops allowed (set with --tolerance)
TOOL = {"name": "propose_rule", "description": "Propose one rule for the mechanic this combo depends on.",
        "input_schema": {"type": "object", "properties": {
            "reasoning": {"type": "string", "description": "One or two sentences: which mechanic the graph is missing."},
            "rule": {"type": "object"}}, "required": ["reasoning", "rule"]}}


def condition_helpers(db, cards, deck):
    """Deck cards that meet a combo card's "as long as you control a Zombie": the loop may name them as helpers."""
    if not deck:
        return []
    texts = {r["n"]: (r["t"] or "").lower() for r in db.execute("MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS n, c.oracleText AS t", {"n": cards})}
    needs = {w for t in texts.values() for w in re.findall(r"as long as you control (?:a|an|another) ([a-z-]+)", t)}
    if not needs:
        return []
    rows = list(db.execute("MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS n, c.typeLine AS tl", {"n": sorted(set(deck) - set(cards))}))
    return [next(r["n"] for r in rows if w.rstrip("s") in (r["tl"] or "").lower()) for w in needs
            if any(w.rstrip("s") in (r["tl"] or "").lower() for r in rows)]


def traced(db, cards, deck=None):
    helpers = condition_helpers(db, cards, deck)
    allowed = set(cards) | set(helpers)
    test = sorted(allowed)
    return any(set(l["cards"]) <= allowed for l in find_loops(db, test, budget_s=10) + find_wins(db, test))


def current_reading(db, combo):
    """How the graph reads these cards now, and which links it sees: what Claude needs to find the gap."""
    g = engine.deck_subgraph(db, combo)
    A = g["abilities"]
    lines = []
    for aid, x in A.items():
        bits = [f"{x['card']} ({x['kind']}{', tap' if x.get('tapCost') else ''}): {(x['text'] or '')[:110]}"]
        for label, key in (("triggered by", "TRIGGERED_BY"), ("produces", "PRODUCES"), ("costs", "COSTS")):
            vals = [t for t, _ in g[key].get(aid, [])]
            if vals:
                bits.append(f"{label} {vals}")
        rts = [f"{t}/{p.get('scope')}" for t, p in g["RETRIGGERS"].get(aid, [])]
        if rts:
            bits.append(f"causes {rts}")
        lines.append("  - " + "; ".join(bits))
    edges = engine.causal_edges(g)
    links = sorted({f"{A[a]['card']} -> {A[b]['card']}" for a, bs in edges.items() for b in bs if A[a]["card"] != A[b]["card"]})
    cyc = engine.cycles(edges)
    return ("Current graph reading:\n" + "\n".join(lines[:30]) +
            f"\nLinks between cards: {links or 'none'}\nCycles: {len(cyc)} found" + (" (all rejected by the rules checks)" if cyc else ""))


def misses_for_deck(db, variants, path):
    from decks import load_deck, load_deck_text
    text = open(path, encoding="utf-8").read()
    d = load_deck(db, path) if re.search(r"^commander\s*$", text, re.I | re.M) else load_deck_text(db, text)
    ds = set(d["commanders"] + d["cards"])
    combos = [sorted(u["card"]["name"] for u in v["uses"]) for v in variants
              if not v["requires"] and {u["card"]["name"] for u in v["uses"]} <= ds]
    deck = sorted(ds)
    return [c for c in combos if not traced(db, c, deck)]


def misses_for_benchmark(db, variants_path, n):
    from coverage import sample
    combos, _ = sample(db, variants_path)
    return [c for c, _ in combos if not traced(db, c)][:n]


def verify(db, rule, combo, controls, randoms, baseline, deck=None):
    """Accept only a rule that traces its combo without adding false loops."""
    saved = engine.custom_rules() if engine.RULES_OVERRIDE is None else engine.RULES_OVERRIDE
    try:
        engine.RULES_OVERRIDE = list(saved) + [rule]
        if not traced(db, combo, deck):
            return False, "doesn't trace the combo"
        bad = [c for c in controls if find_loops(db, c)]
        if bad:
            return False, f"makes a must-not-loop control loop: {bad[0]}"
        hits = sum(bool(find_loops(db, d, budget_s=10)) for d in randoms)
        if hits > baseline + TOLERANCE:
            return False, f"adds false loops in random decks ({baseline} -> {hits} of {len(randoms)})"
        return True, "traces the combo; controls and random decks unchanged"
    finally:
        engine.RULES_OVERRIDE = None


def ask(client, model, history):
    msg = client.messages.create(model=model, max_tokens=1500, system=SCHEMA, tools=[TOOL], tool_choice={"type": "auto"}, messages=history)
    use = next((b for b in msg.content if b.type == "tool_use"), None)
    return msg, use


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("store")
    ap.add_argument("variants")
    ap.add_argument("--deck")
    ap.add_argument("--benchmark", type=int, default=0)
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--out", default="mined_rules.json")
    ap.add_argument("--tolerance", type=int, default=0, help="extra random-deck loops a rule may add (default 0)")
    ap.add_argument("--attempts", type=int, default=3, help="tries per combo, each with the verifier's feedback")
    a = ap.parse_args()
    global TOLERANCE
    TOLERANCE = a.tolerance
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY isn't set in this terminal. Run:  export ANTHROPIC_API_KEY=your-key")
    import anthropic
    from checks import MUST_NOT_LOOP
    client = anthropic.Anthropic()
    db = relate.connect(a.store)
    variants = [json.loads(l) for l in gzip.open(a.variants, "rt")]
    misses = misses_for_deck(db, variants, a.deck) if a.deck else misses_for_benchmark(db, a.variants, a.benchmark or 10)
    print(f"{len(misses)} combos the graph can't trace")
    texts = {r["n"]: r["t"] for r in db.execute("MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS n, c.oracleText AS t",
                                                 {"n": sorted({c for m in misses for c in m})})}
    pool = [x["n"] for x in db.execute("MATCH (c:Card) WHERE 'commander' IN c.legalIn AND NOT 'Land' IN c.types RETURN c.name AS n")]
    random.seed(11)
    randoms = [random.sample(pool, 62) for _ in range(60)]
    engine.RULES_OVERRIDE = engine.custom_rules()
    baseline = sum(bool(find_loops(db, d, budget_s=10)) for d in randoms)
    engine.RULES_OVERRIDE = None
    controls = [c for c, _ in MUST_NOT_LOOP]
    results = []
    deck_cards = None
    if a.deck:
        from decks import load_deck, load_deck_text
        text = open(a.deck, encoding="utf-8").read()
        dd = load_deck(db, a.deck) if re.search(r"^commander\s*$", text, re.I | re.M) else load_deck_text(db, text)
        deck_cards = dd["commanders"] + dd["cards"]
    for combo in misses:
        cards = "\n".join(f"- {c}: {texts.get(c, '')}" for c in combo)
        history = [{"role": "user", "content": f"These cards form a known infinite combo, but our graph can't trace it:\n{cards}\n\n"
                    f"{current_reading(db, combo)}\n\nWhich general mechanic is the graph missing, given how it reads these cards now? "
                    "Don't re-describe a mechanic the reading already shows; add the missing link. Answer only by calling propose_rule."}]
        ok, why, rule, prop = False, "no rule proposed", None, None
        try:
          for attempt in range(1, a.attempts + 1):
              msg, use = ask(client, a.model, history)
              if not use or not isinstance(use.input.get("rule"), dict):
                  break
              prop = use.input
              rule = {**prop["rule"], "example": combo}
              rule.setdefault("name", "mined rule")
              ok, why = verify(db, rule, combo, controls, randoms, baseline, deck_cards)
              print(f"  attempt {attempt}: {rule.get('name')}: {why}")
              if ok:
                  break
              engine.RULES_OVERRIDE = engine.custom_rules() + [rule]          # show what the graph sees WITH the rule
              seen = current_reading(db, combo)
              engine.RULES_OVERRIDE = None
              calls = [b for b in msg.content if b.type == "tool_use"]        # the API needs a result for every call
              history += [{"role": "assistant", "content": msg.content},
                          {"role": "user", "content": [{"type": "tool_result", "tool_use_id": b.id,
                           "content": (f"Rejected: {why}.\nWith your rule the graph reads:\n{seen}\nPropose ONE corrected rule."
                                       if b.id == use.id else "Only one rule is tested per attempt; this one was skipped.")} for b in calls]}]
        except Exception as err:                       # an API hiccup on one combo shouldn't end the run
            why = f"error: {err.__class__.__name__}: {str(err)[:120]}"
        print(f"{'ACCEPT' if ok else 'reject'}  {' + '.join(combo)}  ({why})")
        results.append({"combo": combo, "accepted": ok, "why": why, "reasoning": (prop or {}).get("reasoning"), "rule": rule})
        json.dump({"rules": [r["rule"] for r in results if r["accepted"]], "all": results}, open(a.out, "w"), indent=1)
    json.dump({"rules": [r["rule"] for r in results if r["accepted"]], "all": results}, open(a.out, "w"), indent=1)
    print(f"{sum(r['accepted'] for r in results)} of {len(results)} accepted -> {a.out}")


if __name__ == "__main__":
    main()
