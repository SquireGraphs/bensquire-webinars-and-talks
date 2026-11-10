"""Preflight: check every dependency without running the agent.

    python -m setup.check

Each check prints PASS or FAIL with a hint, and the script keeps going so you
see everything that's wrong at once. The last section prints the ground-truth
answers for each demo question, computed straight from BigQuery, so you can
grade agent runs during rehearsal.
"""

import asyncio
import os
import shutil
from numbers import Number

from rich.console import Console

from agent import config

console = Console()
results: list[bool] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    mark = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
    console.print(f"{mark}  {name}" + (f"  [dim]{detail}[/dim]" if detail else ""))


def warn(name: str, detail: str) -> None:
    console.print(f"[yellow]WARN[/yellow]  {name}  [dim]{detail}[/dim]")


def check_env() -> None:
    for var in ["GCP_PROJECT_ID", "BIGQUERY_DATASET_ID", "NEO4J_URI", "NEO4J_PASSWORD", "ANTHROPIC_API_KEY"]:
        report(f"env {var}", bool(os.getenv(var)), "" if os.getenv(var) else "set it in .env")


def check_bigquery():
    try:
        from google.cloud import bigquery

        client = bigquery.Client(project=config.GCP_PROJECT_ID)
        tables = list(client.list_tables(f"{config.GCP_PROJECT_ID}.{config.BIGQUERY_DATASET_ID}"))
        report("BigQuery dataset has 33 tables", len(tables) == 33, f"found {len(tables)}")
        return client
    except Exception as e:
        report("BigQuery reachable", False, f"{type(e).__name__}: {e}. Try: gcloud auth application-default login")
        return None


def check_neo4j():
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(config.NEO4J_URI, auth=config.neo4j_auth())
        records, _, _ = driver.execute_query(
            "CALL dbms.components() YIELD versions, edition RETURN versions[0] AS v, edition",
            database_=config.NEO4J_DATABASE,
        )
        version, edition = records[0]["v"], records[0]["edition"]
        major, minor = (int(x) for x in version.split(".")[:2])
        report("Neo4j reachable, 5.20+", (major, minor) >= (5, 20), f"{version} {edition}")
        return driver
    except Exception as e:
        report("Neo4j reachable", False, f"{type(e).__name__}: {e}. Is the Desktop DBMS started?")
        return None


def check_semantic_layer(driver) -> None:
    records, _, _ = driver.execute_query(
        """
        RETURN COUNT { (:Table) } AS tables, COUNT { (:Metric) } AS metrics,
               COUNT { ()-[:TAGGED_WITH]->(:GovernanceTag) } AS tags,
               COUNT { (:Table:Entity) } AS adopted,
               COUNT { (:User)-[:CLEARED_FOR]->() } AS clearances,
               COUNT { (:BusinessTerm {name: 'bookings'}) } AS bookings_term
        """,
        database_=config.NEO4J_DATABASE,
    )
    r = dict(records[0])
    hint = "run: make setup"
    report("Neocarta tables = 33", r["tables"] == 33, f"{r['tables']} ({hint})" if r["tables"] != 33 else "")
    report("Neocarta metrics = 9", r["metrics"] == 9, str(r["metrics"]))
    report("Synonym 'bookings' present", r["bookings_term"] > 0)
    report("Governance tags applied (>= 11)", r["tags"] >= 11, str(r["tags"]))
    report("Tables adopted by Agent Memory = 33", r["adopted"] == 33, str(r["adopted"]))
    report("Priya's clearances (2)", r["clearances"] == 2, str(r["clearances"]))


async def check_mcp() -> None:
    if not shutil.which("neocarta-mcp"):
        report("neocarta-mcp on PATH", False, "run: make install")
        return
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command="neocarta-mcp", env=dict(os.environ))
        with open(config.MCP_LOG_FILE, "a") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as s:
                await s.initialize()
                names = sorted(t.name for t in (await s.list_tools()).tools)
        report("Neocarta MCP starts", True, f"{len(names)} tools")
        report(
            "Neocarta metric tools registered",
            "get_metric_expression" in names,
            "missing: check the neocarta pin in requirements.txt" if "get_metric_expression" not in names else "",
        )
        if any("business_term_hybrid" in n for n in names):
            report("Synonym-aware search (business-term hybrid)", True)
        else:
            warn(
                "Synonym-aware search not registered",
                "full-text search won't map 'bookings' to its metric. For the alignment beat, set "
                "OPENAI_API_KEY and run: make setup ARGS='--skip-bigquery --embeddings'",
            )
        console.print(f"      [dim]{', '.join(names)}[/dim]")
    except Exception as e:
        report("Neocarta MCP starts", False, f"{type(e).__name__}: {e}")


