"""Write what users taught by drawing into the graph itself.

    python bake_models.py mtg_live.rl        # run by build_all.sh after states.py

Each saved model's relationships (custom_model.json) become real relationships in Relate, marked source "user",
so they show in Cypher, the schema and Neo4j Browser, and "Check in the graph" reports them as already there.
Each model whose test found a loop also becomes (:Combo {source: "user"})-[:USES]->(:Card): a combo the graph
learned from you, counted as confirmed in deck analysis.
"""
import json
import sys

import relate

ALLOWED = {"TRIGGERED_BY": "Event", "RETRIGGERS": "Event", "PRODUCES": "Resource", "COSTS": "Resource",
           "REQUIRES_STATE": "State", "CAUSES_STATE": "State", "CLEARS_STATE": "State"}


def main(store, path="custom_model.json"):
    try:
        models = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        print("no custom_model.json: nothing to bake"); return
    db = relate.connect(store)
    db.execute("MATCH (:Ability)-[r]->() WHERE r.source = 'user' DELETE r")
    db.execute("MATCH (c:Combo {source: 'user'}) DETACH DELETE c")
    db.execute("MATCH ()-[r:FIRES]->() WHERE r.source = 'user' DELETE r")
    n_rel = n_combo = 0
    for m in models:
        for e in m.get("edges", []):
            typ, t = e.get("type"), str(e.get("t", ""))
            if typ == "FIRES" and t:                # "counts as": a resource (or event) that is also an event
                src = str(e.get("s", "")); src = src.split(":", 1)[1] if src[:2] in ("n:", "r:", "e:") else src
                lab = "Event" if e.get("st") == "Event" else "Resource"
                db.execute(f"""MERGE (r:{lab} {{name: $s}}) MERGE (t:Event {{name: $n}})
                               MERGE (r)-[f:FIRES]->(t) SET f.source = 'user', f.model = $m""",
                           {"s": src, "n": t, "m": m.get("name", "")})
                n_rel += 1; continue
            if typ not in ALLOWED or not t:
                continue
            label, name = ALLOWED[typ], t[2:] if t.startswith("s:") else t
            p = {k: v for k, v in (e.get("p") or {}).items() if isinstance(v, (str, int, float))}
            db.execute(f"""MATCH (a:Ability {{id: $a}}) MERGE (t:{label} {{name: $n}})
                           CREATE (a)-[r:{typ}]->(t) SET r += $p, r.source = 'user', r.model = $m""",
                       {"a": e.get("s"), "n": name, "p": p, "m": m.get("name", "")})
            n_rel += 1
        if m.get("loop"):
            db.execute("""CREATE (c:Combo {id: $id, name: $name, source: 'user'}) WITH c
                          UNWIND $cards AS cn MATCH (card:Card {name: cn}) MERGE (c)-[:USES]->(card)""",
                       {"id": "user:" + m.get("name", ""), "name": m.get("name", ""), "cards": m["loop"].get("cards", [])})
            n_combo += 1
    print(f"baked {n_rel} relationships and {n_combo} learned combos from {len(models)} models into {store}")
    db.close()


if __name__ == "__main__":
    main(sys.argv[1])
