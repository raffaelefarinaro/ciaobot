<template>
  <div class="schedule-panel">
    <PaneHeader
      v-if="!schedule && !showNew"
      page-tag="Automations"
      @open-sidebar="emit('open-sidebar')"
    />
    <PaneHeader v-else page-tag="Automations" @open-sidebar="emit('open-sidebar')">
      <template #title>
        <div class="header-left">
          <button class="btn-icon close-btn desktop-only" @click="closeSchedule" title="Close" aria-label="Close automation">&times;</button>
          <span v-if="schedule" class="pane-title">{{ schedule.title || promptTitle(schedule.prompt) }}</span>
          <span v-else-if="showNew" class="pane-title">New automation</span>
        </div>
      </template>
      <template #actions>
        <template v-if="schedule">
          <!-- One primary (Run now), the pause toggle beside it, and the rare
               destructive action behind the menu instead of a red button in
               the header. On narrow panes the toggle joins the menu too. -->
          <button
            class="btn-small btn-run"
            :class="{ 'btn-running': showRunning }"
            :disabled="isStarting && !runningChatId"
            @click="onRunButtonClick"
          >{{ showRunning ? 'Running...' : 'Run now' }}</button>
          <button class="btn-small desktop-only" @click="onToggleEnabled">
            {{ enabledToggleLabel(schedule) }}
          </button>
          <DropdownMenuRoot :modal="false">
            <DropdownMenuTrigger as-child>
              <button
                type="button"
                class="btn-icon overflow-trigger"
                :class="{ 'mobile-only-trigger': schedule.scope === 'system' }"
                aria-label="More automation actions"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" />
                </svg>
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuPortal>
              <DropdownMenuContent
                as-child
                align="end"
                :side-offset="6"
                :collision-padding="8"
              >
                <!-- as-child, so the panel is this template's own element and
                     picks up the scoped styles even though it is portalled. -->
                <div class="schedule-actions-menu">
                <DropdownMenuItem as-child>
                  <button type="button" class="menu-toggle-item" @click="onToggleEnabled">
                    {{ enabledToggleLabel(schedule) }}
                  </button>
                </DropdownMenuItem>
                <DropdownMenuItem v-if="schedule.scope !== 'system'" as-child>
                  <button type="button" class="danger" @click="onDelete">Delete automation…</button>
                </DropdownMenuItem>
                </div>
              </DropdownMenuContent>
            </DropdownMenuPortal>
          </DropdownMenuRoot>
        </template>

      </template>
    </PaneHeader>

    <div
      v-if="store.schedulesLoaded && store.scheduleLoadError"
      class="schedule-stale"
      role="status"
    >
      <span>Could not refresh automations. Showing the last successful load.</span>
      <button
        type="button"
        class="btn-small"
        :disabled="store.scheduleLoading"
        @click="refreshSchedules"
      >{{ store.scheduleLoading ? 'Retrying…' : 'Retry' }}</button>
    </div>

    <!-- New automation form -->
    <div v-if="showNew" class="scroll-body scroll-body--padded">
      <details class="field-info field-info--block">
        <summary aria-label="What's possible with automations" title="What's possible with automations">i</summary>
        <div class="field-info-panel">
          <p>
            An automation dispatches its prompt on a cadence you choose: at a time
            of day (daily, weekly, monthly, or once), every N minutes, or only
            when you click Run.
          </p>
          <p>
            Point it at a <strong>project</strong> and each run opens a fresh chat
            there with its own model and provider. Point it at a <strong>chat</strong>
            and every run continues that conversation, inheriting its model and
            mode — the way to keep context between runs.
          </p>
          <p>
            Missed time-of-day runs (the app was off when one was due, or a run
            stopped before it finished) are caught up on the next launch.
            Interval runs are not replayed; their cadence just resumes. Any
            automation can be run on demand, and paused without deleting it.
          </p>
          <p>
            Set <strong>archive behavior</strong> to automatic and a classifier
            reviews each clean run — routine results with nothing to judge (no
            proposals, decisions, or warnings) get archived out of the way.
          </p>
        </div>
      </details>
      <NewScheduleForm @created="onCreated" />
    </div>

    <div
      v-else-if="store.scheduleLoadError && !store.schedulesLoaded"
      class="schedule-load-state schedule-load-state--error"
      role="alert"
    >
      <p>Could not load automations. {{ store.scheduleLoadError }}</p>
      <button type="button" class="btn-small" @click="refreshSchedules">Retry</button>
    </div>

    <div
      v-else-if="!store.schedulesLoaded"
      class="schedule-load-state"
      role="status"
    >
      Loading automations…
    </div>

    <!-- Detail -->
    <div v-else-if="schedule" class="scroll-body">
      <div class="page-grid schedule-grid">
      <div class="page-main">
      <p v-if="!schedule.enabled" class="disabled-banner">
        {{ isIntervalSchedule(schedule) ? 'Stopped' : 'Paused' }} — it won't run
        on its own. "Run now" still works.
      </p>
      <div class="prop-cards">
        <!-- Schedule -->
        <section class="prop-card prop-card-wide" :class="{ 'card-editing': editingCard === 'schedule' }">
          <div class="prop-card-head">
            <h2 class="prop-card-name">When</h2>
            <span v-if="editingCard === 'schedule'" class="esc-hint"><kbd>Esc</kbd> cancels</span>
            <button
              v-else-if="canEditSchedule && !editingCard"
              type="button"
              class="card-edit"
              :aria-label="'Edit schedule'"
              @click="startCardEdit('schedule')"
            >Edit</button>
          </div>

          <dl v-if="editingCard !== 'schedule'" class="prop-rows">
            <div class="prop-row">
              <dt>Repeats</dt><dd>{{ frequencyLabel(schedule) }}</dd>
            </div>
            <div v-if="showsTimeOfDay(schedule)" class="prop-row">
              <dt>At</dt><dd>{{ schedule.daily_time_utc }} · {{ schedule.timezone_name }}</dd>
            </div>
            <div v-if="schedule.frequency !== 'manual'" class="prop-row">
              <dt>Next run</dt><dd>{{ nextRunLabel(schedule) }}</dd>
            </div>
            <!-- Interval cadence has no expected slot, so the missed-run check
                 cannot report its health. last_status does instead. A
                 wall-clock entry shows the row too when its last dispatch
                 failed. The missed-run check now counts a failed run as an
                 unserved slot (issue #486), but it still waits out the
                 5-minute grace, and this row names the failure rather than
                 leaving it to a badge. -->
            <div
              v-if="isIntervalSchedule(schedule) || schedule.last_status === 'error' || schedule.last_status === 'skipped'"
              class="prop-row"
            >
              <dt>Status</dt><dd>{{ intervalStatusLabel(schedule) }}</dd>
            </div>
            <div class="prop-row">
              <dt>Last run</dt>
              <dd>
                {{ schedule.last_dispatched_at ? formatWhen(schedule.last_dispatched_at) : (schedule.last_triggered_on || 'never') }}
                <span
                  v-if="showRunning && (schedule.last_dispatched_at || schedule.last_run_chat_id)"
                  class="spinner-dot"
                  title="Running now"
                />
                <router-link
                  v-if="schedule.last_run_chat_id"
                  class="text-link"
                  :to="`/chat/${schedule.last_run_chat_id}`"
                > · open result</router-link>
              </dd>
            </div>
          </dl>

          <div v-else class="card-form">
            <div class="form-grid">
              <div class="form-group">
                <label :for="`${uid}-frequency`">Repeats</label>
                <select :id="`${uid}-frequency`" v-model="editData.frequency">
                  <option value="daily">Daily</option>
                  <option value="weekly">Weekly</option>
                  <option value="monthly">Monthly</option>
                  <option value="interval">Every N minutes</option>
                  <option value="manual">Manual (run on click only)</option>
                </select>
              </div>
              <div v-if="editData.frequency === 'interval'" class="form-group">
                <label :for="`${uid}-interval`">Every (minutes)</label>
                <input :id="`${uid}-interval`" v-model.number="editData.interval_minutes" type="number" min="1" />
              </div>
              <div v-if="editShowsTimeOfDay" class="form-group">
                <label :for="`${uid}-time`">Time</label>
                <input :id="`${uid}-time`" v-model="editData.time" type="time" :aria-invalid="cardEditBlocked || undefined" />
                <!-- Switching an interval entry to a wall-clock cadence leaves
                     this empty; without a time the automation would save as
                     enabled and never fire. -->
                <p v-if="cardEditBlocked" class="field-hint field-hint--warn">
                  Pick a time — this cadence needs one to run.
                </p>
              </div>
              <div v-if="editShowsTimeOfDay" class="form-group">
                <label :for="`${uid}-timezone`">Timezone</label>
                <select :id="`${uid}-timezone`" v-model="editData.timezone">
                  <option value="Europe/Zurich">Europe/Zurich</option>
                  <option value="Europe/Rome">Europe/Rome</option>
                  <option value="UTC">UTC</option>
                  <option value="America/New_York">US East</option>
                  <option value="America/Los_Angeles">US West</option>
                  <option value="Asia/Tokyo">Tokyo</option>
                </select>
              </div>
            </div>
            <div v-if="editData.frequency === 'weekly'" class="form-group">
              <label :id="`${uid}-days-label`">Days</label>
              <div class="days-row" role="group" :aria-labelledby="`${uid}-days-label`">
                <label v-for="d in allDays" :key="d" class="checkbox-pill" :class="{ active: editData.days_of_week.includes(d) }">
                  <input type="checkbox" :value="d" v-model="editData.days_of_week" hidden />
                  {{ d }}
                </label>
              </div>
            </div>
            <div v-if="editData.frequency === 'monthly'" class="form-group">
              <label :for="`${uid}-day-of-month`">Day of month</label>
              <input :id="`${uid}-day-of-month`" v-model.number="editData.day_of_month" type="number" min="1" max="31" placeholder="1-31" />
            </div>
            <div class="card-actions">
              <span v-if="cardDirty" class="dirty-flag"><span class="dirty-dot" />Unsaved</span>
              <button class="btn-chip" @click="cancelCardEdit">Cancel</button>
              <button class="btn-primary" :disabled="!cardDirty || !cardEditValid" @click="saveCardEdit">Save</button>
            </div>
          </div>
        </section>

        <!-- Delivery -->
        <section class="prop-card" :class="{ 'card-editing': editingCard === 'delivery' }">
          <div class="prop-card-head">
            <h2 class="prop-card-name">Where it runs</h2>
            <span v-if="editingCard === 'delivery'" class="esc-hint"><kbd>Esc</kbd> cancels</span>
            <button
              v-else-if="canEditSchedule && !editingCard"
              type="button"
              class="card-edit"
              :aria-label="'Edit delivery'"
              @click="startCardEdit('delivery')"
            >Edit</button>
          </div>

          <template v-if="editingCard !== 'delivery'">
            <dl class="prop-rows">
              <div class="prop-row">
                <dt>Workspace</dt><dd>{{ workspaceDisplayName(schedule.workspace) }}</dd>
              </div>
              <div class="prop-row">
                <dt>Deliver to</dt>
                <dd :class="{ 'context-unavailable': contextUnavailable(schedule) }">
                  {{ contextLabel(schedule) }}
                </dd>
              </div>
            </dl>
            <p v-if="contextUnavailable(schedule)" class="context-help">
              This target no longer exists — edit to choose an available one.
            </p>
            <div v-if="schedule.scope === 'system'" class="system-workspace-control">
              <!-- A per-workspace routine already has one row per workspace, so
                   its workspace is identity, not a setting: offering to move it
                   would collide with the sibling row. Only a global routine —
                   one whose subject is a shared artifact — gets the choice. -->
              <template v-if="isPerWorkspaceRoutine(schedule)">
                <p class="hint">
                  This routine runs once per workspace. You are looking at the
                  {{ workspaceDisplayName(schedule.workspace) }} run; each one can be
                  paused on its own and uses that workspace's provider and default model unless you set an override in Engine.
                </p>
              </template>
              <template v-else>
                <div class="form-group">
                  <label>Run this routine in</label>
                  <select :value="schedule.workspace" @change="onSystemWorkspaceChange">
                    <option v-for="workspace in projectStore.workspaceOptions" :key="workspace.name" :value="workspace.name">
                      {{ workspaceDisplayName(workspace.name) }}
                    </option>
                  </select>
                </div>
                <p class="hint">Uses that workspace's provider and default model unless you set an override in Engine.</p>
              </template>
            </div>
          </template>

          <div v-else class="card-form">
            <div class="form-group">
              <label>Workspace</label>
              <select v-model="editData.workspace" @change="onEditWorkspaceChange">
                <option v-for="workspace in projectStore.workspaceOptions" :key="workspace.name" :value="workspace.name">
                  {{ workspaceDisplayName(workspace.name) }}
                </option>
              </select>
            </div>
            <div class="form-group">
              <label>Deliver to</label>
              <select v-model="editData.contextKey">
                <optgroup v-for="group in contextGroups" :key="group.label" :label="group.label">
                  <option v-for="ctx in group.items" :key="ctx.key" :value="ctx.key">
                    {{ ctx.label || ctx.key }}
                  </option>
                </optgroup>
              </select>
            </div>
            <div class="card-actions">
              <span v-if="cardDirty" class="dirty-flag"><span class="dirty-dot" />Unsaved</span>
              <button class="btn-chip" @click="cancelCardEdit">Cancel</button>
              <button class="btn-primary" :disabled="!cardDirty || !cardEditValid" @click="saveCardEdit">Save</button>
            </div>
          </div>
        </section>

        <!-- Engine -->
        <section class="prop-card" :class="{ 'card-editing': editingCard === 'engine' }">
          <div class="prop-card-head">
            <h2 class="prop-card-name">Model</h2>
            <span v-if="editingCard === 'engine'" class="esc-hint"><kbd>Esc</kbd> cancels</span>
            <button
              v-else-if="canEditEngine && !editingCard"
              type="button"
              class="card-edit"
              :aria-label="'Edit engine'"
              @click="startCardEdit('engine')"
            >Edit</button>
          </div>

          <dl v-if="editingCard !== 'engine'" class="prop-rows">
            <div class="prop-row">
              <dt>Model</dt><dd>{{ modelLabel(schedule) }}</dd>
            </div>
            <div class="prop-row">
              <dt>Provider</dt><dd>{{ providerLabel(schedule) }}</dd>
            </div>
          </dl>

          <div v-else class="card-form">
            <!-- An interval run into one existing chat inherits that chat's
                 model and mode — prepare_schedule_chat skips the override — so
                 offering a picker here reported a setting that never took
                 effect. Same rule NewScheduleForm applies on create. -->
            <p v-if="inheritsChatModel" class="hint">
              This automation runs inside its chat, so every run uses that
              chat's own model. Change it from the chat's model picker.
            </p>
            <template v-else>
              <div class="form-group">
                <label>Model</label>
                <ModelSelector
                  :model-value="editData.model"
                  :sections="scheduleModelSections"
                  :placeholder="editInheritedModelLabel"
                  :empty-placeholder="editInheritedModelLabel"
                  @select="selectScheduleModel"
                />
              </div>
              <p class="hint">Leave empty to inherit the workspace default.</p>
            </template>
            <div class="card-actions">
              <span v-if="cardDirty" class="dirty-flag"><span class="dirty-dot" />Unsaved</span>
              <button class="btn-chip" @click="cancelCardEdit">Cancel</button>
              <button class="btn-primary" :disabled="!cardDirty || !cardEditValid" @click="saveCardEdit">Save</button>
            </div>
          </div>
        </section>

        <!-- Advanced -->
        <section class="prop-card" :class="{ 'card-editing': editingCard === 'advanced' }">
          <div class="prop-card-head">
            <h2 class="prop-card-name">Results</h2>
            <span v-if="editingCard === 'advanced'" class="esc-hint"><kbd>Esc</kbd> cancels</span>
            <button
              v-else-if="canEditAdvanced && !editingCard"
              type="button"
              class="card-edit"
              :aria-label="'Edit advanced settings'"
              @click="startCardEdit('advanced')"
            >Edit</button>
          </div>

          <dl v-if="editingCard !== 'advanced'" class="prop-rows">
            <div class="prop-row">
              <dt>Afterwards</dt><dd>{{ scheduleSupportsAutoArchive(schedule) ? archiveLabel(schedule.archive_policy) : 'Kept — it runs inside one chat' }}</dd>
            </div>
          </dl>

          <div v-else class="card-form">
            <div v-if="editSupportsAutoArchive" class="form-group">
              <label>Archive behavior</label>
              <select v-model="editData.archive_policy">
                <option value="manual">Manual, keep as normal chat</option>
                <option value="auto">Automatically archive routine results</option>
              </select>
            </div>
            <p v-if="editSupportsAutoArchive" class="hint">
              Auto runs a post-run classifier. If it finds proposals, decisions, warnings, or
              anything useful for the user to judge, the chat stays visible.
            </p>
            <p v-else class="hint">
              Runs into one existing chat, so the conversation is kept —
              auto-archive does not apply to this binding.
            </p>
            <div class="card-actions">
              <span v-if="cardDirty" class="dirty-flag"><span class="dirty-dot" />Unsaved</span>
              <button class="btn-chip" @click="cancelCardEdit">Cancel</button>
              <button class="btn-primary" :disabled="!cardDirty || !cardEditValid" @click="saveCardEdit">Save</button>
            </div>
          </div>
        </section>
      </div>

      <!-- Name, description and prompt -->
      <div v-if="editingCard === 'content'" class="card-form content-form">
        <div class="prop-card-head">
          <h2 class="prop-card-name">Prompt</h2>
          <span class="esc-hint"><kbd>Esc</kbd> cancels</span>
        </div>
        <div class="form-group">
          <label>Name</label>
          <input v-model="editData.title" type="text" placeholder="e.g. Weekly customer intel sweep" />
        </div>
        <div class="form-group">
          <label>Description <span class="label-hint">(shown above the prompt)</span></label>
          <textarea v-model="editData.description" rows="2" placeholder="Optional plain-language summary"></textarea>
        </div>
        <div class="form-group">
          <label>Prompt</label>
          <textarea v-model="editData.prompt" rows="10"></textarea>
        </div>
        <div class="card-actions">
          <span v-if="cardDirty" class="dirty-flag"><span class="dirty-dot" />Unsaved</span>
          <button class="btn-chip" @click="cancelCardEdit">Cancel</button>
          <button class="btn-primary" :disabled="!cardDirty || !cardEditValid" @click="saveCardEdit">Save</button>
        </div>
      </div>

      <section v-else class="prompt-display">
        <div class="prompt-heading">
          <h2 class="prompt-label">Prompt</h2>
          <div class="prompt-actions">
            <button type="button" class="text-link" @click="copyPrompt(schedule.prompt, schedule.schedule_id)">
              {{ promptCopyLabel(schedule.schedule_id) }}
            </button>
            <button v-if="canEditSchedule && !editingCard" type="button" class="text-link" @click="startCardEdit('content')">Edit</button>
          </div>
        </div>
        <p v-if="schedule.description" class="schedule-description">{{ schedule.description }}</p>
        <p class="full-prompt">{{ schedule.prompt }}</p>
      </section>
      </div>

      <!-- Rail: this automation's own run state. The job-run log covers
           background jobs, not routine runs, so there are no per-routine
           totals to show - only what the schedule itself records. -->
      <aside class="page-rail" aria-labelledby="schedule-status-title">
        <h2 id="schedule-status-title" class="rail-title">Status</h2>
        <div class="rail-kvs">
          <div class="rail-kv">
            <span>State</span>
            <strong :class="{ 'rail-attention': scheduleNeedsAttention(schedule) }">{{ scheduleStateLabel(schedule) }}</strong>
          </div>
          <div v-if="schedule.frequency !== 'manual'" class="rail-kv">
            <span>Next run</span><strong>{{ schedule.enabled && schedule.next_run ? relativeWhen(schedule.next_run, schedule.timezone_name) : '—' }}</strong>
          </div>
          <div class="rail-kv">
            <span>Last run</span><strong>{{ schedule.last_dispatched_at ? relativeWhen(schedule.last_dispatched_at) : (schedule.last_triggered_on || 'never') }}</strong>
          </div>
        </div>
        <div v-if="schedule.last_run_chat_id" class="rail-list rail-runs">
          <router-link class="rail-item" :to="`/chat/${schedule.last_run_chat_id}`">
            Last result
            <small>{{ lastRunChatTitle(schedule) }}</small>
          </router-link>
        </div>
        <p v-if="schedule.missed" class="rail-note rail-note--attention">
          A run was due {{ formatWhen(schedule.last_expected_run) }} and didn't happen. It is caught up on the next launch, or use Run now.
        </p>
      </aside>
      </div>
    </div>

    <!-- Overview: shown when nothing is selected but automations exist -->
    <div v-else-if="workspaceSchedules.length" ref="overviewEl" class="scroll-body overview-body">
      <div class="page-grid">
        <div class="page-main">
          <section v-if="missedSchedules.length" class="ov-section ov-section--alert" aria-labelledby="ov-missed-title">
            <div class="ov-head">
              <h2 id="ov-missed-title">Missed</h2>
              <span class="ov-hint">expected to run, didn't</span>
              <button
                type="button"
                class="btn-small ov-run-all"
                :disabled="runningAllMissed"
                :aria-busy="runningAllMissed"
                @click.stop="runAllMissed"
              >{{ runningAllMissed ? 'Starting…' : 'Run all' }}</button>
            </div>
            <router-link
              v-for="s in missedSchedules"
              :key="s.schedule_id"
              :to="`/schedules/${s.schedule_id}`"
              class="ov-item"
            >
              <span class="ov-main">
                <span class="ov-title">{{ s.title || promptTitle(s.prompt) }}</span>
                <span class="ov-sub">{{ cadenceSummary(s) }}</span>
              </span>
              <span class="ov-when ov-when--alert">due {{ relativeWhen(s.last_expected_run) }}</span>
            </router-link>
          </section>

          <section class="ov-section" aria-labelledby="ov-next-title">
            <div class="ov-head">
              <h2 id="ov-next-title" title="Soonest first. Missed time-of-day runs are caught up on the next launch; interval runs just resume.">Next up</h2>
            </div>
            <router-link
              v-for="s in upcomingRoutines"
              :key="s.schedule_id"
              :to="`/schedules/${s.schedule_id}`"
              class="ov-item"
            >
              <span class="ov-main">
                <span class="ov-title">
                  {{ s.title || promptTitle(s.prompt) }}
                  <span v-if="s.web_chat_id && projectStore.isChatStreaming(s.web_chat_id)" class="spinner-dot" title="Running now" />
                </span>
                <span class="ov-sub">{{ cadenceSummary(s) }} · {{ deliverySummary(s) }}</span>
              </span>
              <span class="ov-when">{{ relativeWhen(s.next_run, s.timezone_name) }}</span>
            </router-link>
            <p v-if="!upcomingRoutines.length" class="ov-empty">
              Nothing scheduled. The rest run only when you click Run now, or are paused.
            </p>
          </section>

          <section v-if="pausedRoutines.length" class="ov-section" aria-labelledby="ov-paused-title">
            <div class="ov-head"><h2 id="ov-paused-title">Paused</h2></div>
            <router-link
              v-for="s in pausedRoutines"
              :key="s.schedule_id"
              :to="`/schedules/${s.schedule_id}`"
              class="ov-item ov-item--paused"
            >
              <span class="ov-main">
                <span class="ov-title">{{ s.title || promptTitle(s.prompt) }}</span>
                <span class="ov-sub">{{ cadenceSummary(s) }} · paused</span>
              </span>
            </router-link>
          </section>

          <section v-if="systemRoutines.length" class="ov-section" aria-labelledby="ov-system-title">
            <div class="ov-head"><h2 id="ov-system-title">System routines</h2></div>
            <p class="ov-intro">Ciaobot's own upkeep. They run on their own and never need you.</p>
            <router-link
              v-for="s in systemRoutines"
              :key="s.schedule_id"
              :to="`/schedules/${s.schedule_id}`"
              class="ov-item"
              :class="{ 'ov-item--paused': !s.enabled }"
            >
              <span class="ov-main">
                <span class="ov-title">{{ s.title || promptTitle(s.prompt) }}</span>
                <span class="ov-sub">{{ cadenceSummary(s) }}<template v-if="!s.enabled"> · paused</template></span>
              </span>
              <span v-if="s.enabled && s.next_run" class="ov-when">{{ relativeWhen(s.next_run, s.timezone_name) }}</span>
            </router-link>
          </section>
        </div>

        <aside class="page-rail" aria-labelledby="ov-rail-title">
          <h2 id="ov-rail-title" class="rail-title">In this workspace</h2>
          <div class="rail-kvs">
            <div class="rail-kv"><span>Active</span><strong>{{ activeCount }}</strong></div>
            <div class="rail-kv"><span>Paused</span><strong>{{ pausedCount }}</strong></div>
          </div>

          <template v-if="needsLook.length">
            <h2 class="rail-title rail-title--spaced">Needs a look</h2>
            <div class="rail-list">
              <router-link
                v-for="s in needsLook"
                :key="s.schedule_id"
                :to="`/schedules/${s.schedule_id}`"
                class="rail-item"
              >
                {{ s.title || promptTitle(s.prompt) }}
                <small class="rail-attention">{{ s.missed ? 'missed a run' : intervalStatusLabel(s) }}</small>
              </router-link>
            </div>
          </template>

          <template v-if="recentRuns.length">
            <h2 class="rail-title rail-title--spaced">Recent runs</h2>
            <div class="rail-list">
              <router-link
                v-for="r in recentRuns"
                :key="r.id"
                :to="`/chat/${r.chatId}`"
                class="rail-item"
              >
                {{ r.title }}
                <small>{{ relativeWhen(r.lastRunAt) }}<template v-if="projectStore.isChatStreaming(r.chatId)"> · running</template></small>
              </router-link>
            </div>
          </template>
        </aside>
      </div>
    </div>

    <div v-else class="empty-state">
      <h2>No automations yet</h2>
      <p class="empty-hint">
        An automation runs a prompt on a schedule — at a time of day or every
        N minutes — in a new chat each time, or continuing one you pick. Start
        one with <strong>New Automation</strong> in the sidebar.
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, useId, watch } from 'vue'
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuRoot,
  DropdownMenuTrigger,
} from 'reka-ui'
import { bindsFixedChat, contextBindsFixedChat, scheduleSupportsAutoArchive } from '../lib/scheduleBinding'
import { useRoute, useRouter } from 'vue-router'
import { useTaskStore } from '../stores/tasks'
import type { ScheduleUpdate } from '../stores/tasks'
import { useProjectStore } from '../stores/projects'
import type { RuntimeProvider, Schedule, ScheduleArchivePolicy } from '../lib/types'
import NewScheduleForm from './NewScheduleForm.vue'
import PaneHeader from './PaneHeader.vue'
import ModelSelector from './ModelSelector.vue'
import { providerForModelSection, sectionsFromModelsResponse } from '../lib/modelSections'
import { isPerWorkspaceRoutine, scheduleInWorkspace, workspaceForSchedule } from '../lib/automationWorkspace'
import { askConfirm } from '../lib/confirm'
import { writeClipboard } from '../lib/codeCopy'