def check_embeddings() -> None:
    try:
        import litellm

        kwargs = {"dimensions": config.EMBEDDING_DIMENSIONS}
        try:
            resp = litellm.embedding(model=config.EMBEDDING_MODEL, input=["bookings"], **kwargs)
        except Exception:
            resp = litellm.embedding(model=config.EMBEDDING_MODEL, input=["bookings"])
        dims = len(resp.data[0]["embedding"])
        report(f"Embedding model {config.EMBEDDING_MODEL}", True, f"{dims} dimensions")
        if dims != config.EMBEDDING_DIMENSIONS:
            warn(
                "Embedding dimensions differ from EMBEDDING_DIMENSIONS",
                f"got {dims}, expected {config.EMBEDDING_DIMENSIONS}; pick a model that supports {config.EMBEDDING_DIMENSIONS}",
            )
    except Exception as e:
        msg = str(e).splitlines()[0][:220]
        warn(f"Embedding model {config.EMBEDDING_MODEL} unreachable", f"{type(e).__name__}: {msg}")


def check_llm() -> None:
    try:
        from anthropic import Anthropic

        Anthropic(**config.anthropic_client_kwargs()).messages.create(
            model=config.AGENT_MODEL, max_tokens=5, messages=[{"role": "user", "content": "ping"}]
        )
        report(f"Anthropic model {config.AGENT_MODEL}", True)
    except Exception as e:
        report(f"Anthropic model {config.AGENT_MODEL}", False, f"{type(e).__name__}: {e}")


GROUND_TRUTH = {
    "Bookings, governed (closed-won opportunities)":
        "SELECT SUM(amount_usd) FROM opportunities WHERE stage = 'closed_won'",
    "Bookings, naive (sum of orders)": "SELECT SUM(total_usd) FROM orders",
    "FY2025 bookings, governed (Jul 2024 - Jun 2025)":
        "SELECT SUM(amount_usd) FROM opportunities WHERE stage = 'closed_won' "
        "AND close_date BETWEEN '2024-07-01' AND '2025-06-30'",
    "FY2025 bookings, naive (calendar 2025 deals)":
        "SELECT SUM(amount_usd) FROM opportunities WHERE stage = 'closed_won' "
        "AND EXTRACT(YEAR FROM close_date) = 2025",
    "FY2025 bookings, naive (calendar 2025 orders)":
        "SELECT SUM(total_usd) FROM orders WHERE EXTRACT(YEAR FROM order_date) = 2025",
    "ARR, governed (active subscriptions)":
        "SELECT SUM(CASE WHEN status = 'active' THEN arr_usd ELSE 0 END) FROM subscriptions",
    "ARR, naive (every subscription row)": "SELECT SUM(arr_usd) FROM subscriptions",
    "Logos, governed (active customers)":
        "SELECT COUNT(DISTINCT CASE WHEN status = 'active' THEN customer_id END) FROM customers",
    "Logos, naive (all customers)": "SELECT COUNT(*) FROM customers",
    "Planted comment present (0 or 1)":
        "SELECT COUNT(*) FROM ticket_comments WHERE comment_id = 'TCOM013'",
}


def print_ground_truth(client) -> None:
    from google.cloud import bigquery

    console.rule("Ground truth for grading rehearsals")
    job_config = bigquery.QueryJobConfig(default_dataset=f"{config.GCP_PROJECT_ID}.{config.BIGQUERY_DATASET_ID}")
    for label, sql in GROUND_TRUTH.items():
        try:
            value = next(iter(client.query(sql, job_config=job_config).result()))[0]
            console.print(f"  {label:<48} {value:,}" if isinstance(value, Number) else f"  {label:<48} {value}")
        except Exception as e:
            console.print(f"  {label:<48} [red]{type(e).__name__}[/red]")


def main() -> None:
    console.rule("Environment")
    check_env()
    console.rule("BigQuery")
    bq = check_bigquery()
    console.rule("Neo4j")
    driver = check_neo4j()
    if driver:
        check_semantic_layer(driver)
        driver.close()
    console.rule("Neocarta MCP")
    asyncio.run(check_mcp())
    console.rule("Embeddings (optional)")
    check_embeddings()
    console.rule("LLM")
    check_llm()
    if bq:
        print_ground_truth(bq)
    failed = results.count(False)
    console.rule(f"{len(results) - failed} passed, {failed} failed")


if __name__ == "__main__":
    main()
