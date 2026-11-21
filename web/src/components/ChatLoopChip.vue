<template>
  <DropdownMenuRoot v-if="loops.length" v-model:open="menuOpen">
    <DropdownMenuTrigger as-child>
      <button
        type="button"
        class="composer-chip chat-loop-chip"
        aria-haspopup="menu"
        :title="fullLabel"
        :aria-label="fullLabel"
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 2l4 4-4 4" /><path d="M3 11v-1a4 4 0 0 1 4-4h14" /><path d="M7 22l-4-4 4-4" /><path d="M21 13v1a4 4 0 0 1-4 4H3" /></svg>
        <span class="chat-loop-chip__label chat-loop-chip__label--full" aria-hidden="true">{{ fullText }}</span>
        <span class="chat-loop-chip__label chat-loop-chip__label--short" aria-hidden="true">{{ shortText }}</span>
      </button>
    </DropdownMenuTrigger>
    <DropdownMenuPortal>
      <!-- as-child, with the box in this template: the scoped styles reach only
           elements rendered here, not the popper wrapper Reka builds. -->
      <DropdownMenuContent
        as-child
        align="start"
        side="top"
        :side-offset="6"
        :collision-padding="8"
      >
        <div class="chat-loop-menu">
          <template v-for="s in loops" :key="s.schedule_id">
            <DropdownMenuLabel class="chat-loop-menu__summary">
              {{ scheduleTitle(s) }} · {{ loopSummary(s, Date.now()) }}
            </DropdownMenuLabel>
            <DropdownMenuItem as-child @select="openLoop(s.schedule_id)">
              <button type="button" class="chat-loop-menu__item">Open in Automations</button>
            </DropdownMenuItem>
            <DropdownMenuItem as-child @select="(event: Event) => onStop(event, s.schedule_id)">
              <button type="button" class="chat-loop-menu__item chat-loop-menu__item--stop" :disabled="stopping">Stop loop</button>
            </DropdownMenuItem>
          </template>
          <p v-if="stopError" class="chat-loop-menu__error" role="alert">{{ stopError }}</p>
        </div>
      </DropdownMenuContent>
    </DropdownMenuPortal>
  </DropdownMenuRoot>
</template>

<script setup lang="ts">
/*
 * The composer-bar chip for a chat's own loops (lib/loops.ts). A loop is a
 * model-made, bounded interval automation that runs inside this chat; the chip
 * says so where the next message is written, and its menu opens the loop in
 * Automations or stops it. Stop is the existing schedule delete: no confirm,
 * since the menu is already the second tap. The chip keys only on its own
 * `chatId`, so two side-by-side panes each show their own chat's loops.
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuPortal,
  DropdownMenuRoot,
  DropdownMenuTrigger,
} from 'reka-ui'
import { useTaskStore } from '../stores/tasks'
import { loopCadenceLabel, loopEndsLabel, loopShortLabel, loopSummary, loopsForChat } from '../lib/loops'
import { scheduleTitle } from '../lib/scheduleList'
import type { Schedule } from '../lib/types'

const props = defineProps<{ chatId: string; archived?: boolean }>()

const taskStore = useTaskStore()
const router = useRouter()
const menuOpen = ref(false)
const stopping = ref(false)
const stopError = ref('')

// Labels are computed from the schedules, not on a timer: the end is absolute,
// and runs-left moves when the engine publishes a run's end (schedules_changed).
const loops = computed<Schedule[]>(() => (props.archived ? [] : loopsForChat(taskStore.schedules, props.chatId)))

const fullText = computed(() => {
  if (loops.value.length > 1) return `Loops · ${loops.value.length}`
  const [only] = loops.value
  return only ? `Loop · ${loopCadenceLabel(only)} · ${loopEndsLabel(only, Date.now())}` : ''
})
const shortText = computed(() => {
  if (loops.value.length > 1) return `Loops · ${loops.value.length}`
  const [only] = loops.value
  return only ? loopShortLabel(only) : ''
})
const fullLabel = computed(() => {
  if (loops.value.length > 1) return `${loops.value.length} loops in this chat`
  const [only] = loops.value
  return only ? `Loop: ${loopSummary(only, Date.now())}` : ''
})

function openLoop(id: string) {
  void router.push(`/schedules/${id}`)
}

async function onStop(event: Event, id: string) {
  // Keep the menu open while the request runs: a failure must be read in it.
  event.preventDefault()
  if (stopping.value) return
  stopping.value = true
  stopError.value = ''
  try {
    await taskStore.deleteSchedule(id)
    menuOpen.value = false
  } catch (error) {
    if ((error as { status?: number } | null)?.status === 404) {
      // Already gone (it ended, or its chat was archived meanwhile): the loop
      // is stopped as far as the user is concerned. Refresh so the chip agrees.
      await taskStore.fetchSchedules().catch(() => undefined)
      menuOpen.value = false
    } else {
      stopError.value = 'Could not stop the loop. Try again.'
    }
  } finally {
    stopping.value = false
  }
}
</script>

<style scoped>
/* The composer's chip shape (ChatPanel's `.composer-chip`), restated here
   because scoped styles do not reach this component from the composer. */
.chat-loop-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: none;
  max-width: 100%;
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elev);
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  transition: border-color 120ms var(--ease), color 120ms var(--ease);
}

.chat-loop-chip:hover,
.chat-loop-chip[aria-expanded="true"] {
  border-color: var(--border-strong);
  color: var(--fg);
}

.chat-loop-chip:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.chat-loop-chip svg { flex: none; }

.chat-loop-chip__label {
  white-space: nowrap;
}

/* The full summary may ellipsise in a wide bar; the short form never does. */
.chat-loop-chip__label--full {
  min-width: 0;
  max-width: 40ch;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-loop-chip__label--short { display: none; }

.chat-loop-menu {
  z-index: 100;
  box-sizing: border-box;
  min-width: 240px;
  max-width: min(360px, calc(100vw - 16px));
  padding: var(--space-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 16px 36px -12px rgb(0 0 0 / 60%);
}

.chat-loop-menu__summary {
  padding: var(--space-2) var(--space-3) var(--space-1);
  color: var(--fg3);
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}

.chat-loop-menu__item {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: var(--touch);
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-base);
  text-align: left;
  cursor: pointer;
}

.chat-loop-menu__item:hover,
.chat-loop-menu__item[data-highlighted] {
  background: var(--bg3);
}

.chat-loop-menu__item--stop { color: var(--error); }

.chat-loop-menu__item:disabled { color: var(--fg3); cursor: default; }

.chat-loop-menu__error {
  margin: var(--space-1) var(--space-3) var(--space-2);
  color: var(--error);
  font-size: var(--text-sm);
}

/* Narrow composers: the short form, which never wraps the bar. */
@media (pointer: coarse), (max-width: 700px) {
  .chat-loop-chip { min-height: var(--touch); }
}

@media (max-width: 700px) {
  .chat-loop-chip__label--full { display: none; }
  .chat-loop-chip__label--short { display: inline; }
}
</style>
