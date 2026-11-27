<template>
  <section class="docked-window" :aria-labelledby="titleId">
    <div class="docked-window-head">
      <h2 :id="titleId" class="rail-title docked-window-title">{{ title }}</h2>
      <span v-if="$slots.head" class="docked-window-head-end"><slot name="head" /></span>
    </div>
    <slot />
  </section>
</template>

<script setup lang="ts">
/*
 * One docked window on a page's right rail, in Chat details' vocabulary
 * (ChatPanel .chat-rail / .chat-rail-head) without its close control: a `--bg2`
 * surface, a 1px border, the 14px radius, the docked shadow and a 44px title
 * bar with 16px insets. Home's At a glance and Memory's rail windows use it.
 *
 * Slots: the default slot is the body; `head` sits at the right end of the
 * title bar (a mono total, one quiet action).
 */
defineProps<{
  title: string
  /** Id of the heading, which also names the section. */
  titleId: string
}>()
</script>

<style scoped>
.docked-window {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  padding: 0 16px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg2);
  box-shadow: 0 12px 32px -14px rgb(0 0 0 / 45%);
  font-size: var(--text-sm);
}

:global(:root.theme-light) .docked-window {
  box-shadow: 0 10px 28px -14px rgb(26 26 46 / 28%);
}

.docked-window-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  min-height: 44px;
  margin: 0 -16px 6px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
}

.docked-window-title {
  margin: 0;
}

.docked-window-head-end {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}
</style>
