import { useQuery } from '@tanstack/react-query'

export interface FleetComponent {
  name: string
  present: boolean
  pod?: string
  phase?: string
  ready?: string
  restarts?: number
  replication?: string | null
  drift?: number
  incidents?: number
  healthy?: boolean
}

export interface DriftRow {
  key: string
  component: string
  golden: string
  live: string | null
  state: 'ok' | 'drift' | 'unknown'
}

export interface Fleet {
  error: string | null
  components: FleetComponent[]
  drift: DriftRow[]
  pf_error: string | null
  version: string | null
}

export interface Occurrence {
  time: string
  role: string | null
  pod: string | null
  message: string
}

export interface Incident {
  id: number
  status: string
  pf_role: string | null
  log_type: string | null
  logger: string | null
  exception_type: string | null
  sample_message: string | null
  first_seen: string
  last_seen: string
  occurrence_count: number
  fingerprint: string
  recent_logs?: Occurrence[]
  result?: Result | null
  result_kind?: string | null
  result_url?: string | null
}

export interface Decision {
  kind: 'repo_fix' | 'escalate'
  diagnosis: { root_cause: string; confidence: string; evidence_ids: string[]; explanation: string }
  changes?: { key_path: string; new_value: string }[]
  next_step?: string
}

export interface Result {
  run_id: number
  model: string | null
  started_at: string
  finished_at: string | null
  status: string
  outcome: { kind: string; url: string | null; detail: string } | null
  error: string | null
  decision: Decision | null
  policy: { passed: boolean; failed_check: number | null; reason: string | null } | null
  evidence: { id: string; tool: string; summary: string }[]
  commit: string | null
  branch: string | null
  diff: string | null
  pr_number: number | null
  issue_number: number | null
}

export interface Activity {
  time: string
  incident_id: number
  text: string
  url?: string | null
}

export interface TimelineStep {
  time: string | null
  title: string
  detail: string
  state: 'done' | 'active' | 'pending' | 'bad'
  url: string | null
}

export interface Timeline {
  incident: { id: number; status: string } | null
  steps: TimelineStep[]
}

export interface RemediationPr {
  number: number
  title: string
  headRefName: string
  state: string
  createdAt: string
  url: string
}

export interface Approvals {
  error: string | null
  prs: RemediationPr[]
}

export interface SimStatus {
  running: boolean
  mode: string | null
  exit_code: number | null
  tail: string
  state: 'healthy' | 'corrupted' | 'unknown'
}

export async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* body wasn't JSON */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export const useFleet = () =>
  useQuery({ queryKey: ['fleet'], queryFn: () => getJson<Fleet>('/api/fleet'), refetchInterval: 10_000 })

export const useIncidents = (scope: 'open' | 'past' | 'archived') =>
  useQuery({
    queryKey: ['incidents', scope],
    queryFn: () => getJson<Incident[]>(`/api/incidents?scope=${scope}`),
    refetchInterval: 5_000,
  })

export const useIncident = (id: string | undefined) =>
  useQuery({ queryKey: ['incident', id], queryFn: () => getJson<Incident>(`/api/incidents/${id}`) })

export const useActivity = () =>
  useQuery({ queryKey: ['activity'], queryFn: () => getJson<Activity[]>('/api/activity'), refetchInterval: 5_000 })

export const useConfig = () =>
  useQuery({ queryKey: ['config'], queryFn: () => getJson<{ phoenix_url: string }>('/api/config'), staleTime: Infinity })

export const useTimeline = () =>
  useQuery({ queryKey: ['timeline'], queryFn: () => getJson<Timeline>('/api/timeline'), refetchInterval: 3_000 })

export const useApprovals = () =>
  useQuery({ queryKey: ['approvals'], queryFn: () => getJson<Approvals>('/api/approvals'), refetchInterval: 30_000 })

export const useSimA = () =>
  useQuery({
    queryKey: ['simA'],
    queryFn: () => getJson<SimStatus>('/api/simulations/a'),
    refetchInterval: (q) => (q.state.data?.running ? 2_000 : 15_000),
  })
