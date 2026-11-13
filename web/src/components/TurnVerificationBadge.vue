<template>
  <div v-if="badge" class="vault-check">
    <button
      type="button"
      class="vault-check-toggle"
      :class="`vault-check-toggle--${badge.tone}`"
      :aria-expanded="open"
      :aria-label="badge.ariaLabel"
      @click="open = !open"
    >
      <span aria-hidden="true">{{ badge.tone === 'warn' ? '!' : '✓' }}</span>
      <span>{{ badge.label }}</span>
    </button>
    <ul v-if="open" class="vault-check-list">
      <li
        v-for="(c, k) in verification.claims"
        :key="k"
        class="vault-check-claim"
        :class="`vault-check-claim--${c.status}`"
      >
        <span class="vault-check-status">{{ c.status === 'supported' ? 'Supported' : c.status === 'unsupported' ? 'Not in your notes' : 'General knowledge' }}</span>
        <span class="vault-check-text">{{ c.text }}</span>
        <code v-if="c.source" class="vault-check-source">{{ c.source }}</code>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { verificationBadge } from '../lib/turnVerification'
import type { TurnVerification } from '../lib/types'

const props = defineProps<{ verification: TurnVerification }>()

// Local disclosure state: the list opens on the reader's click only.
const open = ref(false)
const badge = computed(() => verificationBadge(props.verification))
</script>

<style scoped>
/* Always visible under the turn's footer: a warning must not hide behind hover. */
.vault-check {
  margin-top: 4px;
  font-size: var(--text-xs);
}

.vault-check-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-height: 32px;
  padding: 0 6px;
  border: 0;
  background: none;
  color: var(--fg3);
  font: inherit;
  cursor: pointer;
}

.vault-check-toggle--warn {
  color: var(--warning);
}

.vault-check-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

@media (pointer: coarse) {
  .vault-check-toggle {
    min-height: 44px;
  }
}

.vault-check-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 2px 0 0;
  /* Align the claims with the toggle's label, not its padding box. */
  padding: 0 6px;
  list-style: none;
  font-size: var(--text-sm);
  color: var(--fg2);
}

/* Status word, then the claim; the source note wraps onto its own line. */
.vault-check-claim {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0 8px;
}

.vault-check-status {
  flex: none;
  font-weight: 600;
}

.vault-check-text {
  flex: 1 1 12em;
  min-width: 0;
}

.vault-check-claim--unsupported {
  color: var(--warning);
}

.vault-check-source {
  flex-basis: 100%;
  font-size: var(--text-xs);
  font-family: var(--font-mono, monospace);
  color: var(--fg3);
  overflow-wrap: anywhere;
}
</style>
