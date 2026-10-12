#!/usr/bin/env bash
# Creates or refreshes .env at the repo root.
#   - copies .env.example if .env does not exist
#   - fills AGENT_DB_DSN and PF_ADMIN_PASSWORD from the cluster's Kubernetes Secrets
#   - never touches ANTHROPIC_API_KEY or anything else you set yourself
# Values are never printed. Safe to rerun.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
CTX="${KUBE_CONTEXT:-kind-self-healing-iam}"
NS="pingfederate"

[[ -f "$ENV_FILE" ]] || cp "$ROOT_DIR/.env.example" "$ENV_FILE"
chmod 600 "$ENV_FILE"

secret() {  # secret <name> <key>  -> decoded value, or fails
  kubectl --context "$CTX" -n "$NS" get secret "$1" -o jsonpath="{.data.$2}" | base64 -d
}

DB_PASSWORD="$(secret postgres-credentials POSTGRES_JDBC_PASSWORD)" || { echo "Could not read postgres-credentials. Is the cluster up (kubectl --context $CTX)?" >&2; exit 1; }
PF_PASSWORD="$(secret pingfederate-license PING_IDENTITY_PASSWORD)" || { echo "Could not read pingfederate-license." >&2; exit 1; }

DB_PASSWORD="$DB_PASSWORD" PF_PASSWORD="$PF_PASSWORD" ENV_FILE="$ENV_FILE" python3 - <<'PY'
import os, re
from urllib.parse import quote

path = os.environ["ENV_FILE"]
values = {
    "AGENT_DB_DSN": "postgresql://postgres:%s@localhost:5432/postgres" % quote(os.environ["DB_PASSWORD"], safe=""),
    "PF_ADMIN_PASSWORD": os.environ["PF_PASSWORD"],
}
lines = open(path).read().splitlines()
for key, value in values.items():
    new = f"{key}={value}"
    for i, line in enumerate(lines):
        if re.match(rf"^{key}=", line):
            lines[i] = new
            break
    else:
        lines.append(new)
open(path, "w").write("\n".join(lines) + "\n")
PY

# a random shared secret for the deploy step, generated once and never overwritten
if ! grep -q '^HELM_DEPLOY_TOKEN=.\+' "$ENV_FILE"; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  if grep -q '^HELM_DEPLOY_TOKEN=' "$ENV_FILE"; then
    TOKEN="$TOKEN" ENV_FILE="$ENV_FILE" python3 -c 'import os,re; p=os.environ["ENV_FILE"]; s=open(p).read(); open(p,"w").write(re.sub(r"(?m)^HELM_DEPLOY_TOKEN=.*$", "HELM_DEPLOY_TOKEN="+os.environ["TOKEN"], s))'
  else
    echo "HELM_DEPLOY_TOKEN=$TOKEN" >> "$ENV_FILE"
  fi
  echo "Generated HELM_DEPLOY_TOKEN."
fi
echo "Updated $ENV_FILE from the cluster: AGENT_DB_DSN, PF_ADMIN_PASSWORD."
grep -q '^ANTHROPIC_API_KEY=.\+' "$ENV_FILE" && echo "ANTHROPIC_API_KEY: set." || echo "ANTHROPIC_API_KEY: EMPTY. Open .env and paste your key."