const props = defineProps<{ showNew?: boolean }>()
const emit = defineEmits<{ (e: 'created'): void; (e: 'open-sidebar'): void; (e: 'close'): void }>()

const route = useRoute()
const router = useRouter()
const store = useTaskStore()
const uid = useId()
const projectStore = useProjectStore()

const allDays = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
// Properties are edited one card at a time ('' = nothing being edited), so the
// rest of the panel stays readable and the way out is always on screen.
type EditCard = '' | 'schedule' | 'delivery' | 'engine' | 'advanced' | 'content'
const editingCard = ref<EditCard>('')
// Serialised editData as it looked when the card opened, for the dirty check.
const editBaseline = ref('')
const copiedPromptKey = ref('')
const startingBySchedule = ref<Set<string>>(new Set())
const runningAllMissed = ref(false)
// schedule_id -> chat_id while the linked chat is still streaming
const runningBySchedule = ref<Record<string, string>>({})
const editData = ref({
  workspace: '',
  title: '',
  description: '',
  time: '',
  prompt: '',
  timezone: 'Europe/Zurich',
  frequency: 'daily',
  interval_minutes: 10,
  days_of_week: [] as string[],
  day_of_month: null as number | null,
  contextKey: '',
  model: '',
  archive_policy: 'manual' as ScheduleArchivePolicy,
})

