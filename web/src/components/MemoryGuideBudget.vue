<template>
  <!-- The workspace guide (AGENTS.md; CLAUDE.md pre-migration) is the one file
       every chat loads, and the vault graph does not show it because it is not
       a vault note. Its two bounded regions budget every session, so their use
       sits in a docked window on the Memory rail, in Chat details' vocabulary. -->
  <DockedWindow
    class="guide-budget"
    :class="{ 'guide-budget--over': guideOverCap }"
    title="Always loaded"
    title-id="guide-budget-title"
  >
    <template v-if="guideLoading" #head>
      <span class="guide-budget-muted">loading…</span>
    </template>
    <template v-else-if="guideStats" #head>
      <span :title="`The whole guide, sent with every chat: ${(guideStats?.totalTokens ?? 0).toLocaleString()} tokens`">{{ formatTokens(guideStats?.totalTokens ?? 0) }} tokens</span>
    </template>
    <p class="guide-budget-lede">Loaded into every chat; keep it short.</p>
    <p v-if="guideError" class="guide-budget-note guide-budget-error" role="alert">{{ guideError }}</p>
    <template v-else-if="guideStats">
      <div class="guide-card-regions">
        <div v-for="r in guideStats.regions" :key="r.key" class="guide-region">
          <div class="guide-region-head">
            <span class="guide-region-name">{{ r.label }}</span>
            <span
              class="guide-region-count"
              :class="{ 'guide-region-count--warn': r.overCap }"
              :title="`${r.usedChars} chars ≈ ${r.tokens} tokens`"
            >{{ r.usedChars.toLocaleString() }} / {{ r.charLimit.toLocaleString() }}</span>
          </div>
          <div
            class="guide-region-bar"
            :class="{ 'guide-region-bar--warn': r.overCap, 'guide-region-bar--high': !r.overCap && r.pct >= TRIM_AT_PCT }"
            role="meter"
            :aria-label="`${r.label}: ${r.pct}% of its cap`"
            aria-valuemin="0"
            :aria-valuemax="r.charLimit"
            :aria-valuenow="r.usedChars"
          >
            <span :style="{ width: Math.min(100, r.pct) + '%' }"></span>
          </div>
          <p class="guide-region-meta">
            <span class="guide-region-pct">{{ r.pct }}%</span>
            · {{ r.entryCount }} {{ r.entryCount === 1 ? 'entry' : 'entries' }}
            <span v-if="r.overCap" class="guide-region-meta--warn"> · over its cap</span>
            <span v-if="r.expiredCount"> · {{ r.expiredCount }} expired</span>
            <span v-if="r.malformedCount" class="guide-region-meta--warn"> · {{ r.malformedCount }} malformed tag</span>
          </p>
        </div>
      </div>
      <div class="guide-card-title">
        <span class="guide-budget-path" :title="guidePathLabel">{{ guidePathLabel }}</span>
        <span
          class="guide-budget-tokens"
          :title="'The bars count entry text inside the markers; this is the whole file, markers and headings included'"
        >≈ {{ guideStats.totalTokens.toLocaleString() }} {{ guideStats.totalTokens === 1 ? 'token' : 'tokens' }}</span>
      </div>
    </template>
    <p v-else-if="!guideLoading" class="guide-budget-note">No guide file found for this workspace.</p>
    <div class="guide-budget-actions" :class="{ 'guide-budget-actions--trim': needsTrim }">
      <p class="guide-budget-hint">Trim these with a chat. Ciaobot asks before changing anything.</p>
      <div class="guide-budget-buttons">
        <button
          type="button"
          :class="needsTrim ? 'btn-small guide-budget-optimize' : 'guide-budget-link'"
          :disabled="!canOptimize"
          title="Start a chat that proposes a shorter version for you to approve"
          @click="optimize"
        >Optimize</button>
        <button type="button" class="guide-budget-link" :disabled="guideLoading || !!guideError || !guideResolvedPath" :title="`Open ${guidePathLabel}`" @click="openGuideFile">Open</button>
      </div>
    </div>
  </DockedWindow>
