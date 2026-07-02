const API_BASE = '/api'

function getToken() {
  return localStorage.getItem('token')
}

export async function apiFetch(path, options = {}) {
  const url = `${API_BASE}${path}`
  const headers = {
    'Content-Type': 'application/json',
    ...(options.headers || {}),
  }
  const token = getToken()
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }

  const res = await fetch(url, { ...options, headers, credentials: 'include' })
  const data = await res.json().catch(() => ({}))

  if (!res.ok) {
    const err = new Error(data.error || `HTTP ${res.status}`)
    err.status = res.status
    err.data = data
    throw err
  }
  return data
}

export async function apiBlob(path, options = {}) {
  const url = `${API_BASE}${path}`
  const headers = {
    ...(options.headers || {}),
  }
  const token = getToken()
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }

  const res = await fetch(url, { ...options, headers, credentials: 'include' })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    const err = new Error(data.error || `HTTP ${res.status}`)
    err.status = res.status
    err.data = data
    throw err
  }
  return res.blob()
}

export function getCharacters() {
  return apiFetch('/characters')
}

export function getCharacter(id) {
  return apiFetch(`/characters/${id}`)
}

export function createCharacter(payload) {
  return apiFetch('/characters', { method: 'POST', body: JSON.stringify(payload) })
}

export function updateCharacter(id, payload) {
  return apiFetch(`/characters/${id}`, { method: 'PUT', body: JSON.stringify(payload) })
}

export function deleteCharacter(id) {
  return apiFetch(`/characters/${id}`, { method: 'DELETE' })
}

export function getCampaigns() {
  return apiFetch('/campaigns')
}

export function getCampaign(id) {
  return apiFetch(`/campaigns/${id}`)
}

export function getCampaignDevAudit(id) {
  return apiFetch(`/campaigns/${id}/dev`)
}

export function createCampaign(payload) {
  return apiFetch('/campaigns', { method: 'POST', body: JSON.stringify(payload) })
}

export function fetchRandomCampaignBrief(payload = {}) {
  return apiFetch('/campaigns/random-brief', { method: 'POST', body: JSON.stringify(payload) })
}

export function quickCreateCampaign(payload = {}) {
  return apiFetch('/campaigns/quick-create', { method: 'POST', body: JSON.stringify(payload) })
}

export function createDevCharacter() {
  return apiFetch('/dev/character', { method: 'POST' })
}

export function listAutomationKeys() {
  return apiFetch('/automation-keys')
}

export function createAutomationKey(payload = {}) {
  return apiFetch('/automation-keys', { method: 'POST', body: JSON.stringify(payload) })
}

export function deleteAutomationKey(id) {
  return apiFetch(`/automation-keys/${id}`, { method: 'DELETE' })
}

export function listCombatSandboxMaps() {
  return apiFetch('/dev/combat-sandbox/maps')
}

export function createCombatSandbox(payload) {
  return apiFetch('/dev/combat-sandboxes', { method: 'POST', body: JSON.stringify(payload) })
}

export function getDevModelSettings() {
  return apiFetch('/dev/model')
}

export function updateDevModel(model) {
  return apiFetch('/dev/model', { method: 'PUT', body: JSON.stringify({ model }) })
}

export function resetDevModel() {
  return apiFetch('/dev/model', { method: 'PUT', body: JSON.stringify({ reset: true }) })
}

// -- Automation Workspace API --

export function getAutomationWorkspace() {
  return apiFetch('/automation')
}

export function getAutomationWorkspaceStreamUrl() {
  const token = getToken()
  return `${API_BASE}/automation/stream?token=${encodeURIComponent(token || '')}`
}

export function listAutomationScenarios() {
  return apiFetch('/automation/scenarios')
}

export function listAutomationScorecards() {
  return apiFetch('/automation/scorecards')
}

export function createAutomationScorecard(payload) {
  return apiFetch('/automation/scorecards', { method: 'POST', body: JSON.stringify(payload) })
}

export function updateAutomationScorecard(scorecardId, payload) {
  return apiFetch(`/automation/scorecards/${scorecardId}`, { method: 'PUT', body: JSON.stringify(payload) })
}

export function createAutomationScenario(payload) {
  return apiFetch('/automation/scenarios', { method: 'POST', body: JSON.stringify(payload) })
}

export function getAutomationScenario(scenarioId) {
  return apiFetch(`/automation/scenarios/${scenarioId}`)
}

export function updateAutomationScenario(scenarioId, payload) {
  return apiFetch(`/automation/scenarios/${scenarioId}`, { method: 'PUT', body: JSON.stringify(payload) })
}

