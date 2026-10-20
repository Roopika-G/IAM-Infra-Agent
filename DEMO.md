# Demo runbook: one simulated fault, end to end

The story: **a bad commit breaks PingFederate's database connection. The agent notices, finds the cause,
opens a pull request with the fix, you merge it, and the fix is deployed and verified.**

## Before the call (10 minutes before)

```sh
open -a Docker                  # wait until the cluster is back (about 1 minute)
./reset-demo.sh                 # clean start; ends with "Ready" or a list of what to fix
./start-all.sh                  # Phoenix, detector, agent worker, UI
```

Open three browser tabs: the dashboard `http://localhost:8000`, Phoenix `http://localhost:6006`
(project `iam-agent` once a run exists), and the GitHub repo's pull requests page. Never open `.env` on screen
(it holds your API key). If anything looks off, `./reset-demo.sh` again; it is safe to repeat.

## The demo (about 8 minutes of real time; cut the waits if recording)

| # | You do | What to say | Time |
|---|---|---|---|
| 1 | Show the dashboard: the fleet is healthy | "PingFederate admin and engine behind a Postgres. Everything is healthy, config matches the known-good baseline." | 30 s |
| 2 | Press **Trigger incident (Sim A)** and confirm | "This commits a bad database URL to main, like a bad deploy, and redeploys. The pods restart." | 10 s |
| 3 | Wait. The timeline shows "Fault injected, waiting for PingFederate to log the error" | "PingFederate is up but can't reach its datastore. It only logs an error; nothing crashes." | 1 to 2 min |
| 4 | The incident appears: "Error detected", then "Agent started" | "A detector turned that log line into an incident and started the agent." | 10 s |
| 5 | Watch the steps appear: snapshot, read the repo, ask PingFederate, decision | "It checks the running value against the known-good value, reads the repo, asks PingFederate itself, and concludes the URL points at a nonexistent host." | 30 s |
| 6 | "Opened pull request #N": click it, show the diff, the diagnosis and the evidence | "It never touches the cluster. It proposes a fix as a pull request and waits for a human." | 1 min |
| 7 | Phoenix: Tracing, `iam-agent`, Traces, click the run | "Here is every step, including each model call, with its prompt and answer." Click one `ChatAnthropic` row. | 1 to 2 min |
| 8 | **Merge** the pull request on GitHub | "My merge is the approval." | 10 s |
| 9 | The timeline goes: "Merged. Deploying the fix" | "The worker deploys that exact commit and rolls back if the check fails." | 2 to 4 min |
| 10 | "Resolved. The fix is live"; the fleet is healthy | "Pods ready, no config drift, and the error stayed away for a minute. The incident is closed." | 30 s |

## If something goes wrong

| Symptom | Fix |
|---|---|
| The incident never appears after 3 minutes | `tail .run/detector.log .run/worker.log`; the pods must have restarted. Is Docker up? |
| The agent errors (check `.run/worker.log`) | Usually the API key or model name in `.env`. The incident returns to New and is retried. |
| The timeline shows a bad step (red) | Read its detail. A failed deploy check rolls back and retries once, then opens an issue. |
| The cluster is still broken after the demo | `./Error_Simulation/sim_a_jdbc_url.sh restore`, then `./reset-demo.sh` |
| Helm says "another operation is in progress" | A deploy was killed mid-way. Ask before deleting any `pending-*` Helm release record. |

Stop everything: `./stop-all.sh`.
