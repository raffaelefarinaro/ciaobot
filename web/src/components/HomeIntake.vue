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
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M3.5 7.5h6l2-2h9v13h-17z" />
          </svg>
          <span>{{ defaultProjectLabel }}</span>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="m7 9 5 5 5-5" />
          </svg>
        </button>
        <!-- Read-only: an empty home has no chat to hold a model yet, so this
             names what a new chat here will actually run on — the workspace's
             default provider — instead of pretending to be a model picker. -->
        <span
          v-if="providerLabel"
          class="home-intake-provider"
          :title="`New chats in ${workspaceLabel(store.activeWorkspace)} run on ${providerLabel}. Change the default in Settings → Workspaces.`"
        >
          <span class="home-intake-provider-dot" aria-hidden="true" />
          <span><span class="sr-only">Runs on </span>{{ providerLabel }}</span>
        </span>
        <span class="home-intake-note">context is added automatically</span>
      </div>

      <label class="sr-only" for="home-intake-prompt">Ask Ciao to do something</label>
      <textarea
        id="home-intake-prompt"
        v-model="prompt"
        rows="3"
        autocomplete="off"
        placeholder="Describe a task or ask a question"
        :disabled="starting"
        @keydown="onPromptKeydown"
      ></textarea>

      <div class="home-intake-bottom">
        <span class="home-intake-hint">
          <template v-if="prompt.trim()"><kbd>{{ sendChord }}</kbd> sends to the project you pick</template>
          <template v-else>Send opens the project picker</template>
        </span>
        <!-- Keeps "New" as its accessible name: the control still opens the
             shared project picker first, with or without a prompt. -->
        <button
          type="submit"
          class="home-intake-new"
          :disabled="starting || !hasProjects"
          :aria-label="starting ? 'Opening…' : 'New'"
          :title="prompt.trim() ? `Send (${sendChord})` : 'New chat'"
          aria-haspopup="dialog"
        >
          <span v-if="starting" class="home-intake-spinner" aria-hidden="true" />
          <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="m5 12 14-7-4 14-3-6z" />
            <path d="M12 13 19 5" />
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
import { isApplePlatform } from '../lib/desktop'

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

// The provider a chat started here will run on. New chats take the
// workspace's default provider (the picker only chooses a project), so this is
// the one model-ish fact home can state without a chat to read it from.
const providerLabel = computed(() => {
  const provider = store.workspaceOptions.find(
    workspace => workspace.name === store.activeWorkspace,
  )?.default_provider
  if (!provider) return ''
  return store.workspaceProviderOptions.find(option => option.value === provider)?.label || provider
})

// Same send chord as the chat composer: bare Enter is a newline everywhere.
const sendChord = isApplePlatform() ? '⌘↩' : 'Ctrl+↩'

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

function onPromptKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.isComposing) return
  if (!(event.metaKey || event.ctrlKey) || event.altKey) return
  event.preventDefault()
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
  margin: 0 auto;
  text-align: left;
}

.home-intake-copy {
  padding: var(--space-4) 0 var(--space-6);
}

.home-intake-eyebrow {
  margin: 0;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.2;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

/* The one large line on the page (prototype A's hero). Everything else on
   Today is quiet list density, so this is where the scale goes. */
.home-intake h1 {
  max-width: 700px;
  margin: 10px 0 12px;
  color: var(--fg);
  font-size: clamp(32px, 4.3vw, 57px);
  font-weight: 650;
  line-height: 1.03;
  letter-spacing: -0.055em;
  text-wrap: balance;
}

.home-intake-copy p:last-child {
  max-width: 570px;
  margin: 0;
  color: var(--fg2);
  font-size: var(--text-lg);
  line-height: 1.55;
}

/* The command surface: one bordered canvas holding the project context, the
   prompt, and the single forward action. */
.home-intake-form {
  display: flex;
  flex-direction: column;
  padding: 7px;
  border: 1px solid var(--border-strong);
  border-radius: 15px;
  background: var(--bg2);
  box-shadow: 0 18px 55px rgb(0 0 0 / 14%);
  transition: border-color 160ms var(--ease), box-shadow 160ms var(--ease);
}

.home-intake-form:focus-within {
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border-strong));
  box-shadow: 0 18px 55px rgb(0 0 0 / 14%), 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
}

.home-intake-context {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  min-height: 36px;
  padding: 0 8px 5px;
}

.home-intake-project,
.home-intake-provider {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: 0 1 auto;
  min-width: 0;
  min-height: 27px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elev);
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-xs);
}

.home-intake-project {
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

.home-intake-project svg {
  flex: none;
}

/* A project name can be a long unbreakable token; the chip ellipses rather
   than widening the composer it sits inside. */
.home-intake-project > span,
.home-intake-provider > span:last-child {
  min-width: 0;
  max-width: 22ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-intake-provider {
  flex-shrink: 2;
  cursor: default;
}

.home-intake-provider-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--accent2);
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
  min-height: 116px;
  padding: 14px 12px;
  border: 0;
  resize: none;
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: calc(17px * var(--font-scale));
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
  gap: 6px;
  min-height: 42px;
  padding: 4px 0 0 8px;
  border-top: 1px solid var(--border);
}

.home-intake-hint {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: var(--fg3);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-intake-hint kbd {
  padding: 0 4px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}

.home-intake-new {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  background: var(--accent);
  color: var(--on-accent);
  cursor: pointer;
  transition: background 120ms var(--ease), transform 60ms var(--ease);
}

.home-intake-new:hover:not(:disabled) {
  background: var(--accent-strong);
}

.home-intake-new:active:not(:disabled) {
  transform: scale(0.96);
}

.home-intake-new:disabled {
  cursor: default;
  opacity: 0.55;
}

.home-intake-new:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.home-intake-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, var(--on-accent) 35%, transparent);
  border-top-color: var(--on-accent);
  border-radius: 50%;
  animation: home-intake-spin 0.8s linear infinite;
}

@keyframes home-intake-spin {
  to { transform: rotate(360deg); }
}

/* Touch layouts: the chips and the send button are real controls, so they
   meet the 44px minimum even though they read as quiet labels. */
@media (pointer: coarse), (max-width: 700px) {
  .home-intake-project,
  .home-intake-provider {
    min-height: var(--touch);
  }

  .home-intake-new {
    width: var(--touch);
    height: var(--touch);
  }
}

@media (max-width: 700px) {
  .home-intake-copy {
    padding: 0 0 var(--space-5);
  }

  .home-intake h1 {
    font-size: 34px;
  }

  .home-intake-note {
    display: none;
  }

  .home-intake-form > textarea {
    min-height: 96px;
    /* 16px floor: iOS zooms the page into any smaller focused field. */
    font-size: max(16px, calc(16px * var(--font-scale)));
  }
}

@media (prefers-reduced-motion: reduce) {
  .home-intake-form,
  .home-intake-new {
    transition: none;
  }

  .home-intake-spinner {
    animation-duration: 2.4s;
  }
}
</style>
