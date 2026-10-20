#!/usr/bin/env bash
# Puts the demo back to a clean start. Safe to run between takes.
#   stops the services, clears incidents/runs/events (numbering restarts at 1), moves the detector's
#   bookmark to now, deletes Phoenix's old traces, deletes remediation/inc-* branches (GitHub and local),
#   fast-forwards local main to GitHub, then checks the demo is ready.
# Does NOT touch the cluster or fix a live fault: if the JDBC URL is still broken it tells you to run
#   ./Error_Simulation/sim_a_jdbc_url.sh restore   (PR history on GitHub cannot be deleted).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
CTX="${KUBE_CONTEXT:-kind-self-healing-iam}"
ok()   { echo "  ok   $*"; }
warn() { echo "  !!   $*"; PROBLEMS=$((PROBLEMS + 1)); }
PROBLEMS=0

echo "1. stopping services"; ./stop-all.sh >/dev/null 2>&1; ok "stopped"

echo "2. checking Docker and the cluster"
docker info >/dev/null 2>&1 || { echo "Docker is not running. Start Docker Desktop and rerun." >&2; exit 1; }
kubectl --context "$CTX" -n pingfederate get pods >/dev/null 2>&1 || { echo "The cluster is not reachable yet (kubectl --context $CTX). Wait a minute and rerun." >&2; exit 1; }
ok "cluster reachable"

echo "   waiting for all pods to be ready (up to 5 minutes)"
for _ in $(seq 1 60); do
  NOTREADY="$(kubectl --context "$CTX" -n pingfederate get pods --no-headers 2>/dev/null | awk '{split($2,a,"/"); if (a[1]!=a[2] || $3!="Running") print $1}')"
  [[ -z "$NOTREADY" ]] && break
  sleep 5
done
[[ -z "$NOTREADY" ]] && ok "all pods ready" || { echo "Pods are still not ready: $NOTREADY" >&2; exit 1; }

set -a; source .env; set +a

echo "3. clearing incidents, runs and events"
.venv/bin/python - <<'PY' || { echo "Could not reach Postgres on localhost:5432; nothing was cleared. Rerun in a minute." >&2; exit 1; }
import os, time, psycopg
for attempt in range(12):                      # Postgres can take a moment to accept connections after a restart
    try:
        psycopg.connect(os.environ["AGENT_DB_DSN"]).close(); break
    except psycopg.OperationalError:
        time.sleep(5)
with psycopg.connect(os.environ["AGENT_DB_DSN"]) as c:
    c.execute("TRUNCATE agent.run_events, agent.runs, agent.incidents RESTART IDENTITY")
    c.execute("UPDATE agent.ingest_cursor SET last_seen_time = (SELECT COALESCE(max(time), now() at time zone 'utc') FROM public.pf_logs_raw)")
print("  ok   cleared; the next incident will be #1; detector starts from now")
PY

echo "4. clearing Phoenix traces"
STARTED=0
if ! curl -s -o /dev/null http://localhost:6006/healthz; then
  [[ -x .venv/bin/phoenix ]] && { mkdir -p .run; nohup .venv/bin/phoenix serve > .run/phoenix-reset.log 2>&1 & echo $! > .run/phoenix-reset.pid; STARTED=1
    for _ in $(seq 1 30); do curl -s -o /dev/null http://localhost:6006/healthz && break; sleep 1; done; }
fi
if curl -s -o /dev/null http://localhost:6006/healthz; then
  curl -s -X DELETE http://localhost:6006/v1/projects/iam-agent -o /dev/null; ok "old traces deleted"
else
  warn "Phoenix is not installed or did not start (uv sync --extra tracing); traces not cleared"
fi
[[ $STARTED == 1 ]] && { kill "$(cat .run/phoenix-reset.pid)" 2>/dev/null; rm -f .run/phoenix-reset.pid; }

echo "5. removing remediation branches"
for b in $(git ls-remote --heads github 'remediation/inc-*' 2>/dev/null | awk '{print $2}' | sed 's#refs/heads/##'); do
  git push -q github --delete "$b" && ok "deleted github/$b"
done
for w in .worktrees/inc-*; do [[ -d "$w" ]] && git worktree remove --force "$w" && ok "removed $w"; done
git worktree prune
for b in $(git branch --list 'remediation/inc-*' | tr -d ' *'); do git branch -q -D "$b" && ok "deleted local $b"; done

echo "6. syncing local main with GitHub"
git checkout -q main 2>/dev/null
if git pull -q --ff-only github main 2>/dev/null; then ok "main is up to date ($(git rev-parse --short HEAD))"; else warn "could not fast-forward main (local changes or diverged history); resolve by hand"; fi

echo "7. demo readiness"
GOLD="jdbc:postgresql://postgres.pingfederate.svc.cluster.local:5432/postgres"
if [[ "$(grep -c "POSTGRES_JDBC_URL: \"$GOLD\"" helm/ping-devops/values.yaml)" == "2" ]]; then ok "values.yaml holds the golden JDBC URL"; else warn "values.yaml JDBC URL is not the golden one: run ./Error_Simulation/sim_a_jdbc_url.sh restore"; fi
LIVE="$(kubectl --context "$CTX" -n pingfederate exec deploy/pingfederate-pingfederate-admin -c pingfederate-admin -- printenv POSTGRES_JDBC_URL 2>/dev/null)"
[[ "$LIVE" == "$GOLD" ]] && ok "the running admin pod uses the golden URL" || warn "the running admin pod does not use the golden URL ($LIVE)"
NOTREADY="$(kubectl --context "$CTX" -n pingfederate get pods --no-headers 2>/dev/null | awk '{split($2,a,"/"); if (a[1]!=a[2] || $3!="Running") print $1}')"
[[ -z "$NOTREADY" ]] && ok "all pods ready" || warn "pods not ready: $NOTREADY"
grep -q '^ANTHROPIC_API_KEY=.\+' .env && ok "ANTHROPIC_API_KEY is set" || warn "ANTHROPIC_API_KEY is empty in .env"
grep -q '^HELM_DEPLOY_TOKEN=.\+' .env && ok "HELM_DEPLOY_TOKEN is set" || warn "HELM_DEPLOY_TOKEN is empty (run ./create-env.sh)"
gh auth status >/dev/null 2>&1 && ok "gh is logged in" || warn "gh is not logged in (gh auth login)"
[[ -f ui/web/dist/index.html ]] && ok "the UI is built" || warn "the UI is not built: cd ui/web && npm install && npm run build"
helm history pingfederate -n pingfederate --kube-context "$CTX" --max 1 2>/dev/null | tail -1 | grep -q -E "deployed" && ok "Helm release is in a clean state" || warn "Helm release is not 'deployed' (a stuck operation?)"

echo
[[ $PROBLEMS == 0 ]] && echo "Ready. Run ./start-all.sh, then press Trigger incident." || echo "$PROBLEMS thing(s) to fix before the demo (marked !! above)."
