/**
 * Campaign capacity projection — issue #255.
 *
 * The backend (#253 ledger + #254 pause/resume policy) owns all accounting.
 * This module only projects the participant-safe aggregate served by
 * `GET /api/campaigns/{id}/capacity-state` into a simple UI view:
 * a percentage plus one of `normal | low | grace | paused | unavailable`.
 *
 * Deliberately excluded from the view: raw costs, token counts, provider
 * details, contributor identities, payment details, and BYOK credentials.
 * Capacity is campaign-level/shared; copy must never blame a participant.
 */

/** Raw participant-safe payload from GET /api/campaigns/{id}/capacity-state. */
export interface CapacityStatePayload {
  campaign_id?: string
  /** Aggregate percent of funded capacity consumed (0–100). */
  percent_used?: number | null
  ai_paused?: boolean
  grace_active?: boolean
  gate_reason?: string
  funded_cents?: number
  consumed_cents?: number
  remaining_cents?: number
  contributor_count?: number
  overage_allowance_cents?: number
}

/** Simple UI states. `low` is a quiet heads-up; `grace` means the table is
 *  finishing the current moment before new AI narration may pause. */
export type CapacityViewState = 'normal' | 'low' | 'grace' | 'paused' | 'unavailable'

export interface CapacityView {
  state: CapacityViewState
  /** Rounded display percentage, or null when unknown (never invented). */
  percent: number | null
}

/** Percent at/above which the meter quietly notes capacity is getting low. */
export const CAPACITY_LOW_THRESHOLD_PCT = 80

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.max(0, Math.min(100, Math.round(value)))
}

export function deriveCapacityView(payload: CapacityStatePayload | null | undefined): CapacityView {
  if (!payload) return { state: 'unavailable', percent: null }
  const percent = typeof payload.percent_used === 'number' && Number.isFinite(payload.percent_used)
    ? clampPercent(payload.percent_used)
    : null
  if (payload.ai_paused) return { state: 'paused', percent }
  if (payload.grace_active) return { state: 'grace', percent }
  if (percent !== null && percent >= CAPACITY_LOW_THRESHOLD_PCT) return { state: 'low', percent }
  return { state: 'normal', percent }
}

/** Whether an API failure is the #254 pause signal (HTTP 409,
 *  `ai_paused_capacity`): the unsent text stays an editable local draft and
 *  the client should resync from the capacity-state hook, not clear the draft
 *  as accepted work. */
export function isCapacityPausedError(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const record = error as { status?: unknown; data?: unknown }
  if (record.status !== 409) return false
  const data = record.data as { detail?: unknown; code?: unknown } | null | undefined
  if (!data || typeof data !== 'object') return false
  const detail = (data as { detail?: unknown }).detail
  if (detail && typeof detail === 'object') {
    return (detail as { code?: unknown }).code === 'ai_paused_capacity'
  }
  return (data as { code?: unknown }).code === 'ai_paused_capacity'
}

/** UI telemetry seam for capacity observability (#255): projection load
 *  errors, paused-state views, resume-path choice (once #256 flows exist),
 *  and submission attempts made while paused. The app wires these to its
 *  observability path; default consumers may ignore them. */
export type CapacityUiEvent =
  | { type: 'projection_load_error'; message: string }
  | { type: 'paused_view' }
  | { type: 'resume_choice'; path: 'add_funds' | 'byok' }
  | { type: 'paused_submit_attempt' }
