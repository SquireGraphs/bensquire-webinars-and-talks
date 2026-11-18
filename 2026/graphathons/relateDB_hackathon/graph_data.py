"""Add the graph's schema and each deck's ability subgraph to the demo data.

    python graph_data.py mtg_sonnet_union.rl demo_data.json

schema: (label)-[TYPE]->(label) triples with counts, from the graph itself.
decks[i].graph: the deck's cards, their linked abilities, and the events and
resources those abilities connect: the subgraph the engine searches for loops.
"""
import json
import re
import sys
from collections import Counter

import relate

from decks import load_deck

KINDS = ("Activated", "Triggered", "Static", "Spell")
ENGINE_RELS = ("TRIGGERED_BY", "PRODUCES", "COSTS", "RETRIGGERS", "PREVENTS", "MODIFIES", "AMPLIFIES")
LEGACY = {("Card", "PRODUCES", "Resource"), ("Card", "USES", "Resource")}     # v1 card-level tags


def main_label(labels):
    return "Ability" if "Ability" in labels else labels[0]


def schema(db):
    counts = Counter()
    for r in db.execute("MATCH (a)-[r]->(b) RETURN labels(a) AS a, type(r) AS t, labels(b) AS b, count(*) AS n"):
        key = (main_label(r["a"]), r["t"], main_label(r["b"]))
        if key not in LEGACY:
            counts[key] += r["n"]
    nodes = Counter()
    for r in db.execute("MATCH (n) RETURN labels(n) AS l, count(*) AS n"):
        nodes[main_label(r["l"])] += r["n"]
    return {"nodes": [{"label": k, "count": v} for k, v in nodes.most_common()],
            "edges": [{"from": a, "type": t, "to": b, "count": n} for (a, t, b), n in counts.most_common()]}


def clean(props, drop=()):
    """Relationship/node properties as JSON-friendly values, without bulky or internal keys."""
    out = {}
    for k, v in (props or {}).items():
        if k in drop or v is None or v == [] or v == "":
            continue
        out[k] = v if isinstance(v, (str, int, float, bool, list)) else str(v)
    return out


