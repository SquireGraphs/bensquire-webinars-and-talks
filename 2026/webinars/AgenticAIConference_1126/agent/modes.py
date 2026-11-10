"""The two modes. Same model, same loop, same question; different context.

BEFORE: list_tables / describe_table / run_sql, plus a flat shared notes file.
AFTER:  Neocarta's MCP tools (exactly as shipped), a policy-gated run_sql, and
        team notes stored in Neo4j Agent Memory with provenance.
"""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from neo4j import GraphDatabase

from . import config, policy, warehouse
from .llm import Tool
from .memory import AuditedSession

SYSTEM_PROMPT = """You are ACME Corp's analytics agent, answering questions for {user}.
You query ACME's BigQuery warehouse. The dataset `{dataset}` is the default dataset,
so write table names without a prefix (e.g. `FROM subscriptions`).

Find the right tables and definitions with your tools before writing SQL. In your final
answer give the number(s), the SQL you ran, and one sentence on how you computed it.

Analysts share team notes. If you learn something durable about how ACME's data should
be used (a caveat, a correct metric definition, a known data issue), save it with
save_team_note so the next analyst benefits.

Team notes from colleagues:
{notes}
"""

SQL_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string", "description": "A single BigQuery SELECT statement."}},
    "required": ["sql"],
}
NOTE_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "The note to share with teammates."}},
    "required": ["text"],
}


def render_notes(notes: list[str]) -> str:
    return "\n".join(f"- {n}" for n in notes) if notes else "(none)"


# --- BEFORE ------------------------------------------------------------------


def _load_notes_file(path: Path) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


async def before_mode(user: str, notes_file: str | None = None) -> tuple[str, list[Tool]]:
    path = Path(notes_file or config.NOTES_FILE)

    async def list_tables(_: dict) -> str:
        return await asyncio.to_thread(warehouse.list_tables)

    async def describe_table(args: dict) -> str:
        return await asyncio.to_thread(warehouse.describe_table, args["table_name"])

    async def run_sql(args: dict) -> str:
        return await asyncio.to_thread(warehouse.run_sql, args["sql"])

    async def save_team_note(args: dict) -> str:
        notes = _load_notes_file(path) + [{"text": args["text"]}]
        path.write_text(json.dumps(notes, indent=1))
        return "Note saved and shared with the team."

    tools = [
        Tool("list_tables", "List tables in the ACME dataset.", {"type": "object", "properties": {}}, list_tables),
        Tool(
            "describe_table",
            "List a table's columns and types.",
            {"type": "object", "properties": {"table_name": {"type": "string"}}, "required": ["table_name"]},
            describe_table,
        ),
        Tool("run_sql", "Run a read-only BigQuery SELECT.", SQL_SCHEMA, run_sql),
        Tool("save_team_note", "Share a durable note with teammates.", NOTE_SCHEMA, save_team_note),
    ]
    notes = [n["text"] for n in _load_notes_file(path)]
    system = SYSTEM_PROMPT.format(user=user, dataset=config.BIGQUERY_DATASET_ID, notes=render_notes(notes))
    return system, tools


# --- AFTER -------------------------------------------------------------------


def _mcp_text(result) -> str:
    parts = [getattr(c, "text", "") for c in getattr(result, "content", []) or []]
    return "\n".join(p for p in parts if p) or json.dumps(getattr(result, "structuredContent", None), default=str)


async def after_mode(
    user: str, session: AuditedSession, stack: AsyncExitStack
) -> tuple[str, list[Tool]]:
    driver = GraphDatabase.driver(config.NEO4J_URI, auth=config.neo4j_auth())
    stack.callback(driver.close)

    # Neocarta's MCP server, launched over stdio exactly as a user would run it.
    params = StdioServerParameters(command="neocarta-mcp", env=dict(os.environ))
    errlog = stack.enter_context(open(config.MCP_LOG_FILE, "a"))
    read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
    mcp_session = await stack.enter_async_context(ClientSession(read, write))
    await mcp_session.initialize()
    listed = await mcp_session.list_tools()

    def neocarta_tool(t) -> Tool:
        async def handler(args: dict, _name=t.name) -> str:
            text = _mcp_text(await mcp_session.call_tool(_name, args))
            await session.record_tool(_name, args, text)
            return text

        return Tool(t.name, t.description or "", t.inputSchema, handler)

    tools = [neocarta_tool(t) for t in listed.tools if t.name not in config.HIDDEN_NEOCARTA_TOOLS]

    async def run_sql(args: dict) -> str:
        sql = args["sql"]
        decision = await asyncio.to_thread(policy.check, driver, user, sql)
        if not decision.allowed:
            result = "DENIED by data policy:\n" + "\n".join(f"- {r}" for r in decision.reasons)
            await session.record_tool("run_sql", args, result, ok=False, touched_table_ids=decision.touched_table_ids)
            return result
        result = await asyncio.to_thread(warehouse.run_sql, sql)
        session.untrusted_columns.update(decision.untrusted_columns)
        session.sensitive_sources.update(decision.sensitive_source_ids)
        await session.record_tool("run_sql", args, result, touched_table_ids=decision.touched_table_ids)
        return result

    async def save_team_note(args: dict) -> str:
        return await session.save_team_note(args["text"])

    tools += [
        Tool(
            "run_sql",
            "Run a read-only BigQuery SELECT. Checked against data-access policy before it runs.",
            SQL_SCHEMA,
            run_sql,
        ),
        Tool("save_team_note", "Share a durable note with teammates.", NOTE_SCHEMA, save_team_note),
    ]
    notes = [n["text"] for n in await session.recall_team_notes()]
    system = SYSTEM_PROMPT.format(user=user, dataset=config.BIGQUERY_DATASET_ID, notes=render_notes(notes))
    return system, tools
