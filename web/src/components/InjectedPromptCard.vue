<script setup lang="ts">
/**
 * A user turn the engine wrote (a finished background run, lost background
 * tasks, an automation run, a resumed task, a webhook event, an update task,
 * a retried request), drawn as a collapsed notice instead of a user bubble.
 *
 * ChatPanel draws this once {@link parseInjectedPrompt} has recognised the turn
 * from its recorded origin. The raw prompt stays reachable behind its own
 * toggle, or its own disclosure for a retry. Log paths are plain `a.file-link` markup: ChatPanel's delegated click
 * handler opens them.
 */
import { computed, ref, useId } from 'vue'
import AppIcon from './AppIcon.vue'
import type { AppIconName } from './AppIcon.vue'
import { renderUserMarkdown } from '../lib/safeMarkdown'
import { formatTime } from '../lib/time'
import { summarizeInjectedPrompt } from '../lib/injectedPrompt'
import type { InjectedPrompt } from '../lib/injectedPrompt'

const props = defineProps<{
  prompt: InjectedPrompt
  raw: string
  timestamp?: string
  chatTitle?: string
  schedule?: { id: string; title: string } | null
  knownPaths?: string[]
}>()

const open = ref(false)
const rawOpen = ref(false)
const detailOpen = ref(false)
const bodyId = useId()
const detailId = useId()
const rawId = useId()

const ICONS: Record<InjectedPrompt['kind'], AppIconName> = {
  background_run: 'activity',
  cli_task_wake: 'activity',
  schedule: 'clock',
  task_resume: 'doc',
  webhook: 'comment',
  update_task: 'spark',
  retry: 'activity',
}

const summary = computed(() => summarizeInjectedPrompt(props.prompt, props.chatTitle))
const icon = computed(() => ICONS[props.prompt.kind])
const hasRawToggle = computed(() =>
  ['background_run', 'cli_task_wake', 'schedule', 'webhook'].includes(props.prompt.kind),
)
const md = (text: string) => renderUserMarkdown(text, props.knownPaths ?? [])
</script>

