<template>
  <section class="home-intake" aria-labelledby="home-intake-title">
    <!-- The composer is the page's subject, so it carries no visible headline;
         the heading stays for the document outline and screen readers. -->
    <h1 id="home-intake-title" class="sr-only">Start new work</h1>

    <form
      class="home-intake-form"
      :class="{ 'home-intake-form--drag': dragOver }"
      aria-label="Start a new chat"
      @submit.prevent="onSubmit"
      @dragover.prevent="dragOver = true"
      @dragleave.self="dragOver = false"
      @drop.prevent="onDrop"
    >
      <!-- Staged attachments. Nothing is uploaded until the chat exists:
           uploads belong to a chat, so they go out right after it is created
           and ride the first message. -->
      <ul v-if="staged.length" class="home-intake-attachments" aria-label="Attachments">
        <li v-for="item in staged" :key="item.id" class="home-intake-attachment">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path v-if="item.image" d="M4 5h16v14H4zM4 15l4-4 4 4 3-3 5 5" />
            <path v-else d="M6 3h9l3 3v15H6z" />
          </svg>
          <span class="home-intake-attachment-name" :title="item.name">{{ item.name }}</span>
          <button type="button" class="home-intake-attachment-remove" :aria-label="`Remove ${item.name}`" @click="removeStaged(item.id)">×</button>
        </li>
      </ul>
      <label class="sr-only" for="home-intake-prompt">Ask Ciao to do something</label>
      <textarea
        id="home-intake-prompt"
        v-model="prompt"
        rows="3"
        autocomplete="off"
        placeholder="Describe a task or ask a question"
        :disabled="starting"
        @keydown="onPromptKeydown"
        @paste="onPaste"
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

        <input ref="fileInput" type="file" multiple hidden @change="onFileInput" />
        <button type="button" class="home-intake-icon-btn" title="Attach files" aria-label="Attach files" @click="fileInput?.click()">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 12 5-5a3 3 0 0 1 4 4l-7 7a5 5 0 0 1-7-7l7-7" /></svg>
        </button>
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
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useProjectStore, type NewChatRuntime } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'
import { clearChatDraft } from '../lib/chatDrafts'
import { openNewChatPicker } from '../lib/newChat'
import { isApplePlatform } from '../lib/desktop'
import { providerForModelSection, sectionsFromModelsResponse, type ModelSection } from '../lib/modelSections'
import ModelSelector from './ModelSelector.vue'
import { importDesktopDrop, uploadChatAttachments } from '../lib/chatAttachments'

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

