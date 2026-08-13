<template>
  <div class="runs-history">
    <p class="history-title">{{ title }}</p>
    <div v-if="runs.length === 0" class="empty-history">No runs logged in this session.</div>
    <div v-else class="history-scroll">
      <table class="history-table">
        <thead>
          <tr>
            <th>Finished</th>
            <th>Result</th>
            <th>Took</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="run in runs" :key="run.started_at + run.ended_at">
            <td class="td-time">{{ formatTime(run.ended_at || run.started_at) }}</td>
            <td>
              <span class="badge" :class="badgeClass(run.status)">{{ run.status }}</span>
            </td>
            <td class="td-dur">{{ formatDuration(run.duration_ms) || 'unknown' }}</td>
            <td class="td-details">
              <span v-if="run.model" class="run-model-info">{{ run.provider }}/{{ run.model }}</span>
              <span :class="run.status === 'error' ? 'run-error-info' : 'run-summary-info'">
                {{ runOutcome(run) }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { formatDuration, formatTime } from '../../lib/time'
import { runOutcome } from '../../lib/automationView'
import type { JobRun } from '../../lib/types'

defineProps<{ runs: JobRun[]; title: string }>()

function badgeClass(status: string | undefined): string {
  if (status === 'ok') return 'badge--success'
  if (status === 'error') return 'badge--error'
  if (status === 'skipped') return 'badge--warn'
  return 'badge--muted'
}
</script>

<style scoped>
.runs-history {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.history-title {
  margin: 0;
  font-weight: 600;
  font-size: var(--text-xs);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.empty-history {
  font-size: var(--text-xs);
  color: var(--fg3);
}

/* Narrow screens scroll the table rather than the page. */
.history-scroll {
  overflow-x: auto;
}

.history-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-xs);
}

.history-table th {
  text-align: left;
  padding: var(--space-2);
  color: var(--fg3);
  font-weight: 600;
  border-bottom: 1px solid var(--border);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.history-table td {
  padding: var(--space-2);
  border-bottom: 1px solid var(--border);
  color: var(--fg);
  vertical-align: middle;
}

.history-table tbody tr:last-child td {
  border-bottom: none;
}

.td-time {
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.td-dur {
  font-variant-numeric: tabular-nums;
  color: var(--fg2);
}

.run-model-info {
  background: var(--bg2);
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  color: var(--fg2);
  font-size: 10px;
  margin-right: var(--space-2);
}

.run-summary-info {
  color: var(--fg2);
}

.run-error-info {
  color: var(--error);
  overflow-wrap: anywhere;
}
</style>
