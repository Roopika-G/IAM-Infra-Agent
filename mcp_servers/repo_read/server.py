"""repo_read MCP server: read-only access to the two config locations the
agent may diagnose -- helm/ping-devops/values.yaml and helm/server-profile/**.

Deliberately a separate server from repo_config (the only writer), so the
diagnosis phase is structurally never handed a write tool.

  read_values_yaml_key       -- one scalar from values.yaml, with its line and the file's sha256
  read_server_profile_file   -- one server-profile file's content, with its sha256
  diff_vs_golden             -- what changed in a file between a golden ref and another ref

All reads go through `git show` / `git diff` against a ref (default 'main'),
never the working tree, so they reflect what is committed, not local edits.

Run: uv run python mcp_servers/repo_read/server.py
"""

import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mcp_servers import common  # noqa: E402

MAX_CHARS = 20000

mcp = MCPServer("repo_read")


@mcp.tool()
def read_values_yaml_key(key_path: str, ref: str = "main") -> dict:
    """Read one scalar from helm/ping-devops/values.yaml at a git ref.
    key_path is dotted, e.g. 'pingfederate-admin.envs.POSTGRES_JDBC_URL'
    (admin and engine have separate blocks). Returns {key_path, ref, value,
    line, file_sha256}. Pass file_sha256 back to repo_config's patch tool."""
    text = common.git_show(ref, common.VALUES_FILE)
    node = common.find_scalar(text, key_path)
    return {
        "key_path": key_path,
        "ref": ref,
        "value": node.value,
        "line": node.start_mark.line + 1,
        "file_sha256": common.sha256_text(text),
    }


@mcp.tool()
def read_server_profile_file(path: str, ref: str = "main") -> dict:
    """Read a file under helm/server-profile/ at a git ref. Returns {path,
    ref, content, truncated, file_sha256}; content is cut at 20000 chars but
    the sha covers the whole file."""
    path = common.validate_repo_path(path, allow_values=False)
    text = common.git_show(ref, path)
    return {
        "path": path,
        "ref": ref,
        "content": text[:MAX_CHARS],
        "truncated": len(text) > MAX_CHARS,
        "file_sha256": common.sha256_text(text),
    }


@mcp.tool()
def diff_vs_golden(path: str, golden_ref: str, ref: str = "main") -> dict:
    """Unified diff of one allowed file between golden_ref (a known-good
    commit, tag or branch) and ref. An empty diff means the file matches the
    golden version. Returns {path, golden_ref, ref, diff, truncated}."""
    path = common.validate_repo_path(path)
    common.validate_ref(golden_ref)
    common.validate_ref(ref)
    diff = common.git("diff", golden_ref, ref, "--", path)
    return {
        "path": path,
        "golden_ref": golden_ref,
        "ref": ref,
        "diff": diff[:MAX_CHARS],
        "truncated": len(diff) > MAX_CHARS,
    }


if __name__ == "__main__":
    mcp.run()
