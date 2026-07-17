<template>
  <div class="csv-viewer" :class="{ editing: !readOnly }">
    <div v-if="parseError" class="csv-error">{{ parseError }}</div>
    <template v-else>
      <div v-if="!readOnly" class="csv-toolbar">
        <button type="button" class="csv-tool-btn" @click="addRow" title="Add row">+ Row</button>
        <button type="button" class="csv-tool-btn" @click="addColumn" title="Add column">+ Column</button>
        <span class="csv-meta">{{ table.rows.length }} rows · {{ table.headers.length }} cols</span>
      </div>
      <div v-else-if="table.headers.length" class="csv-meta-bar">
        {{ table.rows.length }} rows · {{ table.headers.length }} cols
      </div>
      <div v-if="!table.headers.length" class="csv-empty">Empty CSV.</div>
      <div v-else class="csv-scroll">
        <table class="csv-table">
          <thead>
            <tr>
              <th v-if="!readOnly" class="csv-row-num" scope="col">#</th>
              <th
                v-for="(header, colIdx) in table.headers"
                :key="`h-${colIdx}`"
                scope="col"
              >
                <input
                  v-if="!readOnly && table.hasHeader"
                  class="csv-input csv-input-header"
                  :value="header"
                  :aria-label="`Column ${colIdx + 1} header`"
                  @input="onHeaderInput(colIdx, ($event.target as HTMLInputElement).value)"
                />
                <template v-else>{{ header }}</template>
              </th>
              <th v-if="!readOnly" class="csv-row-actions" scope="col"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, rowIdx) in table.rows" :key="`r-${rowIdx}`">
              <td v-if="!readOnly" class="csv-row-num">{{ rowIdx + 1 }}</td>
              <td
                v-for="(cell, colIdx) in row"
                :key="`c-${rowIdx}-${colIdx}`"
              >
                <input
                  v-if="!readOnly"
                  class="csv-input"
                  :value="cell"
                  :aria-label="`Row ${rowIdx + 1}, ${table.headers[colIdx] || `Column ${colIdx + 1}`}`"
                  @input="onCellInput(rowIdx, colIdx, ($event.target as HTMLInputElement).value)"
                />
                <span v-else class="csv-cell" :title="cell">{{ cell }}</span>
              </td>
              <td v-if="!readOnly" class="csv-row-actions">
                <button
                  type="button"
                  class="csv-tool-btn csv-tool-danger"
                  :title="`Delete row ${rowIdx + 1}`"
                  :aria-label="`Delete row ${rowIdx + 1}`"
                  @click="removeRow(rowIdx)"
                >×</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { parseCsv, serializeCsv, type CsvTable } from '../lib/csv'

const props = withDefaults(
  defineProps<{
    content: string
    readOnly?: boolean
  }>(),
  {
    readOnly: true,
  },
)

const emit = defineEmits<{
  (e: 'change', value: string): void
}>()

const table = ref<CsvTable>({ headers: [], rows: [], hasHeader: false })
const parseError = ref('')
const lastEmitted = ref('')

const contentKey = computed(() => props.content)

function loadFromContent(text: string): void {
  try {
    table.value = parseCsv(text)
    parseError.value = ''
    lastEmitted.value = text
  } catch (e) {
    parseError.value = e instanceof Error ? e.message : String(e)
    table.value = { headers: [], rows: [], hasHeader: false }
  }
}

watch(contentKey, (text) => {
  // Avoid clobbering in-progress edits when we just emitted this value.
  if (!props.readOnly && text === lastEmitted.value) return
  loadFromContent(text)
}, { immediate: true })

function emitChange(): void {
  const next = serializeCsv(table.value)
  if (next === lastEmitted.value) return
  lastEmitted.value = next
  emit('change', next)
}

function onHeaderInput(colIdx: number, value: string): void {
  const headers = [...table.value.headers]
  headers[colIdx] = value
  table.value = { ...table.value, headers }
  emitChange()
}

function onCellInput(rowIdx: number, colIdx: number, value: string): void {
  const rows = table.value.rows.map((row, i) =>
    i === rowIdx ? row.map((cell, j) => (j === colIdx ? value : cell)) : row,
  )
  table.value = { ...table.value, rows }
  emitChange()
}

