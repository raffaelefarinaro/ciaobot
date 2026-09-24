<template>
  <!-- The workspace guide (AGENTS.md; CLAUDE.md pre-migration) is the one file
       every chat loads, and the vault graph does not show it because it is not
       a vault note. Its bounded regions budget every session, so their use sits
       on the Memory page's rail rather than being hidden in the sidebar. -->
  <section class="rail-section guide-budget" :class="{ 'guide-budget--over': guideOverCap }" aria-labelledby="guide-budget-title">
    <h2 id="guide-budget-title" class="rail-title">Always loaded</h2>
    <p class="guide-card-title">
      <span class="guide-budget-path">{{ guidePathLabel }}</span>
      <span v-if="guideOverCap" class="guide-budget-flag" title="A bounded region is over its advisory cap">over cap</span>
      <span v-else-if="guideLoading" class="guide-budget-muted">loading…</span>
    </p>
    <p v-if="guideError" class="rail-note guide-budget-error">{{ guideError }}</p>
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
            :class="{ 'guide-region-bar--warn': r.overCap, 'guide-region-bar--high': !r.overCap && r.pct >= 80 }"
            role="meter"
            :aria-label="`${r.label}: ${r.pct}% of its cap`"
            aria-valuemin="0"
            :aria-valuemax="r.charLimit"
            :aria-valuenow="r.usedChars"
          >
            <span :style="{ width: Math.min(100, r.pct) + '%' }"></span>
          </div>
          <p class="guide-region-meta">
            {{ r.entryCount }} {{ r.entryCount === 1 ? 'entry' : 'entries' }} · {{ r.pct }}%
            <span v-if="r.expiredCount"> · {{ r.expiredCount }} expired</span>
            <span v-if="r.malformedCount" class="guide-region-meta--warn"> · {{ r.malformedCount }} malformed tag</span>
          </p>
        </div>
      </div>
      <p class="rail-note">
        Sent with every chat in this workspace · ≈ {{ guideStats.totalTokens.toLocaleString() }} {{ guideStats.totalTokens === 1 ? 'token' : 'tokens' }}.
      </p>
    </template>
    <p v-else-if="!guideLoading" class="rail-note">No guide file found for this workspace.</p>
    <div class="guide-budget-actions">
      <button type="button" class="guide-budget-link" :disabled="guideLoading || !!guideError || !guideResolvedPath" :title="`Open ${guidePathLabel}`" @click="openGuideFile">Open</button>
      <span aria-hidden="true">·</span>
      <button type="button" class="guide-budget-link" :disabled="!canDiscussGuide" title="Start a chat about this guide" @click="discussGuide">Discuss</button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import { startFileDiscussion } from '../lib/fileDiscussion'

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
function tokensFor(chars: number): number { return Math.ceil(chars / 4) || 0 }
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
  let totalChars = 0
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
    totalChars += used
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
  return { regions, totalTokens: tokensFor(content.length), totalChars: content.length }
})
const guideOverCap = computed(() => !!guideStats.value?.regions.some(r => r.overCap))
const canDiscussGuide = computed(() => !!guideResolvedPath.value && !guideLoading.value && !guideError.value)
let guideFetchSeq = 0
async function fetchGuide(): Promise<void> {
  const seq = ++guideFetchSeq
  guideLoading.value = true
  guideError.value = ''
  // AGENTS.md is the workspace guide; CLAUDE.md is only what an install that
  // has not run the guide migration still has (ciao/workspace_guide.py), so it
  // is tried second and will stop appearing once installs have upgraded.
  //
  // After the workspace re-root migration each guide lives under
  // `<workspace>/AGENTS.md`, so a bare basename would let /api/workspace-file's
  // fuzzy lookup silently resolve to the lexicographically-first workspace's
  // guide. Try the workspace-qualified path first (retained for Open/Discuss/
  // pin), then fall back to the bare basename for installs that have not
  // re-rooted (guide still at the install root).
  const ws = store.activeWorkspace
  const qualified = [`${ws}/AGENTS.md`, `${ws}/CLAUDE.md`]
  const bare = ['AGENTS.md', 'CLAUDE.md']
  let lastError = ''
  let qualifiedErrored = false
  for (const candidate of [...qualified, ...bare]) {
    // A bare basename can fuzzy-resolve to a DIFFERENT workspace's guide
    // (routes_helpers._resolve_workspace_path anchors relative paths to the
    // primary root), so it is only a legitimate fallback when every
    // workspace-qualified probe genuinely 404'd. If one of them errored we
    // do not know whether this workspace has a guide, and showing another
    // one's — with Open/Discuss/pin acting on it — is worse than showing
    // nothing.
    if (bare.includes(candidate) && qualifiedErrored) break
    try {
      // `exact=1`: no fuzzy fallback. Without it, asking for
      // `<ws>/AGENTS.md` on a workspace that has no guide yet
      // filename-matches another workspace's and returns it with a 200,
      // so the card would render someone else's guide as this one's.
      const resp = await fetch(`/api/workspace-file?exact=1&path=${encodeURIComponent(candidate)}`, { credentials: 'same-origin' })
      if (seq !== guideFetchSeq) return
      if (resp.status === 404) continue
      // Keep trying the remaining candidates rather than giving up on the
      // first non-404: a transient 503 (the engine restarting) on the first
      // name used to blank the card even though a later name would have
      // served it. The error is only shown if every candidate fails.
      if (!resp.ok) {
        lastError = `Failed to load ${candidate} (HTTP ${resp.status})`
        if (qualified.includes(candidate)) qualifiedErrored = true
        continue
      }
      const text = await resp.text()
      if (seq !== guideFetchSeq) return
      guideContent.value = text
      guideResolvedPath.value = candidate
      guideError.value = ''
      guideLoading.value = false
      return
    } catch (e) {
      if (qualified.includes(candidate)) qualifiedErrored = true
      if (seq === guideFetchSeq) { guideError.value = e instanceof Error ? e.message : String(e) }
    }
  }
  if (seq !== guideFetchSeq) return
  guideContent.value = ''
  // Every candidate 404'd (no guide yet) or errored. Surface the last real
  // error if there was one; a plain "not found" stays silent, because a
  // workspace with no guide yet is an ordinary state, not a failure.
  if (!guideError.value) guideError.value = lastError
  guideResolvedPath.value = ''
  guideLoading.value = false
}
watch(() => store.activeWorkspace, () => { void fetchGuide() }, { immediate: true })
function openGuideFile(): void {
  if (!guideResolvedPath.value) return
  void fileViewer.open(guideResolvedPath.value)
}
async function discussGuide(): Promise<void> {
  if (!guideResolvedPath.value) return
  const path = guideResolvedPath.value
  // Reuse the generic file-discuss flow (creates a chat and pins the guide).
  await startFileDiscussion(store, { path, seed: `Let's review the workspace guide \`${path}\`. Help me audit it — what should we trim, clarify, or promote from the bounded regions?` })
}
</script>

