import { useMutation, useQueryClient } from '@tanstack/react-query'
import { getJson, useConfig, useFleet, useSimA } from '../api'
import type { FleetComponent, SimStatus } from '../api'
import type { Tone } from '../format'
import Section from '../components/Section'
import Status from '../components/Status'
import TimelineView from '../components/TimelineView'

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
              <th className="num">Ready</th>
              <th className="num">Restarts</th>
              <th>Config replication</th>
              <th className="num">Drifted keys</th>
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
      <TimelineView />
      <FleetSection />
    </>
  )
}
