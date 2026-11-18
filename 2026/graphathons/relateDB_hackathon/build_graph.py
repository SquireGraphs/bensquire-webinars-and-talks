"""Build the MTG card graph in a single-file Relate store.

    python build_graph.py cards.jsonl.gz mtg.rl

(:Card)-[:HAS_SUBTYPE]->(:Subtype)        creature/artifact/land types (tribal synergy)
(:Card)-[:HAS_KEYWORD]->(:Keyword)
(:Card)-[:CREATES]->(:Token)              from Scryfall's related-card links
(:Card)-[:USES_MECHANIC]->(:Mechanic)     rules-helper references (Foretell, Manifest...)
(:Card)-[:REFERENCES]->(:Card)            other named references (meld pairs etc.)

Types, supertypes, colour identity and format legality stay as list properties:
they are low-cardinality filters, and as nodes they would be giant hubs.
"""
import gzip
import json
import sys
import time

import relate

NOT_PLAYABLE = {"planar", "vanguard", "scheme", "emblem", "art_series"}
SUPERTYPES = {"Legendary", "Basic", "Snow", "World", "Ongoing", "Host", "Elite"}
BATCH = 2000


def parse_type_line(type_line):
    """'Legendary Creature — Elf Druid // Land' -> (supertypes, types, subtypes)."""
    sup, typ, sub = set(), set(), set()
    for face in (type_line or "").split("//"):
        left, _, right = face.partition("—")
        for w in left.split():
            (sup if w in SUPERTYPES else typ).add(w)
        sub.update(right.split())
    return sorted(sup), sorted(typ), sorted(sub)


def rebalanced(a, b):
    return a.startswith("A-") or b.startswith("A-")


def token_key(name, type_line):
    return f"{name}|{type_line}"


def load(path):
    cards, tokens = [], []
    for line in gzip.open(path, "rt", encoding="utf-8"):
        c = json.loads(line)
        if c["is_token"]:
            tokens.append(c)
        elif c["layout"] not in NOT_PLAYABLE:
            cards.append(c)
    return cards, tokens


def rows(cards, tokens):
    card_rows, sub_rows, kw_rows, create_rows, mech_rows, ref_rows = [], [], [], [], [], []
    token_rows = {token_key(t["name"], t["type_line"]): {
        "key": token_key(t["name"], t["type_line"]), "name": t["name"],
        "typeLine": t["type_line"], "oracleText": t["oracle_text"],
        "power": t["power"], "toughness": t["toughness"]} for t in tokens}
    names = {c["name"] for c in cards}

    for c in cards:
        sup, typ, sub = parse_type_line(c["type_line"])
        legendary_creature = "Legendary" in sup and "Creature" in typ
        card_rows.append({
            "oracleId": c["oracle_id"], "name": c["name"], "layout": c["layout"],
            "manaCost": c["mana_cost"], "cmc": c["cmc"], "typeLine": c["type_line"],
            "oracleText": c["oracle_text"], "power": c["power"],
            "toughness": c["toughness"], "loyalty": c["loyalty"],
            "supertypes": sup, "types": typ, "colorIdentity": c["color_identity"] or [],
            "keywords": c["keywords"] or [], "producedMana": c["produced_mana"] or [],
            "legalIn": c["legal_in"], "rarity": c["rarity"], "releasedAt": c["released_at"],
            "edhrecRank": c["edhrec_rank"], "gameChanger": bool(c["game_changer"]),
            "imageUrl": c.get("image"),
            "canBeCommander": legendary_creature
                or "can be your commander" in (c["oracle_text"] or ""),
        })
        sub_rows += [{"id": c["oracle_id"], "name": s} for s in sub]
        kw_rows += [{"id": c["oracle_id"], "name": k} for k in c["keywords"] or []]
        for r in c["related"]:
            tl = r["type_line"] or ""
            if "Token" in tl:
                key = token_key(r["name"], tl)
                token_rows.setdefault(key, {"key": key, "name": r["name"], "typeLine": tl,
                                            "oracleText": None, "power": None, "toughness": None})
                create_rows.append({"id": c["oracle_id"], "key": key})
            elif "Checklist" in r["name"]:
                continue  # set checklist cards, not mechanics
            elif tl == "Card" or (tl == "Creature" and r["name"] not in names):
                mech_rows.append({"id": c["oracle_id"], "name": r["name"]})
            elif rebalanced(c["name"], r["name"]):
                continue  # Arena's "A-" rebalanced copy of the same card
            elif r["name"] in names and "Emblem" not in tl:
                ref_rows.append({"id": c["oracle_id"], "name": r["name"],
                                 "component": r["component"]})
    return card_rows, list(token_rows.values()), sub_rows, kw_rows, create_rows, mech_rows, ref_rows


