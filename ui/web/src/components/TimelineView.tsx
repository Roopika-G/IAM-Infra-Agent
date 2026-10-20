import { Link } from 'react-router-dom'
import { useTimeline } from '../api'
import { statusLabel, statusTone } from '../format'
import Section from './Section'
import Status from './Status'

const MARK = { done: '✓', active: '●', pending: '○', bad: '✗' } as const

export default function TimelineView() {
  const q = useTimeline()
  const t = q.data
  const first = t?.steps.find((s) => s.time)?.time
  const since = (iso: string | null) => {
    if (!iso || !first) return ''
    const s = Math.max(0, Math.round((new Date(iso).getTime() - new Date(first).getTime()) / 1000))
    return `+${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
  }
  return (
    <Section
      title={t?.incident ? `Incident #${t.incident.id}` : 'Incident'}
      aside={
        t?.incident ? (
          <>
            <Status tone={statusTone(t.incident.status)}>{statusLabel(t.incident.status)}</Status>
            <Link to={`/incidents/${t.incident.id}`}>details</Link>
          </>
        ) : undefined
      }
    >
      {q.isError && <p className="error">{(q.error as Error).message}</p>}
      {t && t.steps.length === 0 && <p className="muted">No incident. Press “Trigger incident” to start one.</p>}
      <ol className="story">
        {t?.steps.map((s, n) => (
          <li key={n} className={s.state}>
            <span className="mark">{MARK[s.state]}</span>
            <span className="when">{since(s.time)}</span>
            <span className="what">
              {s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.title}</a> : s.title}
              {s.detail && <span className="detail">{s.detail}</span>}
            </span>
          </li>
        ))}
      </ol>
    </Section>
  )
}
