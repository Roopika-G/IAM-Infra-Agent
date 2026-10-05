import { useApprovals } from '../api'
import { ago } from '../format'
import Section from '../components/Section'
import Status from '../components/Status'

export default function Approvals() {
  const q = useApprovals()
  return (
    <>
      <header className="page-head">
        <h1>Approvals</h1>
      </header>
      <Section title="Remediation pull requests">
        <p className="note">
          The agent's fixes arrive as pull requests from <span className="mono">remediation/inc-&lt;id&gt;</span> branches.
          Review and merge happen on GitHub; this lists them.
        </p>
        {q.isLoading && <p className="muted">Loading…</p>}
        {q.isError && <p className="error">{(q.error as Error).message}</p>}
        {q.data?.error && <p className="error">Could not read GitHub: {q.data.error}</p>}
        {q.data && !q.data.error && q.data.prs.length === 0 && <p className="muted">No remediation pull requests yet.</p>}
        {q.data && q.data.prs.length > 0 && (
          <table>
            <thead>
              <tr>
                <th className="num">PR</th>
                <th>Title</th>
                <th>Branch</th>
                <th>State</th>
                <th>Opened</th>
              </tr>
            </thead>
            <tbody>
              {q.data.prs.map((p) => (
                <tr key={p.number}>
                  <td className="num">
                    <a href={p.url} target="_blank" rel="noreferrer">#{p.number}</a>
                  </td>
                  <td>{p.title}</td>
                  <td className="mono dim">{p.headRefName}</td>
                  <td>
                    <Status tone={p.state === 'OPEN' ? 'warn' : p.state === 'MERGED' ? 'ok' : 'muted'}>{p.state.toLowerCase()}</Status>
                  </td>
                  <td className="dim">{ago(p.createdAt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </>
  )
}
