import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mcp_servers import common  # noqa: E402


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"t_{name}", ROOT / "mcp_servers" / name / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kube = load("kubernetes")
pf = load("pf_admin")
helm = load("helm_ops")
repo = load("repo_read")

YAML = """\
pingfederate-engine:
  image:
    tag: "13.1.1"   # trailing comment
  envs:
    POSTGRES_JDBC_URL: "jdbc:x"
    NOTE: >
      folded
      text
"""


def test_yaml_path_at_line():
    assert common.yaml_path_at_line(YAML, 3) == "pingfederate-engine.image.tag"
    assert common.yaml_path_at_line(YAML, 5) == "pingfederate-engine.envs.POSTGRES_JDBC_URL"
    assert common.yaml_path_at_line(YAML, 7) == "pingfederate-engine.envs.NOTE"   # inside a multi-line scalar
    assert common.yaml_path_at_line(YAML, 2) == "pingfederate-engine.image"        # a mapping key line
    assert common.yaml_path_at_line("a: [unclosed", 1) is None                      # unparseable


def test_log_redaction():
    assert kube._redact_log_line("login password=hunter2 ok") == "login password=*** ok"
    assert kube._redact_log_line('{"token":"abc123"}') == '{"token":"***"}'
    assert kube._redact_log_line("api_key: XYZ and more") == "api_key: *** and more"
    assert kube._redact_log_line("x" * 900).__len__() == kube.MAX_LOG_LINE_CHARS
    assert kube._redact_log_line("plain line") == "plain line"
    assert kube._redact_log_line("\x1b[0;32mCloning\x1b[0m into") == "Cloning into"


def test_secret_summary_never_contains_values():
    sec = NS(metadata=NS(name="s", creation_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                         managed_fields=[NS(time=datetime(2026, 2, 1, tzinfo=timezone.utc)), NS(time=None)]),
             type="Opaque", data={"PASSWORD": "aHVudGVyMg==", "lic": "AAAA"})
    out = kube._secret_summary(sec)
    assert [k["key"] for k in out["keys"]] == ["PASSWORD", "lic"]
    assert "aHVudGVyMg==" not in str(out) and "hunter2" not in str(out)
    assert out["last_modified"].startswith("2026-02-01")


def test_secret_name_guard():
    for bad in ("../x", "UPPER", "sh.helm.release.v1.pingfederate.v3", ""):
        with pytest.raises(ValueError):
            kube.get_secret_keys(bad)


def probe(**kw):
    base = dict(http_get=None, tcp_socket=None, _exec=None, initial_delay_seconds=5, period_seconds=10, timeout_seconds=1, failure_threshold=3)
    return NS(**{**base, **kw})


def test_pod_spec_excludes_env_values():
    c = NS(name="pf", image="img:1.2", image_pull_policy="IfNotPresent", resources=NS(requests={"cpu": "1"}, limits={"memory": "2Gi"}),
           readiness_probe=probe(http_get=NS(path="/ready", port=9031)), liveness_probe=None, startup_probe=None,
           env=[NS(name="A", value="secret-literal", value_from=None),
                NS(name="B", value=None, value_from=NS(secret_key_ref=NS(), config_map_key_ref=None))],
           env_from=[NS(config_map_ref=NS(name="cm"), secret_ref=None), NS(config_map_ref=None, secret_ref=NS(name="sec"))])
    pod = NS(metadata=NS(name="p", owner_references=[NS(kind="ReplicaSet", name="rs")]), spec=NS(containers=[c], restart_policy="Always"))
    out = kube._format_pod_spec(pod)
    assert "secret-literal" not in str(out)
    assert out["containers"][0]["env"] == [{"name": "A", "source": "literal"}, {"name": "B", "source": "secret"}]
    assert out["containers"][0]["env_from"] == ["cm", "secret:sec"]
    assert out["containers"][0]["readiness_probe"]["path"] == "/ready"
    assert out["owner"] == "ReplicaSet/rs"


def test_workload_scaled_to_zero_is_visible():
    sts = NS(metadata=NS(name="postgres"), spec=NS(replicas=0, update_strategy=NS(type="RollingUpdate"), template=NS(spec=NS(containers=[NS(image="pg:16")]))),
             status=NS(ready_replicas=None, updated_replicas=None, conditions=None))
    out = kube._format_workload("statefulset", sts)
    assert (out["desired"], out["ready"]) == (0, 0) and out["images"] == ["pg:16"]


def test_days_until():
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert pf.days_until("2027-01-20T00:00:00Z", now) == 107
    assert pf.days_until("2026-10-01T00:00:00Z", now) < 0
    assert pf.days_until(None) is None


def test_license_and_cert_use_whitelists(monkeypatch):
    lic = {"id": "123", "organization": "Acme", "product": "PingFederate", "version": "13.0", "tier": "Free",
           "expirationDate": "2027-01-20T00:00:00Z", "features": [{"x": 1}]}
    monkeypatch.setattr(pf, "_get", lambda path: lic)
    out = pf.get_pf_license()
    assert "organization" not in out and "id" not in out and "features" not in out and out["tier"] == "Free"

    def fake(path):
        if path == "/keyPairs/signing":
            raise RuntimeError("boom")
        return {"items": [{"id": "k1", "subjectDN": "CN=a", "expires": "2036-01-01T00:00:00Z", "status": "VALID",
                           "fileData": "PRIVATEKEYBLOB", "certView": {"pem": "-----BEGIN"}}]}
    monkeypatch.setattr(pf, "_get", fake)
    certs = pf.get_pf_certificates()
    assert "PRIVATEKEYBLOB" not in str(certs) and "BEGIN" not in str(certs)
    assert certs["ssl_server"][0]["id"] == "k1" and certs["ssl_server"][0]["days_until_expiry"] > 0
    assert "error" in certs["signing"]


def test_helm_lookup_and_mask():
    doc = {"a": {"b": {"tag": "1.0", "adminPassword": "x"}}}
    assert helm.lookup_path(doc, "a.b.tag") == "1.0"
    assert helm.lookup_path(doc, "a.zzz.tag") is None
    assert helm._mask("a.b.adminPassword", "x") == "***" and helm._mask("a.b.tag", "1.0") == "1.0"
    with pytest.raises(ValueError):
        helm.get_helm_release(key_paths=["bad path; rm"])
    with pytest.raises(ValueError):
        helm.get_helm_release(revisions=[1, 2, 3, 4])


def test_find_in_repo_returns_exact_key_paths():
    out = repo.find_in_repo("POSTGRES_JDBC_URL")
    paths = {m["key_path"] for m in out["matches"] if m["path"] == common.VALUES_FILE and m["key_path"]}
    assert "pingfederate-admin.envs.POSTGRES_JDBC_URL" in paths
    assert repo.find_in_repo("zzz-no-such-string-zzz")["matches"] == []
    for bad in ("", "a\nb", "x" * 201):
        with pytest.raises(ValueError):
            repo.find_in_repo(bad)
    with pytest.raises(ValueError):
        repo.find_in_repo("x", ref="-evil")
