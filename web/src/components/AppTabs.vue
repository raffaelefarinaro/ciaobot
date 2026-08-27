<template>
  <nav class="app-tabs" aria-label="Main navigation">
    <router-link
      to="/"
      class="app-tab"
      active-class="app-tab--active"
      exact-active-class="app-tab--active"
    >chat</router-link>
    <router-link
      to="/schedules"
      class="app-tab"
      active-class="app-tab--active"
      exact-active-class="app-tab--active"
    >schedule</router-link>
    <router-link
      to="/memory"
      class="app-tab"
      active-class="app-tab--active"
      exact-active-class="app-tab--active"
    >memory</router-link>
    <router-link
      to="/settings"
      class="app-tab"
      active-class="app-tab--active"
      exact-active-class="app-tab--active"
    >settings</router-link>
  </nav>
</template>

<style scoped>
.app-tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  min-height: 44px;
  padding: 4px var(--space-3);
  overflow-x: auto;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
  scrollbar-width: none;
  flex-shrink: 0;
}

.app-tabs::-webkit-scrollbar {
  display: none;
}

.app-tab {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: var(--touch);
  padding: 0 var(--space-3);
  border-bottom: 2px solid transparent;
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
  transition: color 120ms var(--ease), border-color 120ms var(--ease), background 120ms var(--ease);
}

.app-tab:hover {
  color: var(--fg);
  background: var(--bg3);
}

.app-tab--active {
  border-bottom-color: var(--accent);
  color: var(--fg);
}

.app-tab:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

@media (prefers-reduced-motion: reduce) {
  .app-tab { transition: none; }
}
</style>
