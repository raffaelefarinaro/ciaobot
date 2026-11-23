/**
 * Is this browser talking to the engine on this very machine?
 *
 * One engine per install and one origin per browser: there is no second
 * loopback origin to navigate to any more, so the only thing left to ask is
 * whether the origin under the page is this computer's own. Used wherever the
 * PWA offers a device-only control: the engine-offline curtain, opening files
 * externally, the host terminal and app-install actions.
 */

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1'])

/** Loopback test for a hostname. IPv6 literals arrive bracketed. */
export function isLoopbackHostname(hostname: string): boolean {
  return LOOPBACK_HOSTS.has(hostname.toLowerCase().replace(/^\[/, '').replace(/\]$/, ''))
}

/** Loopback test for the origin serving this page. */
export function isLoopbackPage(): boolean {
  return isLoopbackHostname(window.location.hostname)
}