<style scoped>
.guide-card-title {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  margin: 0 0 var(--space-1);
  min-width: 0;
}

.guide-budget-path {
  min-width: 0;
  overflow: hidden;
  color: var(--fg);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.guide-budget-flag {
  color: var(--warning);
  font-size: var(--text-xs);
  font-weight: 600;
}

.guide-budget-muted {
  color: var(--fg3);
  font-size: var(--text-xs);
}

.guide-budget-error {
  color: var(--warning);
}

.guide-card-regions {
  display: grid;
  gap: var(--space-3);
  margin-top: var(--space-2);
}

.guide-region-head {
  display: flex;
  justify-content: space-between;
  gap: var(--space-2);
  color: var(--fg);
}

.guide-region-count {
  color: var(--fg3);
  font-variant-numeric: tabular-nums;
}

.guide-region-count--warn {
  color: var(--warning);
}

.guide-region-bar {
  height: 4px;
  margin-top: 6px;
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
  margin: 4px 0 0;
  color: var(--fg3);
  font-size: var(--text-xs);
}

.guide-region-meta--warn {
  color: var(--warning);
}

.guide-budget-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--fg3);
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

.guide-budget-link:hover:not(:disabled) {
  text-decoration: underline;
  text-underline-offset: 3px;
}

.guide-budget-link:disabled {
  color: var(--fg3);
  cursor: default;
}

.guide-budget-link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}
</style>
