import type { Tone } from '../format'

export default function Status({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return (
    <span className="status">
      <span className={`dot ${tone}`} />
      {children}
    </span>
  )
}
