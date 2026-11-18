"""Split every card into abilities and model what each one costs, produces and triggers.

    python abilities.py mtg.rl

(:Card)-[:HAS_ABILITY]->(:Ability:Activated|Triggered|Static|Spell)
(:Card)-[:GRANTS]->(:Ability)                    abilities in quotes ("creatures have ...")
(:Ability)-[:TRIGGERED_BY]->(:Event)             self_enters, creature_dies, cast_spell ...
(:Ability)-[:COSTS {amount}]->(:Resource)        mana, creature (sacrifice), artifact ...
(:Ability)-[:PRODUCES {amount, ...}]->(:Resource)
(:Ability)-[:USES]->(:Resource)                  payoffs that aren't triggers ("for each artifact")
(:Ability)-[:RETRIGGERS {scope, immediate}]->(:Event)   flicker, reanimation, sacrifice ...
(:Ability:Static)-[:MODIFIES]->(:Event)          "triggers an additional time"
(:Ability)-[:RETURNS_FROM]->(:Ability)           old two-trigger exile wording (Fiend Hunter)

Scope on RETRIGGERS: 'self' (only this card), 'other' (other cards), 'granted'
(an ability given to other creatures, so 'this creature' means the holder).
"""
import re
import sys
import time

import relate

from tag_functions import RULES, matches, mana_value, mana_yield, WORDS, normalise

SELF = r"(?:~|this (?:creature|artifact|enchantment|land|permanent|card|spell|vehicle|aura|equipment|saga))"
C = re.compile


def num(word):
    if word is None:
        return None
    word = word.strip()
    if word.isdigit():
        return int(word)
    return WORDS.get(word)


# --- splitting ----------------------------------------------------------------

TRIGGER_START = C(r"^(?:when|whenever|at the beginning|at end of|at the end of)\b")
ABILITY_WORD = C(r"^[a-z' -]{2,30} — (?=when|whenever|at |\{)")
ACTIVATED = C(r'^([^:"]{1,120}?):\s+(.+)$', re.S)
COST_HINT = C(r"\{|^[+−-]\d|^[+−-]x|^0$|^sacrifice|^discard|^exile|^pay|^remove|^tap|^return|^put")


def split_abilities(text, keywords):
    """Yield (ability text, granted?) for one card's normalised rules text."""
    kw = {k.lower() for k in keywords or []}
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line == "//":
            continue
        first = re.split(r"[ ,]", line, maxsplit=1)[0]
        if first in kw and ":" not in line and not TRIGGER_START.match(line):
            continue                                  # keyword line, already a Keyword node
        line = ABILITY_WORD.sub("", line)
        for m in re.finditer(r'"([^"]+)"', line):
            quoted = m.group(1)
            if ":" in quoted or TRIGGER_START.match(quoted):
                # "gains" = until end of turn: lost when the card changes zones, so it can't loop
                temporary = bool(re.search(r"\bgains?\s*$", line[:m.start()]))
                yield quoted.strip(), ("temporary" if temporary else True)
        # Two triggers sometimes share a line (Animate Dead): split at "When ...".
        for part in re.split(r"(?<=\.)\s+(?=(?:when|whenever|at the beginning)\b)", line):
            yield part.strip(), False


def classify(text, is_spell):
    if TRIGGER_START.match(text):
        return "Triggered"
    m = ACTIVATED.match(text)
    if m and COST_HINT.search(m.group(1)):
        return "Activated"
    return "Spell" if is_spell else "Static"


# --- events -----------------------------------------------------------------------

