<template>
  <div class="mud" :class="{ 'mud--compact': compact }">
    <p class="mud-meta">
      <span class="mud-kind" :class="{ 'mud-kind--chat': effect.chat }" :title="kindTitle">{{ item.malformed ? 'line' : kindLabel(item.kind) }}</span>
      <span v-if="metaLine" class="mud-meta-text">{{ metaLine }}</span>
      <span v-if="item.checked" class="mud-state">Approved</span>
    </p>

    <h3 :id="titleId" class="mud-title" tabindex="-1">{{ item.title }}</h3>
    <p v-if="more" class="mud-more">{{ more }}</p>

    <section v-if="item.reason" class="mud-section" :aria-labelledby="`${titleId}-why`">
      <h4 :id="`${titleId}-why`" class="mud-label">Why it is unsure</h4>
      <p class="mud-reason">
        <template v-for="(part, n) in reason" :key="n">
          <button
            v-if="part.path"
            type="button"
            class="mud-inline-link"
            :title="`Open ${part.path}`"
            @click="openFile(part.path)"
          >{{ part.text }}</button>
          <template v-else>{{ part.text }}</template>
        </template>
      </p>
    </section>

    <section v-if="sources.length && !compact" class="mud-section" :aria-labelledby="`${titleId}-from`">
      <h4 :id="`${titleId}-from`" class="mud-label">Where it came from</h4>
      <ReviewSources :sources="sources" :vault-root="vaultRoot" />
    </section>

    <section class="mud-section" :aria-labelledby="`${titleId}-approve`">
      <h4 :id="`${titleId}-approve`" class="mud-label">{{ item.checked ? 'Now that you approved it' : 'If you approve' }}</h4>
      <div class="mud-effect">
        <p v-if="effect.line" class="mud-effect-line">
          <span v-if="effect.lineRole !== 'write'" class="mud-effect-role">{{ effect.lineRole === 'remove' ? 'Removed:' : 'Put back:' }}</span>
          {{ effect.line }}
        </p>
        <p v-if="rule" class="mud-effect-line"><span class="mud-effect-role">Rule {{ item.qualifier }}:</span> {{ rule.from }} → {{ rule.to }}</p>
        <p v-if="effect.blocked" class="mud-effect-where mud-effect-blocked" role="note">
          <svg class="mud-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 7v6M12 16.5v.5" /></svg> Not allowed as a destination: <span class="mud-mono">{{ effect.where }}</span>
        </p>
        <p v-else-if="effect.where" class="mud-effect-where">
          <svg class="mud-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 3h7l4 4v14H7z" /><path d="M14 3v4h4" /></svg> {{ whereLead }} <button
            v-if="effect.whereIsNote && whereFile"
            type="button"
            class="mud-inline-link mud-mono"
            :title="`Open ${effect.where}`"
            @click="openFile(whereFile)"
          >{{ effect.where }}</button>
          <span v-else class="mud-mono">{{ effect.where }}</span>
        </p>
        <p class="mud-effect-how">{{ effect.how }}</p>
      </div>
    </section>

    <section v-if="sources.length && compact" class="mud-section mud-section--end" :aria-labelledby="`${titleId}-from`">
      <h4 :id="`${titleId}-from`" class="mud-label">Where it came from</h4>
      <ReviewSources :sources="sources" :vault-root="vaultRoot" />
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useFileViewerStore } from '../stores/fileViewer'
import ReviewSources from './ReviewSources.vue'
import {
  approveEffect, foundDate, kindLabel, reasonParts, ruleChange, unsureChatCount, unsureMore, unsureSources,
  unsureVaultRoot, vaultFile, type UnsureItem,
} from '../lib/unsureList'

/**
 * One Unsure item read in full: its kind and when it was found, the whole first
 * sentence, why Ciaobot was not sure (a Markdown link in the reason opens its
 * note), where it came from (chats link to the chat, notes open in the viewer)
 * and what approving does: the quoted line and its destination when the line
 * carries them (never invented), and who writes it and when. The desktop
 * queue's detail pane and the phone's card both draw it; `compact` puts the
 * sources last, as the card does.
 */
