import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp_servers import common  # noqa: E402

VALUES = '''\
pingfederate-admin:
  envs:
    # a comment that must survive
    SERVER_PROFILE_PATH: "helm/server-profile" # trailing comment
    POSTGRES_JDBC_URL: "jdbc:postgresql://postgres.pingfederate.svc.cluster.local:5432/postgres"
    PLAIN: abc
    SINGLE: 'one'
pingfederate-engine:
  envs:
    POSTGRES_JDBC_URL: "jdbc:postgresql://engine-host:5432/postgres"
'''


def test_patch_yaml_changes_only_the_target_scalar():
    out = common.patch_yaml_scalar(VALUES, "pingfederate-admin.envs.POSTGRES_JDBC_URL", "jdbc:postgresql://good:5432/db")
    assert 'POSTGRES_JDBC_URL: "jdbc:postgresql://good:5432/db"' in out
    # the engine block's identically-named key is untouched
    assert 'POSTGRES_JDBC_URL: "jdbc:postgresql://engine-host:5432/postgres"' in out
    # everything else, comments included, is byte-identical
    assert out.replace("jdbc:postgresql://good:5432/db", "jdbc:postgresql://postgres.pingfederate.svc.cluster.local:5432/postgres") == VALUES


def test_patch_yaml_keeps_quote_style():
    assert "SINGLE: 'two'" in common.patch_yaml_scalar(VALUES, "pingfederate-admin.envs.SINGLE", "two")
    assert "PLAIN: xyz\n" in common.patch_yaml_scalar(VALUES, "pingfederate-admin.envs.PLAIN", "xyz")
    # plain scalar given a value that needs quoting gets quoted rather than corrupting the YAML
    assert 'PLAIN: "a: b"' in common.patch_yaml_scalar(VALUES, "pingfederate-admin.envs.PLAIN", "a: b")


def test_patch_yaml_errors():
    with pytest.raises(ValueError, match="key not found"):
        common.patch_yaml_scalar(VALUES, "pingfederate-admin.envs.NOPE", "x")
    with pytest.raises(ValueError, match="not a scalar"):
        common.patch_yaml_scalar(VALUES, "pingfederate-admin.envs", "x")
    with pytest.raises(ValueError, match="already has that value"):
        common.patch_yaml_scalar(VALUES, "pingfederate-admin.envs.PLAIN", "abc")


def test_patch_properties_key():
    text = "a=1\npf.https.port=${PORT}\nb = 2\n"
    assert common.patch_properties_key(text, "b", "3") == "a=1\npf.https.port=${PORT}\nb = 3\n"
    assert "pf.https.port=9031" in common.patch_properties_key(text, "pf.https.port", "9031")
    with pytest.raises(ValueError, match="not found"):
        common.patch_properties_key(text, "zzz", "1")
    with pytest.raises(ValueError, match="2 times"):
        common.patch_properties_key("k=1\nk=2\n", "k", "3")


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "helm/server-profile/../../.env", "infrastructure/.env", "README.md", "", "helm/ping-devops/templates/x.yaml"])
def test_validate_repo_path_rejects(bad):
    with pytest.raises(ValueError):
        common.validate_repo_path(bad)


def test_validate_repo_path_accepts():
    assert common.validate_repo_path("helm/ping-devops/values.yaml") == "helm/ping-devops/values.yaml"
    assert common.validate_repo_path("helm/server-profile/instance/bin/run.properties.subst")


@pytest.mark.parametrize("bad", ["-rf", "a b", "main;ls", "a..b", "$(x)", ""])
def test_validate_ref_rejects(bad):
    with pytest.raises(ValueError):
        common.validate_ref(bad)


def test_protected_keys():
    for k in ("POSTGRES_JDBC_PASSWORD", "PING_IDENTITY_PASSWORD", "pf.jwk", "LICENSE_FILE", "client_secret"):
        assert common.is_protected_key(k), k
    assert not common.is_protected_key("POSTGRES_JDBC_URL")


def test_validate_new_value():
    with pytest.raises(ValueError):
        common.validate_new_value("a\nb")
    with pytest.raises(ValueError):
        common.validate_new_value("")
