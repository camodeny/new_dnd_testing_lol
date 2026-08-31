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

function getStreamUrl(path: string): string {
  const token = getToken()
  return `${API_BASE}${path}?token=${encodeURIComponent(token ?? '')}`
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
  list: () => apiFetch<{ campaigns: import('@/types').Campaign[] }>('/campaigns'),
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
    apiFetch<{ campaign: import('@/types').Campaign }>('/campaigns/quick-create', {
      method: 'POST',
      body: JSON.stringify(payload),
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
  createInvite: (campaignId: string | number) =>
    apiFetch<{ code: string }>(`/campaigns/${campaignId}/invites`, { method: 'POST' }),
  getInvite: (campaignId: string | number) =>
    apiFetch<{ code?: string }>(`/campaigns/${campaignId}/invites`),
  lookupInvite: (code: string) =>
    apiFetch<{ campaign: import('@/types').Campaign; campaign_id?: string }>(`/invites/lookup?code=${encodeURIComponent(code)}`),
  joinCampaign: (campaignId: string | number, code: string) =>
    apiFetch(`/campaigns/${campaignId}/join`, {
      method: 'POST',
      body: JSON.stringify({ code }),
    }),
}

// ── Sessions ──────────────────────────────────────────────────────────────

export const sessions = {
  start: (campaignId: string | number) =>
    apiFetch<{ session: import('@/types').Session }>(`/campaigns/${campaignId}/sessions`, {
      method: 'POST',
    }),
}

// The current campaign page still reads the pre-modular live-table contract.
// Keep those calls explicit until that page moves to snapshots, submissions, and realtime.
export const legacyLiveTable = {
  get: (sessionId: number, opts: { limit?: number; beforeId?: number } = {}) => {
    const params = new URLSearchParams()
    if (opts.limit) params.set('limit', String(opts.limit))
    if (opts.beforeId) params.set('before_id', String(opts.beforeId))
    const qs = params.toString() ? `?${params}` : ''
    return apiFetch<{ session: import('@/types').Session; messages: import('@/types').Message[] }>(
      `/sessions/${sessionId}${qs}`,
    )
  },
  streamUrl: (sessionId: number) => getStreamUrl(`/sessions/${sessionId}/stream`),
  sendMessage: (sessionId: number, content: string, role = 'player') =>
    apiFetch(`/sessions/${sessionId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content, role }),
    }),
  getMessages: (sessionId: number, opts: { limit?: number; beforeId?: number } = {}) => {
    const params = new URLSearchParams()
    if (opts.limit) params.set('limit', String(opts.limit))
    if (opts.beforeId) params.set('before_id', String(opts.beforeId))
    const qs = params.toString() ? `?${params}` : ''
    return apiFetch<{ messages: import('@/types').Message[] }>(`/sessions/${sessionId}/messages${qs}`)
  },
}

// ── World & planning ──────────────────────────────────────────────────────

export const world = {
  get: (campaignId: string | number) =>
    apiFetch<{ world: import('@/types').CampaignWorld }>(`/campaigns/${campaignId}/world`),
}

// ── Encounter maps ────────────────────────────────────────────────────────

export const encounterMaps = {
  getCurrent: (campaignId: string | number) =>
    apiFetch<{ map: import('@/types').EncounterMap }>(`/campaigns/${campaignId}/encounter-maps/current`),
}