EVENTS = [
    ("self_enters", C(rf"{SELF}(?: or [^,]*)? enters|{SELF} enters")),
    ("self_dies", C(rf"{SELF}(?: or [^,]*)? dies")),
    ("self_leaves", C(rf"{SELF} leaves the battlefield")),
    ("creature_enters", C(r"(?:a|another|one or more|an?other) (?:nontoken )?(?:[\w-]+ )?creatures? (?:you control )?enters?")),
    ("creature_dies", C(r"(?:a|another|one or more) (?:nontoken )?(?:[\w-]+ )?creatures?[^,]* dies|creatures? (?:you control )?die\b")),
    # "Whenever enchanted creature / this creature / a source you control deals damage to an opponent"
    # Non-combat damage only (combat can't repeat in a loop). Damage dealt by this card itself is its own event.
    ("self_deals_damage", C(r"(?:whenever|when) (?:~|this creature) deals (?:noncombat )?damage to (?:an opponent|a player|target player|one or more (?:players|opponents)|each opponent)")),
    ("deals_damage", C(r"(?:enchanted creature|equipped creature|paired|the creature it's paired with|a source you control|another source)[^,]* deals (?:noncombat )?damage to (?:an opponent|a player|target player|one or more (?:players|opponents)|each opponent)")),
    # "a/another creature leaves", never "this creature leaves" (that's self_leaves)
    ("creature_leaves", C(r"(?:a|another|one or more|each|other) (?:nontoken )?(?:[\w-]+ )?creatures?[^,]* leaves? the battlefield|(?:a|another|one or more) [^,]*(?:is|are) exiled|exiled from the battlefield")),
    ("land_enters", C(r"a land (?:you control )?enters")),
    ("artifact_enters", C(r"(?:whenever|when) (?:a|an|another|one or more) (?:nontoken )?artifacts? (?:you control )?enters?")),
    # "counters are put on this creature": only counters on this card fire it (Basking Broodscale)
    ("self_counters_placed", C(r"(?:whenever|when) (?:one or more )?(?:\+1/\+1 )?counters? (?:is|are) put on (?:~|this creature|this permanent|it)\b")),
    ("counters_placed", C(r"(?:whenever|when) (?:one or more )?\+1/\+1 counters? (?:is|are) put on (?!~|this creature|this permanent|it\b)|(?:whenever|when) you put one or more (?:\+1/\+1 )?counters? on")),
    ("cast_spell", C(r"whenever you cast")),
    ("gain_life", C(r"you gain life")),
    ("draw", C(r"whenever you draw")),
    ("token_created", C(r"whenever you create")),
    ("each_turn", C(r"at the beginning of|at end of|at the end of")),
    ("combat", C(r"attacks|deals combat damage")),
]


# --- effects --------------------------------------------------------------------

FLICKER = C(r"exile [^\n]*?(?:creature|permanent|~|artifact)[^\n]*?return (?:it|that card|them|those cards|that creature|the exiled card|each card exiled this way) to the battlefield")
REANIMATE = C(r"return (?:up to \w+ )?(?:target |enchanted )?(?:[\w-]+ )?creature cards?(?: with [^.]*?)? (?:from (?:a|your) graveyard )?to the battlefield"
              r"|put (?:up to \w+ )?(?:target |enchanted )?creature cards?(?: with [^.]*?)? (?:from (?:a|your) graveyard )?onto the battlefield")
# Exiling a permanent makes it leave the battlefield (firing its own "leaves" trigger, e.g. Animate Dead's).
EXILE_PERMANENT = C(r"exile (?:up to one |another |any number of other |up to \w+ )?(?:target )?(?:nonland |nontoken |other )*(?:[\w-]+ )?(?:permanents?|creatures?|artifacts?|enchantments?)(?: or (?:artifacts?|creatures?|enchantments?))?")
# "Until this leaves the battlefield": the exiled cards come back when the exiler leaves (a hidden second trigger).
UNTIL_LEAVES = C(rf"exile[^.]*until {SELF} leaves the battlefield")
RETURN_EXILED = C(r"return the exiled (?:cards?|permanents?) to the battlefield")
EXILE_OWN = C(r"exile (?:all|each) other (?:nonland )?permanents you control")
SAC_OTHER = C(r"that creature's controller sacrifices it|sacrifice (?:the )?enchanted creature")
DOUBLER = C(r"(?:entering|enters)[^\n]*triggers an additional time")
DIES_DOUBLER = C(r"(?:dying|dies)[^\n]*triggers? an additional time")
DMG = C(r"deals? (\w+) damage to (any target|each opponent|target player|target opponent|each player|target player or planeswalker|target creature|target creature or planeswalker|each creature)|each opponent loses (\w+) life")
REMOVAL = C(r"(?:destroy|exile) (?:up to one |another |up to \w+ )?target (?:nonland |nontoken |[\w-]+ )*(?:creature|permanent|artifact|enchantment|planeswalker)")
DRAW = C(r"(?<!opponent )\bdraws? (a|an|two|three|four|five|six|seven|x|that many) cards?")
PUTBACK = C(r"put (a|two|three|x) cards? from your hand")
DISCARD_N = C(r"\bdiscard (a|two|three|x) cards?")
OPP_CHOICE = C(r"target opponent may|an opponent (?:may|chooses)|unless (?:any|that|target) (?:player|opponent)")
TAP_COST = C(r"\{t\}")


