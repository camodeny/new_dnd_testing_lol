import type { ApiError } from '@/types'

const API_BASE = '/api'

function getToken(): string | null {
  if (typeof window === 'undefined') return null
  return localStorage.getItem('token')
}

function errorMessageFrom(value: unknown): string | null {
  if (typeof value === 'string') return value.trim() || null
  if (Array.isArray(value)) {
    const messages = value.map(errorMessageFrom).filter((message): message is string => Boolean(message))
    return messages.length ? messages.join('; ') : null
  }
  if (value && typeof value === 'object') {
    const detail = value as { loc?: unknown; message?: unknown; msg?: unknown }
    const message = errorMessageFrom(detail.message) ?? errorMessageFrom(detail.msg)
    if (!message) return null
    const location = Array.isArray(detail.loc)
      ? detail.loc.filter((part) => typeof part === 'string' || typeof part === 'number').join('.')
      : ''
    return location ? `${location}: ${message}` : message
  }
  return null
}

function createApiError(status: number, data: unknown): ApiError {
  if (typeof data === 'string') {
    const trimmed = data.trim()
    if (trimmed) {
      const error = new Error(trimmed) as ApiError
      error.status = status
      error.data = data
      return error
    }
  }
  const payload = data && typeof data === 'object'
    ? data as { detail?: unknown; error?: unknown; message?: unknown }
    : {}
  const message =
    errorMessageFrom(payload.detail) ??
    errorMessageFrom(payload.error) ??
    errorMessageFrom(payload.message) ??
    `HTTP ${status}`
  const error = new Error(message) as ApiError
  error.status = status
  error.data = data
  return error
}

export async function apiFetch<T = unknown>(
  path: string,
  options: Omit<RequestInit, 'headers'> & { headers?: Record<string, string> } = {},
): Promise<T> {
  const url = `${API_BASE}${path}`
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers ?? {}),
  }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  const res = await fetch(url, { ...options, headers, credentials: 'include' })
  const data = await res.json().catch(() => ({}))

  if (!res.ok) {
    throw createApiError(res.status, data)
  }
  return data as T
}

// ── Auth ─────────────────────────────────────────────────────────────────

export const auth = {
  me: (signal?: AbortSignal) => apiFetch<{ user: import('@/types').User }>('/me', { signal }),
  getConfig: () => apiFetch<{
    sso_enabled: boolean
    supabase_url?: string | null
    supabase_configured?: boolean
  }>('/auth/config'),
}

// ── Campaigns ─────────────────────────────────────────────────────────────

