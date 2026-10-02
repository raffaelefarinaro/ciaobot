/**
 * Platform probes shared by the Settings notifications card, the Home setup
 * card and the Settings install guidance, plus the manual install instructions
 * for browsers that never offer a programmatic prompt (Safari, Firefox, iOS).
 *
 * They were local functions in SettingsNotifications.vue; the Home card needs
 * the same answers, and one copy is the only way to keep the surfaces from
 * drifting apart.
 */

/** localStorage key for the Home setup card's Hide action. Per browser and
 *  origin by construction, which is what a device-local decision needs. */
export const SETUP_CARD_DISMISSED_KEY = 'ciao-setup-card-dismissed'

export function isIos(): boolean {
  return /iphone|ipad|ipod/i.test(navigator.userAgent)
}

export function isMacDesktop(): boolean {
  return /macintosh|mac os x/i.test(navigator.userAgent) && !isIos()
}

export function isAndroid(): boolean {
  return /android/i.test(navigator.userAgent)
}

/** Microsoft Edge, whose user agent still also says Chrome. */
export function isEdge(): boolean {
  return /edg\//i.test(navigator.userAgent)
}

/** Chromium on a computer: Chrome, or another browser on its engine. Phones are
 *  excluded, where the same engine puts the install entry in a phone menu. */
export function isChromiumDesktop(): boolean {
  const ua = navigator.userAgent
  if (isIos() || isAndroid()) return false
  return /chrome|crios|chromium/i.test(ua)
}

export function isStandalone(): boolean {
  return (
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  )
}

/** Safari (desktop or iOS), excluding Chromium-based browsers that also say
 *  "Safari" in their user agent. */
export function isSafari(): boolean {
  const ua = navigator.userAgent
  return /safari/i.test(ua) && !/chrome|chromium|crios|edg|opr|fxios/i.test(ua)
}

/** Manual install instructions for browsers without a programmatic prompt. */
export function installInstructions(): string {
  if (isIos()) return 'Tap Share, then "Add to Home Screen".'
  if (isMacDesktop() && isSafari()) return 'In Safari, choose File → "Add to Dock".'
  return 'Use your browser\'s Install option in the address bar or menu.'
}
