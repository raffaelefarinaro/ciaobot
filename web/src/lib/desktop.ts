/**
 * Whether the PWA is being served inside the macOS `Ciaobot.app` shell.
 *
 * The main webview loads the live localhost PWA as *remote* content. The app
 * injects `__CIAOBOT_DESKTOP__` with a document-start initialization script as
 * a one-way marker. A very small Tauri command surface is also exposed so the
 * PWA can ask macOS for native permissions (notifications, camera in the
 * future); the Tauri capability is limited to bundled local
 * pages and does not grant commands to remote PWA content.
 */
declare global {
  interface Window {
    __CIAOBOT_DESKTOP__?: boolean
    __TAURI__?: {
      core: {
        invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>
      }
    }
  }
}

export const isDesktopApp = (): boolean =>
  typeof window !== 'undefined' && window.__CIAOBOT_DESKTOP__ === true

/**
 * Whether the browser runs on macOS or iOS, where the Alt key is labelled
 * Option and shown as the \u2325 glyph. Windows and Linux keyboards say "Alt".
 */
export function isApplePlatform(): boolean {
  if (typeof navigator === 'undefined') return false
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } }
  const platform = nav.userAgentData?.platform || nav.platform || ''
  if (platform) return /mac|iphone|ipad|ipod/i.test(platform)
  return /macintosh|mac os x|iphone|ipad|ipod/i.test(nav.userAgent || '')
}

export type DesktopPermissionKind = 'notifications' | 'camera'
export type DesktopPermissionState =
  | 'not_determined'
  | 'restricted'
  | 'denied'
  | 'authorized'

function canInvokeTauri(): boolean {
  return isDesktopApp() && typeof window.__TAURI__ !== 'undefined'
}

export async function queryDesktopPermission(
  kind: DesktopPermissionKind,
): Promise<DesktopPermissionState | null> {
  if (!canInvokeTauri()) return null
  try {
    return await window.__TAURI__!.core.invoke<DesktopPermissionState>(
      'check_permission',
      { kind },
    )
  } catch (e) {
    console.error('Could not query desktop permission:', e)
    return null
  }
}

export async function requestDesktopPermission(
  kind: DesktopPermissionKind,
): Promise<DesktopPermissionState | null> {
  if (!canInvokeTauri()) return null
  try {
    return await window.__TAURI__!.core.invoke<DesktopPermissionState>(
      'request_permission',
      { kind },
    )
  } catch (e) {
    console.error('Could not request desktop permission:', e)
    return null
  }
}
