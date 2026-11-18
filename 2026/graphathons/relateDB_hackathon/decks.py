"""Parse and validate Commander decklists against the graph.

    python decks.py mtg.rl decks/brago_blink.txt [--fill-basics]

Format (as exported by most deck sites):
    Commander
    1 Brago, King Eternal
    Deck
    1 Deadeye Navigator
Lines starting with # are comments. "1x" and set codes in parentheses are tolerated.
"""
import re
import sys

import relate

BASICS = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}
LINE = re.compile(r"^(\d+)x?\s+(.+?)(?:\s+\([A-Z0-9]+\).*)?$")


def parse(path):
    return parse_text(open(path, encoding="utf-8").read())


def parse_text(text):
    """Decklist text -> (commanders, cards). Accepts Arena/Moxfield-style exports."""
    commanders, cards, section = [], [], "deck"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower().strip("/ :")
        if low in ("commander", "commanders"):
            section = "commander"
            continue
        if low in ("deck", "mainboard", "main", "companion", "sideboard", "maybeboard"):
            if low in ("sideboard", "maybeboard", "companion"):
                section = "skip"
                continue
            section = "deck"
            continue
        if section == "skip":
            continue
        m = LINE.match(line)
        if not m:
            m = re.match(r"^()(.+)$", line)          # a bare card name counts as one copy
        n = int(m.group(1) or 1)
        name = re.sub(r"\s+\*[A-Z]+\*$", "", m.group(2).strip())   # foil / etched markers
        (commanders if section == "commander" else cards).extend([name] * n)
    return commanders, cards


_INDEX = {}


def resolve(db, names):
    """Map names as people type them to graph names: any case, front face of a double-faced card."""
    if not _INDEX:
        for r in db.execute("MATCH (c:Card) RETURN c.name AS n"):
            full = r["n"]
            _INDEX.setdefault(full.lower(), full)
            for face in full.split(" // "):
                _INDEX.setdefault(face.lower(), full)
    out = []
    for n in names:
        out.append(_INDEX.get(n.lower().replace("’", "'"), n))
    return out


def validate(db, commanders, cards):
    names = sorted(set(commanders + cards) - set(BASICS.values()) - {"Wastes"})
    info = {r["name"]: r for r in db.execute("""
        MATCH (c:Card) WHERE c.name IN $n
        RETURN c.name AS name, c.colorIdentity AS ci, c.legalIn AS legal, c.canBeCommander AS cmdr, c.types AS types""",
        {"n": names})}
    problems = [f"not found: {n}" for n in names if n not in info]
    identity = set()
    for c in commanders:
        if c in info:
            identity |= set(info[c]["ci"] or [])
            if not info[c]["cmdr"]:
                problems.append(f"can't be a commander: {c}")
    for n in names:
        if n in info:
            if "commander" not in (info[n]["legal"] or []):
                problems.append(f"not Commander-legal: {n}")
            off = set(info[n]["ci"] or []) - identity
            if off:
                problems.append(f"outside colour identity ({''.join(sorted(off))}): {n}")
    dupes = {n for n in cards if cards.count(n) > 1 and n not in BASICS.values() and n != "Wastes"}
    problems += [f"more than one copy: {n}" for n in sorted(dupes)]
    total = len(commanders) + len(cards)
    return problems, identity, total, info


def fill_basics(cards, identity, total):
    """Top the deck up to 100 with basics split evenly across the commander's colours."""
    colours = sorted(identity) or ["W"]
    need = 100 - total
    return cards + [BASICS[colours[i % len(colours)]] for i in range(max(need, 0))]


def suggest(problem):
    """'not found: Blood Artsit' -> add 'did you mean Blood Artist?' when a close name exists."""
    if not problem.startswith("not found: "):
        return problem
    import difflib
    name = problem[len("not found: "):]
    close = difflib.get_close_matches(name.lower(), list(_INDEX), n=1, cutoff=0.85)
    return f"{problem} (did you mean {_INDEX[close[0]]}?)" if close else problem