SCHEMA = [
    "CREATE CONSTRAINT card_id IF NOT EXISTS FOR (c:Card) REQUIRE c.oracleId IS UNIQUE",
    "CREATE CONSTRAINT token_key IF NOT EXISTS FOR (t:Token) REQUIRE t.key IS UNIQUE",
    "CREATE CONSTRAINT subtype_name IF NOT EXISTS FOR (s:Subtype) REQUIRE s.name IS UNIQUE",
    "CREATE CONSTRAINT keyword_name IF NOT EXISTS FOR (k:Keyword) REQUIRE k.name IS UNIQUE",
    "CREATE CONSTRAINT mechanic_name IF NOT EXISTS FOR (m:Mechanic) REQUIRE m.name IS UNIQUE",
    "CREATE INDEX card_name IF NOT EXISTS FOR (c:Card) ON (c.name)",
]

LOADS = [
    ("cards", "UNWIND $rows AS r MERGE (c:Card {oracleId: r.oracleId}) SET c += r"),
    ("tokens", "UNWIND $rows AS r MERGE (t:Token {key: r.key}) SET t += r"),
    ("subtypes", """UNWIND $rows AS r MATCH (c:Card {oracleId: r.id})
        MERGE (s:Subtype {name: r.name}) MERGE (c)-[:HAS_SUBTYPE]->(s)"""),
    ("keywords", """UNWIND $rows AS r MATCH (c:Card {oracleId: r.id})
        MERGE (k:Keyword {name: r.name}) MERGE (c)-[:HAS_KEYWORD]->(k)"""),
    ("creates", """UNWIND $rows AS r MATCH (c:Card {oracleId: r.id}), (t:Token {key: r.key})
        MERGE (c)-[:CREATES]->(t)"""),
    ("mechanics", """UNWIND $rows AS r MATCH (c:Card {oracleId: r.id})
        MERGE (m:Mechanic {name: r.name}) MERGE (c)-[:USES_MECHANIC]->(m)"""),
    ("references", """UNWIND $rows AS r MATCH (c:Card {oracleId: r.id})
        MATCH (o:Card {name: r.name}) WHERE o <> c
        MERGE (c)-[x:REFERENCES]->(o) SET x.component = r.component"""),
]

VALIDATE = {
    "cards": "MATCH (n:Card) RETURN count(n) AS n",
    "commander-legal": "MATCH (n:Card) WHERE 'commander' IN n.legalIn RETURN count(n) AS n",
    "possible commanders": "MATCH (n:Card) WHERE n.canBeCommander AND 'commander' IN n.legalIn RETURN count(n) AS n",
    "tokens": "MATCH (n:Token) RETURN count(n) AS n",
    "subtypes": "MATCH (n:Subtype) RETURN count(n) AS n",
    "keywords": "MATCH (n:Keyword) RETURN count(n) AS n",
    "mechanics": "MATCH (n:Mechanic) RETURN count(n) AS n",
    "CREATES": "MATCH ()-[r:CREATES]->() RETURN count(r) AS n",
    "REFERENCES": "MATCH ()-[r:REFERENCES]->() RETURN count(r) AS n",
}


def main(src, store):
    t0 = time.time()
    cards, tokens = load(src)
    data = rows(cards, tokens)
    db = relate.connect(store)
    for s in SCHEMA:
        db.execute(s)
    for (name, query), batch in zip(LOADS, data):
        t = time.time()
        for i in range(0, len(batch), BATCH):
            db.execute(query, {"rows": batch[i:i + BATCH]})
        print(f"loaded {name}: {len(batch)} rows ({time.time() - t:.0f}s)")
    for name, q in VALIDATE.items():
        print(f"{name:>20}: {list(db.execute(q))[0]['n']}")
    db.close()
    print(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main(*sys.argv[1:3])
