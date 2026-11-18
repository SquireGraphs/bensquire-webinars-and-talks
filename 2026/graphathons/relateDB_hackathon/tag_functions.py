"""Tag what each card produces and uses, from its rules text.

    python tag_functions.py mtg.rl

(:Card)-[:PRODUCES {rule}]->(:Resource)<-[:USES {rule}]-(:Card)

Synergy is a shared resource between a producer and a user; an engine is a
chain of them. Every edge records the rule that created it, so a tag can always
be explained. Rules are regexes over normalised rules text, plus a few tags
read straight from the graph (CREATES edges, card types, keywords).
"""
import re
import sys
import time

import relate

# --- text normalisation -------------------------------------------------------

SELF = r"(?:~|this (?:creature|artifact|enchantment|land|permanent|card|spell|vehicle))"


def normalise(card):
    text = card["text"] or ""
    text = re.sub(r"\([^)]*\)", "", text)                  # drop reminder text
    names = [card["name"]] + card["name"].split(" // ")
    for n in sorted(set(names), key=len, reverse=True):
        text = text.replace(n, "~")
        short = n.split(",")[0]
        if short != n and len(short) > 3:                   # "Talrand" for "Talrand, Sky Summoner"
            text = text.replace(short, "~")
    return text.lower()


# --- rules: (resource, role, rule id, regex) ---------------------------------

