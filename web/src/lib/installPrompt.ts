import { ref } from 'vue'

interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

export const canPromptInstall = ref(false)
export const installed = ref(false)
/** Chromium tells a same-origin tab the app is installed on this device, via
 *  the manifest's self-referencing related_applications entry. Safari and
 *  Firefox have no such API, so false means unknown there, never "not installed". */
export const installedOnDevice = ref(false)
let deferred: BeforeInstallPromptEvent | null = null
let listening = false

/** Call once at startup: the browser fires beforeinstallprompt early and only once. */
export function listenForInstallPrompt(target: Window = window): void {
  if (listening) return
  listening = true
  target.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault()
    deferred = event as BeforeInstallPromptEvent
    canPromptInstall.value = true
  })
  target.addEventListener('appinstalled', () => {
    installed.value = true
    canPromptInstall.value = false
    deferred = null
  })
}

type RelatedApp = { platform: string }
type RelatedAppsNavigator = Navigator & { getInstalledRelatedApps?: () => Promise<RelatedApp[]> }

let installedProbe: Promise<void> = Promise.resolve()

/** Call once at startup; a no-op where the browser has no getInstalledRelatedApps. */
export function probeInstalledApp(nav: Navigator = navigator): Promise<void> {
  installedProbe = runInstalledProbe(nav)
  return installedProbe
}

/** Settles once the startup probe has, so a view can decide on the answer
 *  instead of rendering a guess and changing its mind a frame later. */
export function installedAppProbed(): Promise<void> {
  return installedProbe
}

async function runInstalledProbe(nav: Navigator): Promise<void> {
  const related = nav as RelatedAppsNavigator
  if (typeof related.getInstalledRelatedApps !== 'function') return
  try {
    const apps = await related.getInstalledRelatedApps()
    installedOnDevice.value = apps.some(app => app.platform === 'webapp')
  } catch { /* blocked or unsupported here: stay unknown */ }
}

/** Show the native install prompt; resolves true when the user accepted. */
export async function promptInstall(): Promise<boolean> {
  const event = deferred
  if (!event) return false
  deferred = null
  canPromptInstall.value = false
  await event.prompt()
  const choice = await event.userChoice
  if (choice.outcome === 'accepted') installed.value = true
  return choice.outcome === 'accepted'
}

/** Test-only reset. */
export function _resetInstallPromptForTests(): void {
  deferred = null
  listening = false
  canPromptInstall.value = false
  installed.value = false
  installedOnDevice.value = false
  installedProbe = Promise.resolve()
}