// Schedules change state server-side (last_status / next_run / missed), so
// refresh them periodically while the panel is open. next_run is always
// computed forward from "now" on the server, so a tab left open across a fire
// time (or asleep overnight) otherwise keeps showing a stale "next run" that
// has drifted into the past.
let pollTimer: number | undefined
let copiedPromptTimer: number | undefined

function refreshSchedules() {
  store.fetchSchedules().catch(() => {})
}

function onVisibilityChange() {
  if (document.visibilityState === 'visible') refreshSchedules()
}

// Escape always leaves the open card. Stop it here so it doesn't also fall
// through to whatever else listens for Escape and close the whole panel.
function onEditKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !editingCard.value) return
  event.stopPropagation()
  event.preventDefault()
  void cancelCardEdit()
}

onMounted(() => {
  if (!store.models) store.fetchModels()
  if (!store.schedulesLoaded) refreshSchedules()
  pollTimer = window.setInterval(refreshSchedules, 30_000)
  document.addEventListener('visibilitychange', onVisibilityChange)
  document.addEventListener('keydown', onEditKeydown, true)
})

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
  if (copiedPromptTimer !== undefined) window.clearTimeout(copiedPromptTimer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  document.removeEventListener('keydown', onEditKeydown, true)
})

const scheduleId = computed(() => (route.params.scheduleId as string) || '')
const schedule = computed(() =>
  store.schedules.find(s => s.schedule_id === scheduleId.value) || null,
)
watch(schedule, (s) => {
  // An entry migrated from a loop may carry no workspace of its own; the
  // helper falls back to the bound chat's, which is where loops kept it.
  const targetWs = s
    ? workspaceForSchedule(s, projectStore.chats, projectStore.projects)
    : undefined
  if (targetWs && targetWs !== projectStore.activeWorkspace) {
    projectStore.activeWorkspace = targetWs
  }
}, { immediate: true })

