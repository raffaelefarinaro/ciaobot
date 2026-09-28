<template>
  <div class="trace-block" :class="{ open }">
    <button
      type="button"
      class="trace-summary"
      :aria-expanded="Boolean(open)"
      @click="emit('toggle')"
    >
      <svg class="trace-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
      <span class="trace-label">{{ durationMs ? `Worked for ${formatDuration(durationMs)}` : 'Activity' }}</span>
      <span class="trace-meta">
        <span
          v-for="part in traceSummaryMetaParts(steps, subs)"
          :key="part.key"
          :class="['trace-meta-part', `part-${part.key}`, { 'part-important': part.isImportant }]"
        >
          <span class="part-text-long">{{ part.text }}</span>
          <span class="part-text-short">{{ part.shortText || part.text }}</span>
        </span>
      </span>
      <span class="sr-only">, {{ open ? 'expanded' : 'collapsed' }}</span>
    </button>
    <!-- The turn as a step timeline: one row per thought, note, tool batch or
         file, each with its kind's glyph on a shared left rule. -->
    <div v-if="open" class="trace-body" @click="emit('body-click', $event)">
      <template v-for="(step, j) in steps" :key="j">
        <div v-if="step.tool_name === '_activity'" class="trace-step trace-step--tool">
          <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m5 8 4 4-4 4M11 16h8" /></svg></span>
          <div class="trace-tools">
            <div
              v-for="(line, k) in activityLines(step.content)"
              :key="k"
              class="activity-line"
              :class="{ subagent: isSubagentLine(line) }"
              v-html="renderActivityLine(line)"
            ></div>
          </div>
        </div>
        <div v-else-if="step.tool_name === '_filecard'" class="trace-step trace-step--file">
          <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h9l3 3v15H6z" /></svg></span>
          <button
            type="button"
            class="file-card"
            @click="emit('open-file', step.file_path || step.content)"
            :title="step.file_path || step.content"
          >
            <AppIcon class="file-card-icon" :name="fileCardIcon(step.file_path || step.content)" :size="16" />
            <span class="file-card-main">
              <span class="file-card-name">{{ fileCardVerb(step.action) }} {{ fileCardBasename(step.file_path || step.content) }}</span>
              <span class="file-card-meta">
                <span class="file-card-action">{{ step.action || 'touched' }}</span>
                <span v-if="fileCardDirname(step.file_path || step.content)" class="file-card-dir"> · {{ fileCardDirname(step.file_path || step.content) }}</span>
              </span>
            </span>
            <span class="file-card-chevron" aria-hidden="true">&#8599;</span>
          </button>
        </div>
        <div v-else-if="step.tool_name === '_thinking'" class="trace-step trace-step--thought">
          <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9V16h7v-2.1A6 6 0 0 0 12 3z" /></svg></span>
          <div class="thinking-block">
            <button
              type="button"
              class="thinking-toggle"
              :aria-expanded="thinkingExpanded"
              @click.stop="emit('toggle-thinking')"
            >
              <span>{{ thinkingExpanded ? 'Thought' : 'Thought (collapsed)' }}</span>
            </button>
            <div v-if="thinkingExpanded" class="trace-text trace-thinking">
              <button
                v-if="step.lazy && typeof step.i === 'number'"
                type="button"
                class="thinking-load"
                @click.stop="emit('expand-step', step)"
              >
                Load full reasoning…
              </button>
              <div v-else v-html="renderMarkdown(step.content)"></div>
            </div>
          </div>
        </div>
        <div v-else class="trace-step trace-step--note">
          <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v11H9l-5 4z" /></svg></span>
          <div class="trace-text" v-html="renderMarkdown(step.content)"></div>
        </div>
      </template>
      <div v-if="subs?.length" class="trace-step trace-step--subagent">
        <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="3" /><path d="M6 20a6 6 0 0 1 12 0" /></svg></span>
        <SubagentPanel :subagents="subs" :chat-id="chatId" />
      </div>
      <div v-if="outputs?.length" class="answer-outputs answer-outputs--trace">
        <button
          type="button"
          class="outputs-summary"
          :aria-expanded="outputsOpen"
          :aria-controls="outputsId"
          @click.stop="emit('toggle-outputs')"
        >
          <span class="outputs-chevron" aria-hidden="true">{{ outputsOpen ? '▾' : '▸' }}</span>
          <span class="outputs-label">Outputs</span>
          <span class="outputs-count">&middot; {{ outputs.length }} {{ outputs.length === 1 ? 'file' : 'files' }}</span>
          <span class="sr-only">, {{ outputsOpen ? 'expanded' : 'collapsed' }}</span>
        </button>
        <ul v-if="outputsOpen" :id="outputsId" class="outputs-list">
          <li v-for="(f, fi) in outputs" :key="fi" class="outputs-row">
            <button
              type="button"
              class="outputs-link"
              @click.stop="emit('open-file', f.file_path)"
              :title="f.file_path"
            >
              <span class="outputs-name">{{ fileCardBasename(f.file_path) }}</span>
              <span class="outputs-open" aria-hidden="true">&#8599;</span>
            </button>
            <span class="outputs-tag" :class="{ 'outputs-tag--new': outputActionTag(f.action) === 'new' }">{{ outputActionTag(f.action) }}</span>
          </li>
        </ul>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * One completed turn's collapsed `Activity` row.
 *
 * Purely presentational: every piece of state it reads is a prop and every
 * action it can start is an emit. It owns no state of its own, not even the
 * open/closed flag — `ChatPanel` keeps that keyed by render index, because
 * the same map is reset when the chat changes and is read back by the
 * scroll-anchoring code. Markdown rendering stays in `ChatPanel` too: the
 * renderer is memoised against the chat's known file paths, and a per-row
 * cache would neither hit nor be invalidated correctly.
 *
 * The live (still streaming) trace is deliberately NOT rendered here. It has
 * a different data shape and its own streaming tail; the two only share
 * styling, which both pull from `chatTrace.css`.
 */
