<template>
  <div v-if="form && options.length" class="card trim-card">
    <div class="settings-card-header">
      <p class="section-title">Claude context trimming</p>
      <p class="hint">
        Built-in Claude Code tools, bundled skills and features this workspace's Claude chats can use.
        Turn one off to hide it from Claude, so every turn carries less context.
      </p>
    </div>
    <div class="trim-body">
      <p class="hint hint--compact trim-scope-note">
        Claude chats only; OpenCode chats ignore these. A change takes effect on each chat's next turn, when Claude restarts with the new set.
      </p>
      <section v-for="group in groups" :key="group.id" class="trim-group" :aria-labelledby="`trim-group-${group.id}`">
        <details v-if="group.collapsible" class="trim-disclosure">
          <summary :id="`trim-group-${group.id}`">{{ group.label }} · {{ hidden(group.options) }} of {{ group.options.length }} hidden</summary>
          <template v-for="option in group.options" :key="option.key">
            <div class="settings-switch-row set-subrow trim-row">
              <span :id="`${claudeTrimDomId(option.key)}-label`" class="ws-label set-subrow-label">{{ option.label }}</span>
              <button
                class="settings-switch"
                type="button"
                role="switch"
                :aria-checked="enabled(option)"
                :aria-labelledby="`${claudeTrimDomId(option.key)}-label`"
                :aria-describedby="`${claudeTrimDomId(option.key)}-hint`"
                :disabled="option.locked || saving"
                @click="toggle(option)"
              >
                <span class="switch-word">{{ enabled(option) ? 'On' : 'Off' }}</span>
                <span class="switch-track" aria-hidden="true"></span>
              </button>
            </div>
            <p :id="`${claudeTrimDomId(option.key)}-hint`" class="hint hint--compact set-subrow-hint">
              {{ option.locked ? `Always hidden — ${option.reason}` : option.reason }}
            </p>
          </template>
        </details>
        <template v-else>
          <p :id="`trim-group-${group.id}`" class="subsection-title trim-group-title">{{ group.label }}</p>
          <template v-for="option in group.options" :key="option.key">
            <div class="settings-switch-row set-subrow trim-row">
              <span :id="`${claudeTrimDomId(option.key)}-label`" class="ws-label set-subrow-label">{{ option.label }}</span>
              <button
                class="settings-switch"
                type="button"
                role="switch"
                :aria-checked="enabled(option)"
                :aria-labelledby="`${claudeTrimDomId(option.key)}-label`"
                :aria-describedby="`${claudeTrimDomId(option.key)}-hint`"
                :disabled="option.locked || saving"
                @click="toggle(option)"
              >
                <span class="switch-word">{{ enabled(option) ? 'On' : 'Off' }}</span>
                <span class="switch-track" aria-hidden="true"></span>
              </button>
            </div>
            <p :id="`${claudeTrimDomId(option.key)}-hint`" class="hint hint--compact set-subrow-hint">
              {{ option.locked ? `Always hidden — ${option.reason}` : option.reason }}
            </p>
          </template>
        </template>
      </section>
      <label class="settings-field set-subrow trim-extra">
        <span class="ws-label set-subrow-label">Extra denied tools</span>
        <input
          :id="`trim-extra-tools-${form.name}`"
          class="routine-input set-subrow-control"
          v-model="form.disallowed_tools"
          :disabled="saving"
          placeholder="mcp__server, Bash"
          autocomplete="off"
          autocapitalize="off"
          spellcheck="false"
          :aria-describedby="`trim-extra-tools-hint-${form.name}`"
        />
      </label>
      <p :id="`trim-extra-tools-hint-${form.name}`" class="hint hint--compact set-subrow-hint">
        Comma-separated, added on top of the switches above: <code>mcp__server</code> blocks one MCP server, <code>Bash</code> one tool.
      </p>
      <div class="trim-actions">
        <button type="button" class="btn-small trim-reset" :disabled="!hasOverrides || saving" @click="resetTrim">Reset to defaults</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// Workspace > Overview > "Claude context trimming": which Claude Code harness
// tools, bundled skills and features this workspace's Claude chats leave out,
// plus extra denied tools. It edits the same draft as the rest of the page;
// the Save changes row in the Chats and models card below sends it.
import { computed, inject } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { WORKSPACE_ADMIN_KEY } from '../../composables/useWorkspaceAdmin'
import {
  claudeTrimDomId, groupClaudeTrimOptions, isTrimmed, setTrimmed, trimmedCount,
} from '../../lib/claudeTrim'
import type { ClaudeTrimOption } from '../../lib/types'

const admin = inject(WORKSPACE_ADMIN_KEY)
if (!admin) throw new Error('SettingsClaudeTrim needs the workspace admin SettingsView provides')
const { activeWorkspaceForm: form, workspacesSaving } = admin
const projectStore = useProjectStore()

const options = computed(() => projectStore.claudeTrimOptions)
const groups = computed(() => groupClaudeTrimOptions(options.value))
const saving = computed(() => !!form.value && workspacesSaving.value === form.value.name)
const hasOverrides = computed(() => !!form.value && Object.keys(form.value.claude_trim).length > 0)

function trimmed(option: ClaudeTrimOption): boolean {
  return isTrimmed(option, form.value?.claude_trim ?? {})
}
// A switch shows what Claude can use: On is the opposite of trimmed. Locked rows are trimmed, so they read Off.
function enabled(option: ClaudeTrimOption): boolean {
  return !trimmed(option)
}
function toggle(option: ClaudeTrimOption): void {
  if (!form.value || option.locked) return
  form.value.claude_trim = setTrimmed(form.value.claude_trim, option, !trimmed(option))
}
function hidden(list: readonly ClaudeTrimOption[]): number {
  return trimmedCount(list, form.value?.claude_trim ?? {})
}
function resetTrim(): void {
  if (form.value) form.value.claude_trim = {}
}
</script>

<style scoped src="./settingsPanels.css"></style>

<style scoped>
.trim-body .set-subrow,
.trim-body .set-subrow-hint { padding-left: 0; }
.trim-body .set-subrow-label { flex: 1 1 auto; min-width: 0; }
.trim-scope-note { margin: 0 0 var(--space-2); }
.trim-group-title { margin: var(--space-3) 0 var(--space-1); }
.trim-disclosure { margin-top: var(--space-2); }
.trim-disclosure > summary {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: var(--touch);
  color: var(--accent);
  font-size: var(--text-sm);
  cursor: pointer;
  list-style: none;
}
.trim-disclosure > summary::-webkit-details-marker { display: none; }
.trim-disclosure > summary::before {
  content: '';
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  margin: 0 2px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  transform: rotate(-45deg);
  transition: transform 120ms var(--ease);
}
.trim-disclosure[open] > summary::before { transform: rotate(45deg); }
.trim-disclosure > summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
.trim-actions { display: flex; justify-content: flex-end; margin-top: var(--space-3); }
.trim-body code { font-family: var(--font-mono); font-size: var(--text-xs); }
</style>
