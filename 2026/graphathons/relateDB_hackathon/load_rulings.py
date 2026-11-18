"""Put the official rules into the graph.

    python load_rulings.py mtg_live.rl rulings-<date>.jsonl comprules_keywords.json

  (Card)-[:HAS_RULING]->(Ruling {text, date, source})       Scryfall's per-card rulings
  (Keyword)-[:DEFINED_BY]->(Rule {number, name, text})       Comprehensive Rules 701/702 sections

comprules_keywords.json comes from the Comprehensive Rules PDF (see comprules.py).
"""
import json
import sys
import time

import relate


def main(store, rulings_path, rules_path):
    t0 = time.time()
    db = relate.connect(store)
    ids = {r["id"] for r in db.execute("MATCH (c:Card) RETURN c.oracleId AS id")}
    rows = []
    for line in open(rulings_path, encoding="utf-8"):
        r = json.loads(line)
        if r.get("oracle_id") in ids:
            rows.append({"id": r["oracle_id"], "text": r.get("comment", ""), "date": r.get("published_at"), "source": r.get("source")})
    db.execute("MATCH (r:Ruling) DETACH DELETE r")
    for i in range(0, len(rows), 5000):
        db.execute("""UNWIND $rows AS x MATCH (c:Card {oracleId: x.id})
                      CREATE (c)-[:HAS_RULING]->(:Ruling {text: x.text, date: x.date, source: x.source})""", {"rows": rows[i:i + 5000]})
    rules = json.load(open(rules_path, encoding="utf-8"))
    db.execute("MATCH (r:Rule) DETACH DELETE r")
    db.execute("""UNWIND $rows AS x CREATE (:Rule {number: x.rule, name: x.name, key: x.key, text: x.text})""",
               {"rows": [{**v, "key": k} for k, v in rules.items()]})
    linked = list(db.execute("""MATCH (k:Keyword), (r:Rule) WHERE toLower(k.name) = r.key
                                MERGE (k)-[:DEFINED_BY]->(r) RETURN count(*) AS n"""))[0]["n"]
    cards = list(db.execute("MATCH (c:Card)-[:HAS_RULING]->() RETURN count(DISTINCT c) AS n"))[0]["n"]
    print(f"rulings: {len(rows)} on {cards} cards; rules: {len(rules)} keyword sections, {linked} linked to keywords ({time.time() - t0:.0f}s)")
    db.close()


if __name__ == "__main__":
    main(*sys.argv[1:4])
