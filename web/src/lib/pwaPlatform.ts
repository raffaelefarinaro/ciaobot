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

/** Windows, by user agent. The install routes name a platform as well as a
 *  browser, and this is the one desktop platform no other probe asks about. */
export function isWindows(): boolean {
  return /windows/i.test(navigator.userAgent)
}

/** An iPad in Safari's default desktop browsing mode. It reports a Macintosh
 *  Safari user agent with nothing in the string to say iPad, so it is invisible
 *  to isIos() and indistinguishable to every probe above. The touch points give
 *  it away: no Mac has a touchscreen, so a Macintosh user agent plus touch is
 *  an iPad and nothing else. Narrow on purpose: the probes above are shared, and
 *  a caller asking "is this a Mac" is not asking "is this an iPad pretending". */
export function isIpadDesktopMode(): boolean {
  return isMacDesktop() && navigator.maxTouchPoints > 1
}

/** Microsoft Edge, whose user agent still also says Chrome. */
export function isEdge(): boolean {
  return /edg\//i.test(navigator.userAgent)
}

/** Google Chrome itself, and deliberately nothing else. A dozen browsers run on
 *  Chromium's engine and every one of them keeps "Chrome/" and "Safari/537.36"
 *  in its user agent, so the engine token alone cannot tell Chrome from Brave,
 *  Vivaldi, Edge, Opera or Samsung Internet, which put install somewhere else.
 *  Chrome puts no product token after Safari's, so that is the check: their own
 *  name is appended and the string stops matching. It can only fail closed —
 *  a browser that stops matching gets no highlight and every route stays on the
 *  page — which is why a strict test is the right side to err on. */
export function isGoogleChrome(): boolean {
  return /(?:^| )Chrome\/\d+(?:\.\d+)*(?: Mobile)? Safari\/537\.36$/.test(navigator.userAgent)
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
