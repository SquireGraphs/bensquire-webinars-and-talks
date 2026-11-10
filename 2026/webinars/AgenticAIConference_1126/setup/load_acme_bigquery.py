"""Load the ACME Corp warehouse (33 tables, about 10 rows each) into BigQuery.

    python -m setup.load_acme_bigquery                # uses GCP_PROJECT_ID / BIGQUERY_DATASET_ID
    python -m setup.load_acme_bigquery --location EU  # pick the dataset location
    python -m setup.load_acme_bigquery --replace      # rebuild an existing dataset

The SQL is Neocarta's own ACME script (data/acme-dataset.sql, from
neo4j-labs/neocarta, Apache-2.0). It hard-codes the dataset `acme_corp`; we
rewrite every reference to `<project>.<dataset>` so you can load it under any
name, then run it as one BigQuery script, the same way Neocarta's loader does.
Table and column descriptions are created inline, which is what Neocarta's
BigQuery connector reads later.
"""

import argparse
import re
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from agent import config

SQL_FILE = Path(__file__).resolve().parent.parent / "data" / "acme-dataset.sql"
EXPECTED_TABLES = 33


def render_sql(project: str, dataset: str, location: str) -> str:
    sql = SQL_FILE.read_text()
    sql = sql.replace("`acme_corp`", f"`{project}.{dataset}`")
    sql = sql.replace("`acme_corp.", f"`{project}.{dataset}.")
    sql = re.sub(r"location = '[A-Za-z0-9-]+'", f"location = '{location}'", sql, count=1)
    if "`acme_corp" in sql:
        raise RuntimeError("Unrewritten acme_corp reference left in the SQL")
    return sql


def existing_tables(client: bigquery.Client, dataset_ref: str) -> list[str]:
    try:
        return [t.table_id for t in client.list_tables(dataset_ref)]
    except NotFound:
        return []


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--location", default="US", help="BigQuery dataset location (default US)")
    p.add_argument("--replace", action="store_true", help="Rebuild even if the dataset has tables")
    args = p.parse_args()
    config.require("GCP_PROJECT_ID")
    project, dataset = config.GCP_PROJECT_ID, config.BIGQUERY_DATASET_ID
    dataset_ref = f"{project}.{dataset}"
    client = bigquery.Client(project=project)

    tables = existing_tables(client, dataset_ref)
    if tables and not args.replace:
        raise SystemExit(
            f"{dataset_ref} already has {len(tables)} tables. Re-run with --replace to rebuild it "
            "(CREATE OR REPLACE drops any rows you added, including the planted comment)."
        )

    print(f"Loading ACME into {dataset_ref} ({args.location}) …")
    client.query(render_sql(project, dataset, args.location), location=args.location).result()

    counts = {}
    for t in sorted(existing_tables(client, dataset_ref)):
        counts[t] = next(iter(client.query(f"SELECT COUNT(*) AS n FROM `{dataset_ref}.{t}`").result())).n
    for t, n in counts.items():
        print(f"  {t:<24}{n:>4}")
    empty = [t for t, n in counts.items() if n == 0]
    print(f"{len(counts)} tables loaded (expected {EXPECTED_TABLES}).")
    if len(counts) != EXPECTED_TABLES or empty:
        raise SystemExit(f"Something is off. Empty tables: {empty or 'none'}")


if __name__ == "__main__":
    main()
