#!/usr/bin/env bash
# Create a virtual environment for MTGraphRAG, separate from Anaconda's base environment.
#   bash setup_venv.sh              # core packages
#   bash setup_venv.sh embeddings   # also the local embedding model (sentence-transformers)
set -e
PY=${PYTHON:-python3}
"$PY" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip -q
pip install -r requirements.txt
if [ "$1" = "embeddings" ]; then pip install -r requirements-embeddings.txt; fi
python -c "import relate, scipy, anthropic; print('ready: relate', relate.__version__)"
echo
echo "Done. In each new terminal, activate it with:   source .venv/bin/activate"
