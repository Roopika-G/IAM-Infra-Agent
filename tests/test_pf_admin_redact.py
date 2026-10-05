import importlib.util
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "mcp_servers" / "pf_admin" / "server.py"


def test_pf_admin_redact():
    spec = importlib.util.spec_from_file_location("pf_admin_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.redact({"connectionUrl": "jdbc:x", "encryptedPassword": "abc", "nested": [{"clientSecret": "s", "name": "n"}]})
    assert out == {"connectionUrl": "jdbc:x", "encryptedPassword": "***", "nested": [{"clientSecret": "***", "name": "n"}]}
