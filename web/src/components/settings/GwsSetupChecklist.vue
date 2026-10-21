<script setup lang="ts">
import { computed } from 'vue'
import { gwsSetupGroups, gwsSetupProgress, type GwsSetupStepId } from '../../lib/gwsSetup'
import type { GwsIntegrationSettings } from '../../lib/types'

const props = defineProps<{ integration: GwsIntegrationSettings }>()

const progress = computed(() => gwsSetupProgress(props.integration))
const groups = computed(() => gwsSetupGroups(progress.value))

const COPY: Record<GwsSetupStepId, { title: string; detail: string }> = {
  install: {
    title: 'Install gws',
    detail: 'Use Install in Chat below, or run npm install -g @googleworkspace/cli.',
  },
  account: {
    title: 'Add a Google account',
    detail: 'Give it a short name below, one per Google login.',
  },
  client: {
    title: 'Add an OAuth client',
    detail: 'Upload the client_secret.json you created in Google Cloud Console on the account card. Desktop app client type.',
  },
  signin: {
    title: 'Sign in with Google',
    detail: 'Click Sign in with Google on the account card and approve access in the browser tab that opens. It connects automatically.',
  },
  link: {
    title: 'Link a workspace',
    detail: 'Pick this account in a workspace\'s Google profile field above.',
  },
}

/** A finished step is a status line, not an instruction. */
const DONE_COPY: Partial<Record<GwsSetupStepId, string>> = {
  install: 'Google Workspace CLI · Installed',
}

const doneLabel = (id: GwsSetupStepId): string => DONE_COPY[id] ?? `${COPY[id].title} · Done`

const showFocusName = computed(() => props.integration.profiles.length > 1 && !!progress.value.focus)
</script>

<template>
  <section v-if="!progress.complete" class="gws-setup" aria-label="Google Workspace setup">
    <p class="ws-label">
      Set up a Google account<span v-if="showFocusName"> — {{ progress.focus!.label || progress.focus!.name }}</span>
    </p>

    <!-- What is finished: quiet status lines, no instructions. -->
    <ul v-if="groups.done.length" class="gws-setup-done" role="list">
      <li v-for="step in groups.done" :key="step.id" class="gws-setup-done-item">
        {{ doneLabel(step.id) }}
      </li>
    </ul>

    <!-- The one step to do now, in full. -->
    <ol v-if="groups.current" class="gws-setup-steps" role="list">
      <li
        class="gws-setup-step"
        data-state="current"
        aria-current="step"
      >
        <strong>{{ COPY[groups.current.id].title }}</strong>
        <span class="hint">{{ COPY[groups.current.id].detail }}</span>
      </li>
    </ol>

    <!-- The steps after that, folded away until they are next. -->
    <details v-if="groups.pending.length" class="gws-setup-more">
      <summary class="gws-setup-more-summary">What happens next?</summary>
      <ol class="gws-setup-steps" role="list">
        <li
          v-for="step in groups.pending"
          :key="step.id"
          class="gws-setup-step"
          data-state="pending"
        >
          <strong>{{ COPY[step.id].title }}</strong>
          <span class="hint">{{ COPY[step.id].detail }}</span>
        </li>
      </ol>
    </details>
  </section>
</template>

<style scoped>
.gws-setup { display: flex; flex-direction: column; gap: var(--space-2); }
.gws-setup-done {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.gws-setup-done-item {
  color: var(--fg3);
  font-size: var(--text-xs);
  line-height: 1.4;
}
.gws-setup-steps {
  list-style: none;
  margin: 0;
  padding: 0;
}
.gws-setup-step {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-2) 0;
  border-top: 1px solid var(--border);
}
.gws-setup-step:first-child { border-top: 0; padding-top: 0; }
.gws-setup-step[data-state="pending"] { color: var(--fg3); }
.gws-setup-step[data-state="current"] strong { color: var(--fg); }
/* The later steps sit in a native disclosure, drawn like the account card's
   own, so its summary is a real button with a 44px touch target. */
.gws-setup-more .gws-setup-steps {
  border-top: 1px solid var(--border);
  margin-top: var(--space-2);
}
.gws-setup-more .gws-setup-step:first-child { padding-top: var(--space-2); }
.gws-setup-more-summary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--fg2);
  font-size: var(--text-sm);
  cursor: pointer;
  list-style: none;
}
.gws-setup-more-summary::-webkit-details-marker { display: none; }
.gws-setup-more-summary::before {
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
.gws-setup-more[open] > .gws-setup-more-summary::before { transform: rotate(45deg); }
@media (pointer: coarse) {
  /* A disclosure is a tap target on touch; a fine pointer keeps the row's
     normal text height. */
  .gws-setup-more-summary { min-height: var(--touch); }
}
@media (prefers-reduced-motion: reduce) {
  .gws-setup-more-summary::before { transition: none; }
}
.gws-setup-more-summary:hover { color: var(--fg); }
.gws-setup-more-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
</style>
