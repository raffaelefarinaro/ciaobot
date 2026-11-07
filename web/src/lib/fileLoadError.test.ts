import { afterEach, describe, expect, it, vi } from 'vitest'
import { fileLoadErrorMessage, imageLoadErrorMessage } from './fileLoadError'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fileLoadErrorMessage', () => {
  it('maps statuses to messages', () => {
    expect(fileLoadErrorMessage(404)).toBe('File not found.')
    expect(fileLoadErrorMessage(403)).toBe('Forbidden — path is outside the workspace.')
    expect(fileLoadErrorMessage(415)).toBe('Unsupported file type.')
    expect(fileLoadErrorMessage(500)).toBe('Failed to load file (HTTP 500).')
  })

  it('shows the default 2 MB limit for 413', () => {
    expect(fileLoadErrorMessage(413)).toBe('File is too large to preview (>2 MB).')
  })

  it('shows the given limit for 413', () => {
    expect(fileLoadErrorMessage(413, '15 MB')).toBe('File is too large to preview (>15 MB).')
  })
})

describe('imageLoadErrorMessage', () => {
  it('reports the server status', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 404 })))
    expect(await imageLoadErrorMessage('/api/workspace-image?path=%2Fws%2Fpic.png')).toBe('File not found.')
  })

  it('reports the 15 MB image limit for 413', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 413 })))
    expect(await imageLoadErrorMessage('/api/workspace-image?path=%2Fws%2Fbig.png')).toBe(
      'File is too large to preview (>15 MB).',
    )
  })

  it('says the bytes did not decode when the re-fetch returns 200', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('bytes', { status: 200 })))
    expect(await imageLoadErrorMessage('/api/workspace-image?path=%2Fws%2Fbad.png')).toBe(
      'This image could not be displayed (unsupported or damaged file).',
    )
  })

  it('never throws when the request itself fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }))
    expect(await imageLoadErrorMessage('/api/workspace-image?path=%2Fws%2Fpic.png')).toBe(
      'Could not load this image.',
    )
  })
})
