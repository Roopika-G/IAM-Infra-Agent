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
  DIAGNOSING: 'Agent working',
  WAITING_FOR_PR_REVIEW: 'PR open, awaiting review',
  MERGED: 'Merged, deploy pending',
  DEPLOYING: 'Deploying',
  RETRY: 'Deploy failed, agent retrying',
  ESCALATED: 'Escalated to a human',
  PR_REJECTED: 'PR rejected',
  RESOLVED: 'Resolved',
  FAILED: 'Failed',
  ARCHIVED: 'Archived (tested)',
}

export type Tone = 'ok' | 'warn' | 'bad' | 'muted'

export const statusLabel = (s: string) => LABELS[s] ?? s
export function statusTone(s: string): Tone {
  if (s === 'RESOLVED') return 'ok'
  if (s === 'FAILED' || s === 'PR_REJECTED') return 'bad'
  if (['DIAGNOSING', 'WAITING_FOR_PR_REVIEW', 'MERGED', 'DEPLOYING', 'RETRY', 'ESCALATED'].includes(s)) return 'warn'
  return 'muted'
}
