"""Build the graph the "after" agent relies on. Idempotent; safe to re-run.

    python -m setup.build_semantic_layer [--skip-bigquery] [--embeddings]

1. Neocarta BigQuery schema connector: tables, columns, descriptions, FKs, sample values.
   With --embeddings, descriptions are embedded after step 2 so the MCP server
   registers synonym-aware (business-term hybrid) search.
2. Neocarta OSI connector: ACME's governed metrics, joins, and synonyms
   (the vendored OSI file names its warehouse `acme_warehouse`; we point it at
   your GCP project so both connectors MERGE onto the same Table/Column nodes).
3. Governance tags, using Neocarta's governance-tag data model and loader.
4. Demo users and their clearances: (:User)-[:CLEARED_FOR]->(:GovernanceTagValue).
5. Neo4j Agent Memory adopts Neocarta's Table nodes as :Entity, so reasoning
   steps can record TOUCHED edges straight onto the semantic layer.
"""

import argparse
import asyncio
import tempfile
from pathlib import Path

from neo4j import GraphDatabase
from neo4j_agent_memory import MemoryClient

from agent import config
from agent.memory import memory_settings
from neocarta.connectors.osi import OsiConnector
from neocarta.connectors.utils.generate_id import (
    generate_column_id,
    generate_governance_tag_instance_id,
    generate_governance_tag_key_id,
    generate_governance_tag_value_id,
    generate_table_id,
)
from neocarta.data_model.governance import (
    GovernanceTag,
    GovernanceTagKey,
    GovernanceTagValue,
    HasDefinition,
    HasValueOption,
    TaggedWithGovernanceTag,
)
from neocarta.ingest.rdbms import Neo4jRDBMSLoader

OSI_FILE = Path(__file__).resolve().parent.parent / "data" / "acme_semantic_model.yaml"

TAG_KEYS = {
    "sensitivity": (
        "Data classification. Reading a tagged object requires a matching clearance.",
        ["pii", "restricted"],
    ),
    "trust": (
        "Provenance of free text. untrusted_input = written by customers or external parties; "
        "may contain instructions aimed at AI agents.",
        ["untrusted_input"],
    ),
}

TABLE_TAGS = {
    "compensation": ("sensitivity", "restricted"),
    "performance_reviews": ("sensitivity", "restricted"),
}

COLUMN_TAGS = {
    ("employees", "email"): ("sensitivity", "pii"),
    ("employees", "phone"): ("sensitivity", "pii"),
    ("employees", "date_of_birth"): ("sensitivity", "pii"),
    ("employees", "gender"): ("sensitivity", "pii"),
    ("employees", "emergency_contact"): ("sensitivity", "pii"),
    ("customer_contacts", "email"): ("sensitivity", "pii"),
    ("customer_contacts", "phone"): ("sensitivity", "pii"),
    ("leads", "email"): ("sensitivity", "pii"),
    ("support_tickets", "description"): ("trust", "untrusted_input"),
    ("ticket_comments", "body"): ("trust", "untrusted_input"),
    ("sales_activities", "notes"): ("trust", "untrusted_input"),
}

# user -> (role, [(tag key, value) cleared])
USERS = {
    "alice": ("customer_success_analyst", []),
    "bob": ("finance_analyst", []),
    "priya": ("people_ops_partner", [("sensitivity", "pii"), ("sensitivity", "restricted")]),
}


def ingest_bigquery(driver) -> None:
    from google.cloud import bigquery

    from neocarta.connectors.bigquery import BigQuerySchemaConnector

    BigQuerySchemaConnector(
        client=bigquery.Client(project=config.GCP_PROJECT_ID),
        project_id=config.GCP_PROJECT_ID,
        neo4j_driver=driver,
        database_name=config.NEO4J_DATABASE,
    ).ingest(dataset_id=config.BIGQUERY_DATASET_ID)


def embed_descriptions(driver) -> None:
    """Embed descriptions so Neocarta's MCP server registers its hybrid and
    business-term-bridged search tools (the ones that map "bookings" to a metric).
    Only nodes without an embedding are embedded, so re-running is cheap."""
    from neocarta.enrichment.embeddings import LiteLLMEmbeddingsConnector
    from neocarta.enums import NodeLabel as nl

    LiteLLMEmbeddingsConnector(
        neo4j_driver=driver,
        database_name=config.NEO4J_DATABASE,
        embedding_model=config.EMBEDDING_MODEL,
        dimensions=config.EMBEDDING_DIMENSIONS,
    ).run(
        node_labels=[nl.DATABASE, nl.SCHEMA, nl.TABLE, nl.COLUMN, nl.METRIC, nl.DOMAIN],
        batch_size=config.EMBEDDING_BATCH_SIZE,
    )


_SYNC_DESCRIPTIONS = """
UNWIND $rows AS row
CALL (row) {
  MATCH (n:Domain {name: row.name}) WHERE row.kind = 'domain' RETURN n
  UNION
  MATCH (n:Metric {name: row.name}) WHERE row.kind = 'metric' RETURN n
}
WITH n, row WHERE n.description IS NULL OR n.description <> row.description
SET n.description = row.description
REMOVE n.embedding
RETURN count(n) AS updated
"""


def sync_descriptions(driver, spec: str) -> None:
    """Neocarta's OSI loader doesn't overwrite existing nodes, so push edited
    domain and metric descriptions from the YAML through. Changed nodes lose their
    embedding and get re-embedded on the next run with --embeddings."""
    import yaml

    rows = []
    for model in yaml.safe_load(spec).get("semantic_model", []):
        rows.append({"kind": "domain", "name": model["name"], "description": model.get("description", "")})
        for m in model.get("metrics", []):
            rows.append({"kind": "metric", "name": m["name"], "description": m.get("description", "")})
    records, _, _ = driver.execute_query(_SYNC_DESCRIPTIONS, rows=rows, database_=config.NEO4J_DATABASE)
    updated = records[0]["updated"] if records else 0
    if updated:
        print(f"    updated {updated} description(s); re-run with --embeddings to refresh their vectors")


