# MTGraphRAG: `make start` sets everything up and opens the app at http://localhost:8000
PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin
PORT   ?= 8000
STORE  ?= mtg_live.rl
VARIANTS_SLIM := variants_slim.jsonl.gz

# Sources for a full rebuild (make build): see the README. Override any of them: make build CARDS=...
CARDS     ?= $(lastword $(sort $(wildcard default-cards-*.json*)))
VARIANTS  ?= variants.json.gz
EXTRACTED ?= extracted_sonnet.jsonl,extracted_sonnet_lotho.jsonl,extracted_haiku_all.jsonl,extracted_haiku_new.jsonl

.PHONY: help start setup data build setup-build test pack release clean clean-data

help:
	@echo "make start    set up, download the data if needed, and open the app"
	@echo "make data     unpack the saved graph from data/ (skips files already here)"
	@echo "make test     run the regression checks"
	@echo "make build    rebuild the graph from Scryfall, Commander Spellbook and the LLM readings"
	@echo "make pack     copy your saved graph into data/ to commit with the code (maintainers)"
	@echo "make release  make pack, and also attach it to a GitHub Release"
	@echo "make clean    remove the virtual environment"
	@echo "make clean-data  remove the graph and combo files (also a graph you built yourself)"

start: setup data
	$(BIN)/python server.py --store $(STORE) --variants $(VARIANTS_SLIM) --port $(PORT) --open

setup: $(VENV)/.app-ready

$(VENV)/.app-ready: requirements-app.txt
	@$(PYTHON) -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 'MTGraphRAG needs Python 3.10 or newer')"
	@test -x $(BIN)/python || $(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install -q --upgrade pip
	$(BIN)/pip install -q -r requirements-app.txt
	@touch $@

data: setup
	$(BIN)/python fetch_data.py

test: setup
	$(BIN)/python checks.py $(STORE) $(VARIANTS_SLIM)

setup-build: $(VENV)/.build-ready

$(VENV)/.build-ready: requirements.txt setup
	$(BIN)/pip install -q -r requirements.txt -r requirements-embeddings.txt
	@touch $@

build: setup-build
	@test -n "$(CARDS)" || (echo "No default-cards-*.json found: download Scryfall's Default Cards bulk file here first."; exit 1)
	. $(BIN)/activate && bash build_all.sh $(CARDS) $(VARIANTS) $(EXTRACTED)

pack: setup
	$(BIN)/python pack_data.py

release: setup
	$(BIN)/python pack_data.py --release

clean:
	rm -rf $(VENV) dist

clean-data:
	rm -f $(STORE) $(VARIANTS_SLIM)