def deck_graph(db, d):
    """d = a loaded deck (decks.load_deck or decks.load_deck_text)."""
    deck = sorted(set(d["commanders"] + d["cards"]))
    abil = list(db.execute("""
        MATCH (c:Card)-[h:HAS_ABILITY|GRANTS]->(a:Ability) WHERE c.name IN $deck AND NOT 'Land' IN c.types
        RETURN c.name AS card, a.id AS id, labels(a) AS labels, a.text AS text, type(h) AS rel, properties(a) AS p""",
        {"deck": deck}))
    ids = {a["id"] for a in abil}
    edges = []
    for r in ENGINE_RELS:
        for row in db.execute(f"""MATCH (c:Card)-[:HAS_ABILITY|GRANTS]->(a:Ability)-[x:{r}]->(t)
                                  WHERE c.name IN $deck RETURN a.id AS id, t.name AS t, properties(x) AS p""", {"deck": deck}):
            if row["id"] in ids and row["t"]:
                edges.append({"s": row["id"], "t": "n:" + row["t"], "type": r, "p": clean(row["p"])})
    # States (states.py): what each ability requires, causes and clears -> edges to State nodes
    for r in ("REQUIRES_STATE", "CAUSES_STATE", "CLEARS_STATE"):
        for row in db.execute(f"""MATCH (c:Card)-[:HAS_ABILITY|GRANTS]->(a:Ability)-[x:{r}]->(t:State)
                                  WHERE c.name IN $deck RETURN a.id AS id, t.name AS t, properties(x) AS p""", {"deck": deck}):
            if row["id"] in ids:
                edges.append({"s": row["id"], "t": "s:" + row["t"], "type": r, "p": clean(row["p"])})
    linked = {e["s"] for e in edges}
    nodes, keep = [], []
    for a in abil:
        if a["id"] in linked:                       # abilities with no engine links (Flying...) add nothing
            text = (a["text"] or "").replace("~", a["card"].split(" // ")[0])
            props = clean(a["p"], drop=("text",))
            props["source"] = "LLM extraction" if props.get("source") == "llm" else "rules (regex)"
            nodes.append({"id": a["id"], "type": "Ability", "kind": [l for l in a["labels"] if l != "Ability"][0],
                          "labels": sorted(a["labels"]), "card": a["card"], "text": text[:220], "props": props})
            keep.append({"s": "c:" + a["card"], "t": a["id"], "type": a["rel"], "p": {}})
    names = sorted({a["card"] for a in abil if a["id"] in linked})
    info = {r["name"]: r for r in db.execute("""MATCH (c:Card) WHERE c.name IN $n
        RETURN c.name AS name, c.oracleId AS oracleId, c.typeLine AS typeLine, c.manaCost AS manaCost, c.cmc AS cmc,
               c.colorIdentity AS colorIdentity, c.rarity AS rarity, c.canBeCommander AS canBeCommander""", {"n": names})}
    nodes += [{"id": "c:" + c, "type": "Card", "name": c, "labels": ["Card"],
               "props": clean({k: v for k, v in info.get(c, {}).items() if k != "name"})} for c in names]
    state_desc = {r["n"]: r["d"] for r in db.execute("MATCH (s:State) RETURN s.name AS n, s.description AS d")}
    for t in sorted({e["t"] for e in edges if e["t"].startswith("s:")}):
        nodes.append({"id": t, "type": "State", "name": t[2:], "labels": ["State"],
                      "props": {"name": t[2:], "description": state_desc.get(t[2:], "")}})
    targets = sorted({e["t"] for e in edges if not e["t"].startswith("s:")})
    # A name can be both (the graph has an Event "mana" and a Resource "mana"): in this deck, it's what its
    # relationships say it is. Something that happens (a trigger, "makes happen") is an Event; else a Resource.
    as_event = {e["t"] for e in edges if e["type"] in ("TRIGGERED_BY", "RETRIGGERS", "PREVENTS")}
    as_resource = {e["t"] for e in edges if e["type"] in ("PRODUCES", "COSTS", "MODIFIES", "AMPLIFIES")}
    for t in targets:
        kind = "Event" if t in as_event or (t not in as_resource and t[2:] in EVENTS) else "Resource"
        nodes.append({"id": t, "type": kind, "name": t[2:], "labels": [kind], "props": {"name": t[2:]}})
    # Resources that are also events: (life)-[:FIRES]->(you gain life), when both are in this deck's graph
    # (and an event that counts as another, drawn in the Teach editor).
    present = {t[2:] for t in targets}
    fires = [{"s": "n:" + r["r"], "t": "n:" + r["e"], "type": "FIRES", "p": {}}
             for r in db.execute("MATCH (r)-[:FIRES]->(e:Event) RETURN r.name AS r, e.name AS e")
             if r["r"] in present and r["e"] in present]
    return {"nodes": nodes, "edges": keep + edges + fires}


EVENTS = set()


def load_events(db):
    if not EVENTS:
        EVENTS.update(r["n"] for r in db.execute("MATCH (e:Event) RETURN e.name AS n"))


def main(store, data_path):
    db = relate.connect(store)
    load_events(db)
    data = json.load(open(data_path))
    data["schema"] = schema(db)
    from demo_data import SAMPLE_DECKS
    from decks import load_deck_text
    for dk, path in zip(data["decks"], SAMPLE_DECKS):
        text = open(path, encoding="utf-8").read()
        dk["graph"] = deck_graph(db, load_deck(db, path) if re.search(r"^commander\s*$", text, re.I | re.M) else load_deck_text(db, text))
        print(f"{dk['commander']}: {len(dk['graph']['nodes'])} nodes, {len(dk['graph']['edges'])} edges")
    print("schema:", len(data["schema"]["nodes"]), "labels,", len(data["schema"]["edges"]), "relationship patterns")
    json.dump(data, open(data_path, "w"), ensure_ascii=False)


if __name__ == "__main__":
    main(*sys.argv[1:3])