function addRow(): void {
  const width = table.value.headers.length || 1
  const headers = table.value.headers.length
    ? table.value.headers
    : Array.from({ length: width }, (_, i) => `Column ${i + 1}`)
  const rows = [...table.value.rows, Array.from({ length: headers.length }, () => '')]
  table.value = {
    headers,
    rows,
    hasHeader: table.value.hasHeader || false,
  }
  emitChange()
}

function addColumn(): void {
  const colIdx = table.value.headers.length
  const headers = [...table.value.headers, `Column ${colIdx + 1}`]
  const rows = table.value.rows.map(row => [...row, ''])
  table.value = {
    ...table.value,
    headers,
    rows,
  }
  emitChange()
}

function removeRow(rowIdx: number): void {
  const rows = table.value.rows.filter((_, i) => i !== rowIdx)
  table.value = { ...table.value, rows }
  emitChange()
}
</script>

<style scoped>
.csv-viewer {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  gap: 8px;
}

.csv-error {
  color: var(--error, #f44336);
  font-size: var(--text-sm, 12px);
  padding: 8px 0;
}

.csv-empty {
  color: var(--fg2, var(--text-muted, #b4b4c4));
  font-size: var(--text-sm, 12px);
  padding: 16px 0;
}

.csv-toolbar,
.csv-meta-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.csv-meta,
.csv-meta-bar {
  color: var(--fg2, var(--text-muted, #b4b4c4));
  font-size: var(--text-xs, 11px);
  letter-spacing: 0.02em;
}

.csv-tool-btn {
  min-height: var(--touch, 44px);
  padding: 0 12px;
  border: 1px solid var(--border, #2e3258);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg3, var(--bg2, #2a2e54));
  color: var(--fg, #e8e8f0);
  font-family: inherit;
  font-size: var(--text-xs, 11px);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  cursor: pointer;
  touch-action: manipulation;
}

.csv-tool-btn:hover {
  border-color: var(--border-strong, #3a3f70);
  background: var(--bg-elev, #23264a);
}

.csv-tool-danger {
  min-width: var(--touch, 44px);
  padding: 0;
  color: var(--error, #f44336);
}

.csv-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  border: 1px solid var(--border, #2e3258);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg, #1a1a2e);
}

.csv-table {
  border-collapse: collapse;
  width: max-content;
  min-width: 100%;
  font-size: var(--text-sm, 12px);
  line-height: 1.4;
}

.csv-table th,
.csv-table td {
  border: 1px solid var(--border, #2e3258);
  padding: 0;
  vertical-align: top;
  min-width: 120px;
  max-width: 320px;
}

.csv-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--bg3, var(--bg2, #2a2e54));
  font-weight: 600;
  text-align: left;
  color: var(--fg, #e8e8f0);
}

.csv-table th:not(.csv-row-num):not(.csv-row-actions),
.csv-table td:not(.csv-row-num):not(.csv-row-actions) {
  padding: 0;
}

.csv-cell {
  display: block;
  padding: 6px 9px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  max-height: 8.5em;
  overflow: auto;
}

.csv-input {
  display: block;
  width: 100%;
  min-height: 36px;
  box-sizing: border-box;
  border: 0;
  background: transparent;
  color: var(--fg, #e8e8f0);
  font-family: inherit;
  font-size: inherit;
  line-height: 1.4;
  padding: 6px 9px;
  resize: none;
}

.csv-input:focus {
  outline: 2px solid var(--accent, #ff4d6d);
  outline-offset: -2px;
  background: var(--bg2, #1f2240);
}

.csv-input-header {
  font-weight: 600;
}

.csv-row-num,
.csv-row-actions {
  min-width: 44px;
  max-width: 56px;
  width: 44px;
  text-align: center;
  color: var(--fg2, var(--text-muted, #b4b4c4));
  background: var(--bg2, #1f2240);
  vertical-align: middle;
  padding: 4px !important;
}

.csv-table thead .csv-row-num,
.csv-table thead .csv-row-actions {
  background: var(--bg3, var(--bg2, #2a2e54));
}

.csv-viewer.editing .csv-table td.csv-row-num {
  position: sticky;
  left: 0;
  z-index: 1;
}
</style>
