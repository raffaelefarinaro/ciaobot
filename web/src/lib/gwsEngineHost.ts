/**
 * Eligibility for the GWS one-click loopback re-login (issue #145), pulled
 * out of SettingsView's `gwsOnEngineHost` so the guard is testable on its
 * own — see that function for how it is wired up to `window.location` and
 * the node-status refs.
 *
 * The loopback re-login listener binds to the *engine's* 127.0.0.1, so the
 * consent redirect only reaches it when the browser is on the engine host
 * (localhost) and the integration API is not proxied to a remote host. From a
 * phone, LAN browser, or client-mode node the popup's redirect would target
 * the client's own loopback and never arrive — those users must use the
 * manual paste flow instead.
 */

/** What SettingsView currently knows about `/api/node/status`. */
export interface GwsNodeStatusKnowledge {
  loaded: boolean
  error: boolean
  isClient: boolean
  stateValid: boolean
}

/**
 * issue #351: node status unknown — not yet loaded, or the fetch failed —
 * reads as host-mode (`isClient: false`) by default. That default is
 * harmless on a loopback hostname, where the checks below still apply, but
 * off a loopback hostname it must not be read as "confirmed not a client":
 * a phone/LAN/client-mode browser would otherwise pass the guard on a
 * transient fetch failure, bind the listener on the wrong loopback, and time
 * out five minutes later. Fail closed instead.
 */
export function isGwsEngineHostEligible(
  hostname: string,
  _inDesktopApp: boolean,
  status: GwsNodeStatusKnowledge,
): boolean {
  const isLoopbackHost = hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]'
  if (!isLoopbackHost || !status.loaded || status.error || !status.stateValid || status.isClient) return false
  return true
}