C = re.compile
# Flicker: exile then return within one ability (one line), across sentences.
FLICKER = C(r"exile [^\n]*?(?:creature|permanent|~|artifact)[^\n]*?return (?:it|that card|them|those cards|that creature|the exiled card|each card exiled this way) to the battlefield")
RULES = [
    # Creatures as fodder and sacrifice outlets (a sacrifice produces a death).
    ("creature", "USES", "sac-outlet", C(r"sacrifice (?:a|another|one or more|x|two|three) (?:other )?(?:nontoken )?(?:\w+ )?creatures?[^.:]*:")),
    ("death", "PRODUCES", "sac-outlet", C(r"sacrifice (?:a|another|one or more|x|two|three) (?:other )?(?:nontoken )?(?:\w+ )?creatures?[^.:]*:")),
    ("death", "PRODUCES", "self-sacrifice", C(rf"sacrifice {SELF}[^.:]*:")),
    ("death", "PRODUCES", "board-wipe", C(r"destroy all (?:other )?(?:nonland )?(?:creatures|permanents)|each (?:player|opponent) sacrifices (?:a|an|two|three|x|\w+) (?:\w+ )?(?:creatures?|permanents?)")),
    ("death", "USES", "death-trigger", C(r"whenever (?:a|another|one or more|an?other)? ?(?:nontoken |equipped |enchanted )?(?:\w+ )?creatures? (?:you control |an opponent controls )?(?:or \w+ )?dies|whenever [^.]*another creature (?:you control )?dies")),
    ("death", "USES", "dies-self", C(rf"when(?:ever)? {SELF} dies")),

    # Tokens in general, and token doublers.
    ("token", "USES", "token-trigger", C(r"whenever you create (?:a|one or more) [^.]*tokens?")),
    ("token", "USES", "token-doubler", C(r"would create (?:one or more |a )?(?:[^.]* )?tokens?")),
    ("token", "PRODUCES", "token-doubler", C(r"would create (?:one or more |a )?(?:[^.]* )?tokens?")),

    # Artifacts (Treasure, Clue, Food... are artifact tokens; see graph rules below).
    ("artifact", "USES", "artifact-sac", C(r"sacrifice (?:an|another|one or more|x|two) (?:other )?(?:\w+ )?artifacts?[^.:]*:|whenever you sacrifice (?:an|one or more) (?:\w+ )?artifacts?")),
    ("artifact", "USES", "artifact-payoff", C(r"whenever (?:an|another|one or more) (?:\w+ )?artifacts? (?:you control )?(?:enters?|(?:is|are) put into a graveyard)|for each artifact you control|artifacts you control (?:have|get)")),

    # Creatures entering (ETB) and flicker.
    ("etb", "USES", "etb-trigger", C(r"whenever (?:a|another|one or more|an?other) (?:nontoken )?(?:\w+ )?creatures? (?:you control )?enters?")),
    ("flicker", "PRODUCES", "flicker", FLICKER),
    ("flicker", "USES", "etb-self", C(rf"when {SELF} enters")),
    ("etb", "PRODUCES", "flicker", FLICKER),

    ("flicker", "USES", "etb-doubler", C(r"(?:entering|enters)[^\n]*triggers an additional time")),
    ("etb", "USES", "etb-doubler", C(r"(?:entering|enters)[^\n]*triggers an additional time")),

    # Mana from nonland sources (rocks, dorks, rituals, land untappers).
    ("mana", "PRODUCES", "mana", C(r"\badd (?:\{|one mana|two mana|three mana|x mana|that much|an amount of)|untap (?:up to \w+ |all |target |two |three |x )?lands")),

    # Card draw.
    ("card_draw", "PRODUCES", "draw", C(r"(?<!opponent )(?<!opponents )\bdraws? (?:a|an additional|two|three|four|x|that many|\w+) cards?|draw cards equal")),
    ("card_draw", "USES", "draw-trigger", C(r"whenever you draw (?:a|your second|one or more) cards?")),

    # Graveyard: fillers and users.
    ("graveyard", "PRODUCES", "mill-discard", C(r"\bmills?\b|put the top [^.]* into your graveyard|\bdiscards? (?:a|two|three|x|that many|\w+) cards?|\bsurveil")),
    ("graveyard", "USES", "graveyard-use", C(r"from (?:your|a|any|an opponent's) graveyard|cards? in (?:your|a) graveyard|from graveyards")),

    # +1/+1 counters.
    ("counters", "PRODUCES", "put-counter", C(r"put (?:a|an additional|two|three|x|that many|\w+) \+1/\+1 counters? on|enters with (?:a|an additional|two|three|x|\w+) (?:additional )?\+1/\+1 counters?|\bproliferate")),
    ("counters", "USES", "counter-payoff", C(r"with (?:a|one or more) \+1/\+1 counters? on (?:it|them)|whenever one or more \+1/\+1 counters are put|for each \+1/\+1 counter|\bproliferate|would put one or more (?:\+1/\+1 )?counters|(?:\+1/\+1 )?counters would be put")),
    ("counters", "PRODUCES", "counter-doubler", C(r"would put one or more (?:\+1/\+1 )?counters|(?:\+1/\+1 )?counters would be put")),

    # Life gain.
    ("lifegain", "PRODUCES", "gain-life", C(r"\bgains? (?:\d+|x|that much|an amount of|\w+) life")),
    ("lifegain", "USES", "lifegain-trigger", C(r"whenever you gain life|if you would gain life|whenever you gain")),

    # Instants and sorceries (spellslinger).
    ("spells", "USES", "spell-trigger", C(r"whenever you cast (?:an|your first|your second|a) (?:instant|sorcery|noncreature)|instant (?:and|or) sorcery spells you cast")),

    # Lands entering (landfall) and ramp that puts lands onto the battlefield.
    ("landfall", "PRODUCES", "land-to-battlefield", C(r"put (?:a|up to \w+|that|those|two|x|\w+) (?:basic )?(?:\w+ )?lands? cards? onto the battlefield|put (?:it|them|those cards) onto the battlefield[^.]*land|search your library for [^.]*lands? cards?[^.]*put (?:it|them|that card|those cards) onto the battlefield|play an additional land|you may play (?:an additional land|lands from)")),
    ("landfall", "USES", "landfall", C(r"whenever a land (?:you control )?enters|\blandfall\b")),
]

KEYWORD_RULES = {
    "Lifelink": ("lifegain", "PRODUCES"), "Prowess": ("spells", "USES"),
    "Magecraft": ("spells", "USES"), "Landfall": ("landfall", "USES"),
    "Flashback": ("graveyard", "USES"), "Unearth": ("graveyard", "USES"),
    "Escape": ("graveyard", "USES"), "Delve": ("graveyard", "USES"),
    "Disturb": ("graveyard", "USES"), "Embalm": ("graveyard", "USES"),
    "Eternalize": ("graveyard", "USES"), "Dredge": ("graveyard", "USES"),
    "Mill": ("graveyard", "PRODUCES"), "Surveil": ("graveyard", "PRODUCES"),
    "Proliferate": ("counters", "PRODUCES"), "Evolve": ("counters", "PRODUCES"),
    "Adapt": ("counters", "PRODUCES"), "Outlast": ("counters", "PRODUCES"),
    "Bolster": ("counters", "PRODUCES"), "Training": ("counters", "PRODUCES"),
    "Mentor": ("counters", "PRODUCES"), "Modular": ("counters", "PRODUCES"),
}

