"""Load LLM-extracted abilities (extract.py output) into a copy of the graph.

    python load_llm.py mtg.rl extracted_haiku.jsonl mtg_haiku_union.rl --mode union
    python load_llm.py mtg.rl extracted_haiku.jsonl mtg_haiku_replace.rl --mode replace

replace: the card's regex abilities are removed; only the LLM's remain.
union:   the LLM's abilities are added next to the regex ones.

The LLM reads the card; a few deterministic rules then tidy its answer into the
engine's schema (vocabulary mapping, plus links it tends to omit, such as "returns
a creature to the battlefield" also meaning that creature *enters*).
"""
import argparse
import json
import re
import shutil
import time

import relate

from tag_functions import normalise
from abilities import EVENTS as TEXT_EVENTS, text_flags

EVENT_MAP = {"you_gain_life": "gain_life", "you_draw": "draw", "attacks": "combat", "combat_damage": "combat"}
RESOURCE_MAP = {"plus_counters": "counters"}
KNOWN_RESOURCES = {"creature", "token", "artifact", "mana", "card_draw", "graveyard", "counters", "minus_counters",
                   "lifegain", "life_loss_opponents", "damage_players", "removal", "landfall", "untap", "spell_copy",
                   "card_copy", "return_to_hand", "cast_from_graveyard", "extra_turn", "win_game"}
RETURN_TO_BF = re.compile(r"(?:return|put)[^.]*(?:to|onto) the battlefield", re.I)
CREATURE_SPELL_COPY = re.compile(r"creature spell", re.I)


def _name(x, key):
    """Accept a plain name or an object like {"resource": "mana"} (models vary)."""
    if isinstance(x, dict):
        x = x.get(key) or x.get("name")
    return x if isinstance(x, str) else None


def ev(name):
    name = _name(name, "event")
    return EVENT_MAP.get(name, name) if name else ""


def res(name):
    name = _name(name, "resource")
    r = RESOURCE_MAP.get(name, name) if name else None
    return r if r in KNOWN_RESOURCES else None


def norm_target(t):
    t = t.lower()
    if "player" in t or "opponent" in t:
        return "player"
    for k in ("any", "creature", "permanent", "artifact", "enchantment", "land", "spell", "planeswalker"):
        if k in t:
            return k
    return t.split()[-1] if t.split() else t


