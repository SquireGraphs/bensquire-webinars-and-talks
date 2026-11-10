"""Answer governance and incident questions with graph traversals.

    python -m audit.audit sessions
    python -m audit.audit trace latest
    python -m audit.audit who-touched compensation
    python -m audit.audit access-review
    python -m audit.audit notes
    python -m audit.audit provenance <note-id|latest|poisoned|downstream>
    python -m audit.audit blast-radius <note-id>
    python -m audit.audit quarantine <note-id> [--cascade]
    python -m audit.audit review <note-id> --approve|--reject --reviewer <name>

Add --show-cypher to print each query before running it (handy on stage).
"""

import argparse

from neo4j import GraphDatabase
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from agent import config

console = Console()
SHOW_CYPHER = False

Q = {
    "sessions": """
MATCH (u:User)-[:HAS_TRACE]->(t:ReasoningTrace)
RETURN u.identifier AS user, t.id AS trace, t.task AS question,
       COUNT { (t)-[:HAS_STEP]->() } AS steps, left(coalesce(t.outcome, ''), 90) AS outcome
ORDER BY t.started_at DESC LIMIT 20""",
    "trace": """
MATCH (u:User)-[:HAS_TRACE]->(t:ReasoningTrace)
WHERE t.id = $trace OR $trace = 'latest'
WITH u, t ORDER BY t.started_at DESC LIMIT 1
MATCH (t)-[:HAS_STEP]->(s:ReasoningStep)
OPTIONAL MATCH (s)-[:USES_TOOL]->(tc:ToolCall)
OPTIONAL MATCH (s)-[:TOUCHED]->(tbl:Table)
OPTIONAL MATCH (tbl)-[:TAGGED_WITH]->(g:GovernanceTag)
RETURN s.step_number AS step, s.action AS action, tc.status AS status,
       collect(DISTINCT tbl.name) AS touched,
       collect(DISTINCT g.key + '=' + g.value) AS table_tags,
       COUNT { (s)-[:RECALLED|ASSERTED]->(:Fact) } AS notes
ORDER BY step""",
    "who-touched": """
MATCH (tbl:Table {name: $table})<-[:TOUCHED]-(s:ReasoningStep)<-[:HAS_STEP]-(t:ReasoningTrace)
      <-[:HAS_TRACE]-(u:User)
OPTIONAL MATCH (s)-[:USES_TOOL]->(tc:ToolCall)
RETURN u.identifier AS user, u.role AS role, t.task AS question,
       tc.status AS status, toString(s.timestamp) AS at
ORDER BY at DESC""",
    "access-review": """
MATCH (u:User)-[:HAS_TRACE]->(t:ReasoningTrace)-[:HAS_STEP]->(s:ReasoningStep)
      -[:USES_TOOL]->(tc:ToolCall {tool_name: 'run_sql'})
OPTIONAL MATCH (s)-[:TOUCHED]->(tbl:Table)-[:TAGGED_WITH]->(g:GovernanceTag {key: 'sensitivity'})
WITH u, t, tc, collect(DISTINCT tbl.name + ' (' + g.value + ')') AS sensitive
WHERE tc.status <> 'success' OR size(sensitive) > 0
RETURN u.identifier AS user, u.role AS role, tc.status AS status, sensitive,
       left(t.task, 70) AS question
ORDER BY user""",
    "notes": """
MATCH (f:Fact {subject: 'team_note'})
OPTIONAL MATCH (f)-[:DERIVED_FROM]->(src)-[:TAGGED_WITH]->(g:GovernanceTag)
OPTIONAL MATCH (srcT:Table)-[:HAS_COLUMN]->(src)
WITH f, collect(DISTINCT coalesce(srcT.name + '.', '') + src.name + ' (' + g.key + '=' + g.value + ')')
        AS derived_from
RETURN f.id AS note, coalesce(f.status, 'active') AS status, f.asserted_by AS author,
       derived_from, left(f.object, 80) AS text,
       COUNT { (f)<-[:RECALLED]-() } AS times_recalled
ORDER BY f.created_at""",
    "provenance": """
MATCH (f:Fact {id: $note})<-[:ASSERTED]-(s:ReasoningStep)<-[:HAS_STEP]-(t:ReasoningTrace)
      <-[:HAS_TRACE]-(u:User)
MATCH (t)-[:HAS_STEP]->(prev:ReasoningStep)-[:TOUCHED]->(tbl:Table)
WHERE prev.step_number < s.step_number
OPTIONAL MATCH (tbl)-[:HAS_COLUMN]->(c:Column)-[:TAGGED_WITH]->(:GovernanceTag {key: 'trust', value: 'untrusted_input'})
OPTIONAL MATCH (prev)-[:USES_TOOL]->(tc:ToolCall)
RETURN u.identifier AS author, t.task AS session_question, prev.step_number AS step,
       tbl.name AS table_read, collect(DISTINCT c.name) AS untrusted_columns,
       left(coalesce(tc.result, ''), 160) AS what_the_agent_saw
ORDER BY step""",
    "blast-radius": """
MATCH (f:Fact {id: $note})<-[:RECALLED]-(:ReasoningStep)<-[:HAS_STEP]-(t:ReasoningTrace)
      <-[:HAS_TRACE]-(u:User)
RETURN u.identifier AS user, u.role AS role, t.id AS trace, t.task AS question,
       left(coalesce(t.outcome, ''), 100) AS answer_given
ORDER BY t.started_at""",
    "contagion": """
MATCH (f:Fact {id: $note})
      ((:Fact)<-[:RECALLED]-(:ReasoningStep)<-[:HAS_STEP]-(:ReasoningTrace)
       -[:HAS_STEP]->(:ReasoningStep)-[:ASSERTED]->(:Fact)){1,5}
      (downstream:Fact)
WHERE downstream <> f
RETURN DISTINCT downstream.id AS note, downstream.asserted_by AS author,
       left(downstream.object, 80) AS text""",
    "quarantine": """
MATCH (f:Fact) WHERE f.id IN $notes
SET f.status = 'quarantined', f.quarantined_at = datetime()
RETURN f.id AS note, f.status AS status""",
    "review": """
MATCH (f:Fact {id: $note})
SET f.status = $status, f.reviewed_by = $reviewer, f.reviewed_at = datetime()
RETURN f.id AS note, f.status AS status, f.reviewed_by AS reviewer""",
}