</template>

<script setup lang="ts">
import { fetchWorkspaceGuide, formatTokens, tokensFor } from '../lib/workspaceGuide'
import { computed, ref, watch } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import { startMemoryOptimizeChat } from '../lib/memoryOptimize'
import DockedWindow from './DockedWindow.vue'

/** At or above this share of its cap a region asks to be trimmed. */
const TRIM_AT_PCT = 85

const store = useProjectStore()
const fileViewer = useFileViewerStore()

const GUIDE_DEFAULTS: Record<string, { label: string; limit: number }> = {
  memory: { label: 'Agent memory', limit: 3000 },
  profile: { label: 'User profile', limit: 1375 },
}
const guideContent = ref('')
const guideLoading = ref(false)
const guideError = ref('')
const guideResolvedPath = ref('') // actual file that existed: AGENTS.md, or a legacy CLAUDE.md
const guidePathLabel = computed(() => guideResolvedPath.value || 'AGENTS.md')
const GUIDE_REGION_RE: Record<string, RegExp> = {
  memory: /<!--\s*ciao:memory:start(?:\s+cap=(\d+))?\s*-->([\s\S]*?)<!--\s*ciao:memory:end\s*-->/i,
  profile: /<!--\s*ciao:profile:start(?:\s+cap=(\d+))?\s*-->([\s\S]*?)<!--\s*ciao:profile:end\s*-->/i,
}
function parseEntriesForRegion(raw: string): string[] {
  const headingStripped = raw.replace(/^\s*##\s*(Agent memory|User profile)\s*\n?/, '')
  const parts = headingStripped.split(/\n?§\n?/)
  return parts.map(p => p.trim()).filter(Boolean)
}
function serializeLen(entries: string[]): number {
  if (!entries.length) return 0
  return entries.join('\n§\n').length + 1 // +1 trailing \n mirrors python serialize_entries
}
function expirationInfo(entry: string): { expired: boolean; malformed: boolean } {
  const hasPrefix = /\[expires\s*:/i.test(entry)
  const m = entry.match(/\[expires:\s*([^\]]*)\]/i)
  if (!m) return { expired: false, malformed: hasPrefix }
  const raw = m[1].trim()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) return { expired: false, malformed: true }
  // Reject impossible dates (e.g. 2026-02-30): JS normalizes them to a later
  // day, so Number.isNaN alone would report them as valid. Round-trip the
  // parsed year/month/day back to the original string to agree with the
  // backend validator, which rejects these as malformed.
  const [y, mo, da] = raw.split('-').map(Number)
  const d = new Date(y, mo - 1, da)
  const roundTrips = d.getFullYear() === y && d.getMonth() === mo - 1 && d.getDate() === da
  if (!roundTrips) return { expired: false, malformed: true }
  const today = new Date(); today.setHours(0,0,0,0)
  return { expired: d < today, malformed: false }
}
const guideStats = computed(() => {
  const content = guideContent.value
  if (!content) return null
  const regions: Array<{
    key: string; label: string; usedChars: number; charLimit: number; pct: number;
    tokens: number; entryCount: number; expiredCount: number; malformedCount: number; overCap: boolean
  }> = []
  for (const key of ['memory', 'profile'] as const) {
    const re = GUIDE_REGION_RE[key]
    const match = content.match(re)
    let cap = GUIDE_DEFAULTS[key].limit
    let body = ''
    if (match) {
      if (match[1]) { const n = Number(match[1]); if (Number.isFinite(n)) cap = n }
      body = match[2] || ''
    }
    const entries = body ? parseEntriesForRegion(body) : []
    const used = serializeLen(entries)
    let expired = 0, malformed = 0
    for (const e of entries) { const info = expirationInfo(e); if (info.expired) expired++; if (info.malformed) malformed++ }
    const pct = cap ? Math.round((used / cap) * 100 * 10) / 10 : 0
    regions.push({
      key, label: GUIDE_DEFAULTS[key].label,
      usedChars: used, charLimit: cap, pct, tokens: tokensFor(used),
      entryCount: entries.length, expiredCount: expired, malformedCount: malformed,
      overCap: used > cap,
    })
  }
  return { regions, totalTokens: tokensFor(content.length) }
})
const guideOverCap = computed(() => !!guideStats.value?.regions.some(r => r.overCap))
/** A region at or over the trim line puts Optimize forward; otherwise it stays quiet. */
const needsTrim = computed(() => !!guideStats.value?.regions.some(r => r.pct >= TRIM_AT_PCT))
const canOptimize = computed(() => !!guideResolvedPath.value && !guideLoading.value && !guideError.value)
let guideFetchSeq = 0
async function fetchGuide(): Promise<void> {
  const seq = ++guideFetchSeq
  guideLoading.value = true
  guideError.value = ''
  const guide = await fetchWorkspaceGuide(store.activeWorkspace)
  if (seq !== guideFetchSeq) return
  guideContent.value = guide.content
  guideResolvedPath.value = guide.path
  // A plain "not found" stays silent: a workspace with no guide yet is an
  // ordinary state, not a failure.
  guideError.value = guide.error
  guideLoading.value = false
}
watch(() => store.activeWorkspace, () => { void fetchGuide() }, { immediate: true })
function openGuideFile(): void {
  if (!guideResolvedPath.value) return
  void fileViewer.open(guideResolvedPath.value)
}
async function optimize(): Promise<void> {
  if (!guideResolvedPath.value) return
  await startMemoryOptimizeChat(store, { guidePath: guideResolvedPath.value })
}
</script>