watch(scheduleId, () => {
  editingCard.value = ''
  editBaseline.value = ''
  copiedPromptKey.value = ''
  purgeFinishedRuns()
})

const isStarting = computed(() =>
  scheduleId.value ? startingBySchedule.value.has(scheduleId.value) : false,
)

const runningChatId = computed(() =>
  scheduleId.value ? runningBySchedule.value[scheduleId.value] : undefined,
)

const showRunning = computed(() => {
  if (isStarting.value) return true
  const chatId = runningChatId.value
  return chatId ? projectStore.isChatStreaming(chatId) : false
})

function purgeFinishedRuns() {
  const next: Record<string, string> = {}
  for (const [sid, chatId] of Object.entries(runningBySchedule.value)) {
    if (projectStore.isChatStreaming(chatId)) next[sid] = chatId
  }
  runningBySchedule.value = next
}

const lastStreamingBySchedule = ref<Record<string, boolean>>({})

watch(
  () => Object.entries(runningBySchedule.value).map(([sid, chatId]) => ({
    sid,
    streaming: projectStore.isChatStreaming(chatId),
  })),
  (entries) => {
    const next = { ...runningBySchedule.value }
    let changed = false
    for (const { sid, streaming } of entries) {
      const wasStreaming = lastStreamingBySchedule.value[sid] ?? false
      lastStreamingBySchedule.value[sid] = streaming
      if (wasStreaming && !streaming && next[sid]) {
        delete next[sid]
        changed = true
      }
    }
    if (changed) runningBySchedule.value = next
  },
  { deep: true },
)

// Overview (homepage): soonest upcoming runs and missed runs (expected to
// fire, no trigger recorded — flagged server-side via the `missed` field).
const workspaceSchedules = computed(() =>
  store.schedules.filter(s => scheduleInWorkspace(
    s,
    projectStore.activeWorkspace,
    projectStore.chats,
    projectStore.projects,
  )),
)
const missedSchedules = computed(() => workspaceSchedules.value.filter(s => s.missed))

// The overview splits the user's own routines from Ciaobot's system ones, and
// keeps paused routines out of "Next up": they have no run coming.
const userRoutines = computed(() => workspaceSchedules.value.filter(s => s.scope !== 'system'))
const upcomingRoutines = computed(() =>
  userRoutines.value
    .filter(s => s.enabled && s.next_run)
    .sort((a, b) => Date.parse(a.next_run!) - Date.parse(b.next_run!))
    .slice(0, 8),
)
const pausedRoutines = computed(() => userRoutines.value.filter(s => !s.enabled))
const systemRoutines = computed(() =>
  workspaceSchedules.value
    .filter(s => s.scope === 'system')
    .sort((a, b) => Date.parse(a.next_run || '') - Date.parse(b.next_run || '') || 0),
)
const activeCount = computed(() => workspaceSchedules.value.filter(s => s.enabled).length)
const pausedCount = computed(() => workspaceSchedules.value.filter(s => !s.enabled).length)
// Schedules whose own record says something went wrong: a missed slot, or a
// last dispatch that failed, stopped short, or lost its chat.
const needsLook = computed(() => workspaceSchedules.value.filter(scheduleNeedsAttention))

const recentRuns = computed(() =>
  workspaceSchedules.value
    .filter(s => s.last_dispatched_at && s.last_run_chat_id)
    .map(s => ({
      id: s.schedule_id,
      title: s.title || promptTitle(s.prompt),
      lastRunAt: s.last_dispatched_at!,
      chatId: s.last_run_chat_id!,
    }))
    // Compared as instants, not as strings. `last_dispatched_at` is written in
    // two offsets — UTC from the interval and run-now paths, the entry's own
    // timezone from the wall-clock tick/catch-up — and ISO strings only sort
    // correctly at a fixed offset, so an evening-local stamp sorted above a
    // genuinely newer UTC one and could push it off the five-row list.
    .sort((a, b) => Date.parse(b.lastRunAt) - Date.parse(a.lastRunAt))
    .slice(0, 5),
)

function archiveLabel(policy: ScheduleArchivePolicy | undefined): string {
  return policy === 'auto'
    ? 'Archived when there is nothing to judge'
    : 'Kept as a normal chat'
}

function formatWhen(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const diffMs = d.getTime() - Date.now()
  const past = diffMs < 0
  const absMin = Math.round(Math.abs(diffMs) / 60000)
  let rel: string
  if (absMin < 1) rel = 'now'
  else if (absMin < 60) rel = `${absMin}m`
  else if (absMin < 60 * 24) rel = `${Math.round(absMin / 60)}h`
  else rel = `${Math.round(absMin / 1440)}d`
  const clock = d.toLocaleString(undefined, {
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
  if (rel === 'now') return clock
  return past ? `${clock} (${rel} ago)` : `${clock} (in ${rel})`
}

// Short relative form for rows and the rail: "Fri 10:00 · in 10h".
// Upcoming slots are shown in the automation's own timezone, the one its
// "At" row names, so the list, the rail and the detail rows agree.
function relativeWhen(iso: string | null | undefined, timeZone?: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const diffMs = d.getTime() - Date.now()
  const absMin = Math.round(Math.abs(diffMs) / 60000)
  let clock: string
  try {
    clock = d.toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false, timeZone })
  } catch {
    clock = d.toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false })
  }
  if (absMin < 1) return `${clock} · now`
  let rel: string
  if (absMin < 60) rel = `${absMin}m`
  else if (absMin < 60 * 24) rel = `${Math.round(absMin / 60)}h`
  else rel = `${Math.round(absMin / 1440)}d`
  return diffMs < 0 ? `${clock} · ${rel} ago` : `${clock} · in ${rel}`
}