import AppIcon from './AppIcon.vue'
import SubagentPanel from './SubagentPanel.vue'
import {
  activityLines,
  fileCardBasename,
  fileCardDirname,
  fileCardIcon,
  isSubagentLine,
  outputActionTag,
  traceSummaryMetaParts,
  type TraceOutput,
} from '../lib/chatActivity'
import type { ChatMessage, SubagentTranscript } from '../lib/types'
import { formatDuration } from '../lib/time'

defineProps<{
  /** The turn's intermediate assistant text and tool calls, in order. */
  steps: ChatMessage[]
  /** Subagent transcripts dispatched by this turn, if any. */
  subs?: SubagentTranscript[]
  /** Files the turn wrote, listed in the collapsible Outputs disclosure. */
  outputs?: TraceOutput[]
  /** Whether that disclosure is expanded. Owned by ChatPanel, like `open`. */
  outputsOpen?: boolean
  /** id for the disclosure's aria-controls; unique per rendered turn. */
  outputsId: string
  /** Whether the body is expanded. Owned by ChatPanel. */
  open: boolean
  /** Chat the turn belongs to; SubagentPanel needs it to fetch transcripts. */
  chatId: string
  /** Shared "show thinking" preference. Owned by ChatPanel. */
  thinkingExpanded: boolean
  /** ChatPanel's memoised markdown renderer. */
  renderMarkdown: (text: string) => string
  /** ChatPanel's file-path linkifier for a single activity line. */
  renderActivityLine: (line: string) => string
  /** Wall-clock length of the turn, from its closing bubble's meta, when known. */
  durationMs?: number
}>()

// The file row's verb, from the tool's recorded action.
function fileCardVerb(action?: string): string {
  const value = (action || '').toLowerCase()
  if (value.startsWith('creat') || value === 'new' || value.startsWith('writ')) return 'Wrote'
  if (value.startsWith('edit') || value.startsWith('modif') || value.startsWith('updat')) return 'Edited'
  return 'Touched'
}

const emit = defineEmits<{
  toggle: []
  'toggle-thinking': []
  'body-click': [event: MouseEvent]
  'toggle-outputs': []
  'open-file': [filePath: string]
  'expand-step': [step: ChatMessage]
}>()
</script>

<style scoped src="./chatTrace.css"></style>
