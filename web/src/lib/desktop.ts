/**
 * Whether the browser runs on macOS or iOS, where the Alt key is labelled
 * Option and shown as the ⌥ glyph. Windows and Linux keyboards say "Alt".
 *
 * This is about the user's keyboard, not about which app is running: the PWA
 * has one runtime, and its modifier chords label themselves per platform.
 */
export function isApplePlatform(): boolean {
  if (typeof navigator === 'undefined') return false
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } }
  const platform = nav.userAgentData?.platform || nav.platform || ''
  if (platform) return /mac|iphone|ipad|ipod/i.test(platform)
  return /macintosh|mac os x|iphone|ipad|ipod/i.test(nav.userAgent || '')
}
