/**
 * Whether the PWA is being served inside the macOS `Ciaobot.app` shell.
 *
 * The main webview loads the live localhost PWA as *remote* content. The app
 * injects `__CIAOBOT_DESKTOP__` with a document-start initialization script as
 * a one-way marker. A very small Tauri command surface is also exposed so the
 * PWA can ask macOS for native permissions (microphone, notifications,
 * camera in the future); these commands are gated by the `main` capability in
 * the Tauri shell and only allow the main window/localhost origin.
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

export type DesktopPermissionKind = 'microphone' | 'notifications' | 'camera'
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
