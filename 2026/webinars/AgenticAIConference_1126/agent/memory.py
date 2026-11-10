"""Graph-native memory for "after" mode, built on neo4j-agent-memory.

What the library provides (used as-is):
  * (:User)-[:HAS_CONVERSATION]->(:Conversation)-[:HAS_MESSAGE]->(:Message)
  * (:User)-[:HAS_TRACE]->(:ReasoningTrace)-[:INITIATED_BY]->(:Message)
  * (:ReasoningTrace)-[:HAS_STEP]->(:ReasoningStep)-[:USES_TOOL]->(:ToolCall)
  * (:ReasoningStep)-[:TOUCHED]->(:Entity) audit edges. Neocarta's Table nodes are
    adopted as :Entity during setup, so TOUCHED lands on the semantic layer itself.
  * (:Fact) nodes for team notes.

What this demo adds on top (three small edges and two properties):
  * (:ReasoningStep)-[:ASSERTED]->(:Fact)   which step wrote a note
  * (:ReasoningStep)-[:RECALLED]->(:Fact)   which later session read it
  * (:Fact)-[:DERIVED_FROM]->(:Table|Column) untrusted or sensitive sources the
    session had read. A note inherits its sources' tags: it's only recalled for
    users cleared for every sensitivity tag on them.
  * Fact.status ('active' | 'pending_review' | 'quarantined') and Fact.asserted_by
"""

import json
from typing import Any
from uuid import uuid4

from neo4j import GraphDatabase
from neocarta.connectors.utils.generate_id import generate_column_id
from neo4j_agent_memory import MemoryClient, MemorySettings, ToolCallStatus
from neo4j_agent_memory.schema.models import EntityRef

from . import config

RESULT_PREVIEW_CHARS = 2000


def memory_settings() -> MemorySettings:
    # No embeddings or entity extraction: this demo reads memory by graph
    # traversal, which keeps it deterministic and needs no extra API key.
    return MemorySettings(
        neo4j={
            "uri": config.NEO4J_URI,
            "username": config.NEO4J_USERNAME,
            "password": config.NEO4J_PASSWORD,
            "database": config.NEO4J_DATABASE,
        },
        embedding={"provider": "custom"},
        extraction={"extractor_type": "none"},
        memory={"multi_tenant": True},
    )


_ACTIVE_NOTES = """
MATCH (f:Fact {subject: 'team_note'})
WHERE coalesce(f.status, 'active') = 'active'
  AND coalesce(f.scope, '') = $scope
  AND NOT EXISTS {
    MATCH (f)-[:DERIVED_FROM]->()-[:TAGGED_WITH]->(:GovernanceTag {key: 'sensitivity'})
          -[:HAS_DEFINITION]-(v:GovernanceTagValue)
    WHERE NOT EXISTS { (:User {identifier: $user})-[:CLEARED_FOR]->(v) }
  }
RETURN f.id AS id, f.object AS text, f.asserted_by AS author
ORDER BY f.created_at
"""

# Same recall without the clearance check: what "query policy only" looks like.
_ACTIVE_NOTES_UNFILTERED = """
MATCH (f:Fact {subject: 'team_note'})
WHERE coalesce(f.status, 'active') = 'active'
  AND coalesce(f.scope, '') = $scope
RETURN f.id AS id, f.object AS text, f.asserted_by AS author
ORDER BY f.created_at
"""

_LINK_RECALLED = """
MATCH (s:ReasoningStep {id: $step_id})
UNWIND $fact_ids AS fid
MATCH (f:Fact {id: fid})
MERGE (s)-[r:RECALLED]->(f)
ON CREATE SET r.recorded_at = datetime()
"""

_LINK_ASSERTED = """
MATCH (s:ReasoningStep {id: $step_id}), (f:Fact {id: $fact_id})
MERGE (s)-[:ASSERTED]->(f)
SET f.status = $status, f.asserted_by = $user, f.scope = $scope
WITH f
UNWIND $source_ids AS sid
MATCH (src) WHERE (src:Column OR src:Table) AND src.id = sid
MERGE (f)-[:DERIVED_FROM]->(src)
"""