export function deleteAutomationScenario(scenarioId) {
  return apiFetch(`/automation/scenarios/${scenarioId}`, { method: 'DELETE' })
}

export function cleanupAutomationScenario(scenarioId, payload = {}) {
  return apiFetch(`/automation/scenarios/${scenarioId}/cleanup`, { method: 'POST', body: JSON.stringify(payload) })
}

export function listAutomationSnapshots(scenarioId) {
  return apiFetch(`/automation/scenarios/${scenarioId}/snapshots`)
}

export function createAutomationSnapshot(scenarioId, payload = {}) {
  return apiFetch(`/automation/scenarios/${scenarioId}/snapshots`, { method: 'POST', body: JSON.stringify(payload) })
}

export function listAutomationRuns(scenarioId) {
  return apiFetch(`/automation/scenarios/${scenarioId}/runs`)
}

export function createAutomationRun(scenarioId, payload = {}) {
  return apiFetch(`/automation/scenarios/${scenarioId}/runs`, { method: 'POST', body: JSON.stringify(payload) })
}

export function getAutomationRun(runId) {
  return apiFetch(`/automation/runs/${runId}`)
}

export function stopAutomationRun(runId) {
  return apiFetch(`/automation/runs/${runId}/stop`, { method: 'POST', body: JSON.stringify({}) })
}

export function continueAutomationRun(runId, payload = {}) {
  return apiFetch(`/automation/runs/${runId}/continue`, { method: 'POST', body: JSON.stringify(payload) })
}

export function submitAutomationRunAudit(runId, cycleId, payload) {
  return apiFetch(`/automation/runs/${runId}/audit-cycles/${cycleId}/audit`, { method: 'POST', body: JSON.stringify(payload) })
}

export function getAutomationRunScorecard(runId) {
  return apiFetch(`/automation/runs/${runId}/scorecard`)
}

export function compareAutomationRuns(payload) {
  return apiFetch('/automation/compare', { method: 'POST', body: JSON.stringify(payload) })
}

export function getAutomationRunStreamUrl(runId) {
  const token = getToken()
  return `${API_BASE}/automation/runs/${runId}/stream?token=${encodeURIComponent(token || '')}`
}

// -- Campaign Dashboard API --

export function updateCampaign(id, payload) {
  return apiFetch(`/campaigns/${id}`, { method: 'PUT', body: JSON.stringify(payload) })
}

export function deleteCampaign(id) {
  return apiFetch(`/campaigns/${id}`, { method: 'DELETE' })
}

export function exportCampaign(id) {
  return apiBlob(`/campaigns/${id}/export`)
}

// Characters in a campaign
export function getCampaignCharacters(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/characters`)
}

export function addCampaignCharacter(campaignId, characterId) {
  return apiFetch(`/campaigns/${campaignId}/characters`, {
    method: 'POST',
    body: JSON.stringify({ character_id: characterId }),
  })
}

// Sessions
export function startSession(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/sessions`, { method: 'POST' })
}

export function getCampaignWorld(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/world`)
}

export function getCurrentEncounterMap(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/encounter-maps/current`)
}

export function getEncounterMapImage(encounterMapId) {
  return apiBlob(`/encounter-maps/${encounterMapId}/image`)
}

export function getEncounterMapLabeledImage(encounterMapId) {
  return apiBlob(`/encounter-maps/${encounterMapId}/labeled-image`)
}

export function moveEncounterMapToken(encounterMapId, col, row) {
  return apiFetch(`/encounter-maps/${encounterMapId}/placements/me`, {
    method: 'PATCH',
    body: JSON.stringify({ col, row }),
  })
}

export function rollPlayerInitiative(encounterMapId, actorType, actorId, initiative) {
  return apiFetch(`/encounter-maps/${encounterMapId}/encounter/roll-initiative`, {
    method: 'POST',
    body: JSON.stringify({ actor_type: actorType, actor_id: actorId, initiative }),
  })
}

export function advanceEncounterTurn(encounterMapId) {
  return apiFetch(`/encounter-maps/${encounterMapId}/encounter/next-turn`, {
    method: 'POST',
  })
}

