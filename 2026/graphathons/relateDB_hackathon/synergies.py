"""Synergies: which cards in a deck make which other cards better.

Two kinds, both grounded in the card text:
  * Type synergies: text that names a creature type (or artifact, enchantment...) the deck runs.
    "Other Humans you control get +1/+1" BUFFS each Human; "whenever a Zombie enters" WATCHES each Zombie;
    "for each Elf you control" COUNTS Elves.
  * Strategy synergies: enablers and payoffs of the same strategy. A sacrifice outlet feeds "whenever a
    creature dies"; token makers feed "whenever you create a token"; and so on.
Each group lists enablers (or lords), payoffs (or the cards of that type), and the links between them.
"""
import re
from collections import Counter

from tag_functions import normalise

STRATEGIES = [
    # (key, name, enabler wording, payoff wording)
    ("sacrifice", "Sacrifice", r"sacrifice (?:a|another|one or more|two|x) (?:other )?(?:nontoken )?creatures?",
     r"whenever (?:~ or )?(?:a|another|one or more) (?:other )?(?:nontoken )?creatures? (?:you control )?(?:dies|die)"),
    ("tokens", "Tokens", r"create (?:a|an|one|two|three|four|x|that many|\d+) [^.]*tokens?",
     r"whenever you create (?:a|one or more) [^.]*tokens?|whenever (?:a|one or more) [^.]*tokens? (?:you control )?enters?|tokens you control (?:get|have)"),
    ("counters", "+1/+1 counters", r"put (?:a|an|one|two|three|x|that many|\d+) \+1/\+1 counters? on",
     r"(?:\+1/\+1 )?counters? (?:is|are) put on|you put one or more (?:\+1/\+1 )?counters|for each \+1/\+1 counter|with (?:a|one or more) \+1/\+1 counters? on"),
    ("drain", "Life drain", r"you gain \d+ life|gains? that much life|you gain life equal|lifelink",
     r"whenever you gain life"),
    ("blink", "Blink", r"exile [^.]*then return (?:it|that card|them|those cards) to the battlefield|exile [^.]*return [^.]*to the battlefield",
     r"^(?:when|whenever) (?:~|this creature) enters"),
    ("reanimate", "Reanimate", r"\bmill\b|\bsurveil\b|put the top [^.]* cards? of your library into your graveyard|discard (?:a|two|x) creature cards?|put (?:a|up to \w+) (?:creature )?cards? from your library into your graveyard",
     r"return [^.]*creature cards? [^.]*from (?:your|a) graveyard to the battlefield|put [^.]*creature card [^.]*from (?:a|your) graveyard onto the battlefield"),
    ("bounce", "Bounce", r"return (?:a|another|target|up to one target) (?:creature|permanent|nonland permanent) you control to (?:its owner's|your) hand",
     r"^(?:when|whenever) (?:~|this creature) enters"),
    ("draw", "Card draw", r"\bdraw (?:a|two|three|x|that many|\d+) cards?", r"whenever you draw"),
    ("spells", "Spellslinger", r"^(?:instant|sorcery)$",
     r"whenever you cast (?:an instant or sorcery|a noncreature|an instant|a sorcery)|instant and sorcery spells you cast"),
    ("landfall", "Landfall", r"search your library for [^.]*lands?[^.]*onto the battlefield|you may play an additional land|put a land card [^.]*onto the battlefield",
     r"whenever a land (?:you control )?enters|landfall"),
]
GENERIC = {"creature", "creatures", "token", "tokens", "card", "cards", "spell", "spells", "permanent", "permanents", "land", "lands"}


def deck_synergies(db, deck, cap=40):
    rows = list(db.execute("""MATCH (c:Card) WHERE c.name IN $n
        RETURN c.name AS name, c.oracleText AS text, c.typeLine AS typeLine, c.types AS types""", {"n": sorted(set(deck))}))
    cards = {r["name"]: {"text": normalise({"name": r["name"], "text": r["text"] or ""}),
                         "typeLine": (r["typeLine"] or "").lower(), "types": [t.lower() for t in (r["types"] or [])]} for r in rows}
    groups = []

    # Type synergies: subtypes the deck actually runs (two or more cards), named in another card's text.
    subtypes = Counter(st for c in cards.values() for st in c["typeLine"].partition("—")[2].split() if st.isalpha())
    for st, n in subtypes.most_common():
        if n < 2 or st in GENERIC:
            continue
        plural = st + "s" if not st.endswith("s") else st
        core = rf"(?:{re.escape(st)}|{re.escape(plural)}|{re.escape(st[:-1])}ves)" if st.endswith("f") else rf"(?:{re.escape(st)}|{re.escape(plural)})"
        word = rf"(?<!non-)(?<!non){core}"           # "other non-Human creatures" rewards everything BUT Humans
        lords, watchers, counters = [], [], []
        for name, c in cards.items():
            t = c["text"]
            if re.search(rf"(?:other )?{word} (?:creatures )?you control (?:get|have|gain)", t):
                lords.append(name)
            elif re.search(rf"whenever (?:a|an|another|one or more) (?:nontoken )?(?:other )?{word}\b(?![^.]*(?:destroy|exile|sacrifice) (?:it|that))", t):
                watchers.append(name)               # (Mikaeus: "whenever a Human deals damage to you, destroy it" punishes)
            elif re.search(rf"for each {word}|number of {word}|{word} spells? (?:you cast )?costs?|{word} you control", t):
                counters.append(name)
        members = [name for name, c in cards.items() if re.search(rf"\b{re.escape(st)}\b", c["typeLine"].partition("—")[2])]
        sources = lords + watchers + counters
        if not sources:
            continue
        links = [{"s": s, "t": m, "kind": "buffs" if s in lords else "watches" if s in watchers else "counts"}
                 for s in sources for m in members if m != s][:cap]
        if links:
            groups.append({"key": f"type:{st}", "label": f"{st.title()} tribal", "category": "Tribal",
                           "enablers": sources, "payoffs": members, "links": links,
                           "summary": f"{len(members)} {st.title()}{'s' if len(members) != 1 else ''}; "
                                      f"{', '.join(sources[:3])}{' and more' if len(sources) > 3 else ''} reward{'s' if len(sources) == 1 else ''} them"})

    # Strategy synergies: enablers feed payoffs of the same strategy.
    for key, label, enable_rx, pay_rx in STRATEGIES:
        en, pay = [], []
        for name, c in cards.items():
            t = c["text"]
            is_enabler = (key == "spells" and ({"instant", "sorcery"} & set(c["types"]))) or \
                         (key != "spells" and re.search(enable_rx, t, re.M))
            is_payoff = re.search(pay_rx, t, re.M) and (key not in ("blink", "bounce") or "creature" in c["types"])
            if is_payoff:
                pay.append(name)
            elif is_enabler:
                en.append(name)
        if key in ("blink", "bounce") and not en:
            continue
        if en and pay:
            links = [{"s": s, "t": p, "kind": "feeds"} for s in en for p in pay if s != p][:cap]
            groups.append({"key": key, "label": label, "category": label, "enablers": en, "payoffs": pay, "links": links,
                           "summary": f"{len(en)} enabler{'s feed' if len(en) != 1 else ' feeds'} {len(pay)} payoff{'s' if len(pay) != 1 else ''}"})

    hubs = Counter()
    for g in groups:
        for l in g["links"]:
            hubs[l["s"]] += 1
            hubs[l["t"]] += 1
    groups.sort(key=lambda g: -len(g["links"]))
    return {"groups": groups, "hubs": hubs.most_common(8),
            "cards": len({c for g in groups for c in g["enablers"] + g["payoffs"]})}
