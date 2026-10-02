<template>
  <div class="card">
    <div class="settings-card-header">
      <h2 class="section-title">Use Ciaobot as an app</h2>
      <p class="hint">
        Ciaobot is a web app: this browser tab already reaches the engine on your host, and
        installing is optional. It adds an icon and a window of its own at this same address,
        and changes nothing else about how Ciaobot behaves. The engine on your host does the
        work and has to stay running — the app is the front end, not a copy of your chats.
        <a class="set-link settings-help-link install-link" :href="REMOTE_GUIDE_URL" target="_blank" rel="noopener noreferrer">
          Reach Ciaobot over HTTPS from another device
        </a>
      </p>
    </div>
    <div class="install-rows">
      <div class="install-row">
        <span class="install-key">This window</span>
        <span class="install-value">
          <span class="install-state">{{ standalone ? 'Running as the installed app' : 'A browser tab' }}</span>
          <span class="install-detail">{{ standaloneDetail }}</span>
        </span>
      </div>
      <div class="install-row">
        <span class="install-key">This address</span>
        <span class="install-value">
          <span class="install-state">{{ originState }}</span>
          <span class="install-detail">{{ originDetail }}</span>
        </span>
      </div>
    </div>
    <details class="install-platforms">
      <summary class="install-summary">Steps for every platform</summary>
      <ul class="install-routes">
        <li
          v-for="route in INSTALL_ROUTES"
          :key="route.id"
          class="install-route"
          :data-current="route.id === currentRoute"
        >
          <h3 class="install-route-name">
            {{ route.name }}
            <span v-if="route.id === currentRoute" class="set-tag">Your browser</span>
          </h3>
          <p class="install-route-steps">{{ route.steps }}</p>
          <a class="set-link set-link--quiet settings-help-link install-link" :href="route.helpUrl" target="_blank" rel="noopener noreferrer">
            {{ route.helpLabel }}
          </a>
        </li>
      </ul>
    </details>
  </div>
</template>

<script setup lang="ts">
import { isLoopbackPage } from '../../lib/loopback'
import {
  isAndroid,
  isChromiumDesktop,
  isEdge,
  isIos,
  isMacDesktop,
  isSafari,
  isStandalone,
} from '../../lib/pwaPlatform'

/** The one page that shows how to put HTTPS in front of a host, which is what
 *  the plain-HTTP case below needs. */
const REMOTE_GUIDE_URL = 'https://www.raffaelefarinaro.com/ciaobot/remote.html'

interface InstallRoute {
  id: string
  name: string
  steps: string
  helpUrl: string
  helpLabel: string
}

/** One row per route a user can install by, with the vendor's own illustrated
 *  page linked from it. Every row ships on every device: a phone is the place
 *  the Home Screen app matters, and the reader is often setting up the other
 *  one from here. */
const INSTALL_ROUTES: InstallRoute[] = [
  {
    id: 'safari-mac',
    name: 'Safari on a Mac (macOS Sonoma 14 or newer)',
    steps: 'Choose Share, or File, then "Add to Dock".',
    helpUrl: 'https://support.apple.com/en-us/104996',
    helpLabel: 'Apple: add a website to the Dock',
  },
  {
    id: 'chrome-desktop',
    name: 'Chrome on Mac or Windows',
    steps: 'Use Install in the address bar, or the menu: Cast, save, and share, then "Install page as app".',
    helpUrl: 'https://support.google.com/chrome/answer/9658361?hl=en&co=GENIE.Platform%3DDesktop',
    helpLabel: 'Google: install a website as an app in Chrome',
  },
  {
    id: 'edge-windows',
    name: 'Edge on Windows',
    steps: 'Use the install icon in the address bar, or the menu: Apps, then "Install this site as an app". The menu wording moves between builds.',
    helpUrl: 'https://support.microsoft.com/en-us/edge/install-manage-or-uninstall-apps-in-microsoft-edge',
    helpLabel: 'Microsoft: install, manage or uninstall apps in Edge',
  },
  {
    id: 'safari-ios',
    name: 'Safari on iPhone or iPad',
    steps: 'Tap Share, then "Add to Home Screen". If the sheet offers "Open as Web App", turn it on, then Add. iPhone and iPad only send notifications from the installed app, and only after you allow them.',
    helpUrl: 'https://support.apple.com/guide/iphone/iphea86e5236/ios',
    helpLabel: 'Apple: add a website to the Home Screen on iPhone',
  },
  {
    id: 'chrome-android',
    name: 'Chrome on Android',
    steps: 'Open the browser menu and choose Install or "Add to Home screen", then follow the prompt.',
    helpUrl: 'https://support.google.com/chrome/answer/9658361?hl=en&co=GENIE.Platform%3DAndroid',
    helpLabel: 'Google: install a website as an app in Chrome for Android',
  },
]

