<script setup lang="ts">
import { computed } from 'vue'
import { gwsSetupProgress, type GwsSetupStepId } from '../../lib/gwsSetup'
import type { GwsIntegrationSettings } from '../../lib/types'

const props = defineProps<{ integration: GwsIntegrationSettings }>()

const progress = computed(() => gwsSetupProgress(props.integration))

const COPY: Record<GwsSetupStepId, { title: string; detail: string }> = {
  install: {
    title: 'Install gws',
    detail: 'Use Install in Chat below, or run `npm install -g @googleworkspace/cli`.',
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

const showFocusName = computed(() => props.integration.profiles.length > 1 && !!progress.value.focus)
</script>

<template>
  <section v-if="!progress.complete" class="gws-setup" aria-label="Google Workspace setup">
    <p class="ws-label">
      Set up a Google account<span v-if="showFocusName"> — {{ progress.focus!.label || progress.focus!.name }}</span>
    </p>
    <ol class="gws-setup-steps">
      <li
        v-for="(step, i) in progress.steps"
        :key="step.id"
        class="gws-setup-step"
        :data-state="step.state"
        :aria-current="step.state === 'current' ? 'step' : undefined"
      >
        <strong>{{ i + 1 }}. {{ COPY[step.id].title }}</strong>
        <span class="hint">{{ step.state === 'done' ? 'Done' : COPY[step.id].detail }}</span>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.gws-setup { display: flex; flex-direction: column; gap: var(--space-2); }
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
.gws-setup-step[data-state="done"] strong { color: var(--fg3); }
.gws-setup-step[data-state="pending"] { color: var(--fg3); }
.gws-setup-step[data-state="current"] strong { color: var(--fg); }
</style>