TOKEN = C(r"\bcreate (a|an|one|two|three|four|five|six|x|that many|\w+) ([^.]*?)\btokens?\b")
SELF_RETURN = C(rf"return {SELF} (?:card )?from your graveyard to the battlefield")
DIES_RETURN = C(r"return (?:it|that card|that creature|those cards) to the battlefield")
ARTIFACT_TOKENS = ("clue", "food", "blood", "map", "artifact", "powerstone", "gold", "junk", "treasure")


def effects(text, kind, card):
    """Products, uses, costs and retriggers of one ability's effect text."""
    prod, uses, costs, retrig, extra = {}, set(), {}, [], {}
    cost_text, effect = "", text
    if kind == "Activated":
        cost_text, effect = ACTIVATED.match(text).groups()
        mv = mana_value(cost_text)
        if mv:
            costs["mana"] = mv
        if re.search(r"sacrifice (?:a|another|one or more|two) (?:other )?(?:nontoken )?(?:[\w-]+ )?creatures?", cost_text):
            costs["creature"] = 1
        if re.search(r"sacrifice (?:an|another) (?:[\w-]+ )?artifacts?", cost_text):
            costs["artifact"] = 1
        # Paying life (Yawgmoth): a cost only lifegain in the loop can repay
        lm = re.search(r"pay (\d+|x) life", cost_text)
        if lm:
            costs["lifegain"] = int(lm.group(1)) if lm.group(1).isdigit() else 1
        # Spending counters: "remove a +1/+1 counter from this creature" (Walking Ballista, Spike Feeder)
        cm = re.search(r"remove (a|an|one|two|three|x|\d+) \+1/\+1 counters?", cost_text)
        if cm:
            costs["counters"] = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "x": 1}.get(cm.group(1), int(cm.group(1)) if cm.group(1).isdigit() else 1)
        # "Tap an untapped creature you control" (Earthcraft): a creature that entered this lap is untapped
        tm = re.search(r"tap (an|one|two|three|x|\d+) untapped (?:[\w-]+ )?creatures? you control", cost_text)
        if tm:
            costs["untapped_creature"] = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "x": 1}.get(tm.group(1), int(tm.group(1)) if tm.group(1).isdigit() else 1)
        extra.update(costText=cost_text, manaValue=mv, tapCost=bool(TAP_COST.search(cost_text)))
        if "creature" in costs:                       # sacrifice outlet: fires death triggers
            retrig += [("self_dies", "other", True, ["creature"]), ("creature_dies", "any", True, ["creature"])]
        if re.search(rf"sacrifice {SELF}", cost_text) and "Creature" in (card["types"] or []):
            retrig += [("self_dies", "self", True), ("creature_dies", "any", True)]
    elif kind == "Triggered":
        clause, _, effect = text.partition(", ")
        pay = re.search(r"\bpay ((?:\{[^}]+\})+)", effect)
        if pay and mana_value(pay.group(1)):
            costs["mana"] = mana_value(pay.group(1))     # Nim Deathmantle: "you may pay {4}"

    for res, role, rule, rx in RULES:
        if res in ("flicker", "etb", "death") or not matches(rule, rx, effect):
            continue
        if role == "PRODUCES" and res not in prod:
            prod[res] = {}
        elif role == "USES":
            uses.add(res)

    d = DRAW.search(effect)
    if d:
        n = num(d.group(1))
        back = PUTBACK.search(effect)
        disc = DISCARD_N.search(effect)
        net = None if n is None else n - (num(back.group(1)) or 0 if back else 0) - (num(disc.group(1)) or 0 if disc else 0)
        prod["card_draw"] = {"amount": n, "net": net,
                             "discardsHand": "discard your hand" in effect,
                             "opponentChoice": bool(OPP_CHOICE.search(effect))}
    my = mana_yield(effect)
    if my and "mana" in prod:
        prod["mana"] = {"amount": my}
    dm = DMG.search(effect)
    if dm:
        amount = dm.group(1) or dm.group(3)
        target = dm.group(2) or "each opponent"
        res = "damage_players" if any(t in target for t in ("any target", "opponent", "player")) else "removal"
        prod[res] = {"amount": num(amount), "variable": num(amount) is None}
    if REMOVAL.search(effect) and not EXILE_OWN.search(effect):
        prod.setdefault("removal", {})
        if re.search(rf"until {SELF} leaves the battlefield", effect):
            prod["removal"]["duration"] = "until_source_leaves"

    tm = TOKEN.search(effect)
    if tm:
        n = num(tm.group(1)) or 1
        desc = tm.group(2)
        prod["token"] = {"amount": n}
        if "creature" in desc or re.search(r"\d+/\d+", desc) or "copy of" in desc:
            prod["creature"] = {"amount": n}
            retrig.append(("creature_enters", "any", True))
            if "copy of" in desc:                      # a copy enters with the original's abilities
                retrig.append(("self_enters", "copy", True))
            prod["creature"]["token"] = True
            prod["creature"]["tokenDesc"] = desc.strip()
        if "treasure" in desc:
            prod["mana"] = {"amount": n}
        if any(w in desc for w in ARTIFACT_TOKENS):
            prod["artifact"] = {"amount": n}

    fm = FLICKER.search(effect)
    if not fm and (SELF_RETURN.search(effect) or (kind == "Triggered" and DIES_RETURN.search(effect))):
        clause = text.partition(", ")[0]
        self_scope = bool(SELF_RETURN.search(effect)) or bool(re.search(rf"{SELF} dies", clause))
        retrig += [("self_enters", "self" if self_scope else "other", True), ("creature_enters", "any", True)]
        prod["creature"] = {"amount": 1}               # the card comes back: more fodder
    if fm:
        self_only = re.match(r"exile (?:~|this (?:creature|permanent|artifact))(?!\w)", fm.group(0)) is not None
        after = effect[fm.start():fm.end() + 80]
        # "...at the beginning of the next end step" or "...when this artifact leaves the battlefield" is delayed.
        immediate = "at the beginning of the next" not in after and not re.search(r"when (?:~|this \w+) leaves the battlefield", after)
        scope = "self" if self_only else "other"
        ftypes = None if self_only else (sorted(set(re.findall(r"permanent|creature|artifact|enchantment", fm.group(0)))) or None)
        for ev in ("self_leaves", "self_enters"):
            retrig.append((ev, scope, immediate, ftypes))
        retrig += [("creature_leaves", "any", immediate, ftypes), ("creature_enters", "any", immediate, ftypes)]
    # "Return this card to its owner's hand" -- and "return a creature you control to its owner's hand" on a creature,
    # which can choose itself (Shrieking Drake): it leaves, and casting it again makes it enter.
    if re.search(rf"return (?:{SELF}|a creature you control|another creature you control|target creature you control) to (?:its owner's|your) hand", effect) \
            and not re.search(r"return another creature", effect) and "Creature" in (card.get("types") or []):
        retrig += [("self_leaves", "self", True), ("self_enters", "self", True)]
        costs["mana"] = costs.get("mana", 0) + int(card.get("cmc") or 0)      # cast it again
    if re.search(r"create (?:a|one|x|two|that many) tokens? that(?:'s| are) (?:a )?cop(?:y|ies) of (?:target|another|enchanted|equipped|that|it|~|each)", effect):
        retrig += [("self_enters", "copy", True, ["creature"]), ("creature_enters", "any", True)]     # Kiki-Jiki
        prod.setdefault("creature", {"amount": 1, "token": True})
    em = EXILE_PERMANENT.search(effect)
    sentence = effect[em.start():effect.find(".", em.start()) if em and "." in effect[em.start():] else None] if em else ""
    opponents_only = bool(re.search(r"an opponent controls|you don't control|opponents control", sentence))
    whole_card = (card.get("text") or "").lower()
    returns_it = re.search(r"until [^.]*leaves|return (?:the exiled|that card|those cards|it|them|the cards)|exiled cards? (?:return|to the battlefield)", whole_card)
    if not fm and em and not opponents_only and returns_it and "graveyard" not in effect[:em.end() + 20]:
        types = sorted(set(re.findall(r"permanent|creature|artifact|enchantment", em.group(0))))
        retrig += [("self_leaves", "other", True, types), ("creature_leaves", "any", True, types)]
    rm = REANIMATE.search(effect)
    if rm and not RETURN_EXILED.search(effect):          # Karmic Guide returns creature cards only
        rtypes = sorted(set(re.findall(r"creature|permanent|artifact|enchantment", rm.group(0)))) or None
        retrig += [("self_enters", "other", True, rtypes), ("creature_enters", "any", True, rtypes)]
    if re.search(rf"return {SELF}\b[^.]*to the battlefield", effect) and "graveyard" in effect:
        retrig += [("self_enters", "self", True), ("enters_from_graveyard", "self", True), ("creature_enters", "any", True)]
    if RETURN_EXILED.search(effect):                  # "return the exiled cards": whatever was exiled
        retrig += [("self_enters", "other", True), ("creature_enters", "any", True)]
    if REANIMATE.search(effect) or RETURN_EXILED.search(effect):
        prod.setdefault("creature", {"amount": 1})
    if EXILE_OWN.search(effect):
        retrig += [("self_leaves", "other", True), ("creature_leaves", "any", True)]
    if SAC_OTHER.search(effect):                     # "that creature's controller sacrifices it": a creature
        retrig += [("self_dies", "other", True, ["creature"]), ("self_leaves", "other", True, ["creature"]),
                   ("creature_dies", "any", True, ["creature"])]
    return prod, uses, costs, retrig, extra


