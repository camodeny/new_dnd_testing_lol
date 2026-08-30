import type { ApiError } from '@/types'

const API_BASE = '/api'

function getToken(): string | null {
  if (typeof window === 'undefined') return null
  return localStorage.getItem('token')
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
    const err = new Error((data as { error?: string }).error ?? `HTTP ${res.status}`) as ApiError
    err.status = res.status
    err.data = data
    throw err
  }
  return data as T
}

export async function apiBlob(
  path: string,
  options: Omit<RequestInit, 'headers'> & { headers?: Record<string, string> } = {},
): Promise<Blob> {
  const url = `${API_BASE}${path}`
  const headers: Record<string, string> = { ...(options.headers ?? {}) }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  const res = await fetch(url, { ...options, headers, credentials: 'include' })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    const err = new Error((data as { error?: string }).error ?? `HTTP ${res.status}`) as ApiError
    err.status = res.status
    err.data = data
    throw err
  }
  return res.blob()
}

export function getStreamUrl(path: string): string {
  const token = getToken()
  return `${API_BASE}${path}?token=${encodeURIComponent(token ?? '')}`
}

// ── Auth ─────────────────────────────────────────────────────────────────

export const auth = {
  me: (signal?: AbortSignal) => apiFetch<{ user: import('@/types').User }>('/me', { signal }),
  login: (username: string, password: string) =>
    apiFetch<{ user: import('@/types').User; token?: string }>('/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  register: (username: string, password: string) =>
    apiFetch<{ user: import('@/types').User; token?: string }>('/register', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => apiFetch('/logout', { method: 'POST' }),
  getConfig: () => apiFetch<{ sso_enabled: boolean; sso_label?: string }>('/auth/config'),
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
  ) => apiFetch<{ campaign: import('@/types').Campaign; event: unknown }>(`/campaigns/${id}`, {
    method: 'PUT',
    body: JSON.stringify({ ...changes, expected_revision: expectedRevision }),
  }),
  delete: (id: string | number) => apiFetch(`/campaigns/${id}`, { method: 'DELETE' }),
  export: (id: string | number) => apiBlob(`/campaigns/${id}/export`),
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
  devAudit: (id: string | number) => apiFetch(`/campaigns/${id}/dev`),
}

// ── Characters ────────────────────────────────────────────────────────────

export const characters = {
  list: () => apiFetch<{ characters: import('@/types').Character[] }>('/characters'),
  get: (id: number | string) => apiFetch<{ character: import('@/types').Character }>(`/characters/${id}`),
  create: (payload: Record<string, unknown>) =>
    apiFetch<{ character: import('@/types').Character }>('/characters', {
      method: 'POST',
      body: JSON.stringify(payload),
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
  addCharacter: (campaignId: string | number, characterId: number) =>
    apiFetch(`/campaigns/${campaignId}/characters`, {
      method: 'POST',
      body: JSON.stringify({ character_id: characterId }),
    }),
  listMembers: (campaignId: string | number) =>
    apiFetch<{ members: import('@/types').CampaignMember[] }>(`/campaigns/${campaignId}/members`),
  removeMember: (campaignId: string | number, userId: string | number) =>
    apiFetch(`/campaigns/${campaignId}/members/${userId}`, { method: 'DELETE' }),
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
  get: (sessionId: number, opts: { limit?: number; beforeId?: number } = {}) => {
    const params = new URLSearchParams()
    if (opts.limit) params.set('limit', String(opts.limit))
    if (opts.beforeId) params.set('before_id', String(opts.beforeId))
    const qs = params.toString() ? `?${params}` : ''
    return apiFetch<{ session: import('@/types').Session; messages: import('@/types').Message[] }>(
      `/sessions/${sessionId}${qs}`,
    )
  },
  end: (sessionId: number, recap?: string) =>
    apiFetch(`/sessions/${sessionId}`, { method: 'PUT', body: JSON.stringify({ recap }) }),
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
  getDmTurnStatus: (sessionId: number) =>
    apiFetch<{ status: string }>(`/sessions/${sessionId}/dm-turn-status`),
}

// ── World & planning ──────────────────────────────────────────────────────

export const world = {
  get: (campaignId: string | number) =>
    apiFetch<{ world: import('@/types').CampaignWorld }>(`/campaigns/${campaignId}/world`),
  generate: (campaignId: string | number) =>
    apiFetch(`/campaigns/${campaignId}/world`, { method: 'POST' }),
}

export const planning = {
  get: (campaignId: string | number) => apiFetch(`/campaigns/${campaignId}/planning`),
  sendMessage: (
    campaignId: string | number,
    content: string,
    opts: { draftCharacter?: unknown; activePage?: string } = {},
  ) =>
    apiFetch(`/campaigns/${campaignId}/planning/messages`, {
      method: 'POST',
      body: JSON.stringify({
        content,
        draft_character: opts.draftCharacter,
        active_page: opts.activePage,
      }),
    }),
  streamUrl: (campaignId: string | number) => getStreamUrl(`/campaigns/${campaignId}/planning/stream`),
  selectCharacter: (campaignId: string | number, characterId: number) =>
    apiFetch(`/campaigns/${campaignId}/planning/character`, {
      method: 'PUT',
      body: JSON.stringify({ character_id: characterId }),
    }),
  setReady: (campaignId: string | number, ready: boolean) =>
    apiFetch(`/campaigns/${campaignId}/planning/ready`, {
      method: 'PUT',
      body: JSON.stringify({ ready }),
    }),
  updateBond: (campaignId: string | number, bondId: number, payload: Record<string, unknown>) =>
    apiFetch(`/campaigns/${campaignId}/planning/bonds/${bondId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
}

// ── Encounter maps ────────────────────────────────────────────────────────

export const encounterMaps = {
  getCurrent: (campaignId: string | number) =>
    apiFetch<{ map: import('@/types').EncounterMap }>(`/campaigns/${campaignId}/encounter-maps/current`),
  image: (encounterMapId: number) => apiBlob(`/encounter-maps/${encounterMapId}/image`),
  labeledImage: (encounterMapId: number) => apiBlob(`/encounter-maps/${encounterMapId}/labeled-image`),
  moveToken: (encounterMapId: number, col: number, row: number) =>
    apiFetch(`/encounter-maps/${encounterMapId}/placements/me`, {
      method: 'PATCH',
      body: JSON.stringify({ col, row }),
    }),
  rollInitiative: (
    encounterMapId: number,
    actorType: string,
    actorId: number,
    initiative: number,
  ) =>
    apiFetch(`/encounter-maps/${encounterMapId}/encounter/roll-initiative`, {
      method: 'POST',
      body: JSON.stringify({ actor_type: actorType, actor_id: actorId, initiative }),
    }),
  nextTurn: (encounterMapId: number) =>
    apiFetch(`/encounter-maps/${encounterMapId}/encounter/next-turn`, { method: 'POST' }),
}

// ── Sheet proposals ───────────────────────────────────────────────────────

export const proposals = {
  list: (sessionId: number) =>
    apiFetch<{ proposals: import('@/types').SheetProposal[] }>(`/sessions/${sessionId}/proposals`),
  apply: (sessionId: number, proposalId: number) =>
    apiFetch(`/sessions/${sessionId}/proposals/${proposalId}/apply`, { method: 'POST' }),
  dismiss: (sessionId: number, proposalId: number) =>
    apiFetch(`/sessions/${sessionId}/proposals/${proposalId}/dismiss`, { method: 'POST' }),
}

// ── LLM players ───────────────────────────────────────────────────────────

export const llmPlayers = {
  list: (campaignId: string | number) =>
    apiFetch<{ llm_players: import('@/types').LlmPlayer[] }>(`/campaigns/${campaignId}/llm-players`),
  create: (campaignId: string | number, payload: Record<string, unknown>) =>
    apiFetch(`/campaigns/${campaignId}/llm-players`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  assign: (campaignId: string | number, llmPlayerId: number) =>
    apiFetch(`/campaigns/${campaignId}/llm-players/assign`, {
      method: 'POST',
      body: JSON.stringify({ llm_player_id: llmPlayerId }),
    }),
  rotateKey: (campaignId: string | number, llmPlayerId: number) =>
    apiFetch(`/campaigns/${campaignId}/llm-players/${llmPlayerId}/rotate-key`, { method: 'POST' }),
  delete: (campaignId: string | number, llmPlayerId: number) =>
    apiFetch(`/campaigns/${campaignId}/llm-players/${llmPlayerId}`, { method: 'DELETE' }),
}

// ── Automation keys ───────────────────────────────────────────────────────

export const automationKeys = {
  list: () => apiFetch<{ keys: import('@/types').AutomationKey[] }>('/automation-keys'),
  create: (payload: Record<string, unknown> = {}) =>
    apiFetch('/automation-keys', { method: 'POST', body: JSON.stringify(payload) }),
  delete: (id: number) => apiFetch(`/automation-keys/${id}`, { method: 'DELETE' }),
}

// ── Automation workspace ──────────────────────────────────────────────────

export const automation = {
  getWorkspace: () => apiFetch('/automation'),
  streamUrl: () => getStreamUrl('/automation/stream'),
  createScenario: (payload: Record<string, unknown>) =>
    apiFetch('/automation/scenarios', { method: 'POST', body: JSON.stringify(payload) }),
  getScenario: (scenarioId: number) => apiFetch(`/automation/scenarios/${scenarioId}`),
  updateScenario: (scenarioId: number, payload: Record<string, unknown>) =>
    apiFetch(`/automation/scenarios/${scenarioId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deleteScenario: (scenarioId: number) =>
    apiFetch(`/automation/scenarios/${scenarioId}`, { method: 'DELETE' }),
  createSnapshot: (scenarioId: number, payload: Record<string, unknown> = {}) =>
    apiFetch(`/automation/scenarios/${scenarioId}/snapshots`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  deleteSnapshot: (snapshotId: number) =>
    apiFetch(`/automation/snapshots/${snapshotId}`, { method: 'DELETE' }),
  createRun: (scenarioId: number, payload: Record<string, unknown> = {}) =>
    apiFetch(`/automation/scenarios/${scenarioId}/runs`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getRun: (runId: number) => apiFetch(`/automation/runs/${runId}`),
  stopRun: (runId: number) =>
    apiFetch(`/automation/runs/${runId}/stop`, { method: 'POST', body: JSON.stringify({}) }),
  deleteRun: (runId: number) => apiFetch(`/automation/runs/${runId}`, { method: 'DELETE' }),
  continueRun: (runId: number, payload: Record<string, unknown> = {}) =>
    apiFetch(`/automation/runs/${runId}/continue`, { method: 'POST', body: JSON.stringify(payload) }),
  runStreamUrl: (runId: number) => getStreamUrl(`/automation/runs/${runId}/stream`),
  getRunProviderCalls: (runId: number, includeArtifacts = false) =>
    apiFetch(`/automation/runs/${runId}/provider-calls?include_artifacts=${includeArtifacts}`),
  compareRuns: (payload: Record<string, unknown>) =>
    apiFetch('/automation/compare', { method: 'POST', body: JSON.stringify(payload) }),
  startAuditors: (runId: number, payload: Record<string, unknown> = {}) =>
    apiFetch(`/automation/runs/${runId}/auditors/start`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  stopAuditors: (runId: number) =>
    apiFetch(`/automation/runs/${runId}/auditors/stop`, { method: 'POST', body: JSON.stringify({}) }),
}

// ── Loot & shops ──────────────────────────────────────────────────────────

export const loot = {
  list: (campaignId: string | number) =>
    apiFetch<{ loot_boxes: import('@/types').LootBox[] }>(`/campaigns/${campaignId}/lootboxes`),
  open: (lootBoxId: number) =>
    apiFetch(`/lootboxes/${lootBoxId}/open`, { method: 'POST' }),
}

export const shops = {
  list: (campaignId: string | number) =>
    apiFetch<{ shops: import('@/types').Shop[] }>(`/campaigns/${campaignId}/shops`),
  buy: (shopId: number, characterId: number, itemName: string) =>
    apiFetch(`/shops/${shopId}/buy`, {
      method: 'POST',
      body: JSON.stringify({ character_id: characterId, item_name: itemName }),
    }),
}

// ── Dev ───────────────────────────────────────────────────────────────────

export const dev = {
  getModelSettings: () => apiFetch('/dev/model'),
  updateModel: (model: string) =>
    apiFetch('/dev/model', { method: 'PUT', body: JSON.stringify({ model }) }),
  resetModel: () => apiFetch('/dev/model', { method: 'PUT', body: JSON.stringify({ reset: true }) }),
  createCharacter: () => apiFetch('/dev/character', { method: 'POST' }),
}