<style scoped>
.guide-budget-lede {
  margin: 10px 0 12px;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.45;
}

.guide-budget-note {
  margin: 0 0 12px;
  color: var(--fg3);
  font-size: var(--text-sm);
}

.guide-budget-muted {
  color: var(--fg3);
}

.guide-budget-error {
  color: var(--warning);
}

/* Hairline rows, like At a glance and Chat details: no boxes inside the window. */
.guide-card-regions {
  display: flex;
  flex-direction: column;
}

.guide-region {
  padding: 10px 0 12px;
  border-bottom: 1px solid var(--border);
}

.guide-region:first-child {
  border-top: 1px solid var(--border);
}

.guide-region-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: var(--space-2);
  color: var(--fg);
  font-size: var(--text-base);
}

.guide-region-count,
.guide-budget-tokens,
.guide-region-pct {
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.guide-region-count--warn {
  color: var(--warning);
}

.guide-region-bar {
  height: 4px;
  margin-top: 8px;
  overflow: hidden;
  border-radius: 2px;
  background: var(--bg3);
}

.guide-region-bar > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--accent2);
}

.guide-region-bar--high > span {
  background: color-mix(in srgb, var(--warning) 70%, var(--accent2));
}

.guide-region-bar--warn > span {
  background: var(--warning);
}

.guide-region-meta {
  margin: 6px 0 0;
  color: var(--fg3);
  font-size: var(--text-xs);
}

.guide-region-meta--warn {
  color: var(--warning);
}

.guide-card-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
  min-width: 0;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
}

.guide-budget-path {
  min-width: 0;
  overflow: hidden;
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.guide-budget-actions {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-top: 10px;
}

.guide-budget-hint {
  margin: 0;
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.45;
}

.guide-budget-buttons {
  display: flex;
  align-items: center;
  gap: var(--space-4);
}

.guide-budget-actions--trim .guide-budget-buttons {
  margin-top: 6px;
}

.guide-budget-optimize {
  min-height: 36px;
}

.guide-budget-link {
  min-height: var(--touch);
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
}

@media (pointer: coarse) {
  .guide-budget-link { min-width: var(--touch); }
  .guide-budget-optimize { min-height: var(--touch); }
}

.guide-budget-link:hover:not(:disabled) {
  text-decoration: underline;
  text-underline-offset: 3px;
}

.guide-budget-link:disabled {
  color: var(--fg3);
  cursor: default;
}

.guide-budget-link:focus-visible,
.guide-budget-optimize:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}
</style>
