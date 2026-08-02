import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { router } from './router'
import App from './App.vue'
import { installViewportPlumbing } from './lib/viewport'

// Restore theme & font scale from localStorage as early as possible
try {
  const savedTheme = localStorage.getItem('ciao-theme') || 'system'
  if (savedTheme === 'light') {
    document.documentElement.classList.add('theme-light')
  } else if (savedTheme === 'system') {
    const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches
    if (!isDark) {
      document.documentElement.classList.add('theme-light')
    }
  }

  // Set up global media query listener for system theme changes
  const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
  const listener = (e: { matches: boolean }) => {
    try {
      const currentTheme = localStorage.getItem('ciao-theme') || 'system'
      if (currentTheme === 'system') {
        if (e.matches) {
          document.documentElement.classList.remove('theme-light')
        } else {
          document.documentElement.classList.add('theme-light')
        }
      }
    } catch { /* localStorage blocked: leave the class as it is */ }
  }
  if (mediaQuery.addEventListener) {
    mediaQuery.addEventListener('change', listener)
  } else {
    // Safari before 14 only has the deprecated MediaQueryList.addListener.
    type LegacyMediaQueryList = MediaQueryList & {
      addListener(cb: (event: { matches: boolean }) => void): void
    }
    (mediaQuery as LegacyMediaQueryList).addListener(listener)
  }

  const savedFontScale = localStorage.getItem('ciao-font-scale') || '1.2'
  document.documentElement.style.setProperty('--font-scale', savedFontScale)
} catch {
  // Ignore localStorage restrictions
}

// Set Excalidraw asset path to host fonts locally (loaded from /fonts)
const excalidrawGlobals = window as unknown as { EXCALIDRAW_ASSET_PATH: string }
excalidrawGlobals.EXCALIDRAW_ASSET_PATH = '/'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')

// Register service worker for PWA installability
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {})
}

// iOS viewport / keyboard plumbing (--app-h, .keyboard-open, scroll lock).
installViewportPlumbing()