// Plain cadence for a row's sub-line: "Every day at 08:00", "Every 30 min".
function cadenceSummary(s: Schedule): string {
  const at = s.daily_time_utc ? ` at ${s.daily_time_utc}` : ''
  if (s.frequency === 'manual') return 'Only when you run it'
  if (s.frequency === 'interval') return `Every ${s.interval_minutes} min`
  if (s.frequency === 'once') return s.run_at_date ? `Once on ${s.run_at_date}${at}` : `Once${at}`
  if (s.frequency === 'monthly') return `Monthly on day ${s.day_of_month}${at}`
  if (s.frequency === 'weekly') {
    return s.days_of_week?.length
      ? `Weekly on ${s.days_of_week.map(d => d.charAt(0).toUpperCase() + d.slice(1)).join(', ')}${at}`
      : `Weekly${at}`
  }
  return `Every day${at}`
}

// Where each run lands, in the sub-line's words.
function deliverySummary(s: Schedule): string {
  if (s.web_chat_id) {
    const chat = projectStore.chats.find(c => c.chat_id === s.web_chat_id)
    return chat ? `continues “${chat.title || 'Untitled chat'}”` : 'continues one chat'
  }
  if (s.web_project_id) {
    const proj = projectStore.projects.find(p => p.project_id === s.web_project_id)
    if (proj) return `new chat in ${proj.name}`
  }
  return s.context_label ? `new chat in ${s.context_label}` : 'new chat each run'
}

function scheduleNeedsAttention(s: Schedule): boolean {
  return Boolean(s.missed)
    || s.last_status === 'error'
    || s.last_status === 'skipped'
    || s.last_status === 'missing-chat'
}

function scheduleStateLabel(s: Schedule): string {
  if (s.missed) return 'Missed a run'
  if (!s.enabled) return isIntervalSchedule(s) ? 'Stopped' : 'Paused'
  if (s.last_status === 'running' || showRunning.value) return 'Running'
  if (scheduleNeedsAttention(s)) return intervalStatusLabel(s).replace(/^./, c => c.toUpperCase())
  if (s.frequency === 'manual') return 'Runs on demand'
  return 'Scheduled'
}

function lastRunChatTitle(s: Schedule): string {
  const chat = projectStore.chats.find(c => c.chat_id === s.last_run_chat_id)
  return chat?.title || 'Open the chat it ran in'
}

const scheduleModelSections = computed(() => sectionsFromModelsResponse(store.models))
const editModelProvider = ref<RuntimeProvider | ''>('')

const editWorkspaceConfig = computed(() =>
  projectStore.workspaceOptions.find(workspace => workspace.name === editData.value.workspace),
)
const editInheritedModelLabel = computed(() => {
  const workspace = editWorkspaceConfig.value
  const provider = projectStore.workspaceProviderOptions.find(
    option => option.value === workspace?.default_provider,
  )?.label || workspace?.default_provider || 'provider'
  // Workspaces no longer pin a model: the effective default is the
  // provider's own default from the Models tab.
  const model = store.models?.provider_defaults?.[workspace?.default_provider || '']
    || store.models?.default
    || ''
  return model ? `Inherit ${provider} / ${model}` : `Inherit ${provider} default`
})

function selectScheduleModel(value: string | string[], sectionKey: string) {
  editData.value.model = Array.isArray(value) ? value[0] || '' : value
  editModelProvider.value = providerForModelSection(sectionKey)
}

const contextGroups = computed(() => {
  const groups: { label: string; items: { key: string; label: string }[] }[] = []
  const workspace = editData.value.workspace || schedule.value?.workspace || projectStore.activeWorkspace
  const projects = projectStore.projects.filter(p => p.workspace === workspace)
  const projItems = projects.map(p => ({
    key: `proj:${p.project_id}`,
    label: p.name,
  }))
  if (projItems.length) groups.push({ label: 'Projects (new chat per run)', items: projItems })
  const webItems: { key: string; label: string }[] = []
  for (const p of projects) {
    const pChats = projectStore.projectChats(p.project_id)
    for (const c of pChats) {
      webItems.push({ key: `web:${c.chat_id}`, label: `${p.name} / ${c.title}` })
    }
  }
  if (webItems.length) groups.push({ label: 'Fixed Web Chat', items: webItems })
  return groups
})

function promptTitle(prompt: string): string {
  const first = prompt.split('\n')[0].trim()
  return first.length > 60 ? first.slice(0, 57) + '...' : first
}

