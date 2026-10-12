"""Agent settings, read once from the environment."""

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[1]

# Environment an MCP server needs. A stdio MCP server does NOT inherit your
# shell, so these are passed to each server explicitly.
MCP_ENV_KEYS = ("HOME", "PATH", "AGENT_DB_DSN", "PF_ADMIN_PASSWORD", "PF_ADMIN_USER", "PF_ADMIN_URL", "KUBE_CONTEXT", "KUBECONFIG", "HELM_DEPLOY_TOKEN")


class Settings(BaseModel):
    model: str = "claude-haiku-5-5"
    max_steps: int = Field(8, description="Most read-tool calls the agent may make before it must decide.")
    evidence_chars: int = 400
    tool_result_chars: int = 6000
    repo_root: Path = REPO_ROOT
    mcp_env: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        """Read .env (if present) without overriding the shell, then the environment."""
        load_dotenv(env_file or REPO_ROOT / ".env")
        env = {k: os.environ[k] for k in MCP_ENV_KEYS if k in os.environ}
        env.setdefault("HF_HUB_OFFLINE", "1")  # the embedding model is already cached locally
        return cls(model=os.environ.get("AGENT_MODEL", "claude-haiku-5-5"), mcp_env=env)
