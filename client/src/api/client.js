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

  const res = await fetch(url, { ...options, headers })
  const data = await res.json().catch(() => ({}))

  if (!res.ok) {
    const err = new Error(data.error || `HTTP ${res.status}`)
    err.status = res.status
    err.data = data
    throw err
  }
  return data
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

export function createCampaign(payload) {
  return apiFetch('/campaigns', { method: 'POST', body: JSON.stringify(payload) })
}

export function createDevCharacter() {
  return apiFetch('/dev/character', { method: 'POST' })
}

// -- Campaign Dashboard API --

export function updateCampaign(id, payload) {
  return apiFetch(`/campaigns/${id}`, { method: 'PUT', body: JSON.stringify(payload) })
}

export function deleteCampaign(id) {
  return apiFetch(`/campaigns/${id}`, { method: 'DELETE' })
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

export function listSessions(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/sessions`)
}

export function getSession(sessionId) {
  return apiFetch(`/sessions/${sessionId}`)
}

export function endSession(sessionId, recap) {
  return apiFetch(`/sessions/${sessionId}`, { method: 'PUT', body: JSON.stringify({ recap }) })
}

// Messages
export function getMessages(sessionId) {
  return apiFetch(`/sessions/${sessionId}/messages`)
}

export function sendMessage(sessionId, content, role = 'player') {
  return apiFetch(`/sessions/${sessionId}/messages`, { method: 'POST', body: JSON.stringify({ content, role }) })
}

// Members & Invites
export function listMembers(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/members`)
}

export function createInvite(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/invites`, { method: 'POST' })
}

export function joinCampaign(campaignId, code) {
  return apiFetch(`/campaigns/${campaignId}/join`, { method: 'POST', body: JSON.stringify({ code }) })
}

export function updateMemberRole(campaignId, userId, role) {
  return apiFetch(`/campaigns/${campaignId}/members/${userId}`, { method: 'PUT', body: JSON.stringify({ role }) })
}

export function removeMember(campaignId, userId) {
  return apiFetch(`/campaigns/${campaignId}/members/${userId}`, { method: 'DELETE' })
}

// Notes
export function listNotes(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/notes`)
}

export function createNote(campaignId, payload) {
  return apiFetch(`/campaigns/${campaignId}/notes`, { method: 'POST', body: JSON.stringify(payload) })
}

export function updateNote(noteId, payload) {
  return apiFetch(`/notes/${noteId}`, { method: 'PUT', body: JSON.stringify(payload) })
}

export function deleteNote(noteId) {
  return apiFetch(`/notes/${noteId}`, { method: 'DELETE' })
}

// NPCs
export function listNPCs(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/npcs`)
}

export function createNPC(campaignId, payload) {
  return apiFetch(`/campaigns/${campaignId}/npcs`, { method: 'POST', body: JSON.stringify(payload) })
}

export function updateNPC(npcId, payload) {
  return apiFetch(`/npcs/${npcId}`, { method: 'PUT', body: JSON.stringify(payload) })
}

export function deleteNPC(npcId) {
  return apiFetch(`/npcs/${npcId}`, { method: 'DELETE' })
}



