# Graph Data Science in Python: workshop companion

Follow-along material for the Broadcom Graphathon 2026 workshop. A fictional air-freight network, a loader, and one
notebook that walks through four questions graph algorithms answer:

1. **What matters most?** Degree, PageRank (weighted and personalized), Betweenness
2. **What breaks if this fails?** WCC, Articulation Points, Bridges
3. **How do things travel?** BFS, Dijkstra, Yen's k-shortest paths
4. **What belongs together?** Louvain, Node Similarity, painting labels from seeds, FastRP + KNN

Everything runs offline once the packages are installed. The data is generated locally and is fully synthetic.

## Requirements

- Neo4j 5.26+ or 2025.x+ with the **Graph Data Science plugin, version 2.13 or later**.
  Check with `RETURN gds.version()` in Neo4j Browser.
- Python 3.10+

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then set your password (and URI if not local)
jupyter lab gds_workshop.ipynb
```

The notebook loads the data itself. To (re)load from the command line instead: `python load_data.py`.
Loading wipes and recreates only the workshop labels (`Airport`, `Carrier`, `Route`, `Shipper`, `Contract`), so
it is safe to run in a database you also use for other things.

## What's in the repo

| File | Purpose |
|---|---|
| `gds_workshop.ipynb` | The workshop notebook. Cells marked *(optional)* are skipped in the live demo. |
| `data_generator.py` | Generates the dataset deterministically. `python data_generator.py` writes CSVs to `data/`. |
| `load_data.py` | Creates constraints and loads the data with batched `UNWIND` writes. |

The stored model:

```
(:Shipper)-[:HAS_CONTRACT]->(:Contract)-[:SHIPS_ON]->(:Route)-[:FROM|TO]->(:Airport)
(:Carrier)-[:OPERATES]->(:Route)
```

46 airports, 81 routes, 6 carriers, 16 shippers and 144 contracts. Small enough to read every result,
big enough for the algorithms to find things you wouldn't spot by eye.

## Using it with a coding agent

Install the Neo4j agent skills so your agent writes current GDS code:

```bash
# Claude Code
/plugin marketplace add https://github.com/neo4j-contrib/neo4j-skills.git
/plugin install neo4j-skills@neo4j-skills-marketplace

# other agents (Codex, Gemini CLI, ...)
npx skills add https://github.com/neo4j-contrib/neo4j-skills
```

Tell the agent which client version you're on. This repo uses **`graphdatascience` 2.x**, where endpoints are
called directly (`gds.page_rank.stream(...)`). Older 1.x clients used a `gds.v2.` prefix for the same calls.

Prompts worth trying against the loaded data:

- *Which airports are single points of failure, and what would each one cut off?*
- *Rank airports by the contract revenue that depends on them. How different is that from ranking by flights?*
- *If Corvina closed for a week, which shippers would be affected and what are the cheapest reroutes?*

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Unknown function 'gds.version'` | The GDS plugin isn't installed or enabled on this Neo4j instance. |
| Warning: *Client does not support the given server version* | GDS is older than 2.13. Upgrade GDS, or install `graphdatascience<2` (the 1.x API uses different method names). |
| `Graph '...' already exists` | A projection survived an earlier run. The projection cells drop and recreate, so rerun them; or `CALL gds.graph.drop('name', false)`. |
| `Node property 'embedding' already exists` | Re-running FastRP on the same projection. Rerun the projection cell first. |
| Results look stale after a `write` | Projections are snapshots. Re-project to see newly written properties. |