EXPORT = """
MATCH (c:Card)
OPTIONAL MATCH (c)-[:CREATES]->(t:Token)
RETURN c.oracleId AS id, c.name AS name, c.oracleText AS text, c.types AS types,
       c.keywords AS keywords, collect(t.typeLine) AS tokenTypes
"""

WRITE = {
    role: f"""
    UNWIND $rows AS r
    MATCH (c:Card {{oracleId: r.id}})
    MERGE (res:Resource {{name: r.resource}})
    MERGE (c)-[e:{role}]->(res) SET e.rule = r.rule, e += r.props
    """ for role in ("PRODUCES", "USES")
}


# Rules whose matches don't count when the effect is aimed at someone else:
# "that player discards" fills an opponent's graveyard, not yours.
AIMED_ELSEWHERE = C(r"(?:that player|target opponent|each opponent|an opponent|target player|that creature's controller|its controller)\s*$|exile (?:up to (?:one|two|\w+) )?(?:target )?(?:\w+ )*cards? $")
SELF_SCOPED = {"mill-discard", "gain-life", "draw", "graveyard-use"}


def matches(rule, rx, text):
    for m in rx.finditer(text):
        if rule not in SELF_SCOPED or not AIMED_ELSEWHERE.search(text[max(0, m.start() - 30):m.start()]):
            return True
    return False


PRODUCER_RULES = [(res, rule, rx) for res, role, rule, rx in RULES if role == "PRODUCES"]
ETB_CLAUSE = C(rf"when(?:ever)? {SELF} enters[^\n]*")
ACTIVATED = C(r"^([^:\n]*\{[0-9wubrgcx]\}[^:\n]*):(.*)$", re.M)


WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def mana_value(symbols):
    """'{1}{u}' -> 2, '{c}{c}{c}' -> 3, '{x}' -> 0."""
    total = 0
    for sym in re.findall(r"\{([^}]+)\}", symbols):
        if sym in ("t", "q", "e", "x"):
            continue            # tap, untap, energy and X are not fixed mana
        total += int(sym) if sym.isdigit() else 1
    return total


def mana_yield(fragment):
    """How much mana an effect makes: 'untap up to five lands' -> 5, 'add {c}{c}' -> 2."""
    m = re.search(r"untap (?:up to )?(\w+) lands", fragment)
    if m:
        return WORDS.get(m.group(1), int(m.group(1)) if m.group(1).isdigit() else 0)
    m = re.search(r"\badd ((?:\{[^}]+\})+)", fragment)
    if m:
        return mana_value(m.group(1))
    m = re.search(r"\badd (\w+) mana", fragment)
    return WORDS.get(m.group(1), 0) if m else 0


def produced_by(fragment):
    return sorted({res for res, rule, rx in PRODUCER_RULES if matches(rule, rx, fragment)})