_LATEST_NOTE = "MATCH (f:Fact {subject: 'team_note'}) RETURN f.id AS id ORDER BY f.created_at DESC LIMIT 1"
# Notes written after their session read untrusted input, newest first; notes still in
# play (active or awaiting review) come before ones already quarantined.
_UNTRUSTED_NOTES = """
MATCH (f:Fact {subject: 'team_note'})-[:DERIVED_FROM]->(:Column)
      -[:TAGGED_WITH]->(:GovernanceTag {key: 'trust', value: 'untrusted_input'})
WITH DISTINCT f
RETURN f.id AS id
ORDER BY CASE coalesce(f.status, 'active') WHEN 'quarantined' THEN 1 ELSE 0 END, f.created_at DESC"""


def resolve_note(driver, note: str) -> str:
    """Accept a note id, or a keyword so nobody copies the wrong id on stage:
    latest     the most recently written team note
    poisoned   the newest note written after its session read untrusted input,
               preferring notes still in play over quarantined ones
    downstream the newest note written by a session that recalled a poisoned note
    """
    if note not in {"latest", "poisoned", "downstream"}:
        return note
    if note == "latest":
        records, _, _ = driver.execute_query(_LATEST_NOTE, database_=config.NEO4J_DATABASE)
        if not records:
            raise SystemExit("No team notes yet.")
        note_id = records[0]["id"]
    else:
        records, _, _ = driver.execute_query(_UNTRUSTED_NOTES, database_=config.NEO4J_DATABASE)
        if not records:
            raise SystemExit("No note derived from untrusted input found.")
        note_id = records[0]["id"]
        if note == "downstream":
            note_id = None
            for r in records:  # first poisoned note, in that order, that has downstream notes
                rows, _, _ = driver.execute_query(Q["contagion"], note=r["id"], database_=config.NEO4J_DATABASE)
                if rows:
                    note_id = rows[-1]["note"]
                    break
            if note_id is None:
                raise SystemExit("No note was written downstream of a poisoned note.")
    console.print(f"[dim]{note} note: {note_id}[/dim]")
    return note_id


def run(driver, name: str, title: str, **params) -> list[dict]:
    if SHOW_CYPHER:
        console.print(Syntax(Q[name].strip(), "cypher", theme="ansi_dark", word_wrap=True))
    records, _, _ = driver.execute_query(Q[name], database_=config.NEO4J_DATABASE, **params)
    rows = [dict(r) for r in records]
    table = Table(title=title, show_lines=False)
    for col in rows[0].keys() if rows else ["(no results)"]:
        table.add_column(col, overflow="fold")
    for row in rows:
        table.add_row(*[", ".join(map(str, v)) if isinstance(v, list) else str(v) for v in row.values()])
    console.print(table)
    return rows


def main() -> None:
    global SHOW_CYPHER
    p = argparse.ArgumentParser()
    p.add_argument("--show-cypher", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sessions")
    sub.add_parser("trace").add_argument("trace", nargs="?", default="latest")
    sub.add_parser("who-touched").add_argument("table")
    sub.add_parser("access-review")
    sub.add_parser("notes")
    sub.add_parser("provenance").add_argument("note")
    sub.add_parser("blast-radius").add_argument("note")
    q = sub.add_parser("quarantine")
    q.add_argument("note")
    q.add_argument("--cascade", action="store_true", help="Also quarantine notes derived from it")
    r = sub.add_parser("review")
    r.add_argument("note")
    r.add_argument("--reviewer", required=True)
    decision = r.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--reject", action="store_true")
    args = p.parse_args()
    SHOW_CYPHER = args.show_cypher
    config.require("NEO4J_PASSWORD")

    with GraphDatabase.driver(config.NEO4J_URI, auth=config.neo4j_auth()) as d:
        if getattr(args, "note", None):
            args.note = resolve_note(d, args.note)
        match args.cmd:
            case "sessions":
                run(d, "sessions", "Agent sessions")
            case "trace":
                run(d, "trace", f"Trace {args.trace}", trace=args.trace)
            case "who-touched":
                run(d, "who-touched", f"Who touched {args.table}", table=args.table)
            case "access-review":
                run(d, "access-review", "Denied queries and sensitive-table access")
            case "notes":
                run(d, "notes", "Team notes")
            case "provenance":
                run(d, "provenance", f"Where note {args.note} came from", note=args.note)
            case "blast-radius":
                run(d, "blast-radius", "Sessions that recalled this note", note=args.note)
                run(d, "contagion", "Notes written downstream of it", note=args.note)
            case "quarantine":
                notes = [args.note]
                if args.cascade:
                    rows, _, _ = d.execute_query(Q["contagion"], note=args.note, database_=config.NEO4J_DATABASE)
                    notes += [row["note"] for row in rows]
                run(d, "quarantine", "Quarantined", notes=notes)
                run(d, "blast-radius", "Re-review these answers", note=args.note)
            case "review":
                status = "active" if args.approve else "quarantined"
                run(d, "review", "Review recorded", note=args.note, status=status, reviewer=args.reviewer)


if __name__ == "__main__":
    main()
