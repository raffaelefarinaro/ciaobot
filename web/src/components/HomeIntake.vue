<template>
  <section class="home-intake" aria-labelledby="home-intake-title">
    <div class="home-intake-copy">
      <p class="home-intake-eyebrow">{{ greeting }} · {{ workspaceLabel(store.activeWorkspace) }}</p>
      <h1 id="home-intake-title">What should Ciao work on?</h1>
      <p>Describe the outcome. Ciao brings the project's context, runs the work, and leaves a durable trail you can still own.</p>
    </div>

    <form class="home-intake-form" aria-label="Start a new chat" @submit.prevent="onSubmit">
      <div class="home-intake-context">
        <button
          type="button"
          class="home-intake-project"
          :disabled="!hasProjects"
          :aria-label="`Choose a project for the new chat — default ${defaultProjectLabel}`"
          aria-haspopup="dialog"
          @click="chooseProject"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M3.5 7.5h6l2-2h9v13h-17z" />
          </svg>
          <span>{{ defaultProjectLabel }}</span>
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="square" aria-hidden="true">
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
        <span class="home-intake-note">Project context is added automatically.</span>
      </div>

      <label class="sr-only" for="home-intake-prompt">Ask Ciao to do something</label>
      <textarea
        id="home-intake-prompt"
        v-model="prompt"
        rows="2"
        autocomplete="off"
        placeholder="Describe a task or ask a question"
        :disabled="starting"
      ></textarea>

      <div class="home-intake-bottom">
        <span class="home-intake-hint">
          <template v-if="prompt.trim()">Enter sends this request.</template>
          <template v-else>New opens the project picker.</template>
        </span>
        <button
          type="submit"
          class="btn-primary home-intake-new"
          :disabled="starting || !hasProjects"
          aria-haspopup="dialog"
        >
          <span>{{ starting ? 'Opening…' : 'New' }}</span>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="square" aria-hidden="true">
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
      </div>
    </form>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useProjectStore } from '../stores/projects'
import { clearChatDraft } from '../lib/chatDrafts'
import { openNewChatPicker } from '../lib/newChat'
import { workspaceLabel } from '../lib/workspaceLabel'

// One greeting per session, not a live clock: it orients the page without
// re-rendering on the minute, and matches the quiet-reference eyebrow.
const greeting = (() => {
  const hour = new Date().getHours()
  if (hour < 12) return 'Good morning'
  if (hour < 18) return 'Good afternoon'
  return 'Good evening'
})()

const store = useProjectStore()
const prompt = ref('')
const starting = ref(false)
const preferredProjectId = ref('')
const draftsByWorkspace = new Map<string, string>()
const hasProjects = computed(() => store.projects.some(
  project => project.workspace === store.activeWorkspace,
))

// The chip names the project this workspace's next chat will start in. An
// explicit pick wins; otherwise it reads the workspace's General project. It
// never creates anything on its own — a project only changes the context the
// next New or Enter uses.
const defaultProject = computed(() => {
  const workspace = store.activeWorkspace
  const chosen = preferredProjectId.value
    ? store.projects.find(project => project.project_id === preferredProjectId.value)
    : undefined
  if (chosen && chosen.workspace === workspace) return chosen
  return store.projects.find(project => project.workspace === workspace && project.name === 'General')
    ?? store.projects.find(project => project.workspace === workspace)
})
const defaultProjectLabel = computed(() => defaultProject.value?.name || 'Choose a project')

// A workspace switch changes the project set underneath this form. Preserve
// each scope's unsent prompt instead of carrying Personal text into Work (or
// silently replacing the other workspace's draft with it).
watch(
  () => store.activeWorkspace,
  (next, previous) => {
    if (previous) draftsByWorkspace.set(previous, prompt.value)
    prompt.value = draftsByWorkspace.get(next) ?? ''
    preferredProjectId.value = ''
  },
  { immediate: true },
)
watch(prompt, value => draftsByWorkspace.set(store.activeWorkspace, value))
// A rename or delete can invalidate the remembered project while the chip is
// still on screen; fall back to the workspace default rather than keep naming
// a project that no longer exists here.
watch(() => store.projects, () => {
  if (!preferredProjectId.value) return
  const chosen = store.projects.find(project => project.project_id === preferredProjectId.value)
  if (!chosen || chosen.workspace !== store.activeWorkspace) preferredProjectId.value = ''
}, { deep: true })

function titleFromPrompt(value: string): string {
  const firstLine = value.split('\n')[0]?.trim() || 'New work'
  return firstLine.length > 72 ? `${firstLine.slice(0, 69)}…` : firstLine
}

function onSubmit(): void {
  void startWork()
}

