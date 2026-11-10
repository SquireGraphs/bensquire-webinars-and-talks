"""Configuration shared by setup scripts, the agent, and the audit CLI.

Everything comes from environment variables (or a .env file) so the same code
runs against Neo4j Desktop 2, Docker, or Aura without edits.
"""

import os

from dotenv import load_dotenv

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
BIGQUERY_DATASET_ID = os.getenv("BIGQUERY_DATASET_ID", "acme_corp")
# Hard cap on bytes scanned per agent query (ACME is tiny; this is a seatbelt).
BQ_MAX_BYTES_BILLED = int(os.getenv("BQ_MAX_BYTES_BILLED", str(100 * 1024**2)))

AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-5-5")
# Needed only for API keys that are not scoped to a single workspace.
ANTHROPIC_WORKSPACE_ID = os.getenv("ANTHROPIC_WORKSPACE_ID", "")
# Used by Neocarta when you ingest with --embeddings, and by its MCP server at query
# time (it reads the same EMBEDDING_* variables). Any LiteLLM model name works.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
# 1536 matches Agent Memory's Entity vector index; Neocarta's Table nodes carry both labels.
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "100"))
os.environ.setdefault("EMBEDDING_DIMENSIONS", str(EMBEDDING_DIMENSIONS))
# Vertex AI embeddings reuse the BigQuery project and your gcloud login.
if EMBEDDING_MODEL.startswith("vertex_ai/"):
    os.environ.setdefault("VERTEXAI_PROJECT", os.getenv("GCP_PROJECT_ID", ""))
    os.environ.setdefault("VERTEXAI_LOCATION", "us-central1")
AGENT_MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "12"))
# Cost controls. Tool results longer than this are truncated before the model sees
# them (the full text is still recorded in memory). Neocarta tools listed here are
# not offered to the agent: get_full_metadata_schema and get_domain_context dump the
# whole semantic layer (100K+ characters) when targeted search tools do the job.
MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "24000"))
HIDDEN_NEOCARTA_TOOLS = set(
    os.getenv("HIDDEN_NEOCARTA_TOOLS", "get_full_metadata_schema,get_domain_context").split(",")
)
PROMPT_CACHING = os.getenv("PROMPT_CACHING", "1") != "0"

# Namespace for the governance tags this demo applies.
GOVERNANCE_SOURCE = "acme_demo"

# Neocarta's MCP server writes its banner and logs here rather than the terminal.
MCP_LOG_FILE = os.getenv("MCP_LOG_FILE", "neocarta-mcp.log")

# Where "before" mode keeps its shared team notes: a flat file, no provenance.
NOTES_FILE = os.getenv("NOTES_FILE", "team_notes.json")


def embedding_credentials_missing() -> list[str]:
    """Env vars the configured embedding provider needs that aren't set."""
    model = EMBEDDING_MODEL
    if model.startswith("vertex_ai/"):
        return []  # uses Application Default Credentials from gcloud
    if model.startswith("gemini/"):
        return [] if os.getenv("GEMINI_API_KEY") else ["GEMINI_API_KEY"]
    if "/" not in model or model.startswith("openai/"):
        return [] if os.getenv("OPENAI_API_KEY") else ["OPENAI_API_KEY"]
    return []


def anthropic_client_kwargs() -> dict:
    """Extra Anthropic client arguments, e.g. the workspace header for unscoped keys."""
    if ANTHROPIC_WORKSPACE_ID:
        return {"default_headers": {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID}}
    return {}


def neo4j_auth() -> tuple[str, str]:
    return (NEO4J_USERNAME, NEO4J_PASSWORD)


def require(*names: str) -> None:
    """Fail fast with a readable message when a required variable is missing."""
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)} (see .env.example)")
