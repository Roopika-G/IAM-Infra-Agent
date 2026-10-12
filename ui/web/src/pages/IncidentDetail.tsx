import { Link, useParams } from 'react-router-dom'
import { useIncident } from '../api'
import { ago, stamp, statusLabel, statusTone } from '../format'
import Section from '../components/Section'
import Status from '../components/Status'

export default function IncidentDetail() {
  const { id } = useParams()
  const q = useIncident(id)

  if (q.isLoading) return <p className="muted">Loading…</p>
  if (q.isError) return <p className="error">{(q.error as Error).message}</p>
  const i = q.data!
  const r = i.result
  const d = r?.decision

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
        <dd>{i.pf_role ? `pf-${i.pf_role}` : '-'}</dd>
        <dt>Occurrences</dt>
        <dd>{i.occurrence_count}</dd>
        <dt>First seen</dt>
        <dd>{stamp(i.first_seen)} <span className="dim">({ago(i.first_seen)})</span></dd>
        <dt>Last seen</dt>
        <dd>{stamp(i.last_seen)} <span className="dim">({ago(i.last_seen)})</span></dd>
      </dl>

      <Section title="What the agent did">
        {!r && <p className="muted">The agent has not run on this incident.</p>}
        {r?.error && <p className="error">The agent stopped with an error: {r.error}</p>}
        {r?.outcome?.kind === 'pr_opened' && (
          <p>
            Opened <a href={r.outcome.url ?? '#'} target="_blank" rel="noreferrer">pull request #{r.pr_number}</a> for human approval.
          </p>
        )}
        {r?.outcome?.kind === 'escalated' && (
          <p>
            Escalated to a human{r.outcome.url && <> (<a href={r.outcome.url} target="_blank" rel="noreferrer">issue #{r.issue_number}</a>)</>}.
          </p>
        )}
        {r && !r.outcome && !r.error && <p className="muted">The agent is still working.</p>}
        {r && <p className="note">Model {r.model ?? 'unknown'} · started {stamp(r.started_at)}{r.finished_at ? ` · finished ${stamp(r.finished_at)}` : ''}</p>}
      </Section>

      {d && (
        <Section title="Diagnosis">
          <dl className="facts">
            <dt>Root cause</dt><dd>{d.diagnosis.root_cause}</dd>
            <dt>Confidence</dt><dd>{d.diagnosis.confidence}</dd>
            <dt>Explanation</dt><dd>{d.diagnosis.explanation}</dd>
            {d.next_step && (<><dt>Next step</dt><dd>{d.next_step}</dd></>)}
            {r?.policy && (
              <>
                <dt>Policy check</dt>
                <dd>{r.policy.passed ? 'Passed' : `Refused at check ${r.policy.failed_check}: ${r.policy.reason}`}</dd>
              </>
            )}
          </dl>
        </Section>
      )}

      {d?.changes && (
        <Section title="Fix">
          <table>
            <thead><tr><th>Key</th><th>New value</th></tr></thead>
            <tbody>
              {d.changes.map((c) => (
                <tr key={c.key_path}><td className="mono">{c.key_path}</td><td className="mono clip" title={c.new_value}>{c.new_value}</td></tr>
              ))}
            </tbody>
          </table>
          {r?.diff && <pre className="log">{r.diff}</pre>}
          {(r?.branch || r?.commit) && (
            <dl className="facts">
              {r.branch && (<><dt>Branch</dt><dd className="mono">{r.branch}</dd></>)}
              {r.commit && (<><dt>Commit</dt><dd className="mono">{r.commit}</dd></>)}
              {r.outcome?.url && r.pr_number && (<><dt>Pull request</dt><dd><a href={r.outcome.url} target="_blank" rel="noreferrer">#{r.pr_number}</a></dd></>)}
            </dl>
          )}
        </Section>
      )}

      {r && r.evidence.length > 0 && (
        <Section title="Evidence">
          <table>
            <thead><tr><th>ID</th><th>Tool</th><th>What it found</th></tr></thead>
            <tbody>
              {r.evidence.map((e) => (
                <tr key={e.id}>
                  <td className={d?.diagnosis.evidence_ids.includes(e.id) ? 'strong' : 'dim'}>{e.id}</td>
                  <td className="mono">{e.tool}</td>
                  <td className="clip wide" title={e.summary}>{e.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="note">Bold ids are the ones the agent's diagnosis cites. The step-by-step detail is in Phoenix.</p>
        </Section>
      )}
    </>
  )
}
