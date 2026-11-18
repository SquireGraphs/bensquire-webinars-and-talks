"""States in the graph: what each ability requires, causes and clears.

    python states.py mtg_live.rl            # adds (:State) nodes and the three relationships below; run after load_llm.py

  (Ability)-[:REQUIRES_STATE {who}]->(State)     the card it acts on must be in this state
  (Ability)-[:CAUSES_STATE {who, by, via}]->(State)
  (Ability)-[:CLEARS_STATE {by}]->(State)         e.g. this card leaving releases what it holds

States: battlefield, graveyard, exile, hand, held (exiled until a particular card leaves the battlefield).
`who` says which card: self (the ability's own card), target, that card (the one that just died), held cards,
sacrificed, or copy (a token copy). `by` names the holder for held: self, or target (what a flickered card held).

Fiend Hunter, for example:
  enters:  REQUIRES_STATE {who: target} -> battlefield,  CAUSES_STATE {who: target, by: self} -> held
  leaves:  CLEARS_STATE {by: self} -> held,              CAUSES_STATE {who: held cards} -> battlefield
The loop checker walks these to see whether a loop ends where it started (rule 400.7: a card that leaves and
returns is a new object, so a new exile is a new hold).
"""
import re
import sys
import time

import relate

from engine import FLICKER, FROM_GRAVEYARD, HOLDS, RETURN_HELD, RETURN_THAT_CARD, SACRIFICE

STATES = [("battlefield", "On the battlefield"), ("graveyard", "In the graveyard"), ("exile", "In exile"),
          ("hand", "In hand"), ("held", "Held in exile by a card, until that card leaves the battlefield")]


def classify(text, kind, holder):
    """[(relationship, state, props)] for one ability. `holder`: the card has a 'return the exiled card' ability."""
    t = (text or "").lower()
    out = []
    req = lambda st, **p: out.append(("REQUIRES_STATE", st, p))
    cau = lambda st, **p: out.append(("CAUSES_STATE", st, p))
    clr = lambda st, **p: out.append(("CLEARS_STATE", st, p))
    if re.search(r"token that's a copy of", t):
        req("battlefield", who="target"); cau("battlefield", who="copy")
    elif RETURN_THAT_CARD.search(t):                      # Kaya's Ghostform: the card that just died comes back
        req("graveyard", who="that card"); cau("battlefield", who="that card")
        if "enchanted" in t:
            cau("graveyard", who="self", via="its creature left")
    elif FLICKER.search(t):
        req("battlefield", who="target"); cau("battlefield", who="target", via="flicker")
        clr("held", by="target")                          # whatever the flickered card held comes back
    elif (HOLDS.search(t) or holder) and re.search(r"\bexile\b", t) and not RETURN_HELD.search(t):
        req("battlefield", who="target"); cau("held", who="target", by="self")
    elif RETURN_HELD.search(t):
        clr("held", by="self"); cau("battlefield", who="held cards")
    elif FROM_GRAVEYARD.search(t) or re.search(r"cast (?:this card|~) from your graveyard", t):
        who = "self" if re.search(r"(?:return|cast) (?:this card|~) from your graveyard", t) else "target"
        req("graveyard", who=who); cau("battlefield", who=who)
    elif re.search(r"return [^.]*from (?:your|a) graveyard to (?:your|its owner's) hand", t):
        req("graveyard", who="target"); cau("hand", who="target")           # Archaeomancer: recursion, not a bounce
    elif re.search(r"return [^.]*to (?:its owner's|your|their owners') hands?", t):
        who = "self" if re.search(r"return (?:this creature|~|this card|it) to", t) else "target"
        req("battlefield", who=who); cau("hand", who=who)
    elif re.search(r"\bexile (?:another )?target\b", t) and not re.search(r"\breturn\b", t):
        req("battlefield", who="target"); cau("exile", who="target")
    # a sacrifice that's a cost (before the colon), or an immediate one ("that creature's controller sacrifices it");
    # not a delayed cleanup ("sacrifice it at the beginning of the next end step", Kiki-Jiki's token)
    if (kind == "Activated" and "sacrific" in t.split(":")[0]) or (re.search(r"sacrifices it\b", t) and "end step" not in t):
        req("battlefield", who="sacrificed"); cau("graveyard", who="sacrificed"); clr("held", by="sacrificed")
    return out


def main(store):
    t0 = time.time()
    db = relate.connect(store)
    db.execute("MATCH (:Ability)-[r:REQUIRES_STATE|CAUSES_STATE|CLEARS_STATE]->(:State) DELETE r")
    db.execute("MATCH (s:State) DETACH DELETE s")
    db.execute("UNWIND $rows AS r CREATE (:State {name: r[0], description: r[1]})", {"rows": STATES})
    abilities = [dict(r, kind=next((l for l in r["labels"] if l != "Ability"), "Static")) for r in db.execute(
        "MATCH (c:Card)-[:HAS_ABILITY|GRANTS]->(a:Ability) RETURN DISTINCT a.id AS id, a.text AS text, labels(a) AS labels, c.name AS card")]
    # holders: cards with an "until ~ leaves the battlefield" or "return the exiled card" ability (Fiend Hunter's two triggers)
    holders = {r["card"] for r in abilities if HOLDS.search(r["text"] or "") or RETURN_HELD.search(r["text"] or "")}
    rows = {"REQUIRES_STATE": [], "CAUSES_STATE": [], "CLEARS_STATE": []}
    for r in abilities:
        for rel, state, props in classify(r["text"], r["kind"], r["card"] in holders):
            rows[rel].append({"id": r["id"], "state": state, "who": props.get("who", ""), "by": props.get("by", ""), "via": props.get("via", "")})
    seen = len(abilities)
    for rel, rs in rows.items():
        for i in range(0, len(rs), 2000):
            db.execute(f"""UNWIND $rows AS x MATCH (a:Ability {{id: x.id}}), (s:State {{name: x.state}})
                           CREATE (a)-[:{rel} {{who: x.who, by: x.by, via: x.via}}]->(s)""", {"rows": rs[i:i + 2000]})
    counts = {rel: len(rs) for rel, rs in rows.items()}
    n_ab = len({x["id"] for rs in rows.values() for x in rs})
    print(f"states: {len(STATES)} State nodes; {n_ab:,} of {seen:,} abilities linked: " +
          ", ".join(f"{v:,} {k}" for k, v in counts.items()) + f" ({time.time() - t0:.0f}s)")
    db.close()


if __name__ == "__main__":
    main(sys.argv[1])
