"""pf_admin MCP server: read-only view of PingFederate's own admin API --
what PF itself believes its config and cluster state are, independent of
logs, env vars or pod status.

  get_pf_datastore       -- one datastore's config as PF holds it (secrets stripped)
  get_pf_cluster_status  -- nodes, modes, versions, config replication state
  get_pf_version         -- PingFederate version

Only these three GET endpoints are reachable -- no pass-through of
arbitrary API paths (the admin API also exposes administrative accounts,
keys and more). Any field whose name looks secret is stripped from every
response before it leaves this server.

Credentials come from env (an MCP stdio client does NOT inherit your shell,
pass these explicitly): PF_ADMIN_PASSWORD (required), PF_ADMIN_USER
(default 'administrator'), PF_ADMIN_URL (default https://localhost:9999).
Ideally a dedicated read-only (Auditor-role) account rather than the full
administrator -- not yet verified available in this PF version.

Run: uv run python mcp_servers/pf_admin/server.py
"""

import os
import re

import httpx
from mcp.server.mcpserver import MCPServer

PF_URL = os.environ.get("PF_ADMIN_URL", "https://localhost:9999").rstrip("/")
PF_USER = os.environ.get("PF_ADMIN_USER", "administrator")
API = f"{PF_URL}/pf-admin-api/v1"

_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
_SECRET_KEY_RE = re.compile(r"password|secret|encrypted|passphrase|privatekey|private_key|token", re.IGNORECASE)

mcp = MCPServer("pf_admin")


def redact(obj):
    """Recursively replace the value of any secret-looking key with '***'."""
    if isinstance(obj, dict):
        return {k: ("***" if _SECRET_KEY_RE.search(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def _get(path: str) -> dict:
    password = os.environ.get("PF_ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("PF_ADMIN_PASSWORD is not set on this server")
    # PF's admin endpoint uses a self-signed certificate (see README), so
    # verification is off; this only ever talks to the configured PF_ADMIN_URL.
    resp = httpx.get(
        f"{API}{path}", auth=(PF_USER, password),
        headers={"X-XSRF-Header": "PingFederate"}, verify=False, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def get_pf_datastore(datastore_id: str) -> dict:
    """A PingFederate datastore's configuration as PF itself holds it, with
    secrets stripped. datastore_id is the id seen in log messages, e.g.
    'JDBC-FD67494D8AAFD9D8A5D00C310DE08DD078626978' from 'Unable to load
    custom data source instance: JDBC-...'. For a JDBC datastore the key
    fields are name, connectionUrl, driverClass, userName."""
    if not _ID_RE.match(datastore_id):
        raise ValueError(f"invalid datastore id: {datastore_id!r}")
    return redact(_get(f"/dataStores/{datastore_id}"))


@mcp.tool()
def get_pf_cluster_status() -> dict:
    """Cluster nodes with address, mode (CLUSTERED_CONSOLE / CLUSTERED_ENGINE),
    version, configurationTimestamp and replicationStatus, plus
    lastConfigUpdateTime and replicationRequired. replicationStatus other
    than SUCCEEDED, or replicationRequired true, means a config change has
    not reached every node."""
    return redact(_get("/cluster/status"))


@mcp.tool()
def get_pf_version() -> dict:
    """PingFederate version, e.g. {'version': '13.1.1.0'}."""
    return redact(_get("/version"))


if __name__ == "__main__":
    mcp.run()
