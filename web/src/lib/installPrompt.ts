import { ref } from 'vue'

interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

export const canPromptInstall = ref(false)
export const installed = ref(false)
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
}