class AuditedSession:
    """One question from one user, recorded as a reasoning trace."""

    def __init__(
        self,
        user: str,
        question: str,
        review_hold: bool = True,
        scope: str = "",
        memory_filter: bool = True,
    ):
        # memory_filter=False recalls every note regardless of the reader's clearances,
        # to show what a query-level policy alone leaves open.
        self.memory_filter = memory_filter
        # scope isolates team notes (used by the eval harness to run trials in parallel);
        # the live demo uses the default, shared scope "".
        self.scope = scope
        self.user = user
        self.question = question
        self.review_hold = review_hold
        self.session_id = f"{'eval-' if scope else ''}{user}-{uuid4().hex[:8]}"
        self.untrusted_columns: set[str] = set()  # "table.column" read this session
        self.sensitive_sources: set[str] = set()  # Table/Column node ids read this session
        self.memory: MemoryClient | None = None
        self.trace = None
        self.driver = GraphDatabase.driver(config.NEO4J_URI, auth=config.neo4j_auth())

    async def __aenter__(self) -> "AuditedSession":
        self.memory = MemoryClient(memory_settings())
        await self.memory.connect()
        msg = await self.memory.short_term.add_message(
            self.session_id,
            "user",
            self.question,
            extraction_mode="skip",
            generate_embedding=False,
            user_identifier=self.user,
        )
        self.trace = await self.memory.reasoning.start_trace(
            self.session_id,
            self.question,
            generate_embedding=False,
            triggered_by_message_id=msg.id,
            user_identifier=self.user,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.memory is not None:
            await self.memory.close()
        self.driver.close()

    def _write(self, query: str, **params: Any) -> None:
        self.driver.execute_query(query, database_=config.NEO4J_DATABASE, **params)

    async def recall_team_notes(self) -> list[dict]:
        """Load active team notes and record that this session read them."""
        records, _, _ = self.driver.execute_query(
            _ACTIVE_NOTES if self.memory_filter else _ACTIVE_NOTES_UNFILTERED,
            user=self.user,
            scope=self.scope,
            database_=config.NEO4J_DATABASE,
        )
        notes = [dict(r) for r in records]
        step = await self.memory.reasoning.add_step(
            self.trace.id,
            thought="Load shared team notes into context",
            action="recall_team_notes",
            observation=f"{len(notes)} active note(s)",
            generate_embedding=False,
        )
        if notes:
            self._write(_LINK_RECALLED, step_id=str(step.id), fact_ids=[n["id"] for n in notes])
        return notes

    async def record_tool(
        self,
        tool_name: str,
        arguments: dict,
        result: str,
        *,
        ok: bool = True,
        touched_table_ids: list[str] | None = None,
        thought: str | None = None,
    ):
        step = await self.memory.reasoning.add_step(
            self.trace.id, thought=thought, action=tool_name, generate_embedding=False
        )
        await self.memory.reasoning.record_tool_call(
            step.id,
            tool_name,
            arguments,
            result=result[:RESULT_PREVIEW_CHARS],
            status=ToolCallStatus.SUCCESS if ok else ToolCallStatus.FAILURE,
            touched_entities=[EntityRef(id=t) for t in (touched_table_ids or [])],
        )
        return step

    async def save_team_note(self, text: str) -> str:
        """Write a team note as its own audited step.

        Notes written after this session read untrusted input are held for review.
        """
        step = await self.memory.reasoning.add_step(
            self.trace.id, action="save_team_note", generate_embedding=False
        )
        message = await self._write_note(text, str(step.id))
        await self.memory.reasoning.record_tool_call(
            step.id, "save_team_note", {"text": text}, result=message
        )
        return message

    async def _write_note(self, text: str, step_id: str) -> str:
        held = self.review_hold and bool(self.untrusted_columns)
        fact = await self.memory.long_term.add_fact(
            "team_note",
            "states",
            text,
            generate_embedding=False,
            metadata={"session_id": self.session_id, "user": self.user},
        )
        column_ids = []
        for qualified in sorted(self.untrusted_columns):
            table, column = qualified.split(".", 1)
            column_ids.append(
                generate_column_id(
                    config.GCP_PROJECT_ID, config.BIGQUERY_DATASET_ID, table, column
                )
            )
        self._write(
            _LINK_ASSERTED,
            step_id=step_id,
            fact_id=str(fact.id),
            status="pending_review" if held else "active",
            user=self.user,
            scope=self.scope,
            source_ids=column_ids + sorted(self.sensitive_sources),
        )
        visibility = (
            " It inherits the sensitivity of the data this session read, so only users "
            "cleared for that data will see it."
            if self.sensitive_sources
            else ""
        )
        if held:
            return (
                f"Note {fact.id} saved but HELD for human review: this session read untrusted "
                f"input ({', '.join(sorted(self.untrusted_columns))}). It will not be shared "
                "until someone approves it."
            )
        return f"Note {fact.id} saved and shared with the team.{visibility}"

    async def finish(self, answer: str) -> None:
        await self.memory.short_term.add_message(
            self.session_id,
            "assistant",
            answer,
            extraction_mode="skip",
            generate_embedding=False,
            user_identifier=self.user,
        )
        await self.memory.reasoning.complete_trace(self.trace.id, outcome=answer, success=True)


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, indent=1)
