export function ago(iso: string | null | undefined): string {
  if (!iso) return '-'
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  for (const [unit, size] of [['d', 86400], ['h', 3600], ['m', 60]] as const) {
    if (secs >= size) return `${Math.floor(secs / size)}${unit} ago`
  }
  return `${Math.max(secs, 0)}s ago`
}

export function stamp(iso: string | null | undefined): string {
  if (!iso) return '-'
  return new Date(iso).toISOString().replace('T', ' ').slice(0, 19) + ' UTC'
}

const LABELS: Record<string, string> = {
  NEW: 'New',
  DETECTED: 'Detected',
  DIAGNOSING: 'Diagnosing',
  NEEDS_MORE_EVIDENCE: 'Needs evidence',
  PR_CREATED: 'PR created',
  WAITING_FOR_PR_REVIEW: 'Awaiting review',
  PR_REJECTED: 'PR rejected',
  DEPLOYING: 'Deploying',
  VERIFYING: 'Verifying',
  RESOLVED: 'Resolved',
  FAILED: 'Failed',
}

export type Tone = 'ok' | 'warn' | 'bad' | 'muted'

export const statusLabel = (s: string) => LABELS[s] ?? s
export function statusTone(s: string): Tone {
  if (s === 'RESOLVED') return 'ok'
  if (s === 'FAILED' || s === 'PR_REJECTED') return 'bad'
  if (['WAITING_FOR_PR_REVIEW', 'DEPLOYING', 'VERIFYING'].includes(s)) return 'warn'
  return 'muted'
}
