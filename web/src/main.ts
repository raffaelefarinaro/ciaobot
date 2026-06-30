import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { router } from './router'
import App from './App.vue'

// Restore theme & font scale from localStorage as early as possible
try {
  const savedTheme = localStorage.getItem('ciao-theme') || 'dark'
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
      const currentTheme = localStorage.getItem('ciao-theme') || 'dark'
      if (currentTheme === 'system') {
        if (e.matches) {
          document.documentElement.classList.remove('theme-light')
        } else {
          document.documentElement.classList.add('theme-light')
        }
      }
    } catch (err) {}
  }
  if (mediaQuery.addEventListener) {
    mediaQuery.addEventListener('change', listener)
  } else {
    (mediaQuery as any).addListener(listener)
  }

  const savedFontScale = localStorage.getItem('ciao-font-scale') || '1.0'
  document.documentElement.style.setProperty('--font-scale', savedFontScale)
} catch (e) {
  // Ignore localStorage restrictions
}

// Set Excalidraw asset path to host fonts locally (loaded from /fonts)
;(window as any).EXCALIDRAW_ASSET_PATH = '/'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')

// Register service worker for PWA installability
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {})
}

// Drive a --app-h CSS variable off VisualViewport so the layout responds
// instantly when the iOS keyboard opens/closes. `100dvh` alone does not
// update on iOS Safari until the user interacts with the page.
// Also toggle a `.keyboard-open` class so the home-indicator safe-area can
// collapse while the keyboard is covering it.
let maxViewportHeight = window.innerHeight

function updateAppHeight() {
  const vv = window.visualViewport
  const h = vv?.height ?? window.innerHeight
  maxViewportHeight = Math.max(maxViewportHeight, h)
  document.documentElement.style.setProperty('--app-h', `${h}px`)
  // With `interactive-widget=resizes-content` the layout viewport shrinks
  // along with the visual viewport, so the old `innerHeight - h > 100`
  // heuristic always returns false. Detect keyboard open by comparing
  // against the tallest viewport height we've seen for this orientation.
  const keyboardOpen = h < maxViewportHeight * 0.85
  document.documentElement.classList.toggle('keyboard-open', keyboardOpen)
}
updateAppHeight()
// iOS PWA: visualViewport.height can be transiently wrong on first load
// before the standalone UI chrome settles. Re-measure after a short delay.
setTimeout(updateAppHeight, 100)
window.addEventListener('resize', updateAppHeight)
window.addEventListener('orientationchange', () => { maxViewportHeight = 0 })
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', updateAppHeight)
  // Intentionally not listening to `scroll`: iOS fires vv.scroll while the
  // page shifts to keep the caret visible during multi-line typing, and
  // re-reading vv.height there can latch a stale/smaller value, collapsing
  // the messages area and leaving a dead zone between the input and the
  // keyboard.
}

// iOS Safari can still shift the document when the keyboard opens, leaving
// the input bar floating with a gap below it. Lock the page scroll to 0.
window.addEventListener('scroll', () => {
  if (window.scrollY !== 0) window.scrollTo(0, 0)
}, { passive: true })

// Block pinch-to-zoom on iOS Safari. The viewport meta's `user-scalable=no`
// is honored in standalone PWA mode, but plain Safari historically ignores it
// and still pinch-zooms the page. Listening to the (non-standard, WebKit-only)
// `gesturestart` event catches the two-finger zoom gesture before it starts.
// Double-tap zoom is killed via `touch-action: manipulation` on body.
document.addEventListener('gesturestart', (e) => e.preventDefault())
document.addEventListener('gesturechange', (e) => e.preventDefault())
document.addEventListener('gestureend', (e) => e.preventDefault())
