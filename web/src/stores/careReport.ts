import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { api } from '../lib/api'
import type { CareReport } from '../lib/careReport'
import { useTaskStore } from './tasks'

/** How long Home waits for the first read before drawing Needs you without it. */
export const CARE_LOAD_GATE_MS = 3000
/** While a run is in progress the report is re-read this often, so the step moves. */
export const CARE_RUNNING_POLL_MS = 15_000

/**
 * The active workspace's Workspace care report (`GET /api/care-report`), shared
 * by Home's Needs you row, the At a glance line and Memory → Workspace care.
 *
 * Read-only apart from "Run care again", which is the care schedule's ordinary
 * run-now (`POST /api/schedule-run/<id>`): the run rebuilds its checklist from
 * the vault, so only what is still due is redone. The report is re-read when the
 * care schedule's own row changes (a dispatch, an outcome) and, while a run is in
 * progress and something on screen shows it, on a short poll.
 */
export const useCareReportStore = defineStore('careReport', () => {
  const tasks = useTaskStore()
  const report = ref<CareReport | null>(null)
  const workspace = ref('')
  /** The first read for `workspace` has answered, failed or outlived the gate. */
  const settled = ref(false)
  const error = ref('')
  const starting = ref(false)
  let seq = 0
  let users = 0
  let pollTimer: ReturnType<typeof setTimeout> | null = null

  function schedulePoll(): void {
    if (pollTimer !== null) {
      clearTimeout(pollTimer)
      pollTimer = null
    }
    if (users > 0 && report.value?.running) {
      pollTimer = setTimeout(() => { void refresh() }, CARE_RUNNING_POLL_MS)
    }
  }

  async function load(ws: string): Promise<void> {
    const mine = ++seq
    try {
      const body = await api.get<CareReport>(`/api/care-report?workspace=${encodeURIComponent(ws)}`)
      if (mine !== seq || ws !== workspace.value) return
      report.value = body
      error.value = ''
    } catch (cause) {
      if (mine !== seq || ws !== workspace.value) return
      error.value = cause instanceof Error ? cause.message : String(cause)
    } finally {
      if (mine === seq && ws === workspace.value) {
        settled.value = true
        schedulePoll()
      }
    }
  }

  /** Show `ws`'s report: a new workspace starts from nothing, the same one is kept. */
  function show(ws: string): void {
    if (!ws || ws === workspace.value) return
    workspace.value = ws
    report.value = null
    settled.value = false
    error.value = ''
    const gate = setTimeout(() => { if (workspace.value === ws) settled.value = true }, CARE_LOAD_GATE_MS)
    void load(ws).finally(() => clearTimeout(gate))
  }

  async function refresh(): Promise<void> {
    if (workspace.value) await load(workspace.value)
  }

  /** Something that shows the report is on screen; returns its release. */
  function attach(): () => void {
    users += 1
    schedulePoll()
    let released = false
    return () => {
      if (released) return
      released = true
      users -= 1
      schedulePoll()
    }
  }

  // The care schedule's own row moving (dispatched, finished, failed) is the
  // engine telling us the report may have changed.
  watch(
    () => {
      const id = report.value?.schedule?.id
      if (!id) return ''
      const row = tasks.schedules.find(s => s.schedule_id === id)
      return row ? `${row.last_status ?? ''}|${row.last_dispatched_at ?? ''}` : ''
    },
    (next, previous) => { if (previous && next !== previous) void refresh() },
  )

  /**
   * Run care now through the schedule's run-now path. A run already in progress
   * is not started twice: the answer says so and the state shows Running.
   */
  async function runAgain(): Promise<{ ok: boolean; message: string }> {
    const ws = workspace.value
    if (!ws || starting.value) return { ok: false, message: '' }
    starting.value = true
    try {
      await load(ws)
      const current = report.value
      if (!current?.schedule) return { ok: false, message: 'Workspace care is not set up for this workspace.' }
      if (current.running) return { ok: true, message: 'Care is already running.' }
      try {
        await tasks.runScheduleNow(current.schedule.id)
      } catch (cause) {
        return { ok: false, message: `Care did not start: ${cause instanceof Error ? cause.message : String(cause)}` }
      }
      void tasks.fetchSchedules().catch(() => {})
      await load(ws)
      // The dispatch stamps the schedule before this read, but a slow store can
      // answer first; the next poll corrects a Running shown too early.
      if (workspace.value === ws && report.value && !report.value.running) {
        report.value = { ...report.value, running: { since: new Date().toISOString(), step: '', index: 0, total: 0 } }
        schedulePoll()
      }
      return { ok: true, message: 'Care started.' }
    } finally {
      starting.value = false
    }
  }

  return { report, workspace, settled, error, starting, show, refresh, attach, runAgain }
})
