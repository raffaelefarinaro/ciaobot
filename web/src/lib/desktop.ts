/**
 * Whether the PWA is being served inside the macOS `Ciaobot.app` shell.
 *
 * The main webview loads the live localhost PWA as *remote* content and is
 * deliberately kept out of every Tauri capability, so `window.__TAURI__` is not
 * available to detect the shell. The app instead injects this flag with a
 * document-start initialization script. It is a one-way marker and grants the
 * page nothing — do not grow it into an IPC channel.
 */
declare global {
  interface Window {
    __CIAOBOT_DESKTOP__?: boolean
  }
}

export const isDesktopApp = (): boolean =>
  typeof window !== 'undefined' && window.__CIAOBOT_DESKTOP__ === true
