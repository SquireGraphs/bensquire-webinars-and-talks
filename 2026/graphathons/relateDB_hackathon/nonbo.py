"""Find look-alike synergies that don't work (nonbos), and explain why.

    python nonbo.py mtg.rl          # paired tests: each nonbo vs a near-identical pair that works

Most nonbos are near misses between two things players merge into one:
a payoff needs X, and the deck provides a look-alike X'. The rules below pair
each payoff requirement with its real providers and its common look-alikes.
"""
import re
import sys
from collections import defaultdict

import relate

RELS = ("RETRIGGERS", "TRIGGERED_BY", "PRODUCES", "AMPLIFIES", "PREVENTS", "RETURNS_FROM")


def load(db, deck):
    q = lambda s: list(db.execute(s, {"deck": deck}))
    abil = {r["id"]: r for r in q("""
        MATCH (c:Card)-[:HAS_ABILITY|GRANTS]->(a:Ability) WHERE c.name IN $deck
        RETURN a.id AS id, c.name AS card, labels(a) AS labels, a.text AS text,
               a.targets AS targets, a.requiresTargetsOnlySelf AS onlySelf,
               a.needsCommanderIdentity AS needsIdentity, a.copiesCreatureSpells AS copiesCreatures""")}
    edges = {r: defaultdict(list) for r in RELS}
    for r in RELS:
        for row in q(f"""MATCH (c:Card)-[:HAS_ABILITY|GRANTS]->(a:Ability)-[x:{r}]->(t)
                         WHERE c.name IN $deck RETURN a.id AS id, coalesce(t.name, t.id) AS target, properties(x) AS p"""):
            edges[r][row["id"]].append((row["target"], row["p"] or {}))
    cards = q("MATCH (c:Card) WHERE c.name IN $deck RETURN c.name AS name, c.colorIdentity AS ci, c.types AS types")
    identity = {r["name"]: r["ci"] or [] for r in cards}
    for a in abil.values():
        a["cardTypes"] = next((r["types"] or [] for r in cards if r["name"] == a["card"]), [])
    return abil, edges, identity


def has(edges, rel, aid, target):
    return any(t == target for t, _ in edges[rel].get(aid, []))


