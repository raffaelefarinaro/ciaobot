<script setup lang="ts">
import { onBeforeUnmount, onMounted, provide, ref, shallowRef } from 'vue'
import { VueFinder, contextMenuItems, ContextMenuIds, type DirEntry, type ItemDclickEvent } from 'vuefinder'
import 'vuefinder/dist/vuefinder.css'
import {
  FOLDER_BROWSER_FEATURES,
  FOLDER_BROWSER_ID,
  createWorkspaceDirDriver,
  fetchWorkspaceDir,
  folderErrorMessage,
  fromVfPath,
  seedFolderBrowserConfig,
  toVfPath,
  type WorkspaceDirDriver,
  type WorkspaceDirListing,
} from '../lib/folderBrowserDriver'

// Loaded through defineAsyncComponent by hosts: this is the only runtime importer of vuefinder.
const props = withDefaults(defineProps<{ initialPath?: string; chatId?: string }>(), { initialPath: '', chatId: '' })
const emit = defineEmits<{ (e: 'navigate', path: string): void; (e: 'open-file', path: string): void }>()

// Without the plugin installed, VueFinder's settings modal reads these from inject().
provide('VueFinderOptions', { i18n: {}, locale: 'en' })
// Folder double-click navigates through the menu's `open` item, so the list is cut, never emptied.
const menuItems = contextMenuItems.filter(item => item.id === ContextMenuIds.open || item.id === ContextMenuIds.refresh)

const driver = shallowRef<WorkspaceDirDriver | null>(null)
const startPath = ref('')
const error = ref('')
const shownCount = ref(0)
const truncated = ref(false)
const coarse = typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches
// The app's phone breakpoint, or any touch screen (a phone in landscape is wider than 767px): the tree opens over the list.
const narrow = typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 767px), (pointer: coarse)').matches
let aborter: AbortController | null = null

function onListing(listing: WorkspaceDirListing): void {
  truncated.value = listing.truncated
  shownCount.value = listing.entries.length
}

onMounted(async () => {
  aborter = new AbortController()
  const first = await fetchWorkspaceDir(props.initialPath, props.chatId, aborter.signal)
  if (aborter.signal.aborted) return
  if (!first.ok) {
    error.value = folderErrorMessage(first.status, first.error)
    return
  }
  seedFolderBrowserConfig(localStorage, FOLDER_BROWSER_ID, narrow)
  onListing(first.listing)
  const d = createWorkspaceDirDriver({ chatId: props.chatId, roots: first.listing.roots, initial: first.listing, onListing })
  startPath.value = toVfPath(first.listing.path, d.roots)
  driver.value = d
  emit('navigate', first.listing.path)
})

onBeforeUnmount(() => aborter?.abort())

function openFile(item: DirEntry): void {
  const d = driver.value
  if (!d) return
  emit('open-file', fromVfPath(item.path, d.roots))
}

function onFileDclick(ev: ItemDclickEvent): void {
  if (!driver.value) return
  ev.preventDefault()
  openFile(ev.item)
}

function onSelect(items: DirEntry[]): void {
  if (coarse && items.length === 1 && items[0].type === 'file') openFile(items[0])
}

function onPathChange(vfPath: string): void {
  const d = driver.value
  if (!d) return
  emit('navigate', fromVfPath(vfPath, d.roots))
}
</script>

<template>
  <div class="folder-browser" @dragenter.capture.prevent.stop @dragover.capture.prevent.stop @drop.capture.prevent.stop>
    <p v-if="error" class="fbr-error" role="alert">{{ error }}</p>
    <template v-else-if="driver">
      <p v-if="truncated" class="fbr-note" role="status">Showing the first {{ shownCount }} items.</p>
      <VueFinder
        :id="FOLDER_BROWSER_ID"
        :driver="driver"
        :features="FOLDER_BROWSER_FEATURES"
        :config="{ initialPath: startPath, showMenuBar: false, notificationsEnabled: false, loadingIndicator: 'linear' }"
        :context-menu-items="menuItems"
        selection-mode="single"
        :on-file-dclick="onFileDclick"
        :on-select="onSelect"
        :on-path-change="onPathChange"
      />
    </template>
    <p v-else class="fbr-loading" role="status">Loading…</p>
  </div>
</template>

<style scoped>
.folder-browser { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.folder-browser :deep(.vuefinder) { flex: 1; min-height: 0; }
.fbr-error, .fbr-loading { padding: 24px; color: var(--fg2); }
.fbr-error { color: var(--error); }
.fbr-note { margin: 0; padding: 6px 12px; font-size: 0.85em; color: var(--fg3); border-bottom: 1px solid var(--border); }
</style>

<style>
/* Unscoped: VueFinder teleports its dropdown and context menu to <body>, outside this component's scope. */
body .vuefinder__themer[data-theme],
body .vuefinder {
  --vf-bg-primary: var(--bg);
  --vf-bg-secondary: var(--bg2);
  --vf-bg-tertiary: var(--bg3);
  --vf-bg-hover: var(--bg3);
  --vf-bg-selected: color-mix(in srgb, var(--accent) 16%, var(--bg));
  --vf-selection-bg: color-mix(in srgb, var(--accent) 12%, var(--bg));
  --vf-selection-border: var(--accent);
  --vf-text-primary: var(--fg);
  --vf-text-secondary: var(--fg2);
  --vf-text-tertiary: var(--fg3);
  --vf-border-primary: var(--border);
  --vf-border-secondary: var(--border-strong);
  --vf-border-focus: var(--accent);
  --vf-accent-primary: var(--accent);
  --vf-accent-primary-hover: var(--accent-strong);
  --vf-accent-color: var(--accent);
  --vf-interactive-hover: var(--bg3);
  --vf-dropdown-bg: var(--bg-elev);
  --vf-dropdown-border: var(--border);
  color-scheme: inherit;
  font-family: var(--font);
}
/* A Reka modal sets pointer-events:none on <body>; VueFinder teleports its
   breadcrumb dropdown and context menu there. */
body > .vuefinder__themer,
body > div > .vuefinder__themer { pointer-events: auto; }
</style>
