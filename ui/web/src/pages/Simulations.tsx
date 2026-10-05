import { useMutation, useQueryClient } from '@tanstack/react-query'
import { getJson, useSimA } from '../api'
import type { SimStatus } from '../api'
import Section from '../components/Section'
import Status from '../components/Status'

export default function Simulations() {
  const q = useSimA()
  const qc = useQueryClient()
  const run = useMutation({
    mutationFn: (mode: 'inject' | 'restore') => getJson<SimStatus>(`/api/simulations/a/${mode}`, { method: 'POST' }),
    onSettled: () => qc.invalidateQueries({ queryKey: ['simA'] }),
  })
  const sim = q.data
  const busy = !!sim?.running || run.isPending

  const start = (mode: 'inject' | 'restore') => {
    const what =
      mode === 'inject'
        ? 'This points the JDBC datastore at a nonexistent host and redeploys, restarting both PingFederate pods.'
        : 'This puts the JDBC URL back to its golden value and redeploys, restarting both PingFederate pods.'
    if (window.confirm(`${what}\n\nContinue?`)) run.mutate(mode)
  }

  return (
    <>
      <header className="page-head">
        <h1>Simulations</h1>
      </header>
      <Section title="Sim A: POSTGRES_JDBC_URL corruption">
        <p className="note">
          Test injection only, kept apart from the real incident views. Both pods stay Running; PingFederate just
          logs a repeated error that its JDBC datastore cannot connect.
        </p>
        {q.isLoading && <p className="muted">Loading…</p>}
        {q.isError && <p className="error">{(q.error as Error).message}</p>}
        {sim && (
          <>
            <dl className="facts">
              <dt>values.yaml</dt>
              <dd>
                <Status tone={sim.state === 'healthy' ? 'ok' : sim.state === 'corrupted' ? 'bad' : 'muted'}>
                  {sim.state === 'healthy' ? 'golden value' : sim.state === 'corrupted' ? 'corrupted' : 'unknown'}
                </Status>
              </dd>
              <dt>Script</dt>
              <dd>
                {sim.running ? (
                  <Status tone="warn">{sim.mode} running…</Status>
                ) : sim.mode ? (
                  <Status tone={sim.exit_code === 0 ? 'ok' : 'bad'}>
                    {sim.mode} finished (exit {sim.exit_code})
                  </Status>
                ) : (
                  <span className="dim">not run from this page</span>
                )}
              </dd>
            </dl>
            <div className="actions">
              <button className="btn" disabled={busy || sim.state === 'corrupted'} onClick={() => start('inject')}>
                Inject fault
              </button>
              <button className="btn" disabled={busy || sim.state === 'healthy'} onClick={() => start('restore')}>
                Restore
              </button>
            </div>
            {run.isError && <p className="error">{(run.error as Error).message}</p>}
            {sim.tail && <pre className="log">{sim.tail}</pre>}
          </>
        )}
      </Section>
    </>
  )
}
