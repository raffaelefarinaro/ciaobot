<template>
  <section class="home-intake" aria-labelledby="home-intake-title">
    <!-- The composer is the page's subject, so it carries no visible headline;
         the heading stays for the document outline and screen readers. -->
    <h1 id="home-intake-title" class="sr-only">Start new work</h1>

    <form class="home-intake-form" aria-label="Start a new chat" @submit.prevent="onSubmit">
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
        <button
          type="button"
          class="home-intake-chip home-intake-project"
          :disabled="!hasProjects"
          :aria-label="`Choose a project for the new chat — default ${defaultProjectLabel}`"
          aria-haspopup="dialog"
          @click="chooseProject"
        >
          <svg class="home-intake-chip-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M3.5 7.5h6l2-2h9v13h-17z" />
          </svg>
          <span class="home-intake-chip-label">{{ defaultProjectLabel }}</span>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="m7 9 5 5 5-5" />
          </svg>
        </button>

        <!-- The model the next chat starts on. Unset means the workspace
             default, which the server resolves; a pick rides the create call. -->
        <div class="home-intake-model">
          <button
            ref="modelTrigger"
            type="button"
            class="home-intake-chip home-intake-model-trigger"
            :aria-label="`Model for the new chat — ${modelLabel}`"
            aria-haspopup="listbox"
            :aria-expanded="showModelPicker"
            @click="toggleModelPicker"
          >
            <span class="home-intake-model-dot" aria-hidden="true" />
            <span class="home-intake-chip-label">{{ modelLabel }}</span>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="m7 9 5 5 5-5" />
            </svg>
          </button>
          <ModelSelector
            v-if="showModelPicker"
            triggerless
            :model-value="selectedModel?.model || DEFAULT_KEY"
            :sections="modelSections"
            placeholder="Model"
            placement="bottom-start"
            @select="selectModel"
            @close="closeModelPicker"
          />
        </div>

        <span class="home-intake-spacer" />
        <span v-if="prompt.trim()" class="home-intake-kbd" aria-hidden="true"><kbd>{{ sendChord }}</kbd> send</span>
        <!-- Keeps "New" as its accessible name: the control still opens the
             shared project picker first, with or without a prompt. -->
        <button
          type="submit"
          class="home-intake-new"
          :disabled="starting || !hasProjects"
          :aria-label="starting ? 'Opening…' : 'New'"
          :aria-keyshortcuts="prompt.trim() ? sendKeyshortcuts : undefined"
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
import { computed, nextTick, ref, watch } from 'vue'
import { useProjectStore, type NewChatRuntime } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import { clearChatDraft } from '../lib/chatDrafts'
import { openNewChatPicker } from '../lib/newChat'
import { isApplePlatform } from '../lib/desktop'
import { providerForModelSection, sectionsFromModelsResponse, type ModelSection } from '../lib/modelSections'
import ModelSelector from './ModelSelector.vue'

// Sentinel row for "no override": ModelSelector lists models, so the
// workspace default is offered as its own one-model section.
const DEFAULT_KEY = '__workspace-default__'

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

// The provider the workspace starts new chats on; names the default row.
const providerLabel = computed(() => {
  const provider = store.workspaceOptions.find(
    workspace => workspace.name === store.activeWorkspace,
  )?.default_provider
  if (!provider) return ''
  return store.workspaceProviderOptions.find(option => option.value === provider)?.label || provider
})
const defaultModelLabel = computed(() => (providerLabel.value ? `${providerLabel.value} default` : 'Default model'))

const tasks = useTaskStore()
const showModelPicker = ref(false)
const selectedModel = ref<NewChatRuntime | null>(null)
const modelLabel = computed(() => selectedModel.value?.model || defaultModelLabel.value)
const modelSections = computed<ModelSection[]>(() => [
  {
    key: 'default',
    label: 'Workspace default',
    models: [DEFAULT_KEY],
    modelLabels: { [DEFAULT_KEY]: defaultModelLabel.value },
  },
  ...sectionsFromModelsResponse(tasks.models).map(section => (
    section.key === 'anthropic' ? { ...section, label: 'Claude (Anthropic)' } : section
  )),
])

const modelTrigger = ref<HTMLButtonElement | null>(null)

// The triggerless selector does not own the chip, so return focus to it
// ourselves when the list closes (Escape, outside click, or a pick).
function closeModelPicker(): void {
  showModelPicker.value = false
  void nextTick(() => modelTrigger.value?.focus())
}

function toggleModelPicker(): void {
  if (!showModelPicker.value && !tasks.models) void tasks.fetchModels()
  showModelPicker.value = !showModelPicker.value
}

