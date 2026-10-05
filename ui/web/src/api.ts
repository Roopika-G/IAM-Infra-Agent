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

export const useIncidents = (scope: 'open' | 'all') =>
  useQuery({
    queryKey: ['incidents', scope],
    queryFn: () => getJson<Incident[]>(`/api/incidents?scope=${scope}`),
    refetchInterval: 5_000,
  })

export const useIncident = (id: string | undefined) =>
  useQuery({ queryKey: ['incident', id], queryFn: () => getJson<Incident>(`/api/incidents/${id}`) })

export const useApprovals = () =>
  useQuery({ queryKey: ['approvals'], queryFn: () => getJson<Approvals>('/api/approvals'), refetchInterval: 30_000 })

export const useSimA = () =>
  useQuery({
    queryKey: ['simA'],
    queryFn: () => getJson<SimStatus>('/api/simulations/a'),
    refetchInterval: (q) => (q.state.data?.running ? 2_000 : 15_000),
  })
