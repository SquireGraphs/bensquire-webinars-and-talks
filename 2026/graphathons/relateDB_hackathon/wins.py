"""One-shot wins: combinations that win at once rather than by looping.

Each pattern pairs a *condition* card with an *enabler*, both recognised by their wording:
  empty library   Thassa's Oracle / Laboratory Maniac / Jace, Wielder of Mysteries
                  + Demonic Consultation / Tainted Pact (exile your whole library)
  draw punisher   "whenever an opponent draws a card" damage or life loss (Orcish Bowmasters)
                  + a card that makes a player draw a huge number (Peer into the Abyss)
"""
import re

PATTERNS = [
    {
        "name": "empty library",
        "label": "wins the game with an empty library",
        "condition": re.compile(r"greater than or equal to the number of cards in your library[^.]*win the game"
                                r"|draw a card while your library has no cards in it, you win the game", re.I),
        "enabler": re.compile(r"reveal cards from the top of your library until you reveal (?:a card with )?the (?:named|chosen)"
                              r"|until you put a card into your hand or you exile two cards with the same name"
                              r"|exile (?:your library|all cards from your library)", re.I),
    },
    {
        "name": "draw punisher",
        "label": "damage or life loss for every card an opponent draws",
        "condition": re.compile(r"whenever an opponent draws a card[^.]*(?:deals? \d+ damage|loses? \d+ life)", re.I),
        "enabler": re.compile(r"target player draws cards equal to half the number of cards in their library", re.I),
    },
]


def find_wins(db, deck):
    """One-shot wins plus pattern loops (see find_patterns)."""
    return _wins(db, deck) + find_patterns(db, deck)


# Loops that run through casting cards rather than through abilities.
BOUNCE_ON_ENTER = re.compile(r"whenever a (?:nonartifact |nonland )?permanent you control enters, you may return another permanent you control that shares a (?:card |permanent )?type with it", re.I)
BOUNCE_ON_CAST = re.compile(r"whenever you cast a spell, choose up to one[\s\S]{0,200}?return target nonland permanent to its owner's hand", re.I)
FOOD_CHAIN = re.compile(r"exile a creature you control: add x mana[^.]*1 plus", re.I)
CAST_FROM_EXILE = re.compile(r"you may cast (?:this card|~|[a-z ,'-]+) from (?:your graveyard or from )?exile", re.I)


def find_patterns(db, deck):
    rows = list(db.execute("""MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS name, c.oracleText AS text,
                              c.manaCost AS cost, c.types AS types, c.cmc AS cmc""", {"n": list(deck)}))
    line = lambda r, rx: next((s for s in (r["text"] or "").split("\n") if rx.search(s)), r["text"] or "")
    free = [r for r in rows if r["cost"] == "{0}" and "Land" not in (r["types"] or [])]
    out = []
    def add(cards, label, path):
        out.append({"cards": sorted(cards), "kind": "loop", "pattern": label, "label": label, "net": {}, "helpers": [],
                    "outcome": ["infinite self_enters/self_leaves", "cast_spell"], "path": path})
    for r in rows:
        t = r["text"] or ""
        if BOUNCE_ON_ENTER.search(t):        # Cloudstone Curio: two free permanents that share a type return each other
            ok = [f for f in free if "Artifact" not in (f["types"] or []) or "nonartifact" not in t.lower()]
            for i, a in enumerate(ok):
                for b in ok[i + 1:]:
                    if set(a["types"] or []) & set(b["types"] or []) - {"Legendary"}:
                        add([r["name"], a["name"], b["name"]], "free permanents return each other",
                            [{"card": r["name"], "kind": "Triggered", "text": line(r, BOUNCE_ON_ENTER)},
                             {"card": a["name"], "kind": "Spell", "text": f"cast it again for {{0}}"},
                             {"card": b["name"], "kind": "Spell", "text": f"cast it again for {{0}}"}])
        if BOUNCE_ON_CAST.search(t):         # Hullbreaker Horror: casting a free permanent returns it
            for a in free:
                if a["name"] != r["name"]:
                    add([r["name"], a["name"]], "a free permanent cast again and again",
                        [{"card": r["name"], "kind": "Triggered", "text": "whenever you cast a spell, return target nonland permanent to its owner's hand"},
                         {"card": a["name"], "kind": "Spell", "text": "cast it again for {0}"}])
        if FOOD_CHAIN.search(t):             # Food Chain: exile a creature you can recast from exile, net +1 mana
            for a in rows:
                if a["name"] != r["name"] and "Creature" in (a["types"] or []) and CAST_FROM_EXILE.search(a["text"] or ""):
                    add([r["name"], a["name"]], "infinite mana for creature spells",
                        [{"card": r["name"], "kind": "Activated", "text": line(r, FOOD_CHAIN)},
                         {"card": a["name"], "kind": "Spell", "text": line(a, CAST_FROM_EXILE)}])
    return out


def _wins(db, deck):
    rows = list(db.execute("MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS name, c.oracleText AS text",
                           {"n": list(deck)}))
    wins = []
    for p in PATTERNS:
        conds = [r for r in rows if r["text"] and p["condition"].search(r["text"])]
        # Inverter of Truth exiles your library but then refills it from your graveyard: not empty.
        enabs = [r for r in rows if r["text"] and p["enabler"].search(r["text"])
                 and not re.search(r"graveyard into your library", r["text"], re.I)]
        for c in conds:
            for e in enabs:
                if c["name"] == e["name"]:
                    continue
                line = lambda r, rx: next((s for s in r["text"].split("\n") if rx.search(s)), r["text"])
                wins.append({
                    "cards": sorted([c["name"], e["name"]]), "kind": "win", "pattern": p["name"], "label": p["label"],
                    "net": {}, "outcome": ["win_game"], "helpers": [],
                    "path": [{"card": e["name"], "kind": "Spell" if "Sorcery" in line(e, p["enabler"]) else "Ability",
                              "text": line(e, p["enabler"])},
                             {"card": c["name"], "kind": "Triggered", "text": line(c, p["condition"])}],
                })
    return wins
