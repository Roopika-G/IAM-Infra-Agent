import { Link, useParams } from 'react-router-dom'
import { useIncident } from '../api'
import { ago, stamp, statusLabel, statusTone } from '../format'
import Section from '../components/Section'
import Status from '../components/Status'

const NOT_BUILT = 'Nothing recorded yet. The diagnosis agent is not built, so this stays empty for now.'

export default function IncidentDetail() {
  const { id } = useParams()
  const q = useIncident(id)

  if (q.isLoading) return <p className="muted">Loading…</p>
  if (q.isError) return <p className="error">{(q.error as Error).message}</p>
  const i = q.data!

  return (
    <>
      <p className="crumb">
        <Link to="/">Dashboard</Link> / Incident #{i.id}
      </p>
      <header className="page-head">
        <h1>Incident #{i.id}</h1>
        <Status tone={statusTone(i.status)}>{statusLabel(i.status)}</Status>
      </header>

      <blockquote className="quote mono">{i.sample_message}</blockquote>

      <dl className="facts">
        <dt>Component</dt>
        <dd>{i.pf_role ? `pf-${i.pf_role}` : '-'} ({i.log_type ?? 'unknown'} log)</dd>
        <dt>Logger</dt>
        <dd className="mono">{i.logger ?? '-'}</dd>
        <dt>Exception</dt>
        <dd className="mono">{i.exception_type ?? '-'}</dd>
        <dt>Occurrences</dt>
        <dd>{i.occurrence_count}</dd>
        <dt>First seen</dt>
        <dd>{stamp(i.first_seen)} <span className="dim">({ago(i.first_seen)})</span></dd>
        <dt>Last seen</dt>
        <dd>{stamp(i.last_seen)} <span className="dim">({ago(i.last_seen)})</span></dd>
        <dt>Fingerprint</dt>
        <dd className="mono">{i.fingerprint}</dd>
      </dl>

      <Section title="Recent occurrences">
        {i.recent_logs && i.recent_logs.length > 0 ? (
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Role</th>
                <th>Pod</th>
              </tr>
            </thead>
            <tbody>
              {i.recent_logs.map((l, n) => (
                <tr key={n}>
                  <td>{stamp(l.time)}</td>
                  <td>{l.role ?? '-'}</td>
                  <td className="mono dim">{l.pod ?? '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">No matching log rows found.</p>
        )}
      </Section>

      <Section title="Diagnosis">
        <p className="muted">{NOT_BUILT}</p>
      </Section>
      <Section title="Proposed fix">
        <p className="muted">{NOT_BUILT}</p>
      </Section>
      <Section title="Attempts">
        <p className="muted">{NOT_BUILT}</p>
      </Section>
    </>
  )
}