def flicker_details(card, text):
    """What a card's own ETB yields, and whether its flicker effect is repeatable."""
    props = {}
    m = ETB_CLAUSE.search(text)
    if m:
        props[("flicker", "USES")] = {"yields": [r for r in produced_by(m.group(0)) if r != "flicker"],
                                      "manaYield": mana_yield(m.group(0))}
    fm = FLICKER.search(text)
    if fm:
        line_start = text.rfind("\n", 0, fm.start()) + 1
        prefix = text[line_start: fm.start()]
        after = text[fm.end(): text.find("\n", fm.end()) if "\n" in text[fm.end():] else len(text)]
        cost_m = re.search(r'([^"]*\{[^"]*):\s*$', prefix)
        cost = cost_m.group(1) if cost_m else None
        if {"Instant", "Sorcery"} & set(card["types"] or []):
            kind = "one-shot"
        elif cost:
            kind = "activated"      # repeatable as often as you can pay
        elif prefix.startswith(("at the beginning", "whenever")):
            kind = "triggered"      # repeats once per trigger (e.g. each end step)
        elif "enters" in prefix:
            kind = "on-enter"
        else:
            kind = "other"
        self_only = re.match(r"exile (?:~|this (?:creature|permanent|artifact))(?!\w)", fm.group(0)) is not None
        props[("flicker", "PRODUCES")] = {
            "kind": kind,
            "repeatable": kind in ("activated", "triggered"),
            "activationCost": mana_value(cost) if cost else None,
            "tapCost": bool(cost and "{t}" in cost),
            # 'self' flickers only itself; 'granted' gives that ability to other creatures
            "scope": ("granted" if '"' in prefix else "self") if self_only else "other",
            "immediate": "at the beginning of the next" not in fm.group(0) + after[:60],
        }
    return props


def mana_sinks(text):
    """Activated abilities with a mana cost whose effect produces something."""
    for cost, effect in ACTIVATED.findall(text):
        out = [r for r in produced_by(effect) if r != "mana"]
        if out:
            return out
    return []


def tag(card):
    """Return {(resource, role): rule}; extra edge properties go in tag.props."""
    text = normalise(card)
    is_creature = "Creature" in (card["types"] or [])
    tags = {}
    for resource, role, rule, rx in RULES:
        if rule == "self-sacrifice" and not is_creature:
            continue  # sacrificing a land or artifact isn't a creature dying
        if (resource, role) not in tags and matches(rule, rx, text):
            tags[(resource, role)] = rule
    for kw in card["keywords"] or []:
        if kw in KEYWORD_RULES:
            tags.setdefault(KEYWORD_RULES[kw], f"keyword:{kw}")
    for tl in card["tokenTypes"]:
        tags.setdefault(("token", "PRODUCES"), "creates-token")
        if "Creature" in tl:
            tags.setdefault(("creature", "PRODUCES"), "creates-creature-token")
            tags.setdefault(("etb", "PRODUCES"), "creates-creature-token")
        if "Artifact" in tl:
            tags.setdefault(("artifact", "PRODUCES"), "creates-artifact-token")
    if {"Instant", "Sorcery"} & set(card["types"] or []):
        tags.setdefault(("spells", "PRODUCES"), "is-instant-sorcery")
    if "Land" in (card["types"] or []) and tags.get(("mana", "PRODUCES")) == "mana":
        if not re.search(r"untap (?:up to \w+ |all |target |two |three |x )?lands", text):
            del tags[("mana", "PRODUCES")]   # every land taps for mana; not a signal
    if any("Treasure" in tl for tl in card["tokenTypes"]):
        tags.setdefault(("mana", "PRODUCES"), "creates-treasure")
    sinks = mana_sinks(text)
    if sinks:
        tags.setdefault(("mana", "USES"), "mana-sink")
    props = flicker_details(card, text)
    if sinks:
        props[("mana", "USES")] = {"yields": sinks}
    tag.props = {k: v for k, v in props.items() if k in tags}
    return tags


def main(store):
    t0 = time.time()
    db = relate.connect(store)
    db.execute("CREATE CONSTRAINT resource_name IF NOT EXISTS FOR (r:Resource) REQUIRE r.name IS UNIQUE")
    db.execute("MATCH (:Card)-[e:PRODUCES|USES]->(:Resource) DELETE e")   # re-runnable
    cards = list(db.execute(EXPORT))
    rows = []
    for c in cards:
        tags = tag(c)
        rows += [{"id": c["id"], "resource": res, "role": role, "rule": rule,
                  "props": tag.props.get((res, role), {})} for (res, role), rule in tags.items()]
    for role, query in WRITE.items():
        part = [r for r in rows if r["role"] == role]
        for i in range(0, len(part), 2000):
            db.execute(query, {"rows": part[i:i + 2000]})
    print(f"tagged {len({r['id'] for r in rows})} of {len(cards)} cards with "
          f"{len(rows)} edges ({time.time() - t0:.0f}s)")
    db.close()


if __name__ == "__main__":
    main(sys.argv[1])
