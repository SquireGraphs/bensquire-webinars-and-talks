# Before & After: a trustworthy data agent with Neocarta and Neo4j Agent Memory

One text-to-SQL agent over ACME Corp's 33-table BigQuery warehouse, run in two modes:

| | Before | After |
|---|---|---|
| Context | `list_tables`, `describe_table` | [Neocarta](https://github.com/neo4j-labs/neocarta) MCP tools: governed metrics, synonyms, descriptions, joins |
| Access | Any SELECT runs | SQL → tables/columns → governance tags → user clearances, checked in the graph |
| Memory | Flat shared notes file | [Neo4j Agent Memory](https://neo4j.com/labs/agent-memory/): traces, steps, `TOUCHED` audit edges, notes with provenance |

Same model, same loop, same questions. See [`demo/SCRIPT.md`](demo/SCRIPT.md) for the walkthrough.

> **Labs status.** Neocarta and Neo4j Agent Memory are Neo4j Labs projects: experimental and community-supported. Neocarta is pinned to a main-branch commit because the OSI metric tools and governance-tag model aren't in the 0.8.0 release yet.

## Graph model

```
(:User)-[:HAS_TRACE]->(:ReasoningTrace)-[:HAS_STEP]->(:ReasoningStep)-[:USES_TOOL]->(:ToolCall)
(:ReasoningStep)-[:TOUCHED]->(:Table:Entity)-[:HAS_COLUMN]->(:Column)
(:Table|Column)-[:TAGGED_WITH]->(:GovernanceTag)-[:HAS_DEFINITION]->(:GovernanceTagValue)
(:User)-[:CLEARED_FOR]->(:GovernanceTagValue)                       ← added by this demo
(:ReasoningStep)-[:ASSERTED|RECALLED]->(:Fact)-[:DERIVED_FROM]->(:Column)   ← added by this demo
```

Everything else is created by the two libraries as shipped. Neocarta's `Table` nodes are adopted as Agent Memory entities (`adopt_existing_graph`), so audit edges land directly on the semantic layer.

## Setup

1. **Neo4j.** In Neo4j Desktop 2, create a local instance (any 2025.x/2026.x version), start it, and note the password. Docker or Aura Free work the same way.
2. **Google Cloud.** Use a project where you can create BigQuery datasets, with the BigQuery API enabled. Authenticate once with `gcloud auth application-default login`.
3. **Python 3.11+.**
   ```bash
   cp .env.example .env   # fill in NEO4J_PASSWORD, GCP_PROJECT_ID, ANTHROPIC_API_KEY
   make install
   make test              # offline unit tests for the SQL policy parser
   make load-data         # creates the ACME dataset in BigQuery and prints row counts
   make setup             # Neocarta semantic layer, governance tags, users, memory adoption
   make check             # preflight every dependency; prints ground-truth answers
   make smoke             # one question in each mode, then the audit trail
   ```

`make check` should end with every line passing. Its ground-truth section gives the correct and naive answer for each demo question, computed straight from BigQuery, so you can grade agent runs.

## Measuring reliability

`make eval` runs each demo beat many times, per model, and grades every run automatically: whether the fiscal-year trap fires in before mode, whether after mode gets $970K, whether Alice is denied and Priya isn't, whether restricted figures leak through team notes, and how often an agent takes the injection bait. Each trial is isolated, so trials run in parallel. Presets keep runs focused:

* `--preset models`: the cheap model-robustness set (fiscal-year trap and both injection scenarios in before mode, where only the model defends). Good for comparing models.
* `--preset demo`: the after-mode beats you'll run on stage.
* `--preset side_channel`: no controls, query policy only, and policy plus memory filter.

The summary reports each pass rate with a 95% confidence interval, plus token usage split into uncached input, cache writes and cache reads. Results land in `eval/results/<run_id>/`.

Cost controls (all in `.env`): prompt caching is on by default (`PROMPT_CACHING=0` turns it off); tool results over `MAX_TOOL_RESULT_CHARS` (default 24,000) are truncated; and Neocarta's two whole-layer dump tools are hidden from the agent (`HIDDEN_NEOCARTA_TOOLS`).

## Layout

```
agent/    config, BigQuery access, policy check, memory wrapper, tool loop, modes, CLI
setup/    load_acme_bigquery.py, build_semantic_layer.py, check.py, poison.py (plant/fallback/reset)
eval/     run.py: repeated, graded runs of every beat across models
audit/    audit.py: sessions, trace, who-touched, access-review, notes, provenance, blast-radius, quarantine, review
data/     ACME BigQuery SQL and OSI semantic model (from neo4j-labs/neocarta, Apache-2.0)
demo/     SCRIPT.md
tests/    policy parser tests
```

## Known limits (say these out loud)

* `TOUCHED` edges are table-level. Column-level reads are enforced by the policy check but not recorded as edges.
* The user identity is a CLI flag. In production it comes from your identity provider, and policy runs server-side, outside the agent process.
* The review hold contains injected instructions in shared memory. It doesn't detect or prevent injection.
* Agent behavior is non-deterministic. Rehearse each beat; `setup/poison.py` has scripted fallbacks.

## License

Demo code: Apache-2.0. `data/acme-dataset.sql` and `data/acme_semantic_model.yaml` are from [neo4j-labs/neocarta](https://github.com/neo4j-labs/neocarta) (Apache-2.0).