function nextRunLabel(s: Schedule): string {
  if (!s.enabled) return isIntervalSchedule(s) ? 'Stopped' : 'Disabled'
  if (!s.next_run) return '—'
  // Interval cadence is measured from the last run, and a run skipped because
  // the chat was busy leaves next_run in the past. Say "as soon as the chat is
  // free" rather than rendering a stale absolute time as if it were upcoming.
  if (isIntervalSchedule(s) && new Date(s.next_run).getTime() <= Date.now()) {
    return s.last_status === 'busy' ? 'when the chat is free' : 'due now'
  }
  try {
    const d = new Date(s.next_run)
    const when = new Intl.DateTimeFormat(undefined, {
      timeZone: s.timezone_name,
      weekday: 'short', day: 'numeric', month: 'short',
      hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(d)
    const rel = relativeWhen(s.next_run, s.timezone_name).split(' · ')[1]
    return rel ? `${when} · ${rel}` : when
  } catch {
    return s.next_run
  }
}

function modelLabel(s: Schedule): string {
  if (isIntervalSchedule(s) && s.web_chat_id) {
    // A chat-bound interval run always uses the chat's own settings; the
    // backend ignores a model stored here, so never present one as active.
    return s.effective_model
      ? `${s.effective_model} (from the target chat)`
      : 'From the target chat'
  }
  if (s.model) return s.model
  const source = s.web_chat_id ? 'the target chat' : 'the workspace default'
  return s.effective_model
    ? `${s.effective_model} · inherits ${source}`
    : `Provider default · inherits ${source}`
}

function workspaceDisplayName(workspace?: string): string {
  return workspace
    ? workspace.charAt(0).toUpperCase() + workspace.slice(1)
    : 'Workspace'
}

function providerLabel(s: Schedule): string {
  const inheritedProvider = s.web_chat_id
    ? s.effective_provider
    : projectStore.workspaceOptions.find(workspace => workspace.name === s.workspace)?.default_provider
      || s.effective_provider
  const providerValue = s.web_chat_id ? inheritedProvider : s.provider || inheritedProvider || 'claude'
  const label = projectStore.workspaceProviderOptions.find(
    option => option.value === providerValue,
  )?.label || providerValue
  if (s.web_chat_id) return `${label} · inherits the target chat`
  return s.provider ? label || s.provider : `${label} · inherits the workspace default`
}

// An interval entry bound to one existing chat runs inside it and inherits its
// model/mode; `prepare_schedule_chat` deliberately skips the override. Mirrors
// NewScheduleForm's `inheritsChatModel`.
const inheritsChatModel = computed(() => {
  const s = schedule.value
  return !!s && bindsFixedChat(s.frequency, s.web_chat_id, s.web_project_id)
})

function isIntervalSchedule(s: Schedule): boolean {
  return s.frequency === 'interval'
}

// Interval cadence is relative and manual never auto-fires, so neither has a
// time of day or a timezone to show.
function showsTimeOfDay(s: Schedule): boolean {
  return s.frequency !== 'manual' && !isIntervalSchedule(s)
}

const editShowsTimeOfDay = computed(
  () => editData.value.frequency !== 'manual' && editData.value.frequency !== 'interval',
)

// Archiving the chat an interval is bound to makes the next run fork a
// replacement and archive that too, so the dispatcher refuses to archive these.
// The setting was still offered and still rendered as "automatic", describing
// behaviour that never happened. A project-bound interval opens a fresh chat
// per run and is exactly what auto-archive is for, so only this binding is out.
const editSupportsAutoArchive = computed(
  () => !contextBindsFixedChat(editData.value.frequency, editData.value.contextKey),
)
watch(editSupportsAutoArchive, (supported) => {
  if (!supported) editData.value.archive_policy = 'manual'
})

// An interval entry has no `daily_time_utc`, so editing one to a wall-clock
// cadence starts with the time field empty. Saving that persisted an empty
// `daily_time_utc`, which `compute_next_run` cannot parse — leaving an
// automation that reads as enabled in the UI and silently never fires. Every
// Save in this panel sends the whole payload, so all of them are gated.
const cardEditValid = computed(
  () => !editShowsTimeOfDay.value || /^\d{2}:\d{2}$/.test(editData.value.time || ''),
)
const cardEditBlocked = computed(() => cardDirty.value && !cardEditValid.value)

function enabledToggleLabel(s: Schedule): string {
  if (isIntervalSchedule(s)) return s.enabled ? 'Stop' : 'Start'
  return s.enabled ? 'Pause' : 'Resume'
}

// Interval entries have no expected slot, so the missed-run check cannot speak
// for them. last_status is the server's health report instead. A wall-clock
// entry lands here too when its last dispatch failed (the run's outcome is
// stamped back onto the row), which the missed badge alone does not explain.
function intervalStatusLabel(s: Schedule): string {
  if (s.last_status === 'missing-chat') return 'stopped — chat missing'
  if (s.last_status === 'busy') return 'waiting — chat busy'
  if (s.last_status === 'running') return 'run in progress…'
  if (s.last_status === 'error') return 'last run failed'
  // `_schedule_dispatch_status` reports "skipped" when the run reached the
  // provider but stopped short of a result: an approval card, an
  // AskUserQuestion, or a deferred retry after a quota rejection. Without an
  // arm here it fell through to "waiting for first run", so an automation that
  // had run many times and was blocked on a prompt read as one that had never
  // run at all.
  if (s.last_status === 'skipped') return 'last run needs you — check the chat'
  if (s.last_status === 'ok') return 'ok'
  return s.enabled ? 'waiting for first run' : 'never ran'
}

function frequencyLabel(s: Schedule): string {
  if (s.frequency === 'manual') return 'Only when you run it'
  if (s.frequency === 'interval') return `Every ${s.interval_minutes} min`
  if (s.frequency === 'once') return s.run_at_date ? `Once, on ${s.run_at_date}` : 'Once'
  if (s.frequency === 'monthly') return `Monthly, day ${s.day_of_month}`
  if (s.frequency === 'weekly') {
    if (s.days_of_week?.length) return `Weekly (${s.days_of_week.join(', ')})`
    return 'Weekly'
  }
  return 'Daily'
}

function contextLabel(s: Schedule): string {
  if (s.web_project_id) {
    const proj = projectStore.projects.find(p => p.project_id === s.web_project_id)
    if (proj) return `A new chat in ${proj.name} each run`
  }
  if (s.web_chat_id) {
    const chat = projectStore.chats.find(c => c.chat_id === s.web_chat_id)
    if (chat) return `Continues “${chat.title || 'Untitled chat'}”`
  }
  if (s.context_label) return s.context_label
  return 'General'
}

function contextUnavailable(s: Schedule): boolean {
  // The server states this outright. Inferring it from context_label was
  // wrong: that field is always set, so a stale target rendered as an
  // ordinary one and the indicator never appeared.
  if (s.context_available !== undefined) return !s.context_available
  if (s.context_label) return false
  if (s.web_project_id) {
    return !projectStore.projects.some(p => p.project_id === s.web_project_id)
  }
  if (s.web_chat_id) {
    return !projectStore.chats.some(c => c.chat_id === s.web_chat_id)
  }
  return false
}

async function copyPrompt(prompt: string, key: string) {
  const copied = await writeClipboard(prompt)
  if (copied) {
    copiedPromptKey.value = key
    if (copiedPromptTimer !== undefined) window.clearTimeout(copiedPromptTimer)
    copiedPromptTimer = window.setTimeout(() => {
      if (copiedPromptKey.value === key) copiedPromptKey.value = ''
    }, 1800)
    return
  }
  copiedPromptKey.value = `error:${key}`
  if (copiedPromptTimer !== undefined) window.clearTimeout(copiedPromptTimer)
  copiedPromptTimer = window.setTimeout(() => {
    if (copiedPromptKey.value === `error:${key}`) copiedPromptKey.value = ''
  }, 1800)
}

function promptCopyLabel(key: string): string {
  if (copiedPromptKey.value === key) return 'Copied'
  if (copiedPromptKey.value === `error:${key}`) return 'Copy failed'
  return 'Copy'
}

function contextKeyFor(s: Schedule): string {
  if (s.web_project_id) return `proj:${s.web_project_id}`
  if (s.web_chat_id) return `web:${s.web_chat_id}`
  if (s.thread_id) return `${s.chat_id}:${s.thread_id}`
  return `${s.chat_id}`
}

function defaultProjectContext(workspace: string): string {
  const projects = projectStore.projects.filter(project => project.workspace === workspace)
  const general = projects.find(project => project.is_auto && project.name === 'General') || projects[0]
  return general ? `proj:${general.project_id}` : ''
}

function onEditWorkspaceChange() {
  editData.value.contextKey = defaultProjectContext(editData.value.workspace)
  if (!editData.value.model) editModelProvider.value = ''
}
async function onSystemWorkspaceChange(event: Event) {
  if (!schedule.value) return
  const workspace = (event.target as HTMLSelectElement).value
  const scheduleId = schedule.value.schedule_id
  await store.updateSchedule(scheduleId, { workspace })
  if (projectStore.activeWorkspace !== workspace) {
    await projectStore.switchWorkspace(workspace, { transition: false })
  }
  await router.push(`/schedules/${scheduleId}`)
}
const canEditSchedule = computed(() => !!schedule.value && schedule.value.scope !== 'system')
const canEditEngine = computed(() => !!schedule.value)
const canEditAdvanced = computed(() => !!schedule.value)

// Every card seeds the full editData from the schedule, so an unedited field
// always round-trips its current value and saving one card can't clobber another.
function editSnapshot(): string {
  return JSON.stringify([editData.value, editModelProvider.value])
}

const cardDirty = computed(() => editingCard.value !== '' && editSnapshot() !== editBaseline.value)

function startCardEdit(card: Exclude<EditCard, ''>) {
  if (!schedule.value) return
  const isSystem = schedule.value.scope === 'system'
  if (isSystem) {
    if (card !== 'engine' && card !== 'advanced') return
  } else if (!canEditSchedule.value) return
  editData.value = {
    workspace: schedule.value.workspace || projectStore.activeWorkspace,
    title: schedule.value.title || '',
    description: schedule.value.description || '',
    time: schedule.value.daily_time_utc,
    prompt: schedule.value.prompt,
    timezone: schedule.value.timezone_name,
    frequency: schedule.value.frequency || (schedule.value.days_of_week?.length ? 'weekly' : 'daily'),
    interval_minutes: schedule.value.interval_minutes || 10,
    days_of_week: schedule.value.days_of_week ? [...schedule.value.days_of_week] : [],
    day_of_month: schedule.value.day_of_month ?? null,
    contextKey: contextKeyFor(schedule.value),
    model: schedule.value.model || '',
    archive_policy: schedule.value.archive_policy || 'manual',
  }
  editModelProvider.value = schedule.value.provider || ''
  editBaseline.value = editSnapshot()
  editingCard.value = card
}

async function cancelCardEdit() {
  if (cardDirty.value && !(await askConfirm('Discard your unsaved changes?'))) return
  editingCard.value = ''
  editBaseline.value = ''
}

async function saveCardEdit() {
  if (!schedule.value) return
  const d = editData.value
  // System routines persist only a small overlay (enabled + workspace + engine + archive).
  // Sending the full user-schedule payload would try to change title/prompt/cadence
  // which the backend deliberately ignores or rejects, and would confuse the response.
  if (schedule.value.scope === 'system') {
    const updates: ScheduleUpdate = {}
    if (editingCard.value === 'engine') {
      updates.model = d.model
      updates.provider = d.model ? (editModelProvider.value || schedule.value.provider || 'claude') : ''
    } else if (editingCard.value === 'advanced') {
      updates.archive_policy = d.archive_policy
    } else {
      return
    }
    await store.updateSchedule(schedule.value.schedule_id, updates)
    editingCard.value = ''
    editBaseline.value = ''
    return
  }
  const updates: ScheduleUpdate = {
    workspace: d.workspace,
    title: d.title,
    description: d.description,
    time: d.frequency === 'manual' || d.frequency === 'interval' ? '' : d.time,
    prompt: d.prompt,
    timezone: d.timezone,
    frequency: d.frequency,
    interval_minutes: d.frequency === 'interval' ? d.interval_minutes : undefined,
    days_of_week: d.frequency === 'weekly' && d.days_of_week.length > 0 ? d.days_of_week : null,
    day_of_month: d.frequency === 'monthly' ? d.day_of_month : null,
    model: d.model,
    provider: d.model ? (editModelProvider.value || schedule.value.provider || 'claude') : '',
    archive_policy: d.archive_policy,
  }
  if (d.contextKey.startsWith('proj:')) {
    updates.web_project_id = d.contextKey.replace('proj:', '')
    updates.web_chat_id = null
    updates.chat_id = 0
    updates.thread_id = null
  } else if (d.contextKey.startsWith('web:')) {
    updates.web_chat_id = d.contextKey.replace('web:', '')
    updates.web_project_id = null
    updates.chat_id = 0
    updates.thread_id = null
  } else {
    updates.web_chat_id = null
    updates.web_project_id = null
    const parts = d.contextKey.split(':')
    updates.chat_id = parseInt(parts[0], 10)
    updates.thread_id = parts.length > 1 ? parseInt(parts[1], 10) : null
  }
  await store.updateSchedule(schedule.value.schedule_id, updates)
  editingCard.value = ''
  editBaseline.value = ''
}

function openRunningChat() {
  const chatId = runningChatId.value
  if (!chatId) return
  router.push(`/chat/${chatId}`)
}

function onRunButtonClick() {
  if (showRunning.value && runningChatId.value) {
    openRunningChat()
    return
  }
  void runNow()
}

function stopStarting(scheduleKey: string) {
  if (!startingBySchedule.value.has(scheduleKey)) return
  const next = new Set(startingBySchedule.value)
  next.delete(scheduleKey)
  startingBySchedule.value = next
}

async function runNow() {
  if (!schedule.value) return
  const scheduleKey = schedule.value.schedule_id
  if (startingBySchedule.value.has(scheduleKey)) return
  startingBySchedule.value = new Set([...startingBySchedule.value, scheduleKey])
  try {
    let result
    try {
      result = await store.runScheduleNow(scheduleKey)
    } catch (e) {
      // An interval run into a chat that is already streaming is refused
      // rather than queued behind the live turn. Say so instead of failing
      // silently, which read as "the button does nothing".
      await store.fetchSchedules()
      projectStore.pushErrorToast(
        'Run not started',
        e instanceof Error && e.message
          ? e.message
          : 'The target chat has a turn in flight — try again when it finishes.',
      )
      return
    }
    await store.fetchSchedules()
    if (result.chat_id) {
      runningBySchedule.value = { ...runningBySchedule.value, [scheduleKey]: result.chat_id }
      // Refresh chats so the new chat is available for navigation
      await projectStore.fetchAll()
      projectStore.pushToast({
        chat_id: result.chat_id,
        title: 'Schedule started',
        body: schedule.value.title || promptTitle(schedule.value.prompt),
      })
      // Keep "Running..." through the API→stream handoff.
      for (let i = 0; i < 50 && !projectStore.isChatStreaming(result.chat_id); i++) {
        await new Promise(resolve => window.setTimeout(resolve, 100))
      }
    }
  } finally {
    stopStarting(scheduleKey)
  }
}

async function runAllMissed() {
  const targets = missedSchedules.value
  if (!targets.length || runningAllMissed.value) return
  runningAllMissed.value = true
  let started = 0
  let failed = 0
  let firstChatId = ''
  try {
    // Sequential, same as server catch-up — avoid flooding providers.
    for (const s of targets) {
      const scheduleKey = s.schedule_id
      startingBySchedule.value = new Set([...startingBySchedule.value, scheduleKey])
      try {
        const result = await store.runScheduleNow(scheduleKey)
        started += 1
        if (result.chat_id) {
          runningBySchedule.value = {
            ...runningBySchedule.value,
            [scheduleKey]: result.chat_id,
          }
          if (!firstChatId) firstChatId = result.chat_id
        }
      } catch {
        failed += 1
      } finally {
        stopStarting(scheduleKey)
      }
    }
    await Promise.all([store.fetchSchedules(), projectStore.fetchAll()])
    const total = started + failed
    if (started > 0) {
      projectStore.pushToast({
        chat_id: firstChatId,
        title: failed ? `Started ${started} of ${total}` : `Started ${started} schedules`,
        body: failed ? `${failed} failed to start` : 'Missed schedules are running',
      })
    } else {
      projectStore.pushErrorToast(
        'Could not start missed schedules',
        'All run-now requests failed.',
      )
    }
  } finally {
    runningAllMissed.value = false
  }
}

async function onToggleEnabled() {
  if (!schedule.value) return
  await store.updateSchedule(schedule.value.schedule_id, { enabled: !schedule.value.enabled })
}

async function onDelete() {
  if (!schedule.value) return
  if (!await askConfirm('Delete this automation? It will stop running.', {
    title: 'Delete automation',
    confirmLabel: 'Delete automation',
    destructive: true,
  })) return
  const id = schedule.value.schedule_id
  await store.deleteSchedule(id)
  router.push('/schedules')
}

function onCreated() {
  emit('created')
}

// Keyboard roaming for the overview lists (Next up / Recent runs / Missed).
// This mirrors ProjectSidebar's schedule list, but for the main pane's
// .ov-item links when no single schedule is selected. The sidebar owns the
// canonical list, so ChatLayout tries the sidebar first and only falls
// through to here if the sidebar had nothing focused.
const overviewEl = ref<HTMLElement | null>(null)

function overviewItems(): HTMLElement[] {
  const root = overviewEl.value
  if (!root) return []
  return Array.from(root.querySelectorAll<HTMLElement>('.ov-item'))
}

function focusOverviewElement(element: HTMLElement) {
  element.focus()
  element.scrollIntoView({ block: 'nearest' })
}

function clampOverview(value: number, lower: number, upper: number): number {
  return Math.min(upper, Math.max(lower, value))
}

function onArrow(key: string): boolean {
  const items = overviewItems()
  if (!items.length) return false
  const active = document.activeElement as HTMLElement | null
  let index = items.indexOf(active as HTMLElement)
  if (index < 0) {
    const activeLink = overviewEl.value?.querySelector<HTMLElement>('.ov-item.router-link-active')
    if (activeLink && items.includes(activeLink)) {
      index = items.indexOf(activeLink)
    } else {
      focusOverviewElement(items[0])
      return true
    }
  }
  const delta = key === 'ArrowDown' || key === 'ArrowRight' ? 1 : key === 'ArrowUp' || key === 'ArrowLeft' ? -1 : 0
  if (!delta) return false
  const nextIndex = clampOverview(index + delta, 0, items.length - 1)
  if (nextIndex === index) return true
  focusOverviewElement(items[nextIndex])
  return true
}

defineExpose({ onArrow })

function closeSchedule() {
  if (props.showNew) {
    emit('close')
  } else {
    router.push('/schedules')
  }
}
</script>

<style scoped>
.schedule-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
}

.schedule-stale,
.schedule-load-state {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin: var(--space-3) var(--space-4) 0;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--warning) 8%, var(--bg2));
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
}

