import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useFleet, useIncidents } from '../api'
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

function IncidentsSection() {
  const [scope, setScope] = useState<'open' | 'all'>('open')
  const q = useIncidents(scope)
  return (
    <Section
      title="Incidents"
      aside={
        <>
          <button className={scope === 'open' ? 'link on' : 'link'} onClick={() => setScope('open')}>Open</button>
          <button className={scope === 'all' ? 'link on' : 'link'} onClick={() => setScope('all')}>All</button>
        </>
      }
    >
      {q.isLoading && <p className="muted">Loading…</p>}
      {q.isError && <p className="error">Could not load incidents: {(q.error as Error).message}</p>}
      {q.data && q.data.length === 0 && <p className="muted">No {scope === 'open' ? 'open ' : ''}incidents.</p>}
      {q.data && q.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th className="num">ID</th>
              <th>Error</th>
              <th>Role</th>
              <th>Status</th>
              <th className="num">Seen</th>
              <th>Last seen</th>
            </tr>
          </thead>
          <tbody>
            {q.data.map((i) => (
              <tr key={i.id}>
                <td className="num">
                  <Link to={`/incidents/${i.id}`}>#{i.id}</Link>
                </td>
                <td className="clip wide" title={i.sample_message ?? ''}>
                  <Link to={`/incidents/${i.id}`} className="plain">{i.sample_message}</Link>
                </td>
                <td>{i.pf_role ?? '-'}</td>
                <td>
                  <Status tone={statusTone(i.status)}>{statusLabel(i.status)}</Status>
                </td>
                <td className="num">{i.occurrence_count}×</td>
                <td className="dim">{ago(i.last_seen)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  )
}

export default function Dashboard() {
  return (
    <>
      <header className="page-head">
        <h1>Dashboard</h1>
      </header>
      <FleetSection />
      <DriftSection />
      <IncidentsSection />
    </>
  )
}
