"""Add the rules distinctions that separate real synergies from look-alikes.

    python rules_facts.py mtg.rl       # run after abilities.py

Each fact splits one idea players tend to merge into two:
  enters from graveyard  vs  cast from graveyard      (Animate Dead vs Chainer)
  token created          vs  spell copy -> token      (Bitterblossom vs Volo)
  spell copy             vs  card copy                 (Reverberate vs Shiko)
  targets only X         vs  also targets a player     (Expedite vs Sazacap's Brew)
and records abilities that depend on deck rules (commander colour identity) and
static abilities that PREVENT an event from happening at all (Notion Thief).

(:Ability)-[:RETRIGGERS]->(:Event {name: 'enters_from_graveyard'})
(:Ability)-[:TRIGGERED_BY]->(:Event {name: 'enters_from_graveyard' | 'opponent_draws' | 'token_created'})
(:Ability)-[:PRODUCES]->(:Resource {name: 'cast_from_graveyard' | 'spell_copy' | 'card_copy'})
(:Ability)-[:AMPLIFIES]->(:Resource {name: 'token' | 'spell_copy'})
(:Ability)-[:PREVENTS {whose}]->(:Event)
Ability properties: targets (list), requiresTargetsOnlySelf, needsCommanderIdentity
"""
import re
import sys
import time

import relate

C = re.compile
SELF = r"(?:~|this (?:creature|artifact|enchantment|land|permanent|card|spell|aura))"

# Zone of origin
TO_BF_FROM_GY = C(r"(?:return|put) [^.]*?(?:from (?:a|your|any|an opponent's) graveyard|enchanted creature card)[^.]*? (?:to|onto) the battlefield"
                  r"|return enchanted creature card to the battlefield|^(?:persist|undying) \(")
# Permission to cast from the graveyard ("you may cast ..."), not a condition ("if you cast it from ...").
CAST_FROM_GY = C(r"(?:you may|may) (?:play [^.]*? or )?cast [^.]*?from your graveyard|may be cast from your graveyard")
CAST_FROM_GY_TRIGGER = C(r"cast(?:s)? (?:it|a spell|[^,]*?) from (?:your|a) graveyard")
ENTERS_FROM_GY_TRIGGER = C(r"enters? from (?:your|a) graveyard")

# Copies and tokens
SPELL_COPY = C(r"copy (?:that|target|the|each) (?:[\w-]+ )*spell|copy it(?: for each| \w+ times)?(?=[,. ])")
CARD_COPY = C(r"exile [^.]*\bcard\b[^.]*\. copy it|copy (?:target|that|the exiled) card")
TOKEN_REPLACE = C(r"would create (?:one or more |a )?(?:[^.]* )?tokens?")
TOKEN_CREATED_TRIGGER = C(r"whenever you create (?:a|one or more) [^.]*tokens?")
SPELL_COPY_REPLACE = C(r"if you would copy a spell")

# Targeting
TARGETS = C(r"\b(?:up to (?:one|two|\w+) )?(?:another |other )?target ((?:[\w-]+ )*?(?:player|opponent|creature|permanent|artifact|enchantment|planeswalker|land|spell|card))|\bany target\b")
ONLY_SELF = C(rf"that targets only {SELF}")

# Deck rules
COMMANDER_IDENTITY = C(r"commander's color identity")

# Replacement / static effects that stop an event from happening
PREVENTS = [
    (C(r"if an opponent would draw a card[^.]*instead that player skips that draw"), ["opponent_draws"], "opponents",
     "makes opponents skip those draws, so they never draw"),
    (C(r"creatures entering (?:the battlefield )?don't cause abilities to trigger"), ["self_enters", "creature_enters"], "creatures",
     "stops creatures entering from triggering abilities"),
    (C(r"if a card or token would be put into a graveyard from anywhere, exile it instead"), ["self_dies", "creature_dies"], "all",
     "exiles anything that would go to a graveyard, and \"dies\" means going to the graveyard, so creatures never die"),
    (C(r"if a nontoken creature would enter and it wasn't cast, exile it instead"), ["enters_from_graveyard"], "all",
     "exiles nontoken creatures that would enter without being cast, so nothing enters from the graveyard"),
]
OPPONENT_DRAWS_TRIGGER = C(r"whenever an opponent draws")

EXPORT = """
MATCH (c:Card)-[:HAS_ABILITY|GRANTS]->(a:Ability)
RETURN a.id AS id, a.text AS text, labels(a) AS labels, c.types AS types
"""


