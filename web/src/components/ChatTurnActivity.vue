<template>
  <div class="trace-block" :class="{ open }">
    <button
      type="button"
      class="trace-summary"
      :aria-expanded="Boolean(open)"
      @click="emit('toggle')"
    >
      <span class="trace-chevron">{{ open ? '▾' : '▸' }}</span>
      <AppIcon class="trace-icon" name="activity" :size="14" />
      <span class="trace-label">Activity</span>
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
    <div v-if="open" class="trace-body" @click="emit('body-click', $event)">
      <template v-for="(step, j) in steps" :key="j">
        <div v-if="step.tool_name === '_activity'" class="trace-tools">
          <div
            v-for="(line, k) in activityLines(step.content)"
            :key="k"
            class="activity-line"
            :class="{ subagent: isSubagentLine(line) }"
            v-html="renderActivityLine(line)"
          ></div>
        </div>
        <button
          v-else-if="step.tool_name === '_filecard'"
          type="button"
          class="file-card"
          @click="emit('open-file', step.file_path || step.content)"
          :title="step.file_path || step.content"
        >
          <AppIcon class="file-card-icon" :name="fileCardIcon(step.file_path || step.content)" :size="18" />
          <span class="file-card-main">
            <span class="file-card-name">{{ fileCardBasename(step.file_path || step.content) }}</span>
            <span class="file-card-meta">
              <span class="file-card-action">{{ step.action || 'touched' }}</span>
              <span v-if="fileCardDirname(step.file_path || step.content)" class="file-card-dir"> · {{ fileCardDirname(step.file_path || step.content) }}</span>
            </span>
          </span>
          <span class="file-card-chevron" aria-hidden="true">&#8599;</span>
        </button>
        <div v-else-if="step.tool_name === '_thinking'" class="thinking-block">
          <button
            type="button"
            class="thinking-toggle"
            :aria-expanded="thinkingExpanded"
            @click.stop="emit('toggle-thinking')"
          >
            <span aria-hidden="true">{{ thinkingExpanded ? '▾' : '▸' }}</span>
            <span>{{ thinkingExpanded ? 'Thinking' : 'Thinking (collapsed)' }}</span>
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
        <div v-else class="trace-text" v-html="renderMarkdown(step.content)"></div>
      </template>
      <SubagentPanel v-if="subs?.length" :subagents="subs" :chat-id="chatId" />
      <div v-if="outputs?.length" class="trace-files">
        <button
          v-for="(f, fi) in outputs"
          :key="fi"
          type="button"
          class="file-chip"
          @click.stop="emit('open-file', f.file_path)"
          :title="f.file_path"
        >
          <AppIcon class="file-chip-icon" :name="fileCardIcon(f.file_path)" :size="14" />
          <span class="file-chip-name">{{ fileCardBasename(f.file_path) }}</span>
          <span v-if="f.action === 'created'" class="file-chip-action">new</span>
          <span class="file-chip-open" aria-hidden="true">&#8599;</span>
        </button>
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
  traceSummaryMetaParts,
  type TraceOutput,
} from '../lib/chatActivity'
import type { ChatMessage, SubagentTranscript } from '../lib/types'

defineProps<{
  /** The turn's intermediate assistant text and tool calls, in order. */
  steps: ChatMessage[]
  /** Subagent transcripts dispatched by this turn, if any. */
  subs?: SubagentTranscript[]
  /** Files the turn wrote, rendered as chips under the body. */
  outputs?: TraceOutput[]
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
}>()

const emit = defineEmits<{
  toggle: []
  'toggle-thinking': []
  'body-click': [event: MouseEvent]
  'open-file': [filePath: string]
  'expand-step': [step: ChatMessage]
}>()
</script>

<style scoped src="./chatTrace.css"></style>
