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

  it('preserves status and payload for validation-detail arrays', async () => {
    const payload = {
      detail: [
        { loc: ['body', 'name'], msg: 'Field required', type: 'missing' },
      ],
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(payload, 422)))

    const error = await api.apiFetch('/campaigns').catch((e) => e)
    expect(error).toMatchObject({ status: 422, data: payload })
    expect((error as Error).message).toBe('body.name: Field required')
  })

  it('handles numeric loc segments and message field', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      detail: [
        { loc: ['body', 'items', 0, 'name'], msg: 'Required', type: 'missing' },
        { loc: ['body', 'query'], message: 'Invalid query', type: 'value_error' },
      ],
    }, 422)))

    await expect(api.apiFetch('/test')).rejects.toThrow(
      'body.items.0.name: Required; body.query: Invalid query',
    )
  })

  it('trims whitespace-only detail and falls back to status', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: '   ' }, 400)))
    await expect(api.apiFetch('/test')).rejects.toThrow('HTTP 400')
  })

  it('falls back to HTTP status when detail array is empty', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: [] }, 422)))
    await expect(api.apiFetch('/test')).rejects.toThrow('HTTP 422')
  })

  it('prefers detail over error/message fields', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      detail: 'Detail wins',
      error: 'Error fallback',
      message: 'Message fallback',
    }, 400)))
    await expect(api.apiFetch('/test')).rejects.toThrow('Detail wins')
  })

  it('uses message field when detail is absent', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ message: 'From message field' }, 400)))
    await expect(api.apiFetch('/test')).rejects.toThrow('From message field')
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
  it('only exposes registered production groups', () => {
    expect(Object.keys(api).sort()).toEqual([
      'apiFetch',
      'auth',
      'campaignMembers',
      'campaigns',
      'characters',
      'gameplayThreads',
      'sessions',
    ])
    expect(Object.keys(api.auth).sort()).toEqual(['getConfig', 'me'])
    expect(Object.keys(api.campaigns).sort()).toEqual([
      'create',
      'delete',
      'get',
      'list',
      'quickCreate',
      'randomBrief',
      'transitionLifecycle',
      'update',
    ])
    expect(Object.keys(api.characters).sort()).toEqual([
      'chatStream',
      'create',
      'delete',
      'get',
      'list',
      'update',
    ])
    expect(Object.keys(api.campaignMembers).sort()).toEqual([
      'createInvite',
      'getInvite',
      'getLobby',
      'joinCampaign',
      'listCharacters',
      'listMembers',
      'lookupInvite',
      'selectCharacter',
      'setReadiness',
    ])
    expect(Object.keys(api.gameplayThreads).sort()).toEqual([
      'getOrCreateDirect',
      'getOrCreateDm',
      'list',
      'submit',
    ])
    expect(Object.keys(api.sessions)).toEqual(['start'])
  })

  it('does not expose removed legacy groups or blob transport', () => {
    const keys = Object.keys(api)
    expect(keys).not.toContain('planning')
    expect(keys).not.toContain('proposals')
    expect(keys).not.toContain('llmPlayers')
    expect(keys).not.toContain('automation')
    expect(keys).not.toContain('automationKeys')
    expect(keys).not.toContain('loot')
    expect(keys).not.toContain('shops')
    expect(keys).not.toContain('dev')
    expect(keys).not.toContain('apiBlob')
    expect(keys).not.toContain('getStreamUrl')
    expect(keys).not.toContain('legacyLiveTable')
    expect(keys).not.toContain('world')
    expect(keys).not.toContain('encounterMaps')
  })
})
