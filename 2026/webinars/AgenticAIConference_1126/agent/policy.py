"""Permission-aware query execution, decided by a graph traversal.

Before an "after"-mode query runs, we:
  1. parse the SQL to find which tables and columns it reads (pure, unit-tested),
  2. look those objects up in the Neocarta semantic layer and collect their
     governance tags (sensitivity=..., trust=...),
  3. compare sensitivity tags against the user's clearances, which live in the
     same graph as (:User)-[:CLEARED_FOR]->(:GovernanceTagValue).

Rules:
  * A table-level tag applies whenever the table is read at all.
  * A column-level tag applies only when that column is read (SELECT * reads all).
  * Unqualified columns in multi-table queries are attributed to every referenced
    table that has a column with that name (conservative).
  * Tables missing from the semantic layer are denied (fail closed).
  * Reading a column tagged trust=untrusted_input doesn't block the query; it
    flags the session as having read untrusted input, so notes it writes are held for review.
  * Sensitive objects a session reads are remembered too: notes it writes inherit
    their tags, so a note can't leak restricted data to someone who isn't cleared.
"""

from dataclasses import dataclass, field

import sqlglot
from neo4j import Driver
from neocarta.connectors.utils.generate_id import generate_column_id, generate_table_id
from sqlglot import exp

from . import config

STAR = "*"


@dataclass
class SqlRefs:
    tables: set[str] = field(default_factory=set)
    columns: dict[str, set[str]] = field(default_factory=dict)  # table -> columns or {"*"}
    unresolved: set[str] = field(default_factory=set)  # column names with no clear owner
    foreign: set[str] = field(default_factory=set)  # tables outside the ACME dataset


def extract_refs(sql: str, dataset: str = config.BIGQUERY_DATASET_ID) -> SqlRefs:
    """Find the ACME tables and columns a BigQuery SELECT reads."""
    stmt = sqlglot.parse_one(sql, read="bigquery")
    refs = SqlRefs()
    cte_names = {cte.alias_or_name for cte in stmt.find_all(exp.CTE)}

    alias_to_table: dict[str, str] = {}
    for t in stmt.find_all(exp.Table):
        if t.name in cte_names and not t.db:
            continue
        if t.db and t.db != dataset:
            refs.foreign.add(".".join(p for p in (t.catalog, t.db, t.name) if p))
            continue
        refs.tables.add(t.name)
        alias_to_table[t.alias_or_name] = t.name
        alias_to_table[t.name] = t.name

    def add(table: str, column: str) -> None:
        refs.columns.setdefault(table, set()).add(column)

    for col in stmt.find_all(exp.Column):
        qualifier = col.table
        name = STAR if isinstance(col.this, exp.Star) else col.name
        if qualifier in alias_to_table:
            add(alias_to_table[qualifier], name)
        elif not qualifier and len(refs.tables) == 1 and name != STAR:
            add(next(iter(refs.tables)), name)
        elif name != STAR:
            refs.unresolved.add(name)

    # A bare SELECT * reads every column of every table. Only a Star that is itself a
    # projection counts; COUNT(*) reads no column values.
    if any(isinstance(e, exp.Star) for sel in stmt.find_all(exp.Select) for e in sel.expressions):
        for table in refs.tables:
            add(table, STAR)
    return refs


@dataclass
class PolicyDecision:
    allowed: bool
    reasons: list[str]
    touched_table_ids: list[str]
    sensitive_hits: list[str]  # e.g. "employees.email: sensitivity=pii"
    untrusted_columns: list[str]  # e.g. "ticket_comments.body"
    sensitive_source_ids: list[str] = field(default_factory=list)  # Table/Column node ids


_TAGS_QUERY = """
UNWIND $table_ids AS tid
MATCH (t:Table {id: tid})
OPTIONAL MATCH (t)-[:TAGGED_WITH]->(tg:GovernanceTag)
WITH t, [x IN collect(DISTINCT {key: tg.key, value: tg.value}) WHERE x.key IS NOT NULL] AS table_tags
OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column)
OPTIONAL MATCH (c)-[:TAGGED_WITH]->(cg:GovernanceTag)
WITH t, table_tags, c,
     [x IN collect(DISTINCT {key: cg.key, value: cg.value}) WHERE x.key IS NOT NULL] AS col_tags
RETURN t.id AS table_id, t.name AS table, table_tags,
       collect({column: c.name, tags: col_tags}) AS columns
"""

_CLEARANCE_QUERY = """
MATCH (:User {identifier: $user})-[:CLEARED_FOR]->(v:GovernanceTagValue)
      <-[:HAS_VALUE_OPTION]-(k:GovernanceTagKey)
RETURN k.name AS key, v.name AS value
"""


def table_id(table: str) -> str:
    return generate_table_id(config.GCP_PROJECT_ID, config.BIGQUERY_DATASET_ID, table)


def column_id(table: str, column: str) -> str:
    return generate_column_id(config.GCP_PROJECT_ID, config.BIGQUERY_DATASET_ID, table, column)


def check(driver: Driver, user: str, sql: str) -> PolicyDecision:
    refs = extract_refs(sql)
    ids = {table_id(t): t for t in refs.tables}
    records, _, _ = driver.execute_query(
        _TAGS_QUERY, table_ids=list(ids), database_=config.NEO4J_DATABASE
    )
    clear_rows, _, _ = driver.execute_query(
        _CLEARANCE_QUERY, user=user, database_=config.NEO4J_DATABASE
    )
    clearances = {(r["key"], r["value"]) for r in clear_rows}

    reasons: list[str] = []
    if refs.foreign:
        reasons.append(f"Tables outside the governed dataset: {sorted(refs.foreign)}")
    missing = set(ids) - {r["table_id"] for r in records}
    if missing:
        reasons.append(f"Not in the semantic layer: {sorted(ids[m] for m in missing)}")

    sensitive: list[tuple[str, str]] = []  # (object, value)
    sensitive_ids: set[str] = set()
    untrusted: list[str] = []
    for r in records:
        table = r["table"]
        for tag in r["table_tags"]:
            if tag["key"] == "sensitivity":
                sensitive.append((table, tag["value"]))
                sensitive_ids.add(r["table_id"])
        read = refs.columns.get(table, set())
        for col in r["columns"]:
            name = col["column"]
            if name is None:
                continue
            if not (STAR in read or name in read or name in refs.unresolved):
                continue
            for tag in col["tags"]:
                if tag["key"] == "sensitivity":
                    sensitive.append((f"{table}.{name}", tag["value"]))
                    sensitive_ids.add(column_id(table, name))
                elif tag["key"] == "trust" and tag["value"] == "untrusted_input":
                    untrusted.append(f"{table}.{name}")

    hits = [f"{obj}: sensitivity={val}" for obj, val in sensitive]
    for obj, val in sensitive:
        if ("sensitivity", val) not in clearances:
            reasons.append(f"{user} is not cleared for sensitivity={val} ({obj})")

    return PolicyDecision(
        allowed=not reasons,
        reasons=reasons,
        touched_table_ids=sorted(ids),
        sensitive_hits=hits,
        untrusted_columns=sorted(set(untrusted)),
        sensitive_source_ids=sorted(sensitive_ids),
    )