async function chooseProject(): Promise<void> {
  if (starting.value || !hasProjects.value) return
  await startWork({
    workspace: store.activeWorkspace,
    projectId: defaultProject.value?.project_id,
    rememberOnly: true,
  })
}

async function startWork(options: { workspace?: string; projectId?: string; rememberOnly?: boolean } = {}): Promise<void> {
  if (starting.value || !hasProjects.value) return
  const message = options.rememberOnly ? '' : prompt.value.trim()
  const workspace = options.workspace || store.activeWorkspace
  starting.value = true
  try {
    const projectId = await openNewChatPicker({
      workspace,
      projectId: options.projectId || defaultProject.value?.project_id,
    })
    if (!projectId) return

    if (options.rememberOnly) {
      preferredProjectId.value = projectId
      return
    }

    if (message) {
      const chat = await store.newChatInProject(projectId, message, titleFromPrompt(message))
      if (!chat) return
      await store.sendMessage(chat.chat_id, message)
      clearChatDraft(chat.chat_id)
    } else {
      await store.newChatInProject(projectId)
    }

    draftsByWorkspace.delete(workspace)
    if (store.activeWorkspace === workspace) prompt.value = ''
  } catch (error) {
    store.pushErrorToast(
      'Could not start the work',
      error instanceof Error ? error.message : 'The request failed.',
    )
  } finally {
    starting.value = false
  }
}
</script>

<style scoped>
.home-intake {
  width: min(100%, 920px);
  margin: 0 auto var(--space-5);
  text-align: left;
}

.home-intake-copy {
  max-width: 680px;
  margin-bottom: var(--space-3);
}

.home-intake-eyebrow {
  margin: 0 0 var(--space-1);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 650;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.home-intake h1 {
  margin: 0;
  color: var(--fg);
  font-size: clamp(26px, 3.4vw, 40px);
  line-height: 1.08;
  letter-spacing: -0.045em;
  text-wrap: balance;
}

.home-intake-copy p {
  max-width: 62ch;
  margin: var(--space-2) 0 0;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.45;
}

/* The command canvas: one bordered surface holding the project context, the
   prompt, and the single forward action. Mirrors the quiet workbench reference
   without turning the composer into a settings form. */
.home-intake-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  background: var(--bg2);
  box-shadow: 0 0.75rem 2rem rgb(0 0 0 / 12%);
  transition: border-color 160ms var(--ease), box-shadow 160ms var(--ease);
}

.home-intake-form:focus-within {
  border-color: color-mix(in srgb, var(--accent) 62%, var(--border-strong));
  box-shadow: 0 0.75rem 2rem rgb(0 0 0 / 14%), 0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent);
}

.home-intake-context {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  padding: 2px 2px 0;
}

.home-intake-project {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  min-height: 30px;
  padding: 0 var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}

.home-intake-project:hover:not(:disabled) {
  border-color: var(--border-strong);
  color: var(--fg);
}

.home-intake-project:disabled {
  cursor: default;
  opacity: 0.6;
}

.home-intake-project svg:first-child {
  color: var(--accent);
}

/* A project name can be a long unbreakable token; the chip ellipses rather
   than widening the composer it sits inside. */
.home-intake-project > span {
  min-width: 0;
  max-width: 22ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-intake-note {
  min-width: 0;
  overflow: hidden;
  color: var(--fg3);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-intake-form > textarea {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  min-height: 62px;
  padding: var(--space-1) var(--space-2);
  border: 0;
  resize: vertical;
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-lg);
  line-height: 1.5;
}

.home-intake-form > textarea::placeholder {
  color: var(--fg3);
}

.home-intake-form > textarea:focus {
  box-shadow: none;
  outline: none;
}

.home-intake-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-2) 2px 0;
  border-top: 1px solid var(--border);
}

.home-intake-hint {
  min-width: 0;
  overflow: hidden;
  color: var(--fg3);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-intake-new {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  flex: 0 0 auto;
  min-width: 6.5rem;
  min-height: var(--touch);
}

@media (max-width: 600px) {
  .home-intake {
    margin-bottom: var(--space-4);
  }

  .home-intake h1 {
    font-size: 25px;
  }

  .home-intake-copy p {
    margin-top: var(--space-1);
  }

  .home-intake-note {
    display: none;
  }

  .home-intake-project {
    /* The chip is a real control on a touch layout, so it meets the 44px
       minimum even though it reads as a quiet label. */
    min-height: var(--touch);
  }

  .home-intake-form > textarea {
    font-size: var(--text-base);
  }

  .home-intake-new {
    min-width: 5.5rem;
    padding-inline: var(--space-3);
  }
}

@media (prefers-reduced-motion: reduce) {
  .home-intake-form {
    transition: none;
  }
}
</style>