<template>
  <section class="injected-card" :class="`injected-card--${prompt.kind}`" :aria-label="summary.label">
    <div class="injected-card-head">
      <button
        type="button"
        class="injected-card-toggle"
        :aria-expanded="open ? 'true' : 'false'"
        :aria-controls="bodyId"
        @click="open = !open"
      >
        <AppIcon :name="icon" :size="14" />
        <span class="injected-card-kind">{{ summary.label }}</span>
        <span v-if="summary.facts" class="injected-card-facts">· {{ summary.facts }}</span>
        <span v-if="summary.tone" class="injected-pill" :class="`injected-pill--${summary.tone}`">{{ summary.tone }}</span>
        <svg class="injected-card-chevron" :class="{ open }" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
      </button>
      <router-link
        v-if="prompt.kind === 'schedule' && schedule?.id"
        class="injected-card-link"
        :to="`/schedules/${schedule.id}`"
      >{{ schedule.title }}</router-link>
      <time v-if="timestamp" class="injected-card-time" :datetime="timestamp">{{ formatTime(timestamp) }}</time>
    </div>

    <p v-if="!open && summary.hint" class="injected-card-hint">{{ summary.hint }}</p>

    <div v-if="open" :id="bodyId" class="injected-card-body">
      <template v-if="prompt.kind === 'background_run'">
        <ul class="injected-card-runs">
          <li v-for="(run, idx) in prompt.runs" :key="`${run.runId}-${idx}`">
            <div class="injected-card-run-head">
              <span class="injected-pill" :class="`injected-pill--${run.verdict}`">{{ run.verdict }}</span>
              <strong>{{ run.label }}</strong>
              <code class="injected-card-id">{{ run.runId }}</code>
              <span v-if="run.exitCode !== null" class="injected-card-muted">exit {{ run.exitCode }}</span>
            </div>
            <p v-if="run.error" class="injected-card-error">{{ run.error }}</p>
            <p v-if="run.logPath" class="injected-card-log">
              Log: <a class="file-link" href="#" :data-file-path="run.logPath">{{ run.logPath }}</a>
            </p>
            <pre
              v-if="run.tail.length"
              class="injected-card-tail"
              tabindex="0"
              role="region"
              :aria-label="`Last ${run.tail.length} lines of ${run.label}`"
            >{{ run.tail.join('\n') }}</pre>
            <p v-else class="injected-card-muted">No output.</p>
          </li>
        </ul>
      </template>

      <template v-else-if="prompt.kind === 'cli_task_wake'">
        <ul class="injected-card-runs">
          <li v-for="t in prompt.tasks" :key="t.taskId">
            <strong>{{ t.type }}</strong>: {{ t.description }}
            <code class="injected-card-id">{{ t.taskId }}</code>
            <div v-if="t.command"><code>{{ t.command }}</code></div>
          </li>
        </ul>
      </template>

      <template v-else-if="prompt.kind === 'schedule'">
        <!-- eslint-disable-next-line vue/no-v-html -- renderUserMarkdown is the transcript's sanitising renderer -->
        <div class="injected-card-prose" tabindex="0" role="region" aria-label="Injected request" v-html="md(prompt.prompt)"></div>
        <template v-if="prompt.command">
          <button
            type="button"
            class="injected-card-detail-toggle"
            :aria-expanded="detailOpen ? 'true' : 'false'"
            :aria-controls="detailId"
            @click="detailOpen = !detailOpen"
          >
            Command output
            <span
              class="injected-pill"
              :class="`injected-pill--${summary.tone}`"
            >{{ prompt.command.timedOut ? 'timed out' : `exit ${prompt.command.exitCode}` }}</span>
            <code class="injected-card-command">{{ prompt.command.command }}</code>
          </button>
          <pre v-if="detailOpen" :id="detailId" class="injected-card-output" tabindex="0" role="region" aria-label="Command output">{{ prompt.command.output }}</pre>
        </template>
      </template>

      <template v-else-if="prompt.kind === 'webhook'">
        <!-- eslint-disable-next-line vue/no-v-html -- renderUserMarkdown is the transcript's sanitising renderer -->
        <div class="injected-card-prose" tabindex="0" role="region" aria-label="Injected instructions" v-html="md(prompt.instructions)"></div>
        <template v-if="prompt.event">
          <button
            type="button"
            class="injected-card-detail-toggle"
            :aria-expanded="detailOpen ? 'true' : 'false'"
            :aria-controls="detailId"
            @click="detailOpen = !detailOpen"
          >Event</button>
          <pre v-if="detailOpen" :id="detailId" class="injected-card-output" tabindex="0" role="region" aria-label="Webhook event">{{ prompt.event }}</pre>
        </template>
      </template>

      <template v-else-if="prompt.kind === 'task_resume'">
        <p v-if="prompt.previousState">The previous attempt ended as {{ prompt.previousState }}.</p>
        <!-- eslint-disable-next-line vue/no-v-html -- renderUserMarkdown is the transcript's sanitising renderer -->
        <div class="injected-card-prose" tabindex="0" role="region" aria-label="Injected request" v-html="md(raw)"></div>
      </template>

      <template v-else-if="prompt.kind === 'update_task'">
        <!-- eslint-disable-next-line vue/no-v-html -- renderUserMarkdown is the transcript's sanitising renderer -->
        <div class="injected-card-prose" tabindex="0" role="region" aria-label="Injected request" v-html="md(raw)"></div>
      </template>

      <!-- A retry repeats the request that is already in the transcript, so
           the card says what happened and keeps the request one disclosure
           away. The generic full-prompt toggle stays out: it would show the
           same words a second time. -->
      <template v-else-if="prompt.kind === 'retry'">
        <p class="injected-card-note">This turn repeats the request above unchanged.</p>
        <details class="injected-card-original">
          <summary>Show original request</summary>
          <!-- eslint-disable-next-line vue/no-v-html -- renderUserMarkdown is the transcript's sanitising renderer -->
          <div class="injected-card-prose" tabindex="0" role="region" aria-label="Original request" v-html="md(raw)"></div>
        </details>
      </template>

      <template v-if="hasRawToggle">
        <button
          type="button"
          class="injected-card-raw-toggle"
          :aria-expanded="rawOpen ? 'true' : 'false'"
          :aria-controls="rawId"
          @click="rawOpen = !rawOpen"
        >{{ rawOpen ? 'Hide full prompt' : 'Show full prompt' }}</button>
        <pre v-if="rawOpen" :id="rawId" class="injected-card-raw" tabindex="0" role="region" aria-label="Full prompt">{{ raw }}</pre>
      </template>
    </div>
  </section>
