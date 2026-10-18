<script setup lang="ts">
import { PopoverContent, PopoverRoot, PopoverTrigger } from 'reka-ui'
import { computed, ref } from 'vue'
import { repoChip, repoChipLabel } from '../lib/repoLocation'
import type { ProjectRepo, RepoLocation } from '../lib/types'

const props = defineProps<{ repo: ProjectRepo | null; location: Partial<RepoLocation> | null }>()
const open = ref(false)
const chip = computed(() => repoChip(props.repo, props.location))
const label = computed(() => (chip.value ? repoChipLabel(chip.value) : ''))
</script>

<template>
  <PopoverRoot v-if="chip" v-model:open="open" :modal="false">
    <PopoverTrigger as-child>
      <button
        type="button"
        class="repo-chip"
        :class="{ 'repo-chip--missing': chip.missing }"
        :aria-label="label"
        :title="chip.path"
        aria-haspopup="dialog"
      >
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
@media (pointer: coarse) { .repo-chip { min-height: var(--touch); min-width: var(--touch); } }
@media (max-width: 768px) {
  .repo-chip-name, .repo-chip-sep, .repo-chip-kind { display: none; }
  .repo-chip { max-width: 8rem; min-height: var(--touch); min-width: var(--touch); }
}
</style>