def facts(a):
    t = a["text"] or ""
    kind = next(l for l in a["labels"] if l != "Ability")
    trigger_clause = t.partition(", ")[0] if kind == "Triggered" else ""
    effect = t.partition(", ")[2] if kind == "Triggered" else t
    out = {"retrig": [], "trig": [], "prod": [], "amp": [], "prev": [], "props": {}}

    if TO_BF_FROM_GY.search(t) and not CAST_FROM_GY.search(t):
        out["retrig"].append("enters_from_graveyard")
    if CAST_FROM_GY.search(effect):
        out["prod"].append("cast_from_graveyard")
    if ENTERS_FROM_GY_TRIGGER.search(trigger_clause):
        out["trig"].append("enters_from_graveyard")
    if kind == "Triggered" and CAST_FROM_GY_TRIGGER.search(t):
        out["trig"].append("cast_from_graveyard")

    if CARD_COPY.search(effect):
        out["prod"].append("card_copy")
    elif SPELL_COPY.search(effect) and "spell" in t:
        out["prod"].append("spell_copy")
        if re.search(r"creature spell", t):
            out["props"]["copiesCreatureSpells"] = True
    if kind == "Static" and TOKEN_REPLACE.search(t):
        out["amp"].append("token")
    if TOKEN_CREATED_TRIGGER.search(trigger_clause):
        out["trig"].append("token_created")
    if kind == "Static" and SPELL_COPY_REPLACE.search(t):
        out["amp"].append("spell_copy")

    if kind == "Spell":
        # Optional targets ("up to one other target ...") can be left unchosen, so they don't count.
        targets = ["any" if m.group(0) == "any target" else m.group(1).split()[-1]
                   for m in TARGETS.finditer(t) if not m.group(0).startswith("up to")]
        if targets:
            out["props"]["targets"] = targets
    if ONLY_SELF.search(trigger_clause):
        out["props"]["requiresTargetsOnlySelf"] = True
    if COMMANDER_IDENTITY.search(t):
        out["props"]["needsCommanderIdentity"] = True

    if kind == "Static":
        for rx, events, whose, reason in PREVENTS:
            if rx.search(t):
                out["prev"] += [(e, whose, reason) for e in events]
    if OPPONENT_DRAWS_TRIGGER.search(trigger_clause):
        out["trig"].append("opponent_draws")
    return out


WRITES = {
    "retrig": """UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (e:Event {name: r.x})
                 MERGE (a)-[t:RETRIGGERS {scope: 'other'}]->(e) SET t.immediate = true""",
    "trig": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (e:Event {name: r.x}) MERGE (a)-[t:TRIGGERED_BY]->(e) SET t.nontoken = false",
    "prod": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (x:Resource {name: r.x}) MERGE (a)-[:PRODUCES]->(x)",
    "amp": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (x:Resource {name: r.x}) MERGE (a)-[:AMPLIFIES]->(x)",
    "prev": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (e:Event {name: r.x}) MERGE (a)-[p:PREVENTS]->(e) SET p.whose = r.whose, p.reason = r.reason",
    "props": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) SET a += r.x",
}


def main(store):
    t0 = time.time()
    db = relate.connect(store)
    db.execute("MATCH (:Ability)-[r:AMPLIFIES|PREVENTS]->() DELETE r")     # re-runnable
    rows = {k: [] for k in WRITES}
    for a in db.execute(EXPORT):
        f = facts(a)
        rows["retrig"] += [{"id": a["id"], "x": x} for x in f["retrig"]]
        rows["trig"] += [{"id": a["id"], "x": x} for x in f["trig"]]
        rows["prod"] += [{"id": a["id"], "x": x} for x in f["prod"]]
        rows["amp"] += [{"id": a["id"], "x": x} for x in f["amp"]]
        rows["prev"] += [{"id": a["id"], "x": e, "whose": w, "reason": why} for e, w, why in f["prev"]]
        if f["props"]:
            rows["props"].append({"id": a["id"], "x": f["props"]})
    for name, q in WRITES.items():
        for i in range(0, len(rows[name]), 2000):
            db.execute(q, {"rows": rows[name][i:i + 2000]})
    fires = add_fires(db)
    print(f"rules facts: { {k: len(v) for k, v in rows.items()} }, FIRES links: {fires} ({time.time() - t0:.0f}s)")
    db.close()



def add_fires(db):
    """(Resource)-[:FIRES]->(Event): producing some things is also an event other cards watch for.
    Gaining life fires "whenever you gain life"; drawing fires "whenever you draw"."""
    from engine import RESOURCE_EVENTS
    rows = [{"r": res, "e": ev} for res, evs in RESOURCE_EVENTS.items() for ev in evs]
    db.execute("""UNWIND $rows AS x MERGE (r:Resource {name: x.r}) MERGE (e:Event {name: x.e})
                  MERGE (r)-[:FIRES]->(e)""", {"rows": rows})
    return len(rows)

if __name__ == "__main__":
    main(sys.argv[1])