def text_flags(text, kind):
    """Limits read from an ability's own wording, applied to rule and LLM readings alike."""
    flags = {}
    clause = text.partition(", ")[0] if kind == "Triggered" else ""
    if kind == "Triggered" and "without flying" in clause and "flying counter" in text:
        flags["limitedBy"] = "flying"            # Luminous Broodmoth: once per creature
    if kind == "Triggered" and re.search(r"it's an enchantment|it's no longer a creature|if it was a creature", text):
        flags["limitedBy"] = "once"              # Enduring Innocence: comes back as something else
    if kind == "Activated" and re.match(r"^[+−-]\s*(?:\d+|x)\s*:|^0\s*:", text):
        flags["oncePerTurn"] = True              # planeswalker loyalty abilities
    if re.search(r"only once each turn", text):
        flags["oncePerTurn"] = True
    # Valkyrie's Call: returns "non-Angel" creatures as Angels, so each comes back once.
    # Evolve only triggers for a BIGGER creature entering: in a loop of the same small tokens it can't repeat.
    if kind == "Triggered" and re.search(r"^evolve\b|evolve \(|with greater power or toughness enters", text):
        flags["limitedBy"] = "once"
    for nx in re.finditer(r"\bnon-?([a-z]+)\b", text.split(".")[0]):      # the whole first sentence: commas split clauses
        w = nx.group(1)
        if kind == "Triggered" and w not in ("token", "land", "artifact", "creature", "legendary") and \
                re.search(rf"it's an? {w}|{w} in addition to its other types", text):
            flags["limitedBy"] = "once"
    # Persist / undying spelled out without the keyword (an LLM reading may quote only the reminder text).
    if kind == "Triggered" and re.search(r"if it had no -1/-1 counters? on it", text):
        flags["limitedBy"] = "-1/-1"
    elif kind == "Triggered" and re.search(r"if it had no \+1/\+1 counters? on it", text):
        flags["limitedBy"] = "+1/+1"
    return flags


