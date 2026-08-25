// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from './api'

const fetchMock = vi.hoisted(() => vi.fn())
vi.stubGlobal('fetch', fetchMock)

afterEach(() => {
  fetchMock.mockReset()
})

function jsonResponse(body: unknown): Response {
  return {
    status: 200,
    ok: true,
    headers: { get: () => 'application/json' },
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

describe('same-origin request containment', () => {
  it('sends relative paths from the page origin with query strings intact', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))
    await api.get('/api/chats?active_only=1')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/chats?active_only=1',
      expect.objectContaining({ credentials: 'same-origin' }),
    )
  })

  it('keeps encoded path segments byte-for-byte', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}))
    await api.get('/api/workspaces/my%20ws/files/a%2Fb')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/workspaces/my%20ws/files/a%2Fb')
  })

  it('still resolves a same-origin absolute URL', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}))
    await api.get('http://localhost:3000/api/status')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/status')
  })

  it.each([
    'https://evil.example/api/chats',
    '//evil.example/api/chats',
    'javascript:alert(1)',
    'data:text/plain,hi',
  ])('refuses %s before any request is made', async (path) => {
    await expect(api.get(path)).rejects.toThrow(`Blocked non-same-origin API path: ${path}`)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
