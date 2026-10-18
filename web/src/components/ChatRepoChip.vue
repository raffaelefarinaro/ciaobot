<script setup lang="ts">
import { PopoverContent, PopoverRoot, PopoverTrigger } from 'reka-ui'
import { computed, ref, useId, watch } from 'vue'
import { apiErrorMessage } from '../lib/errorMessage'
import { repoChip, repoChipLabel } from '../lib/repoLocation'
import type { ProjectRepo, RepoLocation } from '../lib/types'
import { useProjectStore } from '../stores/projects'

const props = defineProps<{
  repo: ProjectRepo | null
  location: Partial<RepoLocation> | null
  chatId: string
  codeMode: boolean
}>()
const store = useProjectStore()
const open = ref(false)
const pending = ref(false)
const error = ref('')
const switchId = useId()
const hintId = useId()
const chip = computed(() => repoChip(props.repo, props.location))
const label = computed(() => (chip.value ? repoChipLabel(chip.value, props.codeMode) : ''))

async function toggleCodeMode() {
  if (pending.value) return
  pending.value = true
  error.value = ''
  try {
    await store.setChatCodeMode(props.chatId, !props.codeMode)
  } catch (e) {
    error.value = apiErrorMessage(e, 'Could not change code mode.')
  } finally {
    pending.value = false
  }
}

watch(open, (v) => { if (!v) error.value = '' })
</script>

<template>
  <PopoverRoot v-if="chip" v-model:open="open" :modal="false">
    <PopoverTrigger as-child>
      <button
        type="button"
        class="repo-chip"
        :class="{ 'repo-chip--missing': chip.missing, 'repo-chip--code': codeMode }"
        :aria-label="label"
        :title="chip.path"
        aria-haspopup="dialog"
      >
        <template v-if="codeMode">
          <span class="repo-chip-code">code</span><span class="repo-chip-code-sep" aria-hidden="true">·</span>
        </template>
        <span class="repo-chip-name">{{ chip.name }}</span>
        <span class="repo-chip-sep" aria-hidden="true">·</span>
        <span v-if="chip.missing" class="repo-chip-branch">missing</span>
        <span v-else class="repo-chip-branch"><span aria-hidden="true">⑂ </span>{{ chip.branch }}</span>
        <span v-if="chip.isWorktree" class="repo-chip-kind">(worktree)</span>
        <span v-if="chip.dirty" class="repo-chip-dot" aria-hidden="true"></span>
      </button>
    </PopoverTrigger>
    <PopoverContent
      as-child
      side="bottom"
      align="end"
      :side-offset="4"
      :collision-padding="8"
      :position-strategy="'fixed'"
      :update-position-strategy="'always'"
    >
      <div class="repo-popover" role="dialog" aria-label="Repository">
      <dl class="repo-popover-rows">
        <div><dt>Folder</dt><dd><code>{{ chip.path }}</code></dd></div>
        <div v-if="!chip.missing"><dt>Branch</dt><dd>{{ chip.branch }}</dd></div>
        <div><dt>Checkout</dt><dd>{{ chip.isWorktree ? 'Worktree' : 'Main checkout' }}</dd></div>
        <div v-if="chip.dirty !== null"><dt>Changes</dt><dd>{{ chip.dirty ? 'Uncommitted changes' : 'Clean' }}</dd></div>
      </dl>
      <p v-if="chip.missing" class="repo-popover-note">The linked folder is missing. Change or unlink it on the project page.</p>
      <div class="repo-code-row">
        <span :id="switchId" class="repo-code-label">Code mode</span>
        <button
          type="button"
          role="switch"
          class="repo-code-switch"
          :aria-checked="codeMode"
          :aria-labelledby="switchId"
          :aria-busy="pending"
          :aria-disabled="pending"
          :aria-describedby="hintId"
          @click="toggleCodeMode"
        >
          <span class="switch-word">{{ codeMode ? 'On' : 'Off' }}</span>
          <span class="switch-track" aria-hidden="true"></span>
        </button>
      </div>
      <p :id="hintId" class="repo-code-hint">Loads the repository's instructions, skills and write access in this chat.</p>
      <p v-if="error" class="repo-code-error" role="alert">{{ error }}</p>
      </div>
    </PopoverContent>
  </PopoverRoot>
</template>

<style scoped>
.repo-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  max-width: 22rem;
  height: 34px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg2);
  font: 500 var(--text-xs) var(--font-mono);
  cursor: pointer;
}
.repo-chip:hover { background: var(--bg3); color: var(--fg); }
.repo-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.repo-chip-name, .repo-chip-branch { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.repo-chip-kind { color: var(--fg3); }
.repo-chip-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--warning); flex: none; }
.repo-chip--missing .repo-chip-branch { color: var(--warning); }
.repo-popover {
  z-index: 100;
  width: min(22rem, calc(100vw - 16px));
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-elev);
  color: var(--fg);
  box-shadow: 0 12px 32px rgb(0 0 0 / 35%);
}
.repo-popover-rows { margin: 0; display: grid; gap: var(--space-2); }
.repo-popover-rows dt { color: var(--fg3); font-size: var(--text-xs); }
.repo-popover-rows dd { margin: 0; overflow-wrap: anywhere; font-size: var(--text-sm); }
.repo-popover-rows code { font-family: var(--font-mono); }
.repo-popover-note { margin: var(--space-2) 0 0; color: var(--warning); font-size: var(--text-sm); }
.repo-chip--code {
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--fg);
}
.repo-chip-code { color: var(--accent); font-weight: 700; flex: none; }
.repo-chip-code-sep { color: var(--fg3); }
.repo-code-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-top: var(--space-3);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
}
.repo-code-label { font-size: var(--text-sm); }
.repo-code-hint { margin: var(--space-1) 0 0; color: var(--fg3); font-size: var(--text-xs); }
.repo-code-error { margin: var(--space-2) 0 0; color: var(--error); font-size: var(--text-sm); }
/* A switch reads as state; the visible On/Off word means it does not rely on
   colour. Copied from settings/settingsPanels.css, which is scoped. */
.repo-code-switch {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--touch);
  padding: 0 2px;
  border: 0;
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.repo-code-switch[aria-disabled='true'] { opacity: 0.6; cursor: default; }
.repo-code-switch:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
.repo-code-switch .switch-word { min-width: 2.2em; text-align: right; }
.repo-code-switch .switch-track {
  position: relative;
  width: 36px;
  height: 20px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-pill);
  background: var(--bg3);
  transition: background 120ms ease, border-color 120ms ease;
}
.repo-code-switch .switch-track::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--fg3);
  transition: transform 120ms ease, background 120ms ease;
}
.repo-code-switch[aria-checked='true'] .switch-word { color: var(--fg); }
.repo-code-switch[aria-checked='true'] .switch-track {
  border-color: transparent;
  background: var(--accent);
}
.repo-code-switch[aria-checked='true'] .switch-track::after {
  transform: translateX(16px);
  background: var(--on-accent);
}
@media (prefers-reduced-motion: reduce) {
  .repo-code-switch .switch-track,
  .repo-code-switch .switch-track::after { transition: none; }
}
@media (pointer: coarse) {
  .repo-chip { min-height: var(--touch); min-width: var(--touch); }
  .repo-code-switch { min-width: var(--touch); }
}
@media (max-width: 768px) {
  .repo-chip-name, .repo-chip-sep, .repo-chip-code-sep, .repo-chip-kind { display: none; }
  .repo-chip { max-width: 8rem; min-height: var(--touch); min-width: var(--touch); }
}
</style>
