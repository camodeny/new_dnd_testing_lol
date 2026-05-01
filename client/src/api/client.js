const API_BASE = 'http://localhost:5889/api'

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

export function createCampaign(payload) {
  return apiFetch('/campaigns', { method: 'POST', body: JSON.stringify(payload) })
}

export function createDevCharacter() {
  return apiFetch('/dev/character', { method: 'POST' })
}
