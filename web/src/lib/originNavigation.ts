const contentHost = 'localhost'
const deviceHost = '127.0.0.1'
const loopbackHosts = new Set(['localhost', '127.0.0.1', '::1'])

function normalizedHost(hostname: string): string {
  return hostname.toLowerCase().replace(/^\[/, '').replace(/\]$/, '')
}

export function isLoopbackPage(): boolean {
  return loopbackHosts.has(normalizedHost(window.location.hostname))
}

function effectivePort(url: URL): number | null {
  if (url.port) return Number(url.port)
  if (url.protocol === 'https:') return 443
  if (url.protocol === 'http:') return 80
  return null
}

function loopbackHref(host: string, path: string): string {
  const current = new URL(window.location.href)
  if (!loopbackHosts.has(normalizedHost(current.hostname))) {
    throw new Error('local navigation is only available from a loopback page')
  }

  const target = new URL(path, current.origin)
  if (
    (target.protocol !== 'http:' && target.protocol !== 'https:')
    || target.username
    || target.password
  ) {
    throw new Error('invalid local navigation target')
  }

  // A bridge response is an absolute URL on the other loopback hostname. Allow
  // that exact peer, but never let a caller smuggle in an arbitrary origin or
  // port through the path argument.
  const sameOrigin = target.origin === current.origin
  const sameLoopbackPeer =
    loopbackHosts.has(normalizedHost(target.hostname))
    && target.protocol === current.protocol
    && effectivePort(target) === effectivePort(current)
  if (!sameOrigin && !sameLoopbackPeer) {
    throw new Error('invalid local navigation target')
  }

  target.hostname = host
  target.port = current.port
  return target.toString()
}

function sameOriginHref(path: string): string {
  const current = new URL(window.location.href)
  const target = new URL(path, current.origin)
  if (
    (target.protocol !== 'http:' && target.protocol !== 'https:')
    || target.origin !== current.origin
    || target.username
    || target.password
  ) {
    throw new Error('same-origin navigation target is invalid')
  }
  return target.toString()
}

export function contentHref(path = '/'): string {
  // Host mode is also reachable through a LAN address.  Its ordinary login and
  // logout redirects must stay on that address; only a loopback client needs
  // the localhost content/127.0.0.1 control split.
  if (!loopbackHosts.has(normalizedHost(window.location.hostname))) {
    return sameOriginHref(path)
  }
  return loopbackHref(contentHost, path)
}

export function deviceHref(path = '/device'): string {
  return loopbackHref(deviceHost, path)
}

export function navigateToContent(path = '/'): void {
  window.location.assign(contentHref(path))
}

export function replaceToContent(path = '/'): void {
  window.location.replace(contentHref(path))
}

export function navigateToDevice(path = '/device'): void {
  window.location.assign(deviceHref(path))
}
