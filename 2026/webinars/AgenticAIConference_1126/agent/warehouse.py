"""BigQuery access shared by both agent modes.

Both modes run SQL through `run_sql`, which only allows read-only statements and
caps bytes billed. What differs between modes is what happens *before* the query
runs (the policy check in "after" mode) and what context the agent had when it
wrote the query.
"""

from functools import lru_cache

import sqlglot
from google.cloud import bigquery
from sqlglot import exp

from . import config

MAX_ROWS = 50


@lru_cache(maxsize=1)
def client() -> bigquery.Client:
    return bigquery.Client(project=config.GCP_PROJECT_ID)


def assert_read_only(sql: str) -> exp.Expression:
    """Parse BigQuery SQL and reject anything that isn't a single SELECT."""
    statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    if len(statements) != 1:
        raise ValueError("Exactly one SQL statement is allowed.")
    stmt = statements[0]
    if not isinstance(stmt, exp.Query):
        raise ValueError(f"Only read-only SELECT queries are allowed (got {stmt.key.upper()}).")
    return stmt


def run_sql(sql: str) -> str:
    """Execute a read-only query against the ACME dataset and return rows as text."""
    assert_read_only(sql)
    job_config = bigquery.QueryJobConfig(
        default_dataset=f"{config.GCP_PROJECT_ID}.{config.BIGQUERY_DATASET_ID}",
        maximum_bytes_billed=config.BQ_MAX_BYTES_BILLED,
    )
    rows = list(client().query(sql, job_config=job_config).result(max_results=MAX_ROWS))
    if not rows:
        return "(no rows)"
    header = list(rows[0].keys())
    lines = [" | ".join(header)]
    lines += [" | ".join(str(v) for v in row.values()) for row in rows]
    return "\n".join(lines)


# --- The "before" catalog: what most text-to-SQL agents get --------------------


def list_tables() -> str:
    sql = (
        f"SELECT table_name FROM `{config.GCP_PROJECT_ID}.{config.BIGQUERY_DATASET_ID}`"
        ".INFORMATION_SCHEMA.TABLES ORDER BY table_name"
    )
    return "\n".join(r.table_name for r in client().query(sql).result())


def describe_table(table_name: str) -> str:
    sql = (
        f"SELECT column_name, data_type FROM `{config.GCP_PROJECT_ID}.{config.BIGQUERY_DATASET_ID}`"
        ".INFORMATION_SCHEMA.COLUMNS WHERE table_name = @t ORDER BY ordinal_position"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("t", "STRING", table_name)]
    )
    rows = list(client().query(sql, job_config=job_config).result())
    if not rows:
        return f"No table named {table_name!r}."
    return "\n".join(f"{r.column_name} {r.data_type}" for r in rows)
