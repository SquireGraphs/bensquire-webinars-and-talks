.PHONY: install test load-data setup check smoke reset eval animate

install:
	python -m pip install -r requirements.txt

test:
	python -m pytest -q

# Step 1: create the ACME dataset in BigQuery (add ARGS="--location EU" or ARGS=--replace).
load-data:
	python -m setup.load_acme_bigquery $(ARGS)

# Step 2: Neocarta semantic layer, governance tags, users, memory adoption.
setup:
	python -m setup.build_semantic_layer $(ARGS)

# Step 3: preflight every dependency and print ground-truth answers. No agent runs.
check:
	python -m setup.check

# Step 4: one question in each mode, then the audit trail. Starts from a clean slate.
smoke: reset
	python -m agent.run --mode before --user bob "What were our bookings in FY2025?"
	python -m agent.run --mode after --user bob "What were our bookings in FY2025?"
	python -m audit.audit trace latest

reset:
	python -m setup.poison --reset

# Run every demo beat many times and grade each run. Examples:
#   make eval ARGS="--trials 10"
#   make eval ARGS="--preset models --trials 30 --models claude-haiku-5-5,claude-sonnet-5-5,claude-opus-5-5"
#   make eval ARGS="--preset demo --trials 30"
eval:
	python -m eval.run $(ARGS)

# Build the dot-fill animation from eval logs: make animate RUN=eval/results/<run_id>
animate:
	python -m eval.animate $(RUN)/trials.jsonl
