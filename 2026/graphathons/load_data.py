"""Load the synthetic air-freight dataset into Neo4j.

    python load_data.py            # wipe the workshop data (only these labels) and reload it

Stored model:
    (:Shipper)-[:HAS_CONTRACT]->(:Contract)-[:SHIPS_ON]->(:Route)-[:FROM|TO]->(:Airport)
    (:Carrier)-[:OPERATES]->(:Route)

Connection settings come from environment variables (or a .env file):
NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE.
"""
import os

from data_generator import generate

LABELS = ["Airport", "Carrier", "Route", "Shipper", "Contract"]

CONSTRAINTS = [
    "CREATE CONSTRAINT airport_code IF NOT EXISTS FOR (n:Airport) REQUIRE n.code IS UNIQUE",
    "CREATE CONSTRAINT carrier_code IF NOT EXISTS FOR (n:Carrier) REQUIRE n.code IS UNIQUE",
    "CREATE CONSTRAINT route_id IF NOT EXISTS FOR (n:Route) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT shipper_id IF NOT EXISTS FOR (n:Shipper) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT contract_id IF NOT EXISTS FOR (n:Contract) REQUIRE n.id IS UNIQUE",
]

RESET = """
MATCH (n) WHERE n:Airport OR n:Carrier OR n:Route OR n:Shipper OR n:Contract
DETACH DELETE n
"""

LOADS = [
    ("airports", """
        UNWIND $rows AS row
        MERGE (a:Airport {code: row.code})
        SET a.name = row.name, a.region = row.region, a.type = row.type, a.idx = row.idx
    """),
    ("carriers", """
        UNWIND $rows AS row
        MERGE (c:Carrier {code: row.code})
        SET c.name = row.name
    """),
    ("routes", """
        UNWIND $rows AS row
        MATCH (o:Airport {code: row.origin}), (d:Airport {code: row.destination})
        MERGE (r:Route {id: row.id})
        SET r.distanceKm = row.distanceKm, r.weeklyFlights = row.weeklyFlights, r.active = row.active
        MERGE (r)-[:FROM]->(o)
        MERGE (r)-[:TO]->(d)
    """),
    ("operates", """
        UNWIND $rows AS row
        MATCH (c:Carrier {code: row.carrier}), (r:Route {id: row.route})
        MERGE (c)-[:OPERATES]->(r)
    """),
    ("shippers", """
        UNWIND $rows AS row
        MERGE (s:Shipper {id: row.id})
        SET s.name = row.name, s.industry = row.industry
    """),
    ("contracts", """
        UNWIND $rows AS row
        MATCH (s:Shipper {id: row.shipper})
        MERGE (c:Contract {id: row.id})
        SET c.industry = row.industry, c.annualRevenueM = row.annualRevenueM,
            c.slaPenaltyPerDayK = row.slaPenaltyPerDayK
        MERGE (s)-[:HAS_CONTRACT]->(c)
    """),
    ("ships_on", """
        UNWIND $rows AS row
        MATCH (c:Contract {id: row.contract}), (r:Route {id: row.route})
        MERGE (c)-[:SHIPS_ON]->(r)
    """),
]

COUNTS = """
MATCH (n) WHERE n:Airport OR n:Carrier OR n:Route OR n:Shipper OR n:Contract
RETURN labels(n)[0] AS label, count(*) AS count ORDER BY label
"""


def load(run, reset=True):
    """Load the dataset. `run(query, params)` executes one Cypher statement.

    Works with the Python driver or with `gds.run_cypher`, so the notebook and the
    command line share the same code.
    """
    data = generate()
    for q in CONSTRAINTS:
        run(q, {})
    if reset:
        run(RESET, {})
    for name, query in LOADS:
        run(query, {"rows": data[name]})


if __name__ == "__main__":
    from neo4j import GraphDatabase

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    uri = os.getenv("NEO4J_URI", "neo4j://localhost:7687")
    auth = (os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", ""))
    database = os.getenv("NEO4J_DATABASE", "neo4j")

    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()
        load(lambda q, p: driver.execute_query(q, p, database_=database))
        records, _, _ = driver.execute_query(COUNTS, database_=database)
        print("Loaded:", ", ".join(f"{r['count']} {r['label']}" for r in records))