.schedule-load-state {
  flex: 1;
  margin: var(--space-4);
  background: var(--bg2);
}

.schedule-load-state--error {
  border-color: color-mix(in srgb, var(--error) 46%, var(--border));
}

.schedule-stale .btn-small,
.schedule-load-state .btn-small {
  flex: none;
}

.schedule-load-state p {
  margin: 0;
}


.scroll-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-6) 0 48px;
}
.scroll-body--padded {
  padding-inline: var(--page-inset);
}

.disabled-banner {
  margin: 0 0 var(--space-5);
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--fg2);
  font-size: var(--text-sm);
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-3);
  padding-bottom: var(--space-4);
  border-bottom: 1px solid var(--border);
  margin-bottom: var(--space-4);
  font-size: var(--text-sm);
  color: var(--fg2);
}
.meta-grid strong { color: var(--fg); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.5px; }
.system-workspace-control {
  display: grid;
  grid-template-columns: minmax(180px, 320px) 1fr;
  align-items: end;
  gap: var(--space-3);
  padding: 0 0 var(--space-4);
  margin-bottom: var(--space-4);
  border-bottom: 1px solid var(--border);
}
.context-unavailable { color: var(--warning); font-weight: 600; }
.context-help {
  display: block;
  margin-top: 4px;
  color: var(--fg3);
  font-size: var(--text-xs);
  line-height: 1.4;
}

.prompt-display { margin-top: var(--space-6); }
.prompt-label {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.02em;
}
.prompt-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-2);
}
.prompt-actions { display: flex; gap: var(--space-3); }

.schedule-description {
  margin: 0 0 10px;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.5;
}

/* The prompt reads as prose, so it is set in the sans face, not as code. */
.full-prompt {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg);
  font-size: var(--text-base);
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* Quiet text actions: Edit, Copy, open result. */
.text-link {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font: inherit;
  font-size: var(--text-sm);
  text-decoration: none;
  cursor: pointer;
}
.text-link:hover { text-decoration: underline; text-underline-offset: 3px; }
@media (pointer: coarse) {
  .text-link { min-height: var(--touch); }
}
.edit-form { display: flex; flex-direction: column; gap: var(--space-3); }
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--space-3);
}
.form-group { display: flex; flex-direction: column; gap: 4px; }
.form-group label { font-size: var(--text-xs); color: var(--fg2); }
.field-hint { margin: 4px 0 0; font-size: var(--text-xs); }
.field-hint--warn { color: var(--warning); }
.form-group input, .form-group select, .form-group textarea {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--fg);
  font-size: var(--text-base);
}
.form-group textarea { resize: vertical; min-height: 160px; font-family: ui-monospace, monospace; }

.days-row { display: flex; flex-wrap: wrap; gap: 4px; }