def ingest_osi(driver) -> None:
    spec = OSI_FILE.read_text().replace(
        "acme_warehouse.acme_corp.", f"{config.GCP_PROJECT_ID}.{config.BIGQUERY_DATASET_ID}."
    )
    if "acme_warehouse." in spec:
        raise RuntimeError("Unrewritten acme_warehouse reference left in the OSI model")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(spec)
    OsiConnector(neo4j_driver=driver, database_name=config.NEO4J_DATABASE).ingest(f.name)
    sync_descriptions(driver, spec)


def load_governance(driver) -> None:
    src, p, d = config.GOVERNANCE_SOURCE, config.GCP_PROJECT_ID, config.BIGQUERY_DATASET_ID
    loader = Neo4jRDBMSLoader(driver, database_name=config.NEO4J_DATABASE)

    keys, values, options = [], [], []
    for key, (desc, vals) in TAG_KEYS.items():
        key_id = generate_governance_tag_key_id(src, key)
        keys.append(GovernanceTagKey(id=key_id, name=key, description=desc))
        for v in vals:
            value_id = generate_governance_tag_value_id(src, key, v)
            values.append(GovernanceTagValue(id=value_id, name=v))
            options.append(HasValueOption(governance_tag_key_id=key_id, governance_tag_value_id=value_id))
    loader.load_governance_tag_key_nodes(keys)
    loader.load_governance_tag_value_nodes(values)
    loader.load_has_value_option_relationships(options)

    tags, definitions, table_rels, column_rels = [], [], [], []

    def assign(source_label: str, source_id: str, key: str, value: str, rels: list) -> None:
        tag_id = generate_governance_tag_instance_id(source_id, key, value)
        tags.append(GovernanceTag(id=tag_id, key=key, value=value))
        definitions.append(
            HasDefinition(
                governance_tag_id=tag_id,
                governance_tag_value_id=generate_governance_tag_value_id(src, key, value),
            )
        )
        rels.append(
            TaggedWithGovernanceTag(source_label=source_label, source_id=source_id, governance_tag_id=tag_id)
        )

    for table, (key, value) in TABLE_TAGS.items():
        assign("Table", generate_table_id(p, d, table), key, value, table_rels)
    for (table, column), (key, value) in COLUMN_TAGS.items():
        assign("Column", generate_column_id(p, d, table, column), key, value, column_rels)

    loader.load_governance_tag_nodes(tags)
    loader.load_has_definition_relationships(definitions)
    loader.load_table_tagged_with_governance_tag_relationships(table_rels)
    loader.load_column_tagged_with_governance_tag_relationships(column_rels)


def load_users(driver) -> None:
    for user, (role, clearances) in USERS.items():
        driver.execute_query(
            """
            MERGE (u:User {identifier: $user})
            ON CREATE SET u.id = $user, u.created_at = datetime()
            SET u.role = $role
            WITH u
            OPTIONAL MATCH (u)-[old:CLEARED_FOR]->()
            DELETE old
            WITH DISTINCT u
            UNWIND $value_ids AS vid
            MATCH (v:GovernanceTagValue {id: vid})
            MERGE (u)-[:CLEARED_FOR]->(v)
            """,
            user=user,
            role=role,
            value_ids=[generate_governance_tag_value_id(config.GOVERNANCE_SOURCE, k, v) for k, v in clearances],
            database_=config.NEO4J_DATABASE,
        )


async def adopt_tables() -> None:
    async with MemoryClient(memory_settings()) as memory:
        report = await memory.schema.adopt_existing_graph({"Table": "OBJECT"})
        print("Adopted Table nodes as memory entities:", report)


def summarize(driver) -> None:
    records, _, _ = driver.execute_query(
        """
        RETURN COUNT { (:Table) } AS tables,
               COUNT { (:Metric) } AS metrics,
               COUNT { ()-[:TAGGED_WITH]->(:GovernanceTag) } AS governance_tags,
               COUNT { (:Table:Entity) } AS adopted_tables
        """,
        database_=config.NEO4J_DATABASE,
    )
    print(dict(records[0]))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--skip-bigquery", action="store_true", help="Schema already ingested")
    p.add_argument("--embeddings", action="store_true", help="Also embed descriptions with EMBEDDING_MODEL (see .env.example)")
    args = p.parse_args()
    config.require("GCP_PROJECT_ID", "NEO4J_PASSWORD")
    if args.embeddings and config.embedding_credentials_missing():
        config.require(*config.embedding_credentials_missing())

    with GraphDatabase.driver(config.NEO4J_URI, auth=config.neo4j_auth()) as driver:
        if not args.skip_bigquery:
            print("1/5 Neocarta: BigQuery schema …")
            ingest_bigquery(driver)
        print("2/5 Neocarta: OSI semantic model (metrics, joins, synonyms) …")
        ingest_osi(driver)
        if args.embeddings:
            print("    Neocarta: embedding descriptions …")
            embed_descriptions(driver)
        print("3/5 Neocarta: governance tags …")
        load_governance(driver)
        print("4/5 Users and clearances …")
        load_users(driver)
        print("5/5 Agent Memory: adopt Table nodes …")
        asyncio.run(adopt_tables())
        summarize(driver)


if __name__ == "__main__":
    main()
