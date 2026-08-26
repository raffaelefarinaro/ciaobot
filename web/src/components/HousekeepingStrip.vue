<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useHousekeepingStore } from '../stores/housekeeping'
import { useProjectStore } from '../stores/projects'
import type { OperatorAction } from '../lib/types'

const housekeeping = useHousekeepingStore()
const projectStore = useProjectStore()

const chatBusy = ref(false)

/** Actions grouped by the workspace they concern, shared ones first.
 *
 * An action's workspace decides where acting on it writes, so a flat strip made
 * the reader check each tile's prose to work out which one it was about.
 * Anything with no workspace is install-wide, and goes at the top because it
 * applies regardless of which workspace you are looking at.
 */
/** Actions relevant to the workspace on screen.
 *
 * A shared action (no workspace) always applies. A workspace-scoped action
 * names where acting on it writes, and another workspace's pile is not this
 * tab's business: the review queue behind /proposals scopes its rows the same
 * way, so a summed strip made the tile claim more than the opened page showed.
 * With no active workspace yet, show everything rather than an empty strip.
 */
const scopedActions = computed(() => {
  const active = projectStore.activeWorkspace
  if (!active) return housekeeping.actions
  return housekeeping.actions.filter(
    (action) => !action.workspace || action.workspace === active,
  )
})

const groups = computed(() => {
  const byWorkspace = new Map<string, OperatorAction[]>()
  for (const action of scopedActions.value) {
    const key = action.workspace || ''
    const bucket = byWorkspace.get(key)
    if (bucket) bucket.push(action)
    else byWorkspace.set(key, [action])
  }
  return [...byWorkspace.entries()]
    .sort(([a], [b]) => (a === '' ? -1 : b === '' ? 1 : a.localeCompare(b)))
    .map(([workspace, actions]) => ({ workspace, actions }))
})

// A single group with no workspace is the common case (one install, nothing
// workspace-specific); labelling it "shared" there is noise, so headings only
// appear once there is something to distinguish. After scoping, a named group
// is by definition the workspace you are standing in, so only a mixed group
// list — possible while no workspace is active — needs labels.
const showHeadings = computed(
  () => groups.value.some(
    (g) => g.workspace && g.workspace !== projectStore.activeWorkspace,
  ),
)

onMounted(() => {
  housekeeping.init()
})

// The strip has no permanent furniture: with zero actions it renders nothing at
// all (the template guards on a non-empty list before creating any element).
function hasActions(): boolean {
  return housekeeping.actions.length > 0
}

async function runAction(action: OperatorAction): Promise<void> {
  const { ok } = await housekeeping.run(action.id)
  // The star nudge's run records the star; the tile then disappears, so the
  // only feedback is this toast.
  if (ok && action.id === 'github-star') {
    projectStore.pushToast({
      chat_id: '',
      title: '★ Starred — thank you!',
      body: 'It genuinely helps other developers discover Ciaobot.',
    })
  }
}

// A tile's external link opens in a new tab. Only the GitHub-star nudge
// records the run too (so the tile clears after starring). Other links —
// e.g. release notes on the update tile — must NOT run the action, or
// clicking "Release notes" would start an update the user never asked for.
async function onLinkClick(action: OperatorAction): Promise<void> {
  if (action.id === 'github-star') {
    await runAction(action)
  }
}

async function dismissAction(action: OperatorAction): Promise<void> {
  await housekeeping.dismiss(action.id)
}

async function openView(action: OperatorAction): Promise<void> {
  // The queue tiles name a surface that already has per-row accept/dismiss, a
  // destination picker and batch operations. Sending the operator there beats
  // asking them to work through a hundred items in prose.
  const { router } = await import('../router')
  await router.push(action.view_route)
}

async function openChat(action: OperatorAction): Promise<void> {
  if (chatBusy.value || !action.chat_prompt) return
  chatBusy.value = true
  try {
    const workspace = action.workspace || projectStore.activeWorkspace
    if (projectStore.activeWorkspace !== workspace) {
      await projectStore.switchWorkspace(workspace)
    }
    let project = projectStore.projects.find(
      p => p.workspace === workspace && p.is_auto,
    )
    if (!project) {
      project = await projectStore.createProject('General')
    }
    const chat = await projectStore.createChat(project.project_id, 'Housekeeping')
    if (chat) {
      projectStore.sendMessage(chat.chat_id, action.chat_prompt)
      const { router } = await import('../router')
      router.push(`/chat/${chat.chat_id}`)
    }
  } finally {
    chatBusy.value = false
  }
}
</script>

