import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('apiFetch error contract', () => {
  it('uses FastAPI string detail and preserves response metadata', async () => {
    const payload = { detail: 'Campaign not found' }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(payload, 404)))

    const error = await api.apiFetch('/campaigns/missing').catch((caught) => caught)

    expect(error).toMatchObject({
      message: 'Campaign not found',
      status: 404,
      data: payload,
    })
  })

  it('normalizes FastAPI validation detail arrays into a readable message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      detail: [
        { loc: ['body', 'name'], msg: 'Field required', type: 'missing' },
        { loc: ['body', 'required_players'], msg: 'Must be at least 1', type: 'value_error' },
      ],
    }, 422)))

    await expect(api.apiFetch('/campaigns', { method: 'POST' })).rejects.toThrow(
      'body.name: Field required; body.required_players: Must be at least 1',
    )
  })

  it('retains legacy error payload compatibility', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ error: 'Legacy failure' }, 400)))

    await expect(api.apiFetch('/test')).rejects.toThrow('Legacy failure')
  })

  it('falls back to the HTTP status when the response has no readable JSON error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('not json', { status: 503 })))

    await expect(api.apiFetch('/test')).rejects.toThrow('HTTP 503')
  })
})

describe('exported API surface', () => {
  it('only exposes registered production groups plus isolated live-table compatibility calls', () => {
    expect(Object.keys(api).sort()).toEqual([
      'apiFetch',
      'auth',
      'campaignMembers',
      'campaigns',
      'characters',
      'encounterMaps',
      'legacyLiveTable',
      'sessions',
      'world',
    ])
    expect(Object.keys(api.auth).sort()).toEqual(['getConfig', 'me'])
    expect(Object.keys(api.sessions)).toEqual(['start'])
    expect(Object.keys(api.legacyLiveTable).sort()).toEqual([
      'get',
      'getMessages',
      'sendMessage',
      'streamUrl',
    ])
  })
})
