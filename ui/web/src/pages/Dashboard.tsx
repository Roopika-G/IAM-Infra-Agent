import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { getJson, useActivity, useConfig, useFleet, useIncidents, useSimA } from '../api'
import type { SimStatus } from '../api'
import type { FleetComponent } from '../api'
import { ago, statusLabel, statusTone } from '../format'
import type { Tone } from '../format'
import Section from '../components/Section'
import Status from '../components/Status'

const DISPLAY: Record<string, string> = { admin: 'pf-admin', engine: 'pf-engine', postgres: 'postgres' }

function health(c: FleetComponent): { tone: Tone; label: string } {
  if (!c.present) return { tone: 'bad', label: 'Missing' }
  if (!c.healthy) return { tone: 'bad', label: 'Down' }
  if ((c.drift ?? 0) > 0) return { tone: 'warn', label: 'Config drift' }
  return { tone: 'ok', label: 'Healthy' }
}

function FleetSection() {
  const q = useFleet()
  const fleet = q.data
  const updated = q.dataUpdatedAt ? new Date(q.dataUpdatedAt).toLocaleTimeString() : null
  return (
    <Section
      title="Fleet"
      aside={
        <>
          {fleet?.version && <span>PingFederate {fleet.version}</span>}
          {updated && <span>updated {updated}</span>}
        </>
      }
    >
      {q.isLoading && <p className="muted">Loading…</p>}
      {q.isError && <p className="error">Could not load fleet: {(q.error as Error).message}</p>}
      {fleet?.error && <p className="error">{fleet.error}</p>}
      {fleet && !fleet.error && (
        <table>
          <thead>
            <tr>
              <th>Component</th>
              <th>Status</th>
              <th>Pod</th>
              <th className="num">Ready</th>
              <th className="num">Restarts</th>
              <th>Config replication</th>
              <th className="num">Drifted keys</th>
              <th className="num">Open incidents</th>
            </tr>
          </thead>
          <tbody>
            {fleet.components.map((c) => {
              const h = health(c)
              return (
                <tr key={c.name}>
                  <td className="strong">{DISPLAY[c.name] ?? c.name}</td>
                  <td>
                    <Status tone={h.tone}>{h.label}</Status>
                  </td>
                  <td className="mono dim">{c.pod ?? '-'}</td>
                  <td className="num">{c.ready ?? '-'}</td>
                  <td className="num">{c.restarts ?? '-'}</td>
                  <td>
                    {c.replication ? (
                      <Status tone={c.replication === 'SUCCEEDED' ? 'ok' : 'warn'}>{c.replication.toLowerCase()}</Status>
                    ) : (
                      <span className="dim">{c.name === 'postgres' ? 'n/a' : '-'}</span>
                    )}
                  </td>
                  <td className="num">{c.drift ?? '-'}</td>
                  <td className="num">{c.incidents ?? '-'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {fleet?.pf_error && <p className="note">PingFederate admin API unavailable ({fleet.pf_error}); replication status hidden.</p>}
    </Section>
  )
}

function DriftSection() {
  const { data: fleet } = useFleet()
  const [showAll, setShowAll] = useState(false)
  if (!fleet || fleet.error) return null
  const rows = fleet.drift
  const problems = rows.filter((r) => r.state !== 'ok')
  const visible = showAll ? rows : problems
  return (
    <Section
      title="Configuration drift"
      aside={
        <button className="link" onClick={() => setShowAll(!showAll)}>
          {showAll ? 'Hide matching keys' : `Show all ${rows.length} keys`}
        </button>
      }
    >
      <p className="note">Frozen golden value from the baseline, against what each running container actually sees.</p>
      {visible.length === 0 ? (
        <p className="muted">All {rows.length} baseline keys match the running pods.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Key</th>
              <th>Golden</th>
              <th>Live</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((r) => (
              <tr key={r.key}>
                <td className="mono">{r.key}</td>
                <td className="mono clip" title={r.golden}>{r.golden}</td>
                <td className="mono clip" title={r.live ?? ''}>{r.live ?? <span className="dim">(unset)</span>}</td>
                <td>
                  <Status tone={r.state === 'ok' ? 'ok' : r.state === 'drift' ? 'bad' : 'muted'}>
                    {r.state === 'ok' ? 'matches' : r.state === 'drift' ? 'drifted' : 'unknown'}
                  </Status>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  )
}

function ActivitySection() {
  const q = useActivity()
  return (
    <Section title="Agent activity">
      {q.data && q.data.length === 0 && <p className="muted">The agent has not done anything yet.</p>}
      {q.data && q.data.length > 0 && (
        <table>
          <tbody>
            {q.data.map((a, n) => (
              <tr key={n}>
                <td className="dim num">{ago(a.time)}</td>
                <td>{a.text}{a.url && <> · <a href={a.url} target="_blank" rel="noreferrer">open</a></>}</td>
                <td><Link to={`/incidents/${a.incident_id}`}>incident #{a.incident_id}</Link></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  )
}

function IncidentsSection() {
  const [scope, setScope] = useState<'open' | 'past' | 'archived'>('open')
  const q = useIncidents(scope)
  return (
    <Section
      title="Incidents"
      aside={
        <>
          {(['open', 'past', 'archived'] as const).map((s) => (
            <button key={s} className={scope === s ? 'link on' : 'link'} onClick={() => setScope(s)}>{s[0].toUpperCase() + s.slice(1)}</button>
          ))}
        </>
      }
    >
      {q.isLoading && <p className="muted">Loading…</p>}
      {q.isError && <p className="error">Could not load incidents: {(q.error as Error).message}</p>}
      {q.data && q.data.length === 0 && <p className="muted">No {scope} incidents.</p>}
      {q.data && q.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th className="num">ID</th>
              <th>Error</th>
              <th>Status</th>
              <th>Agent result</th>
              <th>Last seen</th>
            </tr>
          </thead>
          <tbody>
            {q.data.map((i) => (
              <tr key={i.id}>
                <td className="num"><Link to={`/incidents/${i.id}`}>#{i.id}</Link></td>
                <td className="clip wide" title={i.sample_message ?? ''}>
                  <Link to={`/incidents/${i.id}`} className="plain">{i.sample_message}</Link>
                </td>
                <td><Status tone={statusTone(i.status)}>{statusLabel(i.status)}</Status></td>
                <td>
                  {i.result_url
                    ? <a href={i.result_url} target="_blank" rel="noreferrer">{i.result_kind === 'pr_opened' ? 'PR #' : 'issue #'}{i.result_url.split('/').pop()}</a>
                    : <span className="dim">-</span>}
                </td>
                <td className="dim">{ago(i.last_seen)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  )
}

function Actions() {
  const sim = useSimA()
  const cfg = useConfig()
  const qc = useQueryClient()
  const inject = useMutation({
    mutationFn: () => getJson<SimStatus>('/api/simulations/a/inject', { method: 'POST' }),
    onSettled: () => qc.invalidateQueries({ queryKey: ['simA'] }),
  })
  const busy = !!sim.data?.running || inject.isPending
  const trigger = () => {
    if (window.confirm('This commits a broken JDBC URL to main and redeploys, restarting both PingFederate pods. The agent should then notice the error and open a pull request. Continue?')) inject.mutate()
  }
  const state = sim.data?.running ? 'Injecting the fault…' : sim.data?.state === 'corrupted' ? 'Fault is live. The agent should pick it up shortly.' : ''
  return (
    <div className="actions-bar">
      <button className="btn" disabled={busy || sim.data?.state === 'corrupted'} onClick={trigger}>Trigger incident (Sim A)</button>
      {cfg.data && <a href={cfg.data.phoenix_url} target="_blank" rel="noreferrer">Watch the agent live in Phoenix ↗</a>}
      <span className="state">{inject.isError ? (inject.error as Error).message : state}</span>
    </div>
  )
}

export default function Dashboard() {
  return (
    <>
      <header className="page-head">
        <h1>Dashboard</h1>
      </header>
      <Actions />
      <FleetSection />
      <ActivitySection />
      <IncidentsSection />
      <DriftSection />
    </>
  )
}