/** Which of the five routes this browser is on, or null when it is not one of
 *  them (Linux, ChromeOS, an unknown browser). A highlight, never a filter:
 *  the other four rows stay exactly as they are. */
function currentInstallRoute(): string | null {
  if (isIos()) return 'safari-ios'
  if (isAndroid()) return 'chrome-android'
  if (isEdge()) return 'edge-windows'
  if (isChromiumDesktop()) return 'chrome-desktop'
  if (isMacDesktop() && isSafari()) return 'safari-mac'
  return null
}

const currentRoute = currentInstallRoute()

/** About this window only. A browser tab cannot see whether the app is
 *  installed on the device, and must never claim that it is. */
const standalone = isStandalone()
const standaloneDetail = standalone
  ? 'Only this window can tell. Another tab, or another device, may still be the browser.'
  : 'Everything already works here. Installing is what adds the icon and the separate window.'

/** Browsers only install the app, and only allow push, on a secure origin:
 *  HTTPS, or the host's own localhost. Plain HTTP on a LAN address is neither,
 *  so the warning names that instead of offering steps that cannot work. */
const secureOrigin = window.isSecureContext === true
const loopback = isLoopbackPage()
const originState = secureOrigin
  ? loopback ? 'Secure: the host’s own localhost' : 'Secure: HTTPS'
  : 'Plain HTTP on the network'
const originDetail = secureOrigin
  ? 'This browser can install the app from this address.'
  : 'A browser only installs over HTTPS or on the host’s own localhost, so installing is not offered here and notifications cannot arrive.'
</script>

<!-- The card scaffolding (`.card`, `.section-title`, `.settings-card-header`,
     `.set-link`, `.set-tag`) lives in the sheet SettingsView also loads, which
     is what keeps this panel looking like the rest of Settings. -->
<style scoped src="./settingsPanels.css"></style>

<style scoped>
/* Two permanent facts about the reader's own window and address, in the same
   key/value row shape the notifications card uses for its status. */
.install-rows { border-top: 1px solid var(--border); }
.install-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-4);
  min-height: var(--touch);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}
.install-key {
  width: 116px;
  flex: none;
  color: var(--fg3);
  line-height: 1.5;
}
.install-value {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.install-state {
  color: var(--fg);
  font-weight: 600;
  line-height: 1.5;
}
.install-detail {
  color: var(--fg3);
  line-height: 1.5;
  max-width: 72ch;
}

/* The per-platform steps are the long tail of the card, so they open on demand.
   A native disclosure, drawn like the provider card's: a summary is a real
   button for keyboard and touch, with the chevron the other settings
   disclosures use. */
.install-platforms { margin-top: var(--space-3); }
.install-summary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--touch);
  color: var(--fg2);
  font-size: var(--text-sm);
  cursor: pointer;
  list-style: none;
}
.install-summary::-webkit-details-marker { display: none; }
.install-summary::before {
  content: '';
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  margin: 0 2px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  transform: rotate(-45deg);
  transition: transform 120ms var(--ease);
}
.install-platforms[open] > .install-summary::before { transform: rotate(45deg); }
@media (prefers-reduced-motion: reduce) {
  .install-summary::before { transition: none; }
}
/* `summary` is not in the global focus rule's :where() list, so the ring this
   control needs is stated here rather than assumed. */
.install-summary:hover { color: var(--fg); }
.install-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
.install-routes {
  list-style: none;
  margin: 0;
  padding: 0;
  border-top: 1px solid var(--border);
}
.install-route {
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--border);
}
.install-route-name {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  color: var(--fg);
  font-family: var(--font-sans);
  font-size: var(--text-sm);
  font-weight: 600;
  line-height: 1.4;
}
.install-route[data-current="true"] .install-route-name { font-weight: 650; }
.install-route-steps {
  margin: 2px 0 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
  max-width: 76ch;
}
/* Every link here is a sentence, not a button: `.settings-help-link` supplies
   the 44px hit area, and this lifts `.set-link`'s nowrap so a long label wraps
   instead of forcing the card sideways on a phone. */
.install-link {
  white-space: normal;
  max-width: 100%;
  overflow-wrap: anywhere;
}
/* A pane can be narrow while the window is wide (an expanded sidebar, page
   zoom), so this follows the pane the way the shared settings sheet does
   rather than the viewport. */
@container (max-width: 720px) {
  .install-row { flex-wrap: wrap; gap: var(--space-2) var(--space-4); }
  .install-key { width: auto; }
  .install-value { flex-basis: 100%; order: 3; }
}
</style>