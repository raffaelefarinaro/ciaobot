import { describe, expect, it, vi } from 'vitest'
import { clearNativeDropCapture, dragHasFiles, dropHost, dropZoneAt, parseNativeDropGrant, requestNativeDropGrant } from './nativeDrop'

const GRANT = '0f8fad5b-d9cb-469f-a165-70867728950e'

function nativeWindow(postMessage: (body: unknown) => Promise<unknown>, hostname = '127.0.0.1') {
  return { location: { hostname }, webkit: { messageHandlers: { ciaobotNativeDrop: { postMessage } } } }
}

describe('dropHost', () => {
  it('offers use-in-place only where the app registered its bridge', () => {
    expect(dropHost(nativeWindow(async () => null))).toBe('native')
    expect(dropHost({ location: { hostname: '127.0.0.1' } })).toBe('loopback')
    expect(dropHost({ location: { hostname: 'localhost' } })).toBe('loopback')
    expect(dropHost({ location: { hostname: '[::1]' } })).toBe('loopback')
    expect(dropHost({ location: { hostname: 'mac.tailnet.ts.net' } })).toBe('remote')
    // The app's other bridges do not make a page native.
    expect(dropHost({ location: { hostname: 'h.example' }, webkit: { messageHandlers: {} } })).toBe('remote')
  })
})

describe('parseNativeDropGrant', () => {
  it('reads a grant and keeps only string names', () => {
    expect(parseNativeDropGrant({ grantId: GRANT, names: ['a.txt', 3, 'b/'] })).toEqual({ grantId: GRANT, names: ['a.txt', 'b/'] })
    expect(parseNativeDropGrant(null)).toBeNull()
  })

  it('refuses anything that is not a canonical grant id', () => {
    expect(() => parseNativeDropGrant({ grantId: '../../etc/passwd' })).toThrow(/invalid file grant/)
    expect(() => parseNativeDropGrant({ grantId: GRANT.toUpperCase() })).toThrow()
    expect(() => parseNativeDropGrant({ paths: ['/etc/passwd'] })).toThrow()
  })
})

describe('requestNativeDropGrant', () => {
  it('sends exactly the op and parses the reply', async () => {
    const postMessage = vi.fn(async () => ({ grantId: GRANT, names: ['a.txt'] }))
    await expect(requestNativeDropGrant('pick', nativeWindow(postMessage))).resolves.toEqual({ grantId: GRANT, names: ['a.txt'] })
    expect(postMessage).toHaveBeenCalledWith({ op: 'pick' })
  })

  it('turns a refusal or a missing bridge into an error', async () => {
    await expect(requestNativeDropGrant('grant', nativeWindow(async () => { throw new Error('refused') })))
      .rejects.toThrow('The app would not hand over the files.')
    await expect(requestNativeDropGrant('grant', { location: { hostname: '127.0.0.1' } }))
      .rejects.toThrow('Only the Ciaobot app')
  })
})

describe('dragHasFiles and clearNativeDropCapture', () => {
  it('shows zones only for a drag that carries files', () => {
    expect(dragHasFiles(['Files', 'text/uri-list'])).toBe(true)
    expect(dragHasFiles(['text/plain'])).toBe(false)
    expect(dragHasFiles(undefined)).toBe(false)
  })

  it('asks the app to forget the drop, and is silent without the bridge', async () => {
    const postMessage = vi.fn(async () => { throw new Error('refused') })
    clearNativeDropCapture(nativeWindow(postMessage))
    expect(postMessage).toHaveBeenCalledWith({ op: 'clear' })
    expect(() => clearNativeDropCapture({ location: { hostname: '127.0.0.1' } })).not.toThrow()
  })
})

describe('dropZoneAt', () => {
  const zones = [
    { zone: 'copy' as const, rect: { left: 0, right: 100, top: 0, bottom: 50 } },
    { zone: 'in-place' as const, rect: { left: 110, right: 210, top: 0, bottom: 50 } },
  ]
  it('names the zone under the point, or none', () => {
    expect(dropZoneAt(10, 10, zones)).toBe('copy')
    expect(dropZoneAt(150, 49, zones)).toBe('in-place')
    expect(dropZoneAt(105, 10, zones)).toBeNull()
    expect(dropZoneAt(10, 10, [{ zone: 'copy', rect: null }])).toBeNull()
  })
})