def check(db, deck, commander=None):
    A, E, identity = load(db, deck)
    found = []
    add = lambda verdict, cards, rule, why: found.append(
        {"verdict": verdict, "cards": sorted(set(cards)), "rule": rule, "why": why})
    other = lambda p: [a for a in A.values() if a["card"] != A[p]["card"]]

    for pid, p in A.items():
        # 1. Zone of origin: "enters from your graveyard" vs "cast from your graveyard".
        if has(E, "TRIGGERED_BY", pid, "enters_from_graveyard"):
            for q in other(pid):
                if has(E, "RETRIGGERS", q["id"], "enters_from_graveyard"):
                    add("works", [p["card"], q["card"]], "zone of origin",
                        f"{q['card']} puts a creature onto the battlefield from the graveyard, so {p['card']} triggers.")
                elif has(E, "PRODUCES", q["id"], "cast_from_graveyard") and not any(
                        has(E, "TRIGGERED_BY", x["id"], "cast_from_graveyard")
                        for x in A.values() if x["card"] == p["card"]):     # payoff also accepts casting
                    add("nonbo", [p["card"], q["card"]], "zone of origin",
                        f"{q['card']} lets you cast creatures from your graveyard. A cast creature moves to the stack "
                        f"first, so it enters from the stack, not from the graveyard: {p['card']} doesn't trigger.")

        # 2. Copy vs create: token doublers and "whenever you create a token" need created tokens.
        if has(E, "AMPLIFIES", pid, "token") or has(E, "TRIGGERED_BY", pid, "token_created"):
            for q in other(pid):
                if has(E, "PRODUCES", q["id"], "token"):
                    add("works", [p["card"], q["card"]], "copy vs create",
                        f"{q['card']} creates tokens, which {p['card']} applies to.")
                elif has(E, "PRODUCES", q["id"], "spell_copy") and q.get("copiesCreatures"):
                    add("nonbo", [p["card"], q["card"]], "copy vs create",
                        f"{q['card']} copies a creature spell. The copy becomes a token as it resolves, but no token "
                        f"is created, so {p['card']} doesn't apply.")

        # 3. Spell copy vs card copy.
        if has(E, "AMPLIFIES", pid, "spell_copy"):
            for q in other(pid):
                if has(E, "PRODUCES", q["id"], "spell_copy"):
                    add("works", [p["card"], q["card"]], "spell copy vs card copy",
                        f"{q['card']} copies a spell, so {p['card']} adds another copy.")
                elif has(E, "PRODUCES", q["id"], "card_copy"):
                    add("nonbo", [p["card"], q["card"]], "spell copy vs card copy",
                        f"{q['card']} copies a card (then you cast the copy). {p['card']} only changes copies of spells.")

        # 4. Targeting: "targets only <this card>".
        if p.get("onlySelf"):
            # Judge each mode / ability separately: a modal spell works if any mode qualifies.
            # Modes are judged per reading: the rules' and the LLM's readings of one spell are not two
            # modes, so the spell qualifies only if every reading has a single-target mode.
            modes = defaultdict(lambda: defaultdict(list))
            for q in other(pid):
                if "Spell" in q["labels"] and q.get("targets"):
                    modes[q["card"]]["llm" if "#llm" in q["id"] else "rules"].append(q["targets"])
            ok = lambda ts: len(ts) == 1 and ts[0] in ("creature", "any", "permanent")
            for card, by_reading in modes.items():
                if "rules" in by_reading:        # the rules read "up to one other target" as optional; trust them
                    by_reading = {"rules": by_reading["rules"]}
                ms = [ts for group in by_reading.values() for ts in group]
                if all(any(ok(ts) for ts in group) for group in by_reading.values()):
                    add("works", [p["card"], card], "targeting",
                        f"{card} can be cast with a single target, {p['card']}, so it can be copied.")
                elif any(t in ("creature", "any", "permanent") for ts in ms for t in ts):
                    ts = next((ts for ts in ms if not ok(ts) and any(t in ("creature", "any", "permanent") for t in ts)),
                              next(ts for ts in ms if any(t in ("creature", "any", "permanent") for t in ts)))
                    others = sorted(set(ts) - {"creature", "any", "permanent"})
                    article = "an" if others and others[0][0] in "aeiou" else "a"
                    why = f"also targets {article} {others[0]}" if others else "needs more than one target"
                    add("nonbo", [p["card"], card], "targeting",
                        f"{card} {why}, so it can't target only {p['card']}: no copies.")

        # 5. Replacement effects that remove the event a trigger needs.
        for ev, props in E["PREVENTS"].get(pid, []):
            if ev in ("other", "", None):
                continue                        # a catch-all category never links two cards
            if props.get("whose") == "opponents" and not ev.startswith("opponent_"):
                continue                        # Dauthi Voidwalker: only opponents' cards, never your triggers
            # Prefer the rule's own wording ("exiles ..."), which reads better than an LLM's free text.
            same_card = [x for x, a in A.items() if a["card"] == A[pid]["card"]]     # rules and LLM readings
            reasons = [pr.get("reason") for x in same_card for e2, pr in E["PREVENTS"].get(x, []) if e2 == ev and pr.get("reason")]
            props = {**props, "reason": next((r for r in reasons if re.match(r"^[a-z]+s ", r)), props.get("reason"))}
            for q in other(pid):
                # "Creatures entering don't cause abilities to trigger": a non-creature's own
                # enters ability (an Aura, an artifact) still triggers.
                if props.get("whose") == "creatures" and ev == "self_enters" and "Creature" not in q["cardTypes"]:
                    continue
                if ev == "enters_from_graveyard" and has(E, "RETRIGGERS", q["id"], ev):
                    add("nonbo", [p["card"], q["card"]], "an effect stops the event",
                        f"{p['card']} {props.get('reason', 'prevents ' + ev.replace('_', ' '))}: the creatures "
                        f"{q['card']} brings back get exiled instead.")
                if has(E, "TRIGGERED_BY", q["id"], ev):
                    stopped = {e2 for e2, _ in E["PREVENTS"].get(pid, [])}
                    still = sorted({e2 for e2, _ in E["TRIGGERED_BY"].get(q["id"], []) if e2 not in stopped and e2 != "other"})
                    if still:                   # "enters or attacks": it still triggers on attack
                        add("note", [p["card"], q["card"]], "an effect stops the event",
                            f"{p['card']} {props.get('reason', 'prevents ' + ev.replace('_', ' '))}, so {q['card']}'s ability "
                            f"won't trigger that way. It still triggers on: {', '.join(e2.replace('_', ' ') for e2 in still)}.")
                    else:
                        add("nonbo", [p["card"], q["card"]], "an effect stops the event",
                            f"{p['card']} {props.get('reason', 'prevents ' + ev.replace('_', ' '))}, so {q['card']}'s ability never triggers.")

        # 6. Deck rules: commander colour identity.
        if p.get("needsIdentity") and commander:
            if not identity.get(commander):
                add("nonbo", [p["card"], commander], "deck rules",
                    f"{p['card']} makes mana in your commander's colours, and {commander} is colourless: it makes no mana.")
            else:
                add("works", [p["card"], commander], "deck rules",
                    f"{p['card']} makes mana in {commander}'s colours ({''.join(identity[commander])}).")

    # 7. Flicker + exile removal: is the "exile it for good" trick available? One finding per removal card.
    FLICKER_TEXT = re.compile(r"\bexile\b[^\n]*\breturn\b[^\n]*to the battlefield")
    flickers = sorted({a["card"] for a in A.values()
                       if FLICKER_TEXT.search(a["text"] or "") and "graveyard" not in (a["text"] or "")
                       and any(ev == "self_enters" and pr.get("scope") in ("other", "granted")
                               for ev, pr in E["RETRIGGERS"].get(a["id"], []))})
    for rid, r in A.items():
        fl = [f for f in flickers if f != r["card"]]
        if not fl:
            continue
        sample = ", ".join(fl[:3]) + (f" and {len(fl) - 3} more" if len(fl) > 3 else "")
        if any(t == "removal" and pr.get("duration") == "until_source_leaves" for t, pr in E["PRODUCES"].get(rid, [])):
            add("note", [r["card"]] + fl, "exile wording",
                f"{r['card']} exiles only until it leaves. Flickering it ({sample}) returns the exiled card and lets "
                f"you pick a new target, but there's no way to make the exile permanent.")
        if E["RETURNS_FROM"].get(rid):
            add("works", [r["card"]] + fl, "exile wording",
                f"{r['card']} uses two separate triggers. Flicker it ({sample}) in response to its enter trigger: the "
                f"leave trigger resolves first with nothing to return, then the exile is permanent.")

    # Text patterns that need no ability graph: replacement effects and blanket prohibitions.
    texts = {r["n"]: (r["t"] or "") for r in db.execute(
        "MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS n, c.oracleText AS t, c.types AS ty", {"n": list(deck)})}
    TOKEN_KINDS = ("clue", "food", "treasure", "blood", "map")
    for rep, rt in texts.items():
        # Academy Manufactor: "If you would create a Clue, Food, or Treasure token, instead create one of each."
        m = re.search(r"if you would create (?:one or more |a )?([a-z, ]+?) tokens?, instead create", rt.lower())
        if m:
            kinds = [k for k in TOKEN_KINDS if k in m.group(1)]
            for card, ct in texts.items():
                if card == rep:
                    continue
                for sent in re.split(r"(?<=\.)\s", ct.lower()):
                    special = re.search(r"token that's a copy|copy of that creature|creature token", sent)
                    kind = next((k for k in kinds if k in sent and "token" in sent), None)
                    if special and kind:
                        add("nonbo", [rep, card], "replacement changes the token",
                            f"{rep} replaces the {kind.title()} token {card} makes with plain Clue, Food and Treasure tokens, "
                            f"so the special {kind.title()} (a copy, with its abilities) is never made.")
                        break
        # Stony Silence, Null Rod, Collector Ouphe: artifacts' activated abilities can't be activated.
        s = re.search(r"activated abilities of artifacts[^.]*can't be activated", rt.lower())
        if s and "opponents control" not in s.group(0):
            for card, ct in texts.items():
                low = ct.lower()
                if card == rep:
                    continue
                made = next((k for k in ("treasure", "clue", "food") if re.search(rf"create[^.]*\b{k}\b", low)), None)
                if made:
                    use = {"treasure": "sacrificed for mana", "clue": "sacrificed to draw", "food": "sacrificed for life"}[made]
                    add("nonbo", [rep, card], "artifact abilities stopped",
                        f"{rep} stops artifacts' activated abilities, so the {made.title()} tokens {card} makes can't be {use}.")
                elif re.search(r"\bartifact\b", (next(iter(db.execute("MATCH (c:Card {name:$n}) RETURN c.typeLine AS t", {"n": card})), {}).get("t") or "").lower()) \
                        and re.search(r"^[^\n\"]*\{[^}]+\}[^\n\"]*:", ct, re.M):
                    add("nonbo", [rep, card], "artifact abilities stopped",
                        f"{rep} stops artifacts' activated abilities, so {card}'s abilities can't be used.")

    info = {r["n"]: r for r in db.execute("MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS n, c.toughness AS tough, c.typeLine AS tl",
                                          {"n": list(deck)})}
    for rep, rt in texts.items():
        low = rt.lower()
        # Rest in Peace: cards go to exile instead of your graveyard, so graveyard recursion has nothing to return.
        if re.search(r"if a card(?: or token)? would be put into a graveyard from anywhere, exile it instead", low):
            for card, ct in texts.items():
                if card != rep and re.search(r"return [^.]*cards? from your graveyard|from your graveyard (?:to|onto) (?:your hand|the battlefield)", ct.lower()):
                    add("nonbo", [rep, card], "graveyard replaced",
                        f"{rep} exiles cards instead of putting them into your graveyard, so {card} has nothing to return.")
        # Solemnity: no counters, so a 0/0 that "enters with counters" (Walking Ballista) dies at once.
        if re.search(r"counters can't be put on (?:artifacts, )?creatures|players can't get counters", low):
            for card, ct in texts.items():
                if card != rep and re.search(r"enters with (?:x|\w+) \+1/\+1 counters?", ct.lower()) and str((info.get(card) or {}).get("tough")) == "0":
                    add("nonbo", [rep, card], "counters prevented",
                        f"{rep} stops counters, so {card} enters as a 0/0 and dies at once.")
        # Myojin of Infinite Rage: "if you cast it" -- putting it onto the battlefield (Ilharg) skips that.
        if re.search(r"put (?:a|an|target|up to one) [^.]*creature card[^.]* onto the battlefield", low):
            for card, ct in texts.items():
                if card != rep and re.search(r"if you cast it|if it was cast", ct.lower()) and "creature" in ((info.get(card) or {}).get("tl") or "").lower():
                    add("nonbo", [rep, card], "needs to be cast",
                        f"{rep} puts creatures onto the battlefield without casting them, so {card} loses what it gets only if you cast it.")

    # One finding per (verdict, cards, rule); a prevention overrides "works" for the same payoff.
    unique, seen = [], set()
    for f in found:
        key = (f["verdict"], tuple(f["cards"]), f["rule"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    blocked = {c for f in unique if f["rule"] == "an effect stops the event" for c in f["cards"]}
    return [f for f in unique if not (f["verdict"] == "works" and blocked & set(f["cards"]))]


PAIRS = [
    ("Flayer of the Hatebound + Chainer", ["Flayer of the Hatebound", "Chainer, Nightmare Adept"], None, "nonbo"),
    ("Flayer of the Hatebound + Animate Dead", ["Flayer of the Hatebound", "Animate Dead"], None, "works"),
    ("Doubling Season + Volo", ["Doubling Season", "Volo, Guide to Monsters"], None, "nonbo"),
    ("Doubling Season + Bitterblossom", ["Doubling Season", "Bitterblossom"], None, "works"),
    ("Twinning Staff + Shiko", ["Twinning Staff", "Shiko, Paragon of the Way"], None, "nonbo"),
    ("Twinning Staff + Reverberate", ["Twinning Staff", "Reverberate"], None, "works"),
    ("Zada + Sazacap's Brew", ["Zada, Hedron Grinder", "Sazacap's Brew"], None, "nonbo"),
    ("Zada + Expedite", ["Zada, Hedron Grinder", "Expedite"], None, "works"),
    ("Zada + Searing Wind (any target)", ["Zada, Hedron Grinder", "Searing Wind"], None, "works"),
    ("Zada + Chaos Charm (modal)", ["Zada, Hedron Grinder", "Chaos Charm"], None, "works"),
    ("River Kelpie + Prized Amalgam (Amalgam returns itself)", ["River Kelpie", "Prized Amalgam"], None, "works"),
    ("Zada + Trick Shot (optional 2nd target)", ["Zada, Hedron Grinder", "Trick Shot"], None, "works"),
    ("Zada + Savage Punch (needs two creatures)", ["Zada, Hedron Grinder", "Savage Punch"], None, "nonbo"),
    ("Notion Thief + Scrawling Crawler", ["Notion Thief", "Scrawling Crawler"], None, "nonbo"),
    ("Scrawling Crawler alone", ["Scrawling Crawler"], None, None),
    ("Arcane Signet, colorless commander", ["Arcane Signet", "Kozilek, the Great Distortion"], "Kozilek, the Great Distortion", "nonbo"),
    ("Arcane Signet, colored commander", ["Arcane Signet", "Atraxa, Praetors' Voice"], "Atraxa, Praetors' Voice", "works"),
    ("Ephemerate + Banishing Light", ["Ephemerate", "Banishing Light"], None, "note"),
    ("Torpor Orb + Banishing Light (not a creature)", ["Torpor Orb", "Banishing Light"], None, None),
    ("Ephemerate + Fiend Hunter", ["Ephemerate", "Fiend Hunter"], None, "works"),
    ("Torpor Orb + Mulldrifter", ["Torpor Orb", "Mulldrifter"], None, "nonbo"),
    ("Rest in Peace + Blood Artist", ["Rest in Peace", "Blood Artist"], None, "nonbo"),
    ("Containment Priest + Flayer + Animate Dead", ["Containment Priest", "Flayer of the Hatebound", "Animate Dead"], None, "nonbo"),
]


def main(store):
    db = relate.connect(store)
    ok = 0
    for label, deck, commander, expect in PAIRS:
        res = check(db, deck, commander)
        verdicts = {r["verdict"] for r in res}
        got = next((v for v in ("nonbo", "note", "works") if v in verdicts), None)
        passed = got == expect
        ok += passed
        print(f"[{'PASS' if passed else 'FAIL'}] {label}: expected {expect}, got {got}")
        for r in res:
            print(f"       {r['verdict'].upper():<5} ({r['rule']}) {r['why']}")
    print(f"\n{ok}/{len(PAIRS)} paired checks passed")


if __name__ == "__main__":
    main(sys.argv[1])