.form-actions { display: flex; gap: 8px; margin-top: 4px; }

.label-hint { color: var(--fg3); font-weight: 400; }

/* ── Routine property cards ────────────────────────────────────── */
/* auto-fit rather than a viewport media query: the panel is often narrow while
   the window is wide (sidebar open), so the cards must react to their own box. */
/* Property sections: a plain heading with a quiet Edit link, then hairline
   definition rows. They used to be four boxed cards with uppercase labels. */
.prop-cards {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}
.prop-card {
  min-width: 0;
}
.prop-card.card-editing .card-form {
  padding: var(--space-3);
  border: 1px solid color-mix(in srgb, var(--accent2) 55%, var(--border));
  border-radius: var(--radius);
  background: var(--bg-elev);
}
.prop-card-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
  min-height: 28px;
}
.prop-card-name {
  flex: 1;
  min-width: 0;
  margin: 0;
  color: var(--fg);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.02em;
}
.card-edit {
  flex: none;
  min-height: 28px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.card-edit:hover { text-decoration: underline; text-underline-offset: 3px; }
.esc-hint { flex: none; font-size: var(--text-xs); color: var(--fg3); }
.esc-hint kbd {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  padding: 1px 4px;
  margin-right: 4px;
}

.prop-rows { margin: 0; border-top: 1px solid var(--border); }
.prop-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-4);
  min-height: 44px;
  padding: 11px 2px;
  box-sizing: border-box;
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
}
.prop-row dt { flex: 0 0 140px; color: var(--fg2); }
.prop-row dd {
  flex: 1;
  margin: 0;
  min-width: 0;
  color: var(--fg);
  overflow-wrap: anywhere;
}

/* System routines keep their inline workspace switcher, but inside a card it
   drops the full-width divider and side-by-side layout it uses standalone. */
.prop-card .system-workspace-control {
  grid-template-columns: 1fr;
  align-items: stretch;
  gap: var(--space-2);
  padding: var(--space-2) 0 0;
  margin: var(--space-2) 0 0;
  border-top: 1px solid var(--border);
  border-bottom: 0;
}

.card-form { display: flex; flex-direction: column; gap: var(--space-3); }
.card-form .form-group textarea { min-height: 64px; }
.content-form {
  padding: var(--space-3);
  border: 1px solid var(--accent2);
  border-radius: var(--radius);
  background: var(--bg-elev);
}
.content-form .form-group textarea:last-child { min-height: 200px; }
.card-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border);
}
.dirty-flag {
  margin-right: auto;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-xs);
  color: var(--fg2);
}
.dirty-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  flex: none;
}
.card-actions .btn-primary:disabled { opacity: 0.45; cursor: not-allowed; }

@media (max-width: 640px) {
  .card-edit { min-height: var(--touch); min-width: var(--touch); }
}

.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: var(--space-2);
  width: 100%;
  max-width: 560px;
  margin: 0 auto;
  padding: var(--space-4) var(--page-gutter);
  box-sizing: border-box;
  color: var(--fg2);
}
.empty-state h2 { margin: 0; color: var(--fg); font-size: calc(20px * var(--font-scale)); letter-spacing: -0.02em; }
.empty-state .empty-hint { margin: 0; color: var(--fg2); font-size: var(--text-sm); line-height: 1.55; }

.hint { font-size: var(--text-xs); color: var(--fg2); margin: 0; }

@media (max-width: 640px) {
  .system-workspace-control { grid-template-columns: 1fr; }
}

/* The "what is this page" disclosure above the new-automation form. Every
   other .field-info sits inline next to a heading; this one is the only block
   on its line, so it needs its own bottom margin. */
.field-info--block { margin-bottom: 12px; }

/* ── Overview ─────────────────────────────────────────────────────
   Sections with a plain heading over hairline rows: title and a plain
   cadence/delivery sub-line on the left, the next run on the right - the same
   row language as Today's "Continue where you left off". */
.overview-body .page-main {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}
.ov-section { min-width: 0; }
/* The shared rail is sticky at --space-4; inside this padded scroll body that
   offset pushed its first heading below the main column's. */
.page-rail { top: 0; }
.rail-title--spaced { margin-top: var(--space-5); }
.rail-note--attention { color: var(--warning); }
.rail-runs { margin-top: var(--space-3); }

.field-info {
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
}
.field-info summary {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--bg);
  color: var(--fg2);
  font-size: var(--text-xs);
  font-weight: 700;
  cursor: pointer;
  line-height: 1;
  user-select: none;
}
.field-info summary::-webkit-details-marker {
  display: none;
}
.field-info[open] summary,
.field-info summary:hover {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 8%, var(--bg));
}
.field-info-panel {
  position: absolute;
  z-index: 30;
  top: calc(100% + 6px);
  left: 0;
  right: auto;
  width: min(380px, calc(100vw - 48px));
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elev);
  box-shadow: 0 12px 30px color-mix(in srgb, #000 24%, transparent);
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.45;
}
.field-info-panel p {
  margin: 0;
}
.field-info-panel p + p {
  margin-top: var(--space-2);
}
.ov-head {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}
.ov-head h2 {
  margin: 0;
  color: var(--fg);
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.02em;
}
.ov-section--alert .ov-head h2 { color: var(--warning); }
.ov-run-all { margin-left: auto; }
.ov-hint { color: var(--fg3); font-size: var(--text-sm); }
.ov-intro { margin: -4px 0 var(--space-2); color: var(--fg3); font-size: var(--text-sm); }
.ov-item {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  min-height: 56px;
  padding: 9px 2px;
  box-sizing: border-box;
  border-bottom: 1px solid var(--border);
  color: var(--fg);
  text-decoration: none;
}
.ov-head + .ov-item,
.ov-intro + .ov-item { border-top: 1px solid var(--border); }
.ov-item:hover .ov-title { color: var(--accent); }
.ov-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}
.ov-item--paused .ov-title { color: var(--fg2); font-weight: 500; }
.ov-main { display: flex; flex: 1; flex-direction: column; gap: 2px; min-width: 0; }
.ov-title {
  overflow: hidden;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: color 120ms var(--ease);
}
.ov-sub {
  overflow: hidden;
  color: var(--fg3);
  font-size: var(--text-sm);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ov-when {
  flex: none;
  color: var(--fg2);
  font-size: var(--text-sm);
  white-space: nowrap;
}
.ov-when--alert { color: var(--warning); }
.ov-empty { margin: 0; padding: 12px 2px; border-top: 1px solid var(--border); color: var(--fg3); font-size: var(--text-sm); }

/* Header actions: Run now is the one primary; the toggle and Delete live in
   the menu on narrow panes. */
.btn-run {
  border-color: transparent;
  background: var(--accent);
  color: var(--on-accent);
  font-weight: 600;
}
.btn-run:hover:not(:disabled) { background: var(--accent-strong); }
.desktop-only { display: inline-flex; }
.overflow-trigger { color: var(--fg2); }
.menu-toggle-item { display: none !important; }

@container chat-pane (max-width: 820px) {
  .header-actions .desktop-only { display: none; }
  .menu-toggle-item { display: flex !important; }
  .close-btn.desktop-only { display: inline-flex; }
}

@container chat-pane (min-width: 821px) {
  /* A system routine has no Delete, so on a wide pane its menu would be empty. */
  .mobile-only-trigger { display: none; }
}

@media (max-width: 768px) {
  .desktop-only,
  .close-btn.desktop-only { display: none; }
  .menu-toggle-item { display: flex !important; }
  .mobile-only-trigger { display: inline-flex; }
  .prompt-heading { align-items: flex-start; }
  .prop-row { flex-direction: column; gap: 2px; }
  .prop-row dt { flex: none; }
}

.schedule-actions-menu {
  z-index: 100;
  min-width: 180px;
  padding: 4px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 12px 32px rgb(0 0 0 / 35%);
}
.schedule-actions-menu button {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: var(--touch);
  padding: 8px 12px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
}
.schedule-actions-menu button:hover,
.schedule-actions-menu button[data-highlighted] { background: var(--bg3); outline: none; }
.schedule-actions-menu button.danger { color: var(--error); }

.close-btn {
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: 20px;
  line-height: 1;
  font-family: var(--font);
  min-width: 30px;
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.close-btn:hover { color: var(--fg); }

.btn-running:not(:disabled) {
  cursor: pointer;
}
.btn-running:not(:disabled):hover {
  filter: brightness(1.08);
}

/* Pulsing dot used inline next to active runs. */
.spinner-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-left: 6px;
  border-radius: 50%;
  background: var(--accent);
  animation: ciao-pulse 1.1s ease-in-out infinite;
  vertical-align: middle;
  flex-shrink: 0;
}

@keyframes ciao-pulse {
  0%, 100% { transform: scale(0.55); opacity: 0.35; }
  50% { transform: scale(1); opacity: 1; }
}

@media (prefers-reduced-motion: reduce) {
  .spinner-dot { animation-duration: 2.2s; }
}
</style>