def last_block(text):
    """Some exports put the commander last, alone after a blank line."""
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    if len(blocks) < 2:
        return []
    names = [re.sub(r"\s+\*[A-Z]+\*$", "", (LINE.match(l.strip()) or re.match(r"^()(.+)$", l.strip())).group(2).strip())
             for l in blocks[-1].splitlines() if l.strip() and not l.strip().lower().startswith(("sideboard", "maybeboard"))]
    return names if 1 <= len(names) <= 2 else []


def first_block(text):
    """Card names in the first paragraph, if blank lines separate it from the rest
    (Moxfield-style exports put the commander first, on its own)."""
    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    if len(blocks) < 2:
        return []
    names = []
    for line in blocks[0].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.lower().strip("/ :") in ("commander", "commanders", "deck"):
            continue
        m = LINE.match(line) or re.match(r"^()(.+)$", line)
        names.append(re.sub(r"\s+\*[A-Z]+\*$", "", m.group(2).strip()))
    return names if 1 <= len(names) <= 2 else []


def load_deck_text(db, text, commander=None):
    commanders, cards = parse_text(text)
    commanders, cards = resolve(db, commanders), resolve(db, cards)
    if not commanders and commander:
        commanders = resolve(db, [commander])
        cards = [c for c in cards if c != commanders[0]]
    if not commanders:
        # A lone first entry that can be a commander is the commander (partners: up to two).
        for block in (first_block(text), last_block(text)):     # commander alone at the top, or at the end
            lead = resolve(db, block)
            able = {r["n"] for r in db.execute("MATCH (c:Card) WHERE c.name IN $n AND c.canBeCommander RETURN c.name AS n", {"n": lead})}
            if lead and all(n in able for n in lead) and all(n in cards for n in lead):
                commanders = lead
                for n in lead:
                    cards.remove(n)
                break
    if not commanders:
        # Say *why*: a lone first/last entry, or the commander typed in, that isn't in the card data at all
        # is most likely newer than the Scryfall file the graph was built from.
        known = {r["n"] for r in db.execute("MATCH (c:Card) WHERE c.name IN $n RETURN c.name AS n",
                                            {"n": resolve(db, first_block(text) + last_block(text) + ([commander] if commander else []))})}
        unknown = [n for n in first_block(text) + last_block(text) + ([commander] if commander else [])
                   if resolve(db, [n])[0] not in known]
        newest = next(iter(db.execute("MATCH (c:Card) RETURN max(c.releasedAt) AS d")), {}).get("d")
        msg = (f"\"{unknown[0]}\" isn't in the card data, so it can't be checked as a commander. It may be newer than "
               f"the Scryfall file the graph was built from{f' (newest card: {newest})' if newest else ''}: download a fresh "
               f"Default Cards file from scryfall.com/docs/api/bulk-data and rebuild.") if unknown else \
              "No commander found. Add a \"Commander\" section or fill in the commander field."
        return {"commanders": [], "cards": cards, "identity": [], "info": {}, "problems": [msg]}
    problems, identity, total, info = validate(db, commanders, cards)
    problems = [suggest(p) for p in problems]
    if total < 100:
        cards = fill_basics(cards, identity, total)
    elif total > 100:
        problems.append(f"{total} cards: a Commander deck has exactly 100.")
    return {"commanders": commanders, "cards": cards, "identity": sorted(identity), "problems": problems, "info": info}


def load_deck(db, path):
    commanders, cards = parse(path)
    problems, identity, total, info = validate(db, commanders, cards)
    if total < 100:
        cards = fill_basics(cards, identity, total)
    return {"commanders": commanders, "cards": cards, "identity": sorted(identity),
            "problems": problems, "info": info}


if __name__ == "__main__":
    db = relate.connect(sys.argv[1])
    for path in sys.argv[2:]:
        d = load_deck(db, path)
        spells = [c for c in set(d["cards"]) if c in d["info"] and "Land" not in (d["info"][c]["types"] or [])]
        print(f"{path}: commander {' + '.join(d['commanders'])} ({''.join(d['identity'])}), "
              f"{len(d['commanders']) + len(d['cards'])} cards, {len(spells)} nonland spells")
        for p in d["problems"]:
            print(f"   PROBLEM: {p}")