// ── Attachments ──────────────────────────────────────────────────────
type StagedItem = { id: number; name: string; image: boolean; file?: File; grantId?: string }
const staged = ref<StagedItem[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
const dragOver = ref(false)
let stagedSeq = 0

function stageFiles(files: File[]): void {
  for (const file of files) {
    staged.value.push({ id: ++stagedSeq, name: file.name || 'pasted image', image: file.type.startsWith('image/'), file })
  }
}

function removeStaged(id: number): void {
  staged.value = staged.value.filter(item => item.id !== id)
}

function onDrop(e: DragEvent): void {
  dragOver.value = false
  const files: File[] = []
  let folder = false
  for (const item of Array.from(e.dataTransfer?.items || [])) {
    if (item.kind !== 'file') continue
    const entry = (item as DataTransferItem & { webkitGetAsEntry?: () => { isDirectory?: boolean } | null }).webkitGetAsEntry?.()
    if (entry?.isDirectory) { folder = true; continue }
    const file = item.getAsFile()
    if (file) files.push(file)
  }
  if (!files.length && e.dataTransfer?.files?.length) files.push(...Array.from(e.dataTransfer.files))
  if (folder) store.pushErrorToast('Could not attach folder', 'Drop individual files instead.')
  stageFiles(files)
}

function onPaste(e: ClipboardEvent): void {
  const images = Array.from(e.clipboardData?.items || [])
    .filter(item => item.type.startsWith('image/'))
    .map(item => item.getAsFile())
    .filter((file): file is File => Boolean(file))
  if (!images.length) return
  e.preventDefault()
  stageFiles(images)
}

function onFileInput(e: Event): void {
  const input = e.target as HTMLInputElement
  stageFiles(Array.from(input.files || []))
  input.value = ''
}

// The desktop app delivers Finder drops as a short-lived grant (5 minutes)
// rather than a DOM drop; hold it and import it once the chat exists.
function onNativeDragEnter(): void { dragOver.value = true }
function onNativeDragLeave(): void { dragOver.value = false }
function onNativeDrop(event: Event): void {
  dragOver.value = false
  const detail = (event as CustomEvent<{ grantId?: string; names?: string[]; error?: string }>).detail || {}
  if (detail.error || !detail.grantId) {
    store.pushErrorToast('Could not attach file', detail.error || 'The native file-drop grant was missing.')
    return
  }
  staged.value.push({
    id: ++stagedSeq,
    name: (detail.names || []).join(', ') || 'Dropped files',
    image: false,
    grantId: detail.grantId,
  })
}
onMounted(() => {
  window.addEventListener('ciao:native-file-drag-enter', onNativeDragEnter)
  window.addEventListener('ciao:native-file-drag-leave', onNativeDragLeave)
  window.addEventListener('ciao:native-file-drop', onNativeDrop)
})
onBeforeUnmount(() => {
  window.removeEventListener('ciao:native-file-drag-enter', onNativeDragEnter)
  window.removeEventListener('ciao:native-file-drag-leave', onNativeDragLeave)
  window.removeEventListener('ciao:native-file-drop', onNativeDrop)
})

/** Upload staged items into the new chat; returns prompt tokens for files. */
async function attachStaged(chatId: string, projectId: string, items: StagedItem[]): Promise<string[]> {
  const refs: string[] = []
  const images = items.filter(item => item.file && item.image).map(item => item.file as File)
  const files = items.filter(item => item.file && !item.image).map(item => item.file as File)
  const grants = items.filter(item => item.grantId)
  const report = (failures: { filename: string; error: string }[]) => {
    for (const failure of failures) store.pushErrorToast(`Could not attach ${failure.filename}`, failure.error)
  }
  if (images.length) {
    try {
      await store.uploadImages(chatId, images)
    } catch (error) {
      store.pushErrorToast('Could not attach images', error instanceof Error ? error.message : String(error))
    }
  }
  if (files.length) {
    try {
      const result = await uploadChatAttachments(chatId, files)
      report(result.failures)
      refs.push(...result.fileRefs)
    } catch (error) {
      store.pushErrorToast('Could not attach files', error instanceof Error ? error.message : String(error))
    }
  }
  for (const grant of grants) {
    try {
      const result = await importDesktopDrop(grant.grantId as string, { chatId, projectId })
      report(result.failures)
      refs.push(...result.fileRefs)
      store.addPendingImageRefs(chatId, result.imageRefs)
    } catch (error) {
      store.pushErrorToast(`Could not attach ${grant.name}`, `${error instanceof Error ? error.message : String(error)} Drop the files again if this was more than five minutes ago.`)
    }
  }
  return refs
}

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

    const items = staged.value.slice()
    if (message || items.length) {
      const runtime = selectedModel.value ?? undefined
      const title = titleFromPrompt(message || items.map(item => item.name).join(', '))
      const chat = runtime
        ? await store.newChatInProject(projectId, message, title, runtime)
        : await store.newChatInProject(projectId, message, title)
      if (!chat) return
      // Attachments belong to a chat, so they upload now that it exists and
      // go out with the first message: images staged on the chat, files as
      // `ciao-drop:` references appended to the text.
      const refs = items.length ? await attachStaged(chat.chat_id, projectId, items) : []
      const text = [message, refs.join(' ')].filter(Boolean).join('\n\n')
      await store.sendMessage(chat.chat_id, text)
      clearChatDraft(chat.chat_id)
      staged.value = staged.value.filter(item => !items.includes(item))
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

.home-intake-form--drag {
  border-color: var(--accent);
  box-shadow: 0 18px 50px rgb(0 0 0 / 14%), 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent);
}

.home-intake-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  padding: 4px 4px 0;
  list-style: none;
}

.home-intake-attachment {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  min-height: 30px;
  padding: 0 4px 0 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elev);
  color: var(--fg2);
  font-size: var(--text-sm);
}

.home-intake-attachment svg { flex: none; color: var(--accent); }

.home-intake-attachment-name {
  min-width: 0;
  max-width: 28ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-intake-attachment-remove {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: 5px;
  background: none;
  color: var(--fg3);
  cursor: pointer;
  font-size: 15px;
}
.home-intake-attachment-remove:hover { background: var(--bg3); color: var(--fg); }

.home-intake-icon-btn {
  display: grid;
  place-items: center;
  flex: none;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  background: none;
  color: var(--fg2);
  cursor: pointer;
}
.home-intake-icon-btn:hover { background: var(--bg3); color: var(--fg); }
.home-intake-icon-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

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

  .home-intake-icon-btn,
  .home-intake-attachment-remove {
    width: var(--touch);
    height: var(--touch);
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
