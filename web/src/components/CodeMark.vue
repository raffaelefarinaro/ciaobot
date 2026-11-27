<template>
  <!-- Decorative: the repository is named in the owner's accessible name and
       tooltip (`projectCodeTitle`), never by this glyph alone. -->
  <span v-if="inCodeMode(repo)" class="code-mark" aria-hidden="true" :title="projectCodeTitle(repo)">›_</span>
</template>

<script setup lang="ts">
import { inCodeMode, projectCodeTitle } from '../lib/repo'
import type { ProjectRepo } from '../lib/types'

/*
 * The quiet mono `›_` a project carries when its chats work in code mode (the
 * project is linked to a repository that exists on this machine). It sits after
 * the project's name in the sidebar tree, the project page header, Home's
 * project chip and the project line of Home's chat rows. Chats carry no mark of
 * their own: code mode is the project's, so one glyph says it for all of them.
 */
defineProps<{ repo: ProjectRepo | null | undefined }>()
</script>

<style scoped>
.code-mark {
  display: inline-flex;
  align-items: center;
  flex: 0 0 auto;
  height: 16px;
  color: var(--fg3);
  font: 700 calc(11px * var(--font-scale))/1 var(--font-mono);
  letter-spacing: -0.04em;
  text-transform: none;
}
</style>