def rows_for(card, types, keywords):
    """One card's LLM abilities -> graph rows (same shapes abilities.py writes)."""
    out = {k: [] for k in ("ability", "has", "grants", "trig", "prod", "uses", "costs", "retrig", "mod", "amp", "prev", "link")}
    is_creature = "Creature" in (types or [])
    ids = []
    raw = card["abilities"]
    if isinstance(raw, str):                     # some answers arrive as a JSON string, not a list
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    abilities = [a for a in raw if isinstance(a, dict)]     # skip anything else malformed
    for i, a in enumerate(abilities):
        aid = f"{card['oracleId']}#llm{i}"
        ids.append(aid)
        text = normalise({"name": card["card"], "text": a.get("text", "")})
        kind = a.get("kind") if a.get("kind") in ("Activated", "Triggered", "Static", "Spell") else "Static"
        if {"Instant", "Sorcery"} & set(types or []):
            kind = "Spell"                      # granted "when it dies this turn" too: it's part of a one-shot spell
        if kind == "Activated" and re.match(r"^(evoke|kicker|flashback|escape|buyback)\b", text):
            kind = "Static"                     # alternative costs aren't activated abilities
        cost = dict(a.get("cost")) if isinstance(a.get("cost"), dict) else {}
        if kind == "Activated" and ":" in text:          # the activation cost is printed before the colon: read it there
            pre = text.split(":")[0]
            printed = sum(int(x) if x.isdigit() else (0 if x in ("x", "t", "q") else 1) for x in re.findall(r"\{([^}]+)\}", pre))
            if printed > (cost.get("mana") or 0):
                cost["mana"] = printed                   # Azorius Signet: "{1}, {T}: add {W}{U}"
            if "{t}" in pre:
                cost["tap"] = True
            lm = re.search(r"pay (\d+|x) life", pre)
            if lm:
                cost["lifegain"] = int(lm.group(1)) if lm.group(1).isdigit() else 1
            cm = re.search(r"remove (a|an|one|two|three|x|\d+) \+1/\+1 counters?", pre)
            if cm:
                cost["counters"] = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "x": 1}.get(cm.group(1), int(cm.group(1)) if cm.group(1).isdigit() else 1)
            tm = re.search(r"tap (an|one|two|three|x|\d+) untapped (?:[\w-]+ )?creatures? you control", pre)
            if tm:
                cost["untapped_creature"] = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "x": 1}.get(tm.group(1), int(tm.group(1)) if tm.group(1).isdigit() else 1)
        if kind == "Static" and a.get("granted") and ((cost.get("mana") or 0) > 0 or cost.get("tap")) \
                and re.search(r'"[^"]*:[^"]*"', text):
            kind = "Activated"                  # the granted "{cost}: effect" is what matters for loops
        trig = a.get("trigger") if isinstance(a.get("trigger"), dict) else {}
        props = {"source": "llm"}
        if kind == "Activated":
            props.update(manaValue=cost.get("mana") or 0, tapCost=bool(cost.get("tap")))
        if a.get("temporary"):
            props["temporary"] = True
        if a.get("targets"):
            props["targets"] = [norm_target(t) for t in a["targets"]]
        if a.get("requiresTargetsOnlySelf") and "targets only" in text:      # Zada-style, from the text
            props["requiresTargetsOnlySelf"] = True
        if a.get("needsCommanderIdentity") and "color identity" in text:
            props["needsCommanderIdentity"] = True
        props.update(text_flags(text, kind))
        if re.match(r"^(?:i|ii|iii|iv|v|vi)(?:, (?:i|ii|iii|iv|v|vi))* —", text):
            props["limitedBy"] = "once"         # a Saga chapter happens once
        if kind == "Triggered" and re.search(r"^evolve\b|with greater power or toughness enters", text):
            props["limitedBy"] = "once"            # evolve needs a bigger creature each time
        if "persist" in text and "limitedBy" not in props:
            props["limitedBy"] = "-1/-1"
        elif "undying" in text and "limitedBy" not in props:
            props["limitedBy"] = "+1/+1"
        out["ability"].append({"id": aid, "kind": kind, "text": text, "props": props})
        out["grants" if a.get("granted") else "has"].append({"card": card["oracleId"], "id": aid})

        clause = text.partition(", ")[0] if kind == "Triggered" else ""
        ex = re.search(r"non-([a-z]+) creature", clause)              # read restrictions from the text itself
        excludes = (trig.get("excludesType") or (ex.group(1) if ex and ex.group(1) != "token" else None))
        excludes = excludes.lower() if isinstance(excludes, str) else None
        nontoken = bool(trig.get("nontoken")) or "nontoken" in clause
        if "Aura" in str(types) or re.search(r"enchanted (?:creature|permanent) dies", clause):
            if re.search(r"enchanted (?:creature|permanent) (?:dies|is put into)", clause):
                props["consumesSource"] = True      # the Aura goes to the graveyard with the creature
        # Prefer events read from the trigger's own wording ("when this creature leaves" is this card leaving).
        text_events = [name for name, rx in TEXT_EVENTS if rx.search(clause)] if clause else []
        if not text_events and re.search(r"^(?:when|whenever) (?:an|each|target) opponent (?:casts|activates|attacks|plays|searches|cycles)\b", clause):
            text_events = ["opponent_acts"]      # Opal Caryatid: an opponent's cast is nothing your loop can cause
        for e in (text_events or [ev(x) for x in trig.get("events") or [] if ev(x) not in ("other", "")]):
            out["trig"].append({"id": aid, "event": e, "nontoken": nontoken, "excludes": excludes})
        produced = set()
        for p in a.get("produces") or []:
            r = res(p.get("resource"))
            if not r:
                continue
            produced.add(r)
            pp = {k: p[k] for k in ("amount", "token", "tokenDesc", "net", "duration", "variable") if p.get(k) is not None}
            if r == "card_draw":                 # net cards from the wording, not the model's guess
                if re.search(r"discard (?:your|their) hand", text):
                    pp.pop("net", None)          # Ox of Agonas: net depends on your hand
                else:
                    dm = re.search(r"then discard (a|one|two|three|\d+) cards?", text)
                    if dm and isinstance(pp.get("amount"), int):
                        pp["net"] = pp["amount"] - {"a": 1, "one": 1, "two": 2, "three": 3}.get(dm.group(1), int(dm.group(1)) if dm.group(1).isdigit() else 1)
            if pp.get("duration") == "until_source_leaves" and "until" not in text:
                del pp["duration"]                 # two-trigger wording (Fiend Hunter) has no "until"
            out["prod"].append({"id": aid, "res": r, "props": pp})
            if r == "spell_copy" and CREATURE_SPELL_COPY.search(text):
                props["copiesCreatureSpells"] = True
        # One Treasure is often described twice (as a token and as an artifact): count it once.
        treasures = max([(p.get("amount") or 1) for p in a.get("produces") or []
                         if isinstance(p, dict) and "treasure" in str(p.get("tokenDesc", "")).lower()] or [0])
        if treasures and "mana" not in produced:
            produced.add("mana")
            out["prod"].append({"id": aid, "res": "mana", "props": {"amount": treasures}})
        if RETURN_TO_BF.search(text) and re.search(r"(?:creature|permanent) cards?|this card", text) \
                and "creature" not in produced:
            produced.add("creature")
            out["prod"].append({"id": aid, "res": "creature", "props": {"amount": 1}})
        for u in a.get("uses") or []:
            if res(u):
                out["uses"].append({"id": aid, "res": res(u)})
        pay = re.search(r"\bpay ((?:\{[^}]+\})+)", text) if kind == "Triggered" else None
        trig_mana = (cost.get("mana") or 0) or (sum(int(x) if x.isdigit() else (0 if x in "xt" else 1)
                                                    for x in re.findall(r"\{([^}]+)\}", pay.group(1))) if pay else 0)
        if kind in ("Activated", "Spell") and (cost.get("mana") or 0) > 0:
            out["costs"].append({"id": aid, "res": "mana", "amount": cost["mana"]})
        elif kind == "Triggered" and trig_mana:
            out["costs"].append({"id": aid, "res": "mana", "amount": trig_mana})
        sac = cost.get("sacrifice")
        if sac in ("creature", "artifact"):
            out["costs"].append({"id": aid, "res": sac, "amount": 1})
        for res_name in ("counters", "untapped_creature", "lifegain"):           # read from the printed cost, above
            if kind == "Activated" and cost.get(res_name):
                out["costs"].append({"id": aid, "res": res_name, "amount": int(cost[res_name])})

        reanimate_types = (sorted(set(re.findall(r"(creature|permanent|artifact|enchantment) cards?", text))) or None) \
            if RETURN_TO_BF.search(text) else None
        retrig = [(ev(r["event"]), r.get("scope", "any"), r.get("immediate", True)) for r in a.get("retriggers") or []
                  if isinstance(r, dict) and ev(r.get("event")) not in ("other", "")]
        # The same text checks the rules use: opponent-only exile can't move your cards; delayed returns aren't immediate.
        if re.search(r"an opponent controls|you don't control|opponents control", text):
            retrig = [(e, s, imm) for e, s, imm in retrig if not (e in ("self_leaves", "self_dies") and s != "self")]
        # "Put a creature card from your hand onto the battlefield" (Bane, Sneak Attack) uses up your hand:
        # it can't bring back the creature the loop just sacrificed.
        if re.search(r"from your hand onto the battlefield", text) and "graveyard" not in text:
            retrig = [(e, s, imm) for e, s, imm in retrig if not (e in ("self_enters", "enters_from_graveyard", "creature_enters") and s != "self")]
            out["prod"] = [p for p in out["prod"] if not (p["id"] == aid and p["res"] == "creature")]   # a finite supply
        if re.search(r"at the beginning of the next|when (?:~|this \w+) leaves the battlefield, return", text):
            retrig = [(e, s, False) if e in ("self_enters", "creature_enters") else (e, s, imm) for e, s, imm in retrig]
        if a.get("granted"):
            retrig = [(e, "granted" if s == "self" else s, imm) for e, s, imm in retrig]
        # Deterministic cleanup: links the LLM tends to leave out.
        if sac == "creature":
            retrig += [("self_dies", "other", True), ("creature_dies", "any", True)]
        if sac == "self" and is_creature:
            retrig += [("self_dies", "self", True), ("creature_dies", "any", True)]
        if "creature" in produced and RETURN_TO_BF.search(text) and not any(e == "self_enters" for e, _, _ in retrig):
            self_return = re.search(r"return (?:~|this card|this creature|it)\b", text) and "self_dies" in (trig.get("events") or [])
            retrig += [("self_enters", "self" if self_return else "other", True), ("creature_enters", "any", True)]
            if "graveyard" in text or "dies" in text:
                retrig.append(("enters_from_graveyard", "other", True))
        if any(p.get("token") for p in a.get("produces") or [] if res(p.get("resource")) == "creature"):
            retrig.append(("creature_enters", "any", True))
        exiled = re.search(r"exile [^.]*", text)          # every type word in the exile phrase
        exile_types = sorted(set(re.findall(r"permanent|creature|artifact|enchantment", exiled.group(0)))) or None if exiled else None
        # (Runs after every cleanup rule, which could add the link back.) Reanimating from an opponent's graveyard (Puppeteer Clique) can't bring back your own cards.
        if re.search(r"opponent's graveyard", text) and "your graveyard" not in text:
            retrig = [(e, s, imm) for e, s, imm in retrig if not (e in ("self_enters", "enters_from_graveyard") and s != "self")]
        # "Put a creature card from your hand onto the battlefield" (Bane, Sneak Attack) uses up your hand:
        # it can't bring back the creature the loop just sacrificed.
        if re.search(r"from your hand onto the battlefield", text) and "graveyard" not in text:
            retrig = [(e, s, imm) for e, s, imm in retrig if not (e in ("self_enters", "enters_from_graveyard", "creature_enters") and s != "self")]
            out["prod"] = [p for p in out["prod"] if not (p["id"] == aid and p["res"] == "creature")]   # a finite supply
        if re.search(r"return[^.]*at the beginning of the next|at the beginning of the next[^.]*return|when (?:~|this \w+) leaves the battlefield, return", text):
            retrig = [(e, s, False) if e in ("self_enters", "creature_enters", "enters_from_graveyard") else (e, s, imm) for e, s, imm in retrig]
        # Exile for good (Solitude) can't be part of a loop: only exile that the card itself undoes can.
        whole = " ".join(str(x.get("text", "")) for x in abilities).lower()
        if "exile" in text and not re.search(r"until [^.]*leaves|return (?:the exiled|that card|those cards|it|them|the cards)|exiled cards? (?:return|to the battlefield)", whole):
            retrig = [(e, s, imm) for e, s, imm in retrig if not (e in ("self_leaves", "creature_leaves") and s != "self")]
        # "Return this card ..." only ever moves this card.
        if re.search(r"return (?:~|this card|this creature)\b", text) and not re.search(r"return (?:up to \w+ )?(?:another |other )?target", text):
            retrig = [(e, "self" if e in ("self_enters", "self_leaves", "self_dies", "enters_from_graveyard") else s, imm)
                      for e, s, imm in retrig]
        seen = set()
        for e, s, imm in retrig:
            if (e, s) not in seen:
                seen.add((e, s))
                ty = exile_types if e.startswith("self_") and s != "self" else None
                if e == "self_enters" and s == "other" and reanimate_types:
                    ty = reanimate_types
                out["retrig"].append({"id": aid, "event": e, "scope": s, "immediate": imm, "types": ty})

        for x in a.get("amplifies") or []:
            x = _name(x, "resource") or _name(x, "event")
            if not x:
                continue
            # Trigger doublers are static and say "an additional time" (Panharmonicon, Teysa).
            if x in ("self_enters", "creature_enters", "self_dies", "creature_dies"):
                if kind == "Static" and "additional time" in text:
                    out["mod"].append({"id": aid, "event": x})
            elif res(x) in ("token", "spell_copy") and kind == "Static":
                out["amp"].append({"id": aid, "res": res(x)})
        # Preventions are static replacement effects: "... instead", "don't cause abilities to trigger".
        real_prevention = kind == "Static" and re.search(r"\binstead\b|don't cause abilities to trigger", text)
        for p in (a.get("prevents") or []) if real_prevention else []:
            e = ev(p.get("event", ""))
            if e in ("other", ""):
                continue                        # a catch-all category never links two cards
            # "nontoken creature ... wasn't cast": only creatures put onto the battlefield.
            if e == "creature_enters" and re.search(r"wasn't cast|without being cast", text):
                e = "enters_from_graveyard"
            out["prev"].append({"id": aid, "event": e, "whose": p.get("whose", "all"), "reason": p.get("reason", "")})
    for i, a in enumerate(abilities):
        j = a.get("returnsExiledFrom")
        if isinstance(j, int) and 0 <= j < len(ids) and j != i:
            out["link"].append({"from": ids[i], "to": ids[j]})
    return out


