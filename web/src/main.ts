import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { router } from './router'
import App from './App.vue'
import { installViewportPlumbing } from './lib/viewport'
import { listenForInstallPrompt, probeInstalledApp } from './lib/installPrompt'
import { restoreAppearanceFromCache } from './lib/appearance'
import { DEFAULT_FONT_SCALE, FONT_SCALE_STORAGE_KEY } from './composables/useFontScale'

// Restore theme, font scale and appearance paint caches from localStorage as early as possible
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

  // Applied before Vue mounts so the first paint is already at the user's
  // size. useFontScale owns the key and the default; this only restores.
  const savedFontScale = localStorage.getItem(FONT_SCALE_STORAGE_KEY)
    || String(DEFAULT_FONT_SCALE)
  document.documentElement.style.setProperty('--font-scale', savedFontScale)

  // Last engine appearance, as a paint cache: the engine value overwrites it
  // as soon as it loads. Runs after the theme class so any mode-dependent
  // value picks the right side.
  restoreAppearanceFromCache(document.documentElement)
} catch {
  // Ignore localStorage restrictions
}

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')

// Register service worker for PWA installability
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {})
}

// Chromium fires beforeinstallprompt early and only once, so the listener has
// to exist before any card is mounted.
listenForInstallPrompt()
void probeInstalledApp()

// iOS viewport / keyboard plumbing (--app-h, .keyboard-open, scroll lock).
installViewportPlumbing()