export function generateCampaignWorld(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/world`, { method: 'POST' })
}

function queryString(params = {}) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, value)
  })
  const query = search.toString()
  return query ? `?${query}` : ''
}

export function getSession(sessionId, options = {}) {
  return apiFetch(`/sessions/${sessionId}${queryString({
    limit: options.limit,
    before_id: options.beforeId,
  })}`)
}

export function endSession(sessionId, recap) {
  return apiFetch(`/sessions/${sessionId}`, { method: 'PUT', body: JSON.stringify({ recap }) })
}

// Messages
export function getMessages(sessionId, options = {}) {
  return apiFetch(`/sessions/${sessionId}/messages${queryString({
    limit: options.limit,
    before_id: options.beforeId,
  })}`)
}

export function sendMessage(sessionId, content, role = 'player') {
  return apiFetch(`/sessions/${sessionId}/messages`, { method: 'POST', body: JSON.stringify({ content, role }) })
}

export function getSessionStreamUrl(sessionId) {
  const token = getToken()
  return token
    ? `${API_BASE}/sessions/${sessionId}/stream?token=${encodeURIComponent(token)}`
    : `${API_BASE}/sessions/${sessionId}/stream`
}

export function listLlmPlayers(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/llm-players`)
}

export function createLlmPlayer(campaignId, payload = {}) {
  return apiFetch(`/campaigns/${campaignId}/llm-players`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function assignLlmPlayer(campaignId, llmPlayerId) {
  return apiFetch(`/campaigns/${campaignId}/llm-players/assign`, {
    method: 'POST',
    body: JSON.stringify({ llm_player_id: llmPlayerId }),
  })
}

export function rotateLlmPlayerKey(campaignId, llmPlayerId) {
  return apiFetch(`/campaigns/${campaignId}/llm-players/${llmPlayerId}/rotate-key`, {
    method: 'POST',
  })
}

export function deleteLlmPlayer(campaignId, llmPlayerId) {
  return apiFetch(`/campaigns/${campaignId}/llm-players/${llmPlayerId}`, {
    method: 'DELETE',
  })
}

// Sheet Proposals
export function getSheetProposals(sessionId) {
  return apiFetch(`/sessions/${sessionId}/proposals`)
}

export function applySheetProposal(sessionId, proposalId) {
  return apiFetch(`/sessions/${sessionId}/proposals/${proposalId}/apply`, { method: 'POST' })
}

export function dismissSheetProposal(sessionId, proposalId) {
  return apiFetch(`/sessions/${sessionId}/proposals/${proposalId}/dismiss`, { method: 'POST' })
}

// Members & Invites
export function listMembers(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/members`)
}

export function createInvite(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/invites`, { method: 'POST' })
}

export function getInvite(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/invites`)
}

export function lookupInvite(code) {
  return apiFetch(`/invites/lookup?code=${encodeURIComponent(code)}`)
}

export function joinCampaign(campaignId, code) {
  return apiFetch(`/campaigns/${campaignId}/join`, { method: 'POST', body: JSON.stringify({ code }) })
}

export function removeMember(campaignId, userId) {
  return apiFetch(`/campaigns/${campaignId}/members/${userId}`, { method: 'DELETE' })
}

// Character planning
export function getCampaignPlanning(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/planning`)
}

export function sendPlanningMessage(campaignId, content, options = {}) {
  return apiFetch(`/campaigns/${campaignId}/planning/messages`, {
    method: 'POST',
    body: JSON.stringify({
      content,
      draft_character: options.draftCharacter,
      active_page: options.activePage,
    }),
  })
}

export function getPlanningStreamUrl(campaignId) {
  const token = getToken()
  return `${API_BASE}/campaigns/${campaignId}/planning/stream?token=${encodeURIComponent(token || '')}`
}

export function selectPlanningCharacter(campaignId, characterId) {
  return apiFetch(`/campaigns/${campaignId}/planning/character`, {
    method: 'PUT',
    body: JSON.stringify({ character_id: characterId }),
  })
}

export function setPlanningReady(campaignId, ready) {
  return apiFetch(`/campaigns/${campaignId}/planning/ready`, {
    method: 'PUT',
    body: JSON.stringify({ ready }),
  })
}

export function updatePlanningBond(campaignId, bondId, payload) {
  return apiFetch(`/campaigns/${campaignId}/planning/bonds/${bondId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

// Loot Boxes
export function getLootBoxes(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/lootboxes`)
}

export function openLootBox(lootBoxId) {
  return apiFetch(`/lootboxes/${lootBoxId}/open`, { method: 'POST' })
}

// Shops
export function getShops(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/shops`)
}

export function buyShopItem(shopId, characterId, itemName) {
  return apiFetch(`/shops/${shopId}/buy`, {
    method: 'POST',
    body: JSON.stringify({ character_id: characterId, item_name: itemName }),
  })
}