const props = defineProps<{
  item: UnsureItem
  /** The list's path as the file viewer opens it (`<vault>/Workspace/Unsure.md`). */
  listPath: string
  titleId: string
  compact?: boolean
}>()

const fileViewer = useFileViewerStore()

const effect = computed(() => approveEffect(props.item))
const rule = computed(() => ruleChange(props.item))
const more = computed(() => unsureMore(props.item))
const reason = computed(() => reasonParts(props.item.reason, props.listPath))
const sources = computed(() => unsureSources(props.item))
const vaultRoot = computed(() => unsureVaultRoot(props.listPath))
const whereFile = computed(() => (effect.value.whereIsNote ? vaultFile(props.listPath, effect.value.where) : ''))
const whereLead = computed(() => {
  switch (effect.value.lineRole) {
    case 'remove': return 'From the region'
    case 'restore': return 'Back into the region'
    default: return props.item.kind === 'repo' ? 'Added to' : props.item.kind === 'rule' ? 'Rule' : 'Saved to'
  }
})
const kindTitle = computed(() => {
  if (props.item.malformed) return 'Not in the list\'s format'
  return props.item.qualifier ? `${kindLabel(props.item.kind)}: ${props.item.qualifier}` : kindLabel(props.item.kind)
})
const metaLine = computed(() => {
  const found = foundDate(props.item.date)
  const chats = unsureChatCount(props.item)
  return [found && `Found ${found}`, chats && `${chats} ${chats === 1 ? 'chat' : 'chats'}`].filter(Boolean).join(' · ')
})

function openFile(path: string): void {
  void fileViewer.open(path)
}
</script>

<style scoped>
.mud { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
.mud--compact { gap: var(--space-3); }

.mud-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; margin: 0; color: var(--fg3); font-size: var(--text-sm); }
/* Squared tag (DESIGN: --radius-xs is for tags that must not read as counts).
   An item a chat must settle has a dashed outline: shape and word, not colour. */
.mud-kind {
  padding: 1px 7px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.5;
}
.mud-kind--chat { border-style: dashed; }
.mud-state { color: var(--fg2); font-weight: 600; }

.mud-title {
  margin: 0;
  color: var(--fg);
  font-size: calc(var(--text-lg) * 1.25);
  font-weight: 700;
  line-height: 1.3;
  overflow-wrap: anywhere;
  text-wrap: pretty;
}
.mud--compact .mud-title { font-size: var(--text-lg); }
.mud-title:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: var(--radius-xs); }
.mud-more { margin: -4px 0 0; color: var(--fg2); line-height: 1.5; max-width: 68ch; overflow-wrap: anywhere; }

.mud-section { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.mud-section--end { margin-top: auto; }
.mud-label { margin: 0; color: var(--fg3); font-size: var(--text-sm); font-weight: 600; }
.mud-reason { margin: 0; color: var(--fg); line-height: 1.55; max-width: 68ch; overflow-wrap: anywhere; }
.mud--compact .mud-reason { color: var(--fg2); }

.mud-effect {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-elev, var(--bg2));
}
.mud-effect-line { margin: 0; color: var(--fg); line-height: 1.5; overflow-wrap: anywhere; }
.mud-effect-role { color: var(--fg3); font-weight: 600; }
.mud-effect-where { margin: 0; color: var(--fg3); font-size: var(--text-sm); line-height: 1.6; overflow-wrap: anywhere; }
.mud-effect-where .mud-icon { margin-right: 2px; vertical-align: -2px; }
.mud-effect-blocked { color: var(--fg); font-weight: 600; }
.mud-effect-how { margin: 0; color: var(--fg3); font-size: var(--text-sm); line-height: 1.45; }
.mud-icon { flex: none; }
.mud-mono { font-family: var(--font-mono); font-size: var(--text-xs); overflow-wrap: anywhere; }

.mud-inline-link {
  display: inline;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  text-align: start;
  text-decoration: underline;
  text-decoration-thickness: 1px;
  text-underline-offset: 3px;
}
.mud-inline-link.mud-mono { font-family: var(--font-mono); font-size: var(--text-xs); }
.mud-inline-link:hover { text-decoration-thickness: 2px; }
.mud-inline-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
</style>