</template>

<style scoped>
.injected-card {
  min-width: 0;
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--fg2);
  font-size: var(--text-sm);
  text-align: left;
}
.injected-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.injected-card-toggle,
.injected-card-detail-toggle,
.injected-card-raw-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  min-width: 0;
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.injected-card-toggle { flex: 1 1 auto; }
.injected-card-toggle:hover,
.injected-card-detail-toggle:hover,
.injected-card-raw-toggle:hover { color: var(--fg); }
.injected-card-toggle:focus-visible,
.injected-card-detail-toggle:focus-visible,
.injected-card-raw-toggle:focus-visible,
.injected-card-link:focus-visible,
.injected-card-prose:focus-visible,
.injected-card-tail:focus-visible,
.injected-card-output:focus-visible,
.injected-card-raw:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.injected-card-kind { font-weight: 600; color: var(--fg); white-space: nowrap; }
.injected-card-facts {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.injected-card-toggle > svg { flex: none; }
.injected-card-chevron {
  flex: none;
  margin-left: auto;
  color: var(--fg3);
  transition: transform 0.12s ease;
}
.injected-card-chevron.open { transform: rotate(90deg); }
@media (prefers-reduced-motion: reduce) {
  .injected-card-chevron { transition: none; }
}
.injected-card-link {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--accent);
}
.injected-card-time {
  margin-left: auto;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}
.injected-card-body {
  padding: 4px 0 8px;
  border-top: 1px solid var(--border);
  overflow-wrap: anywhere;
}
.injected-card-runs { margin: 8px 0 0; padding: 0; list-style: none; }
.injected-card-runs li + li { margin-top: 12px; }
.injected-card-run-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.injected-card-id { font-family: var(--font-mono); font-size: var(--text-xs); color: var(--fg3); }
.injected-card-command {
  min-width: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  text-align: left;
}
.injected-card-muted { color: var(--fg3); }
.injected-card-error { margin: 4px 0 0; color: var(--error); }
.injected-card-hint {
  margin: 0 0 8px;
  color: var(--error);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.injected-card-log { margin: 4px 0 0; }
.injected-card-prose { max-height: 320px; overflow: auto; margin-top: 8px; line-height: 1.5; }
.injected-card-prose :deep(> :first-child) { margin-top: 0; }
.injected-card-prose :deep(> :last-child) { margin-bottom: 0; }
.injected-card-note { margin: 8px 0 0; line-height: 1.5; }
.injected-card-original { margin-top: 4px; }
.injected-card-original > summary {
  /* list-item keeps the native disclosure triangle, the only cue it opens. */
  display: list-item;
  min-height: 32px;
  padding: 4px 0;
  border-radius: var(--radius-xs);
  color: var(--fg2);
  font-size: var(--text-sm);
  cursor: pointer;
}
.injected-card-original > summary:hover { color: var(--fg); }
.injected-card-original > summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.injected-pill {
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  white-space: nowrap;
}
.injected-pill--ok {
  border: 1px solid color-mix(in srgb, var(--success) 45%, var(--border));
  background: color-mix(in srgb, var(--success) 12%, transparent);
}
.injected-pill--cancelled {
  border: 1px solid color-mix(in srgb, var(--warning) 45%, var(--border));
  background: color-mix(in srgb, var(--warning) 12%, transparent);
}
.injected-pill--failed {
  border: 1px solid color-mix(in srgb, var(--error) 45%, var(--border));
  background: color-mix(in srgb, var(--error) 12%, transparent);
}
pre {
  margin: 8px 0 0;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.injected-card-tail,
.injected-card-output { max-height: 240px; overflow: auto; }
.injected-card-raw { max-height: 360px; overflow: auto; }
@media (pointer: coarse) {
  .injected-card-toggle,
  .injected-card-detail-toggle,
  .injected-card-raw-toggle,
  .injected-card-link {
    min-height: var(--touch);
  }
  .injected-card-link { display: inline-flex; align-items: center; }
  .injected-card-original > summary { min-height: var(--touch); padding: 12px 0; }
}
</style>
