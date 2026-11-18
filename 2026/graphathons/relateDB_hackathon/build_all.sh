#!/usr/bin/env bash
# Rebuild the MTGraphRAG graph from source files, then start the live app.
#   bash build_all.sh default-cards-YYYYMMDD.json variants.json.gz extracted_a.jsonl,extracted_b.jsonl
# Inputs: a Scryfall bulk file, Commander Spellbook's variants export, and LLM extractions from extract.py.
# Also picked up automatically from this folder, if present:
#   rulings-*.jsonl     Scryfall's rulings bulk file   -> (Card)-[:HAS_RULING]->(Ruling)
#   comprules_*.txt     the Comprehensive Rules text   -> (:RuleText) nodes + vector index
# Override with RULINGS=... RULES_TXT=... EMBED_BACKEND=st|tfidf|openai
set -euo pipefail
CARDS_JSON=${1:?Scryfall bulk JSON file}
VARIANTS=${2:?Commander Spellbook variants.json.gz}
EXTRACTED=${3:?extracted_*.jsonl from extract.py (several allowed, comma-separated)}

python slim_cards.py "$CARDS_JSON" cards.jsonl.gz                 # ~538 MB -> ~5 MB
python build_graph.py cards.jsonl.gz mtg.rl                       # ~20 s
python abilities.py mtg.rl                                        # ability graph
python rules_facts.py mtg.rl                                      # rules distinctions
python slim_variants.py "$VARIANTS" variants_slim.jsonl.gz        # 735 MB -> 3 MB, streamed
python load_llm.py mtg.rl "$EXTRACTED" mtg_live.rl --mode union   # rules + LLM
python states.py mtg_live.rl                                          # State nodes: what each ability requires, causes, clears
python bake_models.py mtg_live.rl                                     # what you taught by drawing: into the graph itself

# The official rules (the build recreates mtg_live.rl, so they're loaded again every time)
RULINGS=${RULINGS:-$(ls -t rulings-*.jsonl 2>/dev/null | head -1 || true)}
RULES_TXT=${RULES_TXT:-$(ls -t comprules_*.txt 2>/dev/null | head -1 || true)}
if [ -n "$RULINGS" ]; then
  python load_rulings.py mtg_live.rl "$RULINGS" comprules_keywords.json
else
  echo "(no rulings-*.jsonl here: skipping rulings; download 'Rulings' from scryfall.com/docs/api/bulk-data)"
fi
if [ -n "$RULES_TXT" ]; then
  BACKEND=${EMBED_BACKEND:-$(python -c "import sentence_transformers" >/dev/null 2>&1 && echo st || echo tfidf)}
  python embed_rules.py build mtg_live.rl "$RULES_TXT" --backend "$BACKEND"
else
  echo "(no comprules_*.txt here: skipping rule embeddings)"
fi
python engine.py mtg_live.rl && python nonbo.py mtg_live.rl | tail -1
python demo_data.py mtg_live.rl variants_slim.jsonl.gz demo_data.json
python graph_data.py mtg_live.rl demo_data.json
python server.py --store mtg_live.rl                              # http://localhost:8000