# --- build ------------------------------------------------------------------------

EXPORT = """
MATCH (c:Card)
RETURN c.oracleId AS id, c.name AS name, c.oracleText AS text, c.types AS types, c.keywords AS keywords, c.cmc AS cmc
"""


def build_rows(cards):
    rows = {k: [] for k in ("ability", "has", "grants", "trig", "prod", "uses", "costs", "retrig", "mod", "link")}
    for card in cards:
        text = normalise(card)
        is_spell = bool({"Instant", "Sorcery"} & set(card["types"] or []))
        card_abilities = []
        for i, (atext, granted) in enumerate(split_abilities(text, card["keywords"])):
            kind = classify(atext, is_spell)
            aid = f"{card['id']}#{i}"
            prod, uses, costs, retrig, extra = effects(atext, kind, card)
            retrig = [r if len(r) == 4 else (*r, None) for r in retrig]          # (event, scope, immediate, target types)
            # Loyal Cathar: "return it ... at the beginning of the next end step". (Kiki-Jiki's "sacrifice it at
            # the beginning of the next end step" delays nothing about the copy entering.)
            if re.search(r"return[^.]*at the beginning of the next|at the beginning of the next[^.]*return", atext):
                retrig = [(e, s, False if e in ("self_enters", "creature_enters", "enters_from_graveyard") else imm, ty)
                          for e, s, imm, ty in retrig]
            if granted:
                retrig = [(e, "granted" if s == "self" else s, imm, ty) for e, s, imm, ty in retrig]
            events = []
            if kind == "Triggered":
                clause = atext.partition(", ")[0]
                events = [name for name, rx in EVENTS if rx.search(clause)]
            if granted == "temporary":
                extra = {**extra, "temporary": True}
            if {"Instant", "Sorcery"} & set(card.get("types") or []):
                kind = "Spell"                  # every part of an instant or sorcery is the one-shot spell
            extra = {**extra, **text_flags(atext, kind)}
            if re.match(r"^(?:i|ii|iii|iv|v|vi)(?:, (?:i|ii|iii|iv|v|vi))* —", atext):
                extra = {**extra, "limitedBy": "once"}          # a Saga chapter happens once
            rows["ability"].append({"id": aid, "kind": kind, "text": atext, "props": extra})
            rows["grants" if granted else "has"].append({"card": card["id"], "id": aid})
            clause = atext.partition(", ")[0]
            nontoken = "nontoken" in clause
            ex = re.search(r"non-([a-z]+) creature", clause)
            excludes = ex.group(1) if ex and ex.group(1) != "token" else None
            rows["trig"] += [{"id": aid, "event": e, "nontoken": nontoken, "excludes": excludes} for e in events]
            rows["prod"] += [{"id": aid, "res": r, "props": {k: v for k, v in p.items() if v is not None}} for r, p in prod.items()]
            rows["uses"] += [{"id": aid, "res": r} for r in uses]
            rows["costs"] += [{"id": aid, "res": r, "amount": a} for r, a in costs.items()]
            rows["retrig"] += [{"id": aid, "event": e, "scope": s, "immediate": imm, "types": ty} for e, s, imm, ty in retrig]
            if kind == "Static" and DOUBLER.search(atext):
                rows["mod"] += [{"id": aid, "event": e} for e in ("self_enters", "creature_enters")]
            if kind == "Static" and DIES_DOUBLER.search(atext):
                rows["mod"] += [{"id": aid, "event": e} for e in ("self_dies", "creature_dies")]
            card_abilities.append((aid, kind, events, prod, atext))
        for kw, limit in (("Persist", "-1/-1"), ("Undying", "+1/+1")):
            if kw in (card["keywords"] or []):
                aid = f"{card['id']}#{kw.lower()}"
                rows["ability"].append({"id": aid, "kind": "Triggered",
                                        "text": f"{kw.lower()} (returns when it dies without a {limit} counter)",
                                        "props": {"limitedBy": limit}})
                rows["has"].append({"card": card["id"], "id": aid})
                rows["trig"].append({"id": aid, "event": "self_dies", "nontoken": False, "excludes": None})
                rows["retrig"] += [{"id": aid, "event": e, "scope": sc, "immediate": True}
                                   for e, sc in (("self_enters", "self"), ("creature_enters", "any"))]
                rows["prod"].append({"id": aid, "res": "creature", "props": {"amount": 1}})
        # "You may cast this card from your graveyard" (Gravecrawler): once it dies, cast it again for its mana cost.
        whole = normalise({"name": card["name"], "text": card["text"] or ""})
        if re.search(r"you may cast (?:~|this card|this creature) from your graveyard", whole) and "Creature" in (card.get("types") or []):
            aid = f"{card['id']}#castfromgraveyard"
            cond = re.search(r"as long as you control (?:a|an|another) ([a-z-]+)", whole)
            rows["ability"].append({"id": aid, "kind": "Triggered",
                                    "text": "when ~ dies, you may cast it again from your graveyard" + (f" (as long as you control a {cond.group(1)})" if cond else ""),
                                    "props": {}})
            rows["has"].append({"card": card["id"], "id": aid})
            rows["trig"].append({"id": aid, "event": "self_dies", "nontoken": False, "excludes": None})
            rows["costs"].append({"id": aid, "res": "mana", "amount": int(card.get("cmc") or 0)})
            rows["prod"].append({"id": aid, "res": "creature", "props": {"amount": 1}})
            rows["retrig"] += [{"id": aid, "event": e, "scope": s, "immediate": True, "types": None}
                               for e, s in (("self_enters", "self"), ("creature_enters", "any"))]
        # "Exile ... until this leaves the battlefield": add the implied return as its own ability.
        for aid0, kind0, events0, prod0, text0 in list(card_abilities):
            if UNTIL_LEAVES.search(text0) and not re.search(r"an opponent controls|you don't control|opponents control", text0):
                aid = f"{aid0}#returns"
                rows["ability"].append({"id": aid, "kind": "Triggered",
                                        "text": "when ~ leaves the battlefield, the cards it exiled return (implied by \"until ~ leaves\")",
                                        "props": {}})
                rows["has"].append({"card": card["id"], "id": aid})
                rows["trig"].append({"id": aid, "event": "self_leaves", "nontoken": False, "excludes": None})
                rows["retrig"] += [{"id": aid, "event": e, "scope": sc, "immediate": True}
                                   for e, sc in (("self_enters", "other"), ("creature_enters", "any"))]
        # Old two-trigger exile wording: an enters ability exiles, a separate leaves
        # ability returns "the exiled card". Flickering in response exiles for good.
        enter = [a for a in card_abilities if "self_enters" in a[2] and "removal" in a[3]]
        leave = [a for a in card_abilities if "self_leaves" in a[2] and "return the exiled" in a[4]]
        for e in enter:
            for l in leave:
                rows["link"].append({"from": l[0], "to": e[0]})
    return rows


