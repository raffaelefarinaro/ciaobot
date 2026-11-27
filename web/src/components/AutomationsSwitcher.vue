<template>
  <!-- Where the sidebar's sub-navigation is out of sight (a phone, whose
       sidebar is a drawer, or a collapsed desktop sidebar), each Automations
       page starts with the same three destinations as one segmented control. -->
  <nav class="automations-switcher" aria-label="Automations sections">
    <RouterLink
      v-for="page in AUTOMATIONS_PAGES"
      :key="page.id"
      :to="page.to"
      class="automations-switcher-item"
      :class="{ 'automations-switcher-item--current': page.id === current }"
      :aria-current="page.id === current ? 'page' : undefined"
    >{{ page.label }}</RouterLink>
  </nav>
</template>

<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { AUTOMATIONS_PAGES, type AutomationsPage } from '../lib/automationsNav'

defineProps<{ current: AutomationsPage }>()
</script>

<style scoped>
.automations-switcher {
  display: flex;
  min-width: 0;
  margin: 0 0 var(--space-5);
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.automations-switcher-item {
  display: grid;
  flex: 1 1 0;
  min-width: 0;
  min-height: var(--touch);
  place-items: center;
  padding: 0 var(--space-2);
  color: var(--fg2);
  font-size: var(--text-sm);
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
  transition: color 120ms var(--ease), background 120ms var(--ease);
}

.automations-switcher-item + .automations-switcher-item {
  border-left: 1px solid var(--border);
}

.automations-switcher-item:hover {
  background: var(--bg-elev);
  color: var(--fg);
}

.automations-switcher-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

/* The current page wears the destination list's current fill: the accent tint. */
.automations-switcher-item--current,
.automations-switcher-item--current:hover {
  background: color-mix(in srgb, var(--accent) 13%, transparent);
  color: var(--fg);
}

/* The expanded desktop sidebar already lists these three pages. */
@media (min-width: 769px) {
  :global(.chat-layout.sidebar-open) .automations-switcher {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .automations-switcher-item { transition: none; }
}
</style>
