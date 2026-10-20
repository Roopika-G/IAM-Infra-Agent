import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useIncidents } from '../api'
import { ago, statusLabel, statusTone } from '../format'
import Section from '../components/Section'
import Status from '../components/Status'

export default function History() {
  const [scope, setScope] = useState<'open' | 'past' | 'archived'>('open')
  const q = useIncidents(scope)
  return (
    <Section
      title="History"
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