WRITES = {
    "has": "UNWIND $rows AS r MATCH (c:Card {oracleId: r.card}), (a:Ability {id: r.id}) MERGE (c)-[:HAS_ABILITY]->(a)",
    "grants": "UNWIND $rows AS r MATCH (c:Card {oracleId: r.card}), (a:Ability {id: r.id}) MERGE (c)-[:GRANTS]->(a)",
    "trig": """UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (e:Event {name: r.event})
               MERGE (a)-[t:TRIGGERED_BY]->(e) SET t.nontoken = r.nontoken, t.excludesType = r.excludes""",
    "prod": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (x:Resource {name: r.res}) MERGE (a)-[p:PRODUCES]->(x) SET p += r.props",
    "uses": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (x:Resource {name: r.res}) MERGE (a)-[:USES]->(x)",
    "costs": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (x:Resource {name: r.res}) MERGE (a)-[c:COSTS]->(x) SET c.amount = r.amount",
    "retrig": """UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (e:Event {name: r.event})
                 MERGE (a)-[t:RETRIGGERS {scope: r.scope}]->(e) SET t.immediate = r.immediate, t.targetTypes = r.types""",
    "mod": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (e:Event {name: r.event}) MERGE (a)-[:MODIFIES]->(e)",
    "amp": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (x:Resource {name: r.res}) MERGE (a)-[:AMPLIFIES]->(x)",
    "prev": """UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (e:Event {name: r.event})
               MERGE (a)-[p:PREVENTS]->(e) SET p.whose = r.whose, p.reason = r.reason""",
    "link": "UNWIND $rows AS r MATCH (l:Ability {id: r.from}), (e:Ability {id: r.to}) MERGE (l)-[:RETURNS_FROM]->(e)",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("store")
    ap.add_argument("extracted")
    ap.add_argument("out")
    ap.add_argument("--mode", choices=["union", "replace"], default="union")
    args = ap.parse_args()
    t0 = time.time()
    shutil.copyfile(args.store, args.out)
    db = relate.connect(args.out)
    # Several extraction files may be given, comma-separated; a later file wins for the same card.
    by_id = {}
    for path in args.extracted.split(","):
        for line in open(path.strip(), encoding="utf-8"):
            row = json.loads(line)
            if row.get("abilities"):
                by_id[row["oracleId"]] = row
    cards = list(by_id.values())
    ids = [c["oracleId"] for c in cards]
    info = {r["id"]: r for r in db.execute(
        "MATCH (c:Card) WHERE c.oracleId IN $ids RETURN c.oracleId AS id, c.types AS types, c.keywords AS kw", {"ids": ids})}
    if args.mode == "replace":
        db.execute("MATCH (c:Card)-[:HAS_ABILITY|GRANTS]->(a:Ability) WHERE c.oracleId IN $ids DETACH DELETE a", {"ids": ids})
    rows = {k: [] for k in WRITES}
    rows["ability"] = []
    for c in cards:
        if c["oracleId"] not in info:
            continue
        r = rows_for(c, info[c["oracleId"]]["types"], info[c["oracleId"]]["kw"])
        for k in rows:
            rows[k] += r.get(k, [])
    for kind in ("Activated", "Triggered", "Static", "Spell"):
        part = [r for r in rows["ability"] if r["kind"] == kind]
        for i in range(0, len(part), 2000):
            db.execute(f"UNWIND $rows AS r CREATE (a:Ability:{kind} {{id: r.id, text: r.text}}) SET a += r.props",
                       {"rows": part[i:i + 2000]})
    for name, q in WRITES.items():
        for i in range(0, len(rows[name]), 2000):
            db.execute(q, {"rows": rows[name][i:i + 2000]})
    db.close()
    print(f"{args.mode}: loaded {len(rows['ability'])} LLM abilities for {len(cards)} cards into {args.out} "
          f"({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
