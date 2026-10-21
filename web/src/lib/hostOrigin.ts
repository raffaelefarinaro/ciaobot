const LOOPBACK_NAMES = new Set(['localhost', '127.0.0.1', '[::1]', '::1'])

/** True when this page was opened at a loopback name, i.e. on the computer running Ciaobot. UI hint only; the server decides. */
export function isHostOrigin(hostname: string = window.location.hostname): boolean {
  return LOOPBACK_NAMES.has(hostname.toLowerCase())
}