WRITES = {
    "has": "UNWIND $rows AS r MATCH (c:Card {oracleId: r.card}), (a:Ability {id: r.id}) MERGE (c)-[:HAS_ABILITY]->(a)",
    "grants": "UNWIND $rows AS r MATCH (c:Card {oracleId: r.card}), (a:Ability {id: r.id}) MERGE (c)-[:GRANTS]->(a)",
    "trig": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (e:Event {name: r.event}) MERGE (a)-[t:TRIGGERED_BY]->(e) SET t.nontoken = r.nontoken, t.excludesType = r.excludes",
    "prod": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (x:Resource {name: r.res}) MERGE (a)-[p:PRODUCES]->(x) SET p += r.props",
    "uses": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (x:Resource {name: r.res}) MERGE (a)-[:USES]->(x)",
    "costs": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (x:Resource {name: r.res}) MERGE (a)-[c:COSTS]->(x) SET c.amount = r.amount",
    "retrig": """UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (e:Event {name: r.event})
                 MERGE (a)-[t:RETRIGGERS {scope: r.scope}]->(e) SET t.immediate = r.immediate, t.targetTypes = r.types""",
    "mod": "UNWIND $rows AS r MATCH (a:Ability {id: r.id}) MERGE (e:Event {name: r.event}) MERGE (a)-[:MODIFIES]->(e)",
    "link": "UNWIND $rows AS r MATCH (l:Ability {id: r.from}), (e:Ability {id: r.to}) MERGE (l)-[:RETURNS_FROM]->(e)",
}


