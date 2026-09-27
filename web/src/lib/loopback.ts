/**
 * Is this browser talking to the engine on this very machine?
 *
 * One engine per install and one origin per browser: there is no second
 * loopback origin to navigate to any more, so the only thing left to ask is
 * whether the origin under the page is this computer's own. Two consumers:
 * the engine-offline curtain, which can then name a terminal command instead
 * of a host, and the GWS one-click re-login, whose consent redirect binds to
 * the engine's own loopback and never arrives from anywhere else.
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