export const campaigns = {
  list: (includeArchived = false) =>
    apiFetch<{ campaigns: import('@/types').Campaign[] }>(
      includeArchived ? '/campaigns?include_archived=true' : '/campaigns',
    ),
  get: (id: string | number) => apiFetch<{ campaign: import('@/types').Campaign }>(`/campaigns/${id}`),
  create: (payload: Record<string, unknown>) =>
    apiFetch<{ campaign: import('@/types').Campaign }>('/campaigns', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  update: (
    id: string | number,
    expectedRevision: number,
    changes: Record<string, unknown>,
    idempotencyKey: string,
  ) => apiFetch<{ campaign: import('@/types').Campaign; event: unknown }>(`/campaigns/${id}`, {
    method: 'PUT',
    body: JSON.stringify({ ...changes, expected_revision: expectedRevision }),
    headers: { 'Idempotency-Key': idempotencyKey },
  }),
  delete: (id: string | number) => apiFetch(`/campaigns/${id}`, { method: 'DELETE' }),
  randomBrief: (payload: Record<string, unknown> = {}) =>
    apiFetch<{ name?: string; description?: string; random_seed?: string }>(
      '/campaigns/random-brief',
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  quickCreate: (payload: Record<string, unknown> = {}) =>
    apiFetch<{ campaign: import('@/types').Campaign }>(`/campaigns/quick-create`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  transitionLifecycle: (id: string | number, expectedRevision: number, status: string, idempotencyKey: string) =>
    apiFetch<{ campaign: import('@/types').Campaign }>(`/campaigns/${id}/lifecycle`, {
      method: 'POST',
      body: JSON.stringify({ expected_revision: expectedRevision, status }),
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  // Issue #265 — restore target is derived server-side from the
  // authoritative archive event (the events feed is truncated), surfaced on
  // campaign detail as `restore_from` while archived.
  restoreTarget: async (id: string | number): Promise<string> => {
    const data = await apiFetch<{
      campaign: import('@/types').Campaign
      restore_from?: string | null
    }>(`/campaigns/${id}`)
    const from = data.restore_from
    if (from !== 'lobby' && from !== 'starting' && from !== 'active') {
      throw new Error('Campaign cannot be restored: pre-archive status unknown.')
    }
    return from
  },
  // Production world-seed generation (#245): stages durable seed canon and
  // moves the campaign lobby -> starting. The live-table opening is #246.
  worldSeed: (id: string | number, idempotencyKey: string) =>
    apiFetch<{ campaign: import('@/types').Campaign; seed: unknown }>(`/campaigns/${id}/world-seed`, {
      method: 'POST',
      body: JSON.stringify({ operation_id: idempotencyKey }),
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
}

// ── Characters ────────────────────────────────────────────────────────────

export const characters = {
  list: () => apiFetch<{ characters: import('@/types').Character[] }>('/characters'),
  get: (id: number | string) => apiFetch<{ character: import('@/types').Character }>(`/characters/${id}`),
  create: (payload: Record<string, unknown>, idempotencyKey: string) =>
    apiFetch<{ character: import('@/types').Character }>('/characters', {
      method: 'POST',
      body: JSON.stringify(payload),
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  update: (id: number | string, payload: Record<string, unknown>) =>
    apiFetch<{ character: import('@/types').Character }>(`/characters/${id}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  delete: (id: number | string) => apiFetch(`/characters/${id}`, { method: 'DELETE' }),
  chatStream: (id: string | number, payload: Record<string, unknown>) =>
    fetch(`/api/characters/${encodeURIComponent(String(id))}/chat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(typeof window !== 'undefined' && localStorage.getItem('token')
          ? { Authorization: `Bearer ${localStorage.getItem('token')}` }
          : {}),
      },
      body: JSON.stringify(payload),
    }),
}

// ── Campaign characters & members ─────────────────────────────────────────

export const campaignMembers = {
  listCharacters: (campaignId: string | number) =>
    apiFetch<{ characters: import('@/types').Character[] }>(`/campaigns/${campaignId}/characters`),
  listMembers: (campaignId: string | number) =>
    apiFetch<{ members: import('@/types').CampaignMember[] }>(`/campaigns/${campaignId}/members`),
  getLobby: (campaignId: string | number) =>
    apiFetch<{ campaign: import('@/types').Campaign; members: import('@/types').CampaignMember[]; eligibility: import('@/types').LobbyEligibility; launch_locked: boolean; invites?: import('@/types').CampaignInvite[]; outstanding_invites?: number; party_composition?: import('@/types').PartyComposition }>(`/campaigns/${campaignId}/lobby`),
  getPartyComposition: (campaignId: string | number) =>
    apiFetch<{ party_composition: import('@/types').PartyComposition }>(`/campaigns/${campaignId}/party-composition`),
  getPartyAdvice: (campaignId: string | number) =>
    apiFetch<{ advice: import('@/types').PartyAdvice }>(`/campaigns/${campaignId}/party-advice`),
  getCharacterLore: (campaignId: string | number, characterId: string) =>
    apiFetch<{ lore: import('@/types').CharacterLore }>(`/campaigns/${campaignId}/characters/${characterId}/lore`),
  putCharacterLore: (campaignId: string | number, characterId: string, expectedRevision: number, content: string, idempotencyKey: string, operationId?: string) =>
    apiFetch(`/campaigns/${campaignId}/characters/${characterId}/lore`, {
      method: 'PUT',
      body: JSON.stringify({ expected_revision: expectedRevision, content, operation_id: operationId ?? idempotencyKey }),
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  deleteCharacterLore: (campaignId: string | number, characterId: string, expectedRevision: number, idempotencyKey: string) =>
    apiFetch(`/campaigns/${campaignId}/characters/${characterId}/lore`, {
      method: 'DELETE',
      body: JSON.stringify({ expected_revision: expectedRevision }),
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  selectCharacter: (campaignId: string | number, expectedRevision: number, characterId: string, idempotencyKey: string) =>
    apiFetch(`/campaigns/${campaignId}/members/me/character`, {
      method: 'PUT',
      body: JSON.stringify({ expected_revision: expectedRevision, character_id: characterId }),
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  setReadiness: (campaignId: string | number, expectedRevision: number, ready: boolean, idempotencyKey: string) =>
    apiFetch(`/campaigns/${campaignId}/members/me/readiness`, {
      method: 'PUT',
      body: JSON.stringify({ expected_revision: expectedRevision, ready }),
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  // Lobby invitations — issue #242. Canonical multi-invite flow: owners
  // create/list/revoke invites (each with its own code + shareable
  // `/invite/:code` URL); recipients look up minimal safe metadata and
  // accept idempotently; email delivery is optional with link/code fallback.
  createInvite: (campaignId: string | number, options?: { intended_email?: string; recipient_label?: string; expires_in_hours?: number; expires_at?: string }) =>
    apiFetch<{ code: string; invite_url: string; invite_url_path: string }>(`/campaigns/${campaignId}/invites`, {
      method: 'POST',
      body: JSON.stringify(options ?? {}),
    }),
  listInvites: (campaignId: string | number) =>
    apiFetch<{ invites: import('@/types').CampaignInvite[] }>(`/campaigns/${campaignId}/invites`),
  revokeInvite: (campaignId: string | number, code: string, expectedRevision: number, idempotencyKey: string) =>
    apiFetch(`/campaigns/${campaignId}/invites/${encodeURIComponent(code)}`, {
      method: 'DELETE',
      body: JSON.stringify({ expected_revision: expectedRevision }),
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  sendInviteEmail: (campaignId: string | number, code: string, email: string) =>
    apiFetch(`/campaigns/${campaignId}/invites/${encodeURIComponent(code)}/email`, {
      method: 'POST',
      body: JSON.stringify({ to_email: email }),
    }),
  lookupInvite: (code: string) =>
    apiFetch<import('@/types').InviteLookup>(`/invites/lookup?code=${encodeURIComponent(code)}`),
  acceptInvite: (code: string) =>
    apiFetch<{ ok: boolean; campaign_id: string; campaign: import('@/types').Campaign; duplicate?: boolean }>(`/invites/accept`, {
      method: 'POST',
      body: JSON.stringify({ code }),
    }),
  joinCampaign: (campaignId: string | number, code: string) =>
    apiFetch(`/campaigns/${campaignId}/join`, {
      method: 'POST',
      body: JSON.stringify({ code }),
    }),
}

// ── Live-table gameplay threads ──────────────────────────────────────────

export const gameplayThreads = {
  list: (campaignId: string | number) =>
    apiFetch<{ threads: import('@/types').CampaignThread[] }>(`/campaigns/${campaignId}/threads`),
  getOrCreateDm: (campaignId: string | number) =>
    apiFetch<{ thread: import('@/types').CampaignThread; created: boolean }>(`/campaigns/${campaignId}/threads/dm`, {
      method: 'POST',
    }),
  getOrCreateDirect: (campaignId: string | number, participantId: string) =>
    apiFetch<{ thread: import('@/types').CampaignThread; created: boolean }>(`/campaigns/${campaignId}/threads/direct`, {
      method: 'POST',
      body: JSON.stringify({ participant_id: participantId }),
    }),
  submit: (campaignId: string | number, threadId: string, content: string, operationId: string) =>
    apiFetch(`/campaigns/${campaignId}/submissions`, {
      method: 'POST',
      body: JSON.stringify({ thread_id: threadId, content, operation_id: operationId }),
      headers: { 'Idempotency-Key': operationId },
    }),
}

// ── Sessions ──────────────────────────────────────────────────────────────

export const sessions = {
  start: (campaignId: string | number) =>
    apiFetch<{ session: import('@/types').Session }>(`/campaigns/${campaignId}/sessions`, {
      method: 'POST',
    }),
}

// ── Encounter maps ────────────────────────────────────────────────────────
// Removed: the encounter-maps/current stub had no authoritative backend
// (PR #348 re-review). StoryAtlas owns map state client-side via
// onEncounterMapChange until a real map authority lands.