def write(db, query, rows):
    for i in range(0, len(rows), 2000):
        db.execute(query, {"rows": rows[i:i + 2000]})


def main(store):
    t0 = time.time()
    db = relate.connect(store)
    for stmt in ["CREATE CONSTRAINT ability_id IF NOT EXISTS FOR (a:Ability) REQUIRE a.id IS UNIQUE",
                 "CREATE CONSTRAINT event_name IF NOT EXISTS FOR (e:Event) REQUIRE e.name IS UNIQUE",
                 "MATCH (a:Ability) DETACH DELETE a"]:
        db.execute(stmt)
    rows = build_rows(list(db.execute(EXPORT)))
    for kind in ("Activated", "Triggered", "Static", "Spell"):            # labels can't be parameters
        write(db, f"UNWIND $rows AS r CREATE (a:Ability:{kind} {{id: r.id, text: r.text}}) SET a += r.props",
              [r for r in rows["ability"] if r["kind"] == kind])
    for r in rows["retrig"]:
        r.setdefault("types", None)
    for name, query in WRITES.items():
        write(db, query, rows[name])
    counts = {k: len(v) for k, v in rows.items()}
    print(f"built {counts} ({time.time() - t0:.0f}s)")
    db.close()


if __name__ == "__main__":
    main(sys.argv[1])
