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



# --- text search across the allowed files ---

import subprocess

MAX_MATCHES = 30
MAX_MATCH_CHARS = 200


@mcp.tool()
def find_in_repo(text: str, ref: str = "main") -> dict:
    """Case-insensitive plain-text search over the two files the agent may
    diagnose: helm/ping-devops/values.yaml and helm/server-profile/**, at a
    git ref (default main). Returns up to 30 matches as {path, line,
    key_path, text}; for values.yaml, key_path is the exact dotted path to
    pass to read_values_yaml_key / the patch tools (null if the hit is in a
    comment). Use it to find where a value, tag or key is set when you do
    not already know the path."""
    if not text.strip() or len(text) > 200 or "\n" in text:
        raise ValueError("text must be one non-empty line up to 200 characters")
    common.validate_ref(ref)
    result = subprocess.run(
        ["git", "grep", "-n", "-i", "-F", "-I", "--no-color", "-e", text, ref, "--", common.VALUES_FILE, common.SERVER_PROFILE_PREFIX],
        cwd=common.REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode == 1 and not result.stderr.strip():
        return {"query": text, "ref": ref, "matches": [], "truncated": False}
    if result.returncode != 0:
        raise ValueError(f"git grep failed: {result.stderr.strip()[:300]}")

    values_text = None
    matches = []
    rows = result.stdout.splitlines()
    for row in rows[:MAX_MATCHES]:
        _, path, line_no, content = row.split(":", 3)
        key_path = None
        if path == common.VALUES_FILE:
            values_text = values_text or common.git_show(ref, common.VALUES_FILE)
            key_path = common.yaml_path_at_line(values_text, int(line_no))
        shown = "***" if key_path and common.is_protected_key(key_path.split(".")[-1]) else content.strip()[:MAX_MATCH_CHARS]
        matches.append({"path": path, "line": int(line_no), "key_path": key_path, "text": shown})
    return {"query": text, "ref": ref, "matches": matches, "truncated": len(rows) > MAX_MATCHES}


if __name__ == "__main__":
    mcp.run()