<template>
  <section v-if="hasActions()" class="housekeeping" aria-label="Housekeeping actions">
    <template v-for="group in groups" :key="group.workspace || '_shared'">
      <p v-if="showHeadings" class="housekeeping-group">
        {{ group.workspace || 'shared' }}
      </p>
      <article
        v-for="action in group.actions"
        :key="action.id"
        class="housekeeping-tile"
        :class="{ 'housekeeping-tile--blocking': action.blocking }"
      >
      <span class="housekeeping-glyph" aria-hidden="true">{{ action.glyph }}</span>
      <div class="housekeeping-body">
        <p class="housekeeping-title">{{ action.title }}</p>
        <p class="housekeeping-detail">{{ action.detail }}</p>
      </div>
      <div class="housekeeping-actions">
        <button
          v-if="action.run_label"
          type="button"
          class="btn-small btn-primary"
          :disabled="housekeeping.runningIds.has(action.id)"
          @click="runAction(action)"
        >
          {{ housekeeping.runningIds.has(action.id) ? 'Running…' : action.run_label }}
        </button>
        <a
          v-if="action.link_url"
          class="btn-small btn-primary housekeeping-link"
          :href="action.link_url"
          target="_blank"
          rel="noopener noreferrer"
          @click="onLinkClick(action)"
        >{{ action.link_label || 'Open' }}</a>
        <button
          v-if="action.view_route"
          type="button"
          class="btn-small btn-primary"
          @click="openView(action)"
        >
          {{ action.view_label || 'Open' }}
        </button>
        <button
          v-if="action.chat_prompt"
          type="button"
          class="btn-small btn-chip"
          :disabled="chatBusy"
          @click="openChat(action)"
        >
          {{ action.chat_label || 'Discuss in chat' }}
        </button>
        <button
          v-if="action.dismiss_label"
          type="button"
          class="btn-small btn-chip"
          @click="dismissAction(action)"
        >{{ action.dismiss_label }}</button>
        </div>
      </article>
    </template>
  </section>
</template>

<style scoped>
/* A blocking action is a precondition the install cannot get past on its own,
   so it reads as a warning rather than as one tile among several. Not a modal
   and not an app-wide lock: the one realistic cause is an uncommitted vault, and
   locking the app would take away the assistant needed to fix it. */
.housekeeping-tile--blocking {
  border-color: var(--warning);
  background: rgba(210, 153, 34, 0.08);
}

.housekeeping-group {
  margin: var(--space-2) 0 0;
  font-size: 0.72rem;
  text-transform: lowercase;
  letter-spacing: 0.04em;
  color: var(--fg2);
}

.housekeeping-group:first-child {
  margin-top: 0;
}

.housekeeping {
  width: 100%;
  max-width: var(--home-max);
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.housekeeping-tile {
  display: grid;
  grid-template-columns: auto 1fr;
  grid-template-areas:
    'glyph body'
    '. actions';
  column-gap: var(--space-2);
  row-gap: var(--space-2);
  align-items: start;
  padding: var(--space-2) var(--space-3);
  background: var(--bg-elev);
  border-left: 3px solid var(--warning);
  border-radius: var(--radius);
  min-width: 0;
}

@media (min-width: 640px) {
  .housekeeping-tile {
    grid-template-columns: auto 1fr auto;
    grid-template-areas: 'glyph body actions';
    align-items: center;
  }

  .housekeeping-actions {
    min-width: 160px;
  }

  .housekeeping-actions .btn-small {
    width: auto;
    min-width: 140px;
  }
}

.housekeeping-glyph {
  grid-area: glyph;
  font-size: var(--text-base);
  color: var(--warning);
  line-height: 1.4;
}

.housekeeping-body {
  grid-area: body;
  min-width: 0;
}

.housekeeping-title {
  margin: 0;
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--fg);
}

.housekeeping-detail {
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  color: var(--fg2);
}

.housekeeping-actions {
  grid-area: actions;
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-1);
}

.housekeeping-actions .btn-small {
  width: 100%;
}

/* The link variant is an <a> styled like the run button: same size, centered
   text, no underline. It inherits .btn-small/.btn-primary from the global
   button tokens; only the anchor-specific resets are needed here. */
.housekeeping-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  text-decoration: none;
  box-sizing: border-box;
}
</style>
