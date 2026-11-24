import { request } from './client'

/** 事件补录领域 API：导入逐行核对、待核确认、窗口覆盖、市级估算与正式分账。 */

export type BackfillLine = {
  id: number
  file_line_no: number
  source_event_no: string
  business_time: string
  region: string
  region_confirmed: number
  person_key: string
  event_name: string
  line_status: string
  reject_reason: string
  accepted_event_id: number | null
}

export type BackfillBatch = {
  id: number
  batch_no: string
  source_org: string
  source_file: string
  shift_code: string
  status: string
  total_lines: number
  accepted_lines: number
  rejected_lines: number
  confirmed_lines: number
  rejected_file_path: string | null
  created_at: string
  lines: BackfillLine[]
  accepted_events: Array<{
    id: number
    file_line_no: number
    source_event_no: string
    business_time: string
    region: string
    person_key: string
  }>
}

export type CoverageEstimate = {
  id: number
  shift_code: string
  estimate_kind: string
  policy: string
  coverage_count: number
  baseline_count: number
  added_count: number
  window_start: string
  window_end: string
  basis_text: string
  status: string
  scope: {
    policy: string
    baseline_count: number
    basis_batches: number[]
    basis_lines: Array<{
      batch_id: number
      file_line_no: number
      source_event_no: string
      region: string
      person_key: string
      business_time: string
    }>
  }
}

export type RegionLedger = {
  id: number
  shift_code: string
  region: string
  coverage_count: number
  basis_batch_ids: string
  basis_line_refs: string
  status: string
}

async function postJson<T>(path: string, payload: Record<string, unknown>): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(payload) })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

export async function importBackfillFile(payload: {
  content: string
  source_org: string
  source_file: string
  shift_code: string
}): Promise<{ ok: boolean; batch: BackfillBatch }> {
  return postJson('/api/backfill/batches/import', payload)
}

export async function listBatches(shiftCode: string): Promise<{ items: BackfillBatch[] }> {
  return getJson(`/api/backfill/batches?shift_code=${encodeURIComponent(shiftCode)}`)
}

export async function getBatch(batchId: number): Promise<{ batch: BackfillBatch }> {
  return getJson(`/api/backfill/batches/${batchId}`)
}

export async function confirmRegion(batchId: number, region: string): Promise<unknown> {
  return postJson(`/api/backfill/batches/${batchId}/confirm-region`, { region })
}

export async function publishEstimate(payload: {
  shift_code: string
  window_start: string
  window_end: string
  policy: 'partial_accepted' | 'hold_baseline'
}): Promise<{ estimate: CoverageEstimate }> {
  return postJson('/api/backfill/estimates', payload)
}

export async function latestEstimate(shiftCode: string): Promise<{ estimate: CoverageEstimate | null }> {
  return getJson(`/api/backfill/estimates/latest?shift_code=${encodeURIComponent(shiftCode)}`)
}

export async function listLedgers(shiftCode: string): Promise<{ items: RegionLedger[] }> {
  return getJson(`/api/backfill/ledgers?shift_code=${encodeURIComponent(shiftCode)}`)
}

export async function publishLedgers(payload: {
  shift_code: string
  window_start: string
  window_end: string
  estimate_id?: number
}): Promise<{ ok: boolean; ledgers: RegionLedger[] }> {
  return postJson('/api/backfill/ledgers', payload)
}

export function rejectFileUrl(batchId: number): string {
  return `/api/backfill/batches/${batchId}/rejected-file`
}