function selectModel(value: string | string[], sectionKey: string): void {
  const model = Array.isArray(value) ? value[0] : value
  closeModelPicker()
  if (!model || model === DEFAULT_KEY || sectionKey === 'default') {
    selectedModel.value = null
    return
  }
  selectedModel.value = { model, provider: providerForModelSection(sectionKey) }
}

// Same send chord as the chat composer: bare Enter is a newline everywhere.
const sendChord = isApplePlatform() ? '⌘↩' : 'Ctrl+↩'
const sendKeyshortcuts = isApplePlatform() ? 'Meta+Enter' : 'Control+Enter'

// A workspace switch changes the project set underneath this form. Preserve
// each scope's unsent prompt instead of carrying Personal text into Work (or
// silently replacing the other workspace's draft with it).
watch(
  () => store.activeWorkspace,
  (next, previous) => {
    if (previous) draftsByWorkspace.set(previous, prompt.value)
    prompt.value = draftsByWorkspace.get(next) ?? ''
    preferredProjectId.value = ''
    // Another workspace has another default provider; an override picked for
    // this one must not silently carry across.
    selectedModel.value = null
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
      const runtime = selectedModel.value ?? undefined
      const chat = runtime
        ? await store.newChatInProject(projectId, message, titleFromPrompt(message), runtime)
        : await store.newChatInProject(projectId, message, titleFromPrompt(message))
      if (!chat) return
      await store.sendMessage(chat.chat_id, message)
      clearChatDraft(chat.chat_id)
    } else if (selectedModel.value) {
      await store.newChatInProject(projectId, '', undefined, selectedModel.value)
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

/* The command surface: the prompt, then one bar holding its context
   (project, model) and the single forward action. */
.home-intake-form {
  display: flex;
  flex-direction: column;
  padding: 7px;
  border: 1px solid var(--border-strong);
  border-radius: 15px;
  background: var(--bg2);
  box-shadow: 0 18px 50px rgb(0 0 0 / 14%);
  transition: border-color 160ms var(--ease), box-shadow 160ms var(--ease);
}

.home-intake-form:focus-within {
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border-strong));
  box-shadow: 0 18px 50px rgb(0 0 0 / 14%), 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
}

.home-intake-form > textarea {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  min-height: 108px;
  padding: 12px 12px 8px;
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
  min-width: 0;
  padding: 6px 2px 0 4px;
  border-top: 1px solid var(--border);
}

.home-intake-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
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

.home-intake-chip:hover:not(:disabled),
.home-intake-chip[aria-expanded="true"] {
  border-color: var(--border-strong);
  color: var(--fg);
}

.home-intake-chip:disabled {
  cursor: default;
  opacity: 0.6;
}

.home-intake-chip svg {
  flex: none;
}

.home-intake-chip-icon {
  color: var(--accent);
}

/* Long names ellipse inside the chip instead of widening the bar. */
.home-intake-chip-label {
  min-width: 0;
  max-width: 24ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-intake-project {
  flex: 0 1 auto;
}

.home-intake-model {
  position: relative;
  display: flex;
  flex: 0 2 auto;
  min-width: 0;
}

.home-intake-model-trigger {
  max-width: 100%;
}

.home-intake-model-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--accent2);
}

.home-intake-spacer {
  flex: 1;
}

.home-intake-kbd {
  margin-right: 4px;
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--fg3);
  font-size: var(--text-sm);
}
/* A real keycap: readable glyphs at body size, bordered, so the chord is
   legible next to the send button rather than a faint mono smudge. */
.home-intake-kbd kbd {
  display: inline-grid;
  place-items: center;
  min-width: 22px;
  height: 22px;
  padding: 0 6px;
  box-sizing: border-box;
  border: 1px solid var(--border-strong);
  border-bottom-width: 2px;
  border-radius: 5px;
  background: var(--bg-elev);
  color: var(--fg2);
  font-family: var(--font-sans);
  font-size: var(--text-sm);
  font-weight: 600;
  line-height: 1;
}

.home-intake-new {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 36px;
  height: 36px;
  padding: 0;
  border: 0;
  border-radius: 9px;
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
  .home-intake-chip {
    min-height: var(--touch);
  }

  .home-intake-new {
    width: var(--touch);
    height: var(--touch);
  }
}

@media (max-width: 700px) {
  .home-intake-form > textarea {
    min-height: 92px;
    /* 16px floor: iOS zooms the page into any smaller focused field. */
    font-size: max(16px, calc(16px * var(--font-scale)));
  }

  .home-intake-kbd {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .home-intake-form,
  .home-intake-chip,
  .home-intake-new {
    transition: none;
  }

  .home-intake-spinner {
    animation-duration: 2.4s;
  }
}
</style>
