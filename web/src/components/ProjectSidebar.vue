<template>
  <aside class="sidebar" :class="{ collapsed }" v-bind="$attrs">
    <div class="sidebar-header" :class="{ 'sidebar-header--expanded': !collapsed }">
      <div class="sidebar-brand-row">
        <template v-if="!collapsed">
          <span class="sidebar-brand-glyph" aria-hidden="true">›</span>
          <BrandMark class="sidebar-brand" />
        </template>
      <button
        class="toggle-btn touch-hit"
        :class="{ 'toggle-btn--collapsed': collapsed }"
        @click="$emit('toggle')"
        :title="collapsed ? 'Open sidebar' : 'Collapse sidebar'"
        :aria-label="collapsed ? 'Open sidebar' : 'Collapse sidebar'"
      >
        <!-- Panel icon: a rectangle with a vertical bar showing the sidebar's
             position. Mirrors based on `collapsed` so it always points at the
             panel it would reveal/hide. -->
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2"
             stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
          <rect x="3" y="4" width="18" height="16" rx="1" />
          <line x1="9" y1="4" x2="9" y2="20" />
        </svg>
      </button>
      </div>
      <template v-if="!collapsed">
        <!-- Prototype A's rail: brand row, workspace scope, one New chat, then
             the destinations as a labelled vertical list. The pane header drops
             its own centred wordmark on wide panes because this row carries it. -->
        <!-- One workspace scope for the whole rail. It used to be repeated as a
             per-mode row of pills (chat, automations, memory), which said the
             same thing in three places and pushed each mode's own content down
             the column. Selecting a workspace here commits the scope for
             whichever mode is open and keeps the 1-9 shortcuts. -->
        <div v-if="store.workspaceOptions.length" ref="workspaceScopeEl" class="workspace-scope">
          <button
            ref="workspaceScopeTrigger"
            type="button"
            class="workspace-scope-trigger"
            :data-workspace-color="colorForWorkspace(store.workspaceOptions.find(item => item.name === store.activeWorkspace))"
            :aria-label="workspaceActionCount(store.activeWorkspace) ? `Workspace: ${workspaceLabel(store.activeWorkspace)} — ${workspaceActionCount(store.activeWorkspace)} items need attention` : `Workspace: ${workspaceLabel(store.activeWorkspace)}`"
            :aria-haspopup="hasMultipleWorkspaces ? 'menu' : undefined"
            :aria-expanded="hasMultipleWorkspaces ? workspaceScopeOpen : undefined"
            :aria-controls="hasMultipleWorkspaces ? 'workspace-scope-menu' : undefined"
            :aria-keyshortcuts="workspaceShortcut(store.activeWorkspace) || undefined"
            :disabled="!hasMultipleWorkspaces"
            @click="toggleWorkspaceMenu"
            @keydown.down.prevent="openWorkspaceMenu"
          >
            <span class="workspace-scope-dot" aria-hidden="true" />
            <!-- Name only: counts and key hints live in the menu, next to the
                 workspaces they would switch to. -->
            <span class="workspace-scope-name">{{ workspaceLabel(store.activeWorkspace) }}</span>
            <svg v-if="hasMultipleWorkspaces" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="square" aria-hidden="true">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </button>

          <div
            v-if="hasMultipleWorkspaces && workspaceScopeOpen"
            id="workspace-scope-menu"
            ref="workspaceScopeMenu"
            class="workspace-scope-menu"
            role="menu"
            aria-label="Choose workspace"
            @keydown="onWorkspaceMenuKeydown"
          >
            <button
              v-for="workspace in store.workspaceOptions"
              :key="workspace.name"
              type="button"
              role="menuitem"
              class="workspace-scope-option"
              :class="{ active: workspace.name === store.activeWorkspace }"
              :data-workspace-color="colorForWorkspace(workspace)"
              :aria-keyshortcuts="workspaceShortcut(workspace.name) || undefined"
              @click="selectWorkspaceScope(workspace.name)"
            >
              <span class="workspace-scope-dot" aria-hidden="true" />
              <span class="workspace-scope-name">{{ workspaceLabel(workspace.name) }}</span>
              <span v-if="workspaceActionCount(workspace.name)" class="badge">{{ workspaceActionCount(workspace.name) }}</span>
              <kbd v-if="workspaceShortcut(workspace.name)" class="sidebar-keycap" aria-hidden="true">{{ workspaceShortcut(workspace.name) }}</kbd>
            </button>
          </div>
        </div>

        <!-- One global New chat, directly under the scope it belongs to. It opens the same shared project picker as
             everywhere else, so the sidebar never grows a second
             project-selection path. Project-local "+" buttons stay, because
             they preselect their own project. -->
        <button
          v-if="!mode || mode === 'chat' || mode === 'project'"
          type="button"
          class="sidebar-new-chat"
          aria-haspopup="dialog"
          :aria-label="`New chat in ${workspaceLabel(store.activeWorkspace)}`"
          :aria-keyshortcuts="newChatKeyshortcuts"
          :title="`New chat (${newChatShortcut})`"
          @click="chooseNewChat(store.activeWorkspace)"
        >
          <span class="sidebar-new-chat-plus" aria-hidden="true">+</span>
          <span>New chat</span>
        </button>

        <nav class="nav-links" aria-label="Primary navigation">
          <router-link
            to="/"
            class="nav-item"
            :class="{
              'nav-item--active': mode === 'chat' || mode === 'project',
              'nav-item--working': isAnyChatWorking
            }"
            title="Today"
            :aria-label="store.attentionChatCount > 0 ? `Today — ${store.attentionChatCount} chat${store.attentionChatCount === 1 ? '' : 's'} need${store.attentionChatCount === 1 ? 's' : ''} attention` : (isAnyChatWorking ? 'Today (assistant is working)' : 'Today')"
          >
            <span class="nav-item-icon" aria-hidden="true">
              <!-- Stacked message lines: sharper, more "log-window" than a speech bubble -->
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="square" stroke-linejoin="miter">
                <rect x="3" y="4" width="18" height="14" />
                <line x1="6" y1="9" x2="14" y2="9" />
                <line x1="6" y1="13" x2="18" y2="13" />
                <polyline points="8 18 8 21 11 18" />
              </svg>
              <!-- A dot, not a number: the count is on Today itself, and the
                   link's accessible name still states it. -->
              <span
                v-if="store.attentionChatCount > 0"
                class="nav-item-badge nav-item-badge--static"
              />
            </span>
            <span class="nav-item-label" aria-hidden="true">Today</span>
          </router-link>
          <router-link
            to="/schedules"
            class="nav-item"
            :class="{
              'nav-item--active': mode === 'schedules',
              'nav-item--warning': hasAutomationWarning
            }"
            title="automations"
            :aria-label="hasAutomationWarning ? 'automations (attention required)' : 'automations'"
          >
            <span class="nav-item-icon" aria-hidden="true">
              <!-- Clock face with hour markers: more diagrammatic than calendar grid -->
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="square" stroke-linejoin="miter">
                <rect x="3" y="3" width="18" height="18" />
                <line x1="12" y1="3" x2="12" y2="5" />
                <line x1="12" y1="19" x2="12" y2="21" />
                <line x1="3" y1="12" x2="5" y2="12" />
                <line x1="19" y1="12" x2="21" y2="12" />
                <polyline points="12 8 12 12 15 14" />
              </svg>
            </span>
            <span class="nav-item-label" aria-hidden="true">Automations</span>
          </router-link>
          <router-link
            to="/memory"
            class="nav-item"
            :class="{ 'nav-item--active': mode === 'memory' || mode === 'proposals' }"
            :title="proposals.rows.length > 0 ? `memory — ${proposals.rows.length} suggested across all workspaces` : 'memory'"
            :aria-label="proposals.rows.length > 0 ? `memory — ${proposals.rows.length} suggested memories across all workspaces` : 'memory'"
          >
            <!-- Book/tray: rectilinear like the rest of the rail, which the
                 organic-curve brain never was. It also now covers review, since
                 the proposal queue is a segment of this view rather than its own
                 rail entry. -->
            <span class="nav-item-icon" aria-hidden="true">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="square" stroke-linejoin="miter">
                <path d="M4 4h16v12H4z" />
                <path d="M4 16h16v4H4z" />
                <line x1="8" y1="8" x2="16" y2="8" />
              </svg>

            </span>
            <span class="nav-item-label" aria-hidden="true">Memory</span>
          </router-link>
          <!-- mode, not active-class: every settings tab is its own route
               (/settings/models, /settings/workspaces, ...) and none of them match
               the /settings record, so active-class left this item inactive on
               nearly every settings page - and with it the label collapsed. The
               sibling links already key off mode for the same reason. -->
          <router-link
            to="/settings"
            class="nav-item"
            :class="{ 'nav-item--active': mode === 'settings', 'nav-item--warning': hasBlockingHousekeeping }"
            :title="settingsNeedsAttention ? (store.packageStatus?.update_available && hasBlockingHousekeeping ? 'settings — update available and action required' : store.packageStatus?.update_available ? `settings — update to ${store.packageStatus.latest_version} available` : 'settings — action required') : 'settings'"
            :aria-label="settingsNeedsAttention ? (store.packageStatus?.update_available && hasBlockingHousekeeping ? 'settings — update available and action required' : store.packageStatus?.update_available ? `settings — update to ${store.packageStatus.latest_version} available` : 'settings — action required') : 'settings'"
          >
            <!-- Sliders / equalizer: more direct than a gear, mono-grid friendly -->
            <span class="nav-item-icon" aria-hidden="true">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="square" stroke-linejoin="miter">
                <line x1="4" y1="7" x2="20" y2="7" />
                <line x1="4" y1="12" x2="20" y2="12" />
                <line x1="4" y1="17" x2="20" y2="17" />
                <rect x="14" y="5" width="4" height="4" fill="currentColor" />
                <rect x="7" y="10" width="4" height="4" fill="currentColor" />
                <rect x="15" y="15" width="4" height="4" fill="currentColor" />
              </svg>
              <span
                v-if="settingsNeedsAttention"
                class="nav-item-badge"
                :class="{ 'nav-item-badge--warning': hasBlockingHousekeeping }"
              />
            </span>
            <span class="nav-item-label" aria-hidden="true">Settings</span>
          </router-link>
        </nav>

      </template>
    </div>

    <!-- No section title here: the nav pill above already names this view, and
         the create action sits in the footer like the chat sidebar's, so both
         modes put "make a new one" in the same place. -->
    <template v-if="!collapsed && (mode === 'schedules')">
      <div ref="schedulesListEl" class="schedules-list">
        <template v-if="taskStore.loading">
          <div class="mm-loading-heading" role="status" aria-live="polite">
            <span class="history-loading-spinner" aria-hidden="true"></span>
            <span>Loading automations…</span>
          </div>
          <div aria-hidden="true" class="mm-skeleton-block">
            <span class="mm-shimmer-line" style="width: 100%; height: 36px; margin-bottom: 8px;"></span>
            <span class="mm-shimmer-line" style="width: 92%; height: 36px; margin-bottom: 8px;"></span>
            <span class="mm-shimmer-line" style="width: 88%; height: 36px;"></span>
          </div>
        </template>
        <template v-else>
          <div v-if="workspaceSchedules.length === 0" class="empty-hint">// no automations in this workspace</div>

          <template v-if="oneOffSchedules.length">
          <div class="schedule-group schedule-group--once">
            <h2 class="schedule-group-header" title="One-off automations delete themselves after they run">One-offs</h2>
            <div class="schedule-group-items">
              <router-link
                v-for="s in oneOffSchedules"
                :key="s.schedule_id"
                :to="`/schedules/${s.schedule_id}`"
                class="schedule-item schedule-item--once"
                :class="{ 'schedule-item--missed': s.missed, 'schedule-item--disabled': !s.enabled }"
                active-class="active"
              >
                <span class="schedule-label">{{ s.title || promptTitle(s.prompt) }}</span>
                <span class="schedule-time">{{ s.run_at_date?.slice(5) }} {{ s.daily_time_utc }}</span>
                <span v-if="s.missed" class="missed-dot" title="Expected to run but didn't"></span>
              </router-link>
            </div>
          </div>
        </template>

        <template v-if="userRoutines.length">
          <div class="schedule-group">
            <h2 class="schedule-group-header">Routines</h2>
            <div class="schedule-group-items">
              <router-link
                v-for="s in userRoutines"
                :key="s.schedule_id"
                :to="`/schedules/${s.schedule_id}`"
                class="schedule-item"
                :class="{ 'schedule-item--missed': s.missed, 'schedule-item--disabled': !s.enabled }"
                active-class="active"
              >
                <span class="schedule-label">{{ s.title || promptTitle(s.prompt) }}</span>
                <span class="schedule-time">{{ cadenceBadge(s) }}</span>
                <span
                  v-if="s.enabled && s.web_chat_id && store.isChatStreaming(s.web_chat_id)"
                  class="spinner-dot"
                  title="This automation is working"
                />
                <span v-if="s.missed" class="missed-dot" title="Expected to run but didn't"></span>
              </router-link>
            </div>
          </div>
        </template>

        <template v-if="systemAutomations.length">
          <div class="schedule-group schedule-group--system">
            <h2 class="schedule-group-header">System</h2>
            <div class="schedule-group-items">
              <router-link
                v-for="s in systemAutomations"
                :key="s.schedule_id"
                :to="`/schedules/${s.schedule_id}`"
                class="schedule-item"
                :class="{ 'schedule-item--missed': s.missed, 'schedule-item--disabled': !s.enabled }"
                active-class="active"
              >
                <span class="schedule-label">{{ s.title || promptTitle(s.prompt) }}</span>
                <span class="schedule-time">{{ cadenceBadge(s) }}</span>
                <span
                  v-if="s.enabled && s.web_chat_id && store.isChatStreaming(s.web_chat_id)"
                  class="spinner-dot"
                  title="This automation is working"
                />
                <span v-if="s.missed" class="missed-dot" title="Expected to run but didn't"></span>
              </router-link>
            </div>
          </div>
        </template>

        </template>
      </div>

      <div class="sidebar-footer">
        <button class="add-automation-btn" @click="emit('new-schedule')">+ New Automation</button>
      </div>
    </template>

    <template v-if="!collapsed && mode === 'settings'">
      <nav class="settings-nav-list" aria-label="Settings sections">
        <h2 class="sidebar-list-label">Settings</h2>
        <router-link
          v-for="item in SETTINGS_NAV"
          :key="item.to"
          :to="item.to"
          class="settings-nav-item"
          :class="{ active: route.path === item.to }"
          :aria-current="route.path === item.to ? 'page' : undefined"
        >{{ item.label }}</router-link>
      </nav>
    </template>

    <!-- Workspace scope now lives once at the top of the rail. This mode keeps
         its own Memory/Review switcher, which is about what the memory page
         shows rather than which workspace it is scoped to. -->
    <template v-if="!collapsed && (mode === 'memory' || mode === 'proposals')">
      <!-- The Memory/Review switcher lives here rather than in the pane
           header: picking what the memory page shows is the same act as
           scoping it to a workspace, and this keeps every memory control in
           one column.

           Two buttons, not three. Graph and List are two drawings of the same
           notes and belong on one level; sitting beside Review they read as
           three peers, one of which is a different page with a different job.
           Graph/List now lives in the map's own toolbar, where the rest of the
           "how should this look" controls already are. -->
      <div class="workspace-toggle view-toggle">
        <button
          type="button"
          :class="{ active: mm.view !== 'review' }"
          :aria-pressed="mm.view !== 'review'"
          @click="setMemoryView(mm.mapView)"
        >Memory</button>
        <button
          type="button"
          :class="{ active: mm.view === 'review' }"
          :aria-pressed="mm.view === 'review'"
          :title="decisionsWaiting ? `${decisionsWaiting} waiting on a decision in ${workspaceLabel(store.activeWorkspace)}` : undefined"
          :aria-label="decisionsWaiting ? `Review — ${decisionsWaiting} waiting on a decision in ${workspaceLabel(store.activeWorkspace)}` : 'Review'"
          @click="setMemoryView('review')"
        >
          <!-- Scoped, not the global tally: the workspace toggle directly
               above scopes the queues, so a global count here claimed items the
               Review list would not show — 12 next to a selected workspace
               whose list is empty.

               Both queues, not just the proposals one: this button is the
               answer to "is there anything to decide", and a badge that
               counted one of the two tabs behind it said no while the other
               held five notes. -->
          Review<span v-if="decisionsWaiting" class="view-count">{{ decisionsWaiting }}</span>
        </button>
      </div>

      <!-- Retirements is a different queue from Proposals, and this column used
           to report the proposals one whatever tab was open: a panel showing
           five notes to review sat beside "0 of 0 shown". The stats follow the
           tab, and the proposals-only search and kind chips stay with it. -->
      <div v-if="mode === 'proposals' && mm.reviewTab === 'retirement'" class="mm-sidebar-scroll">
        <h3>Notes to revisit</h3>
        <!-- Three states, not one: a zero is a claim about the vault and may
             only be printed once a load for this workspace has succeeded. -->
        <template v-if="retirementPending">
          <div class="mm-loading-heading" role="status" aria-live="polite">
            <span class="history-loading-spinner" aria-hidden="true"></span>
            <span>Loading candidates…</span>
          </div>
          <div class="mm-stat-grid mm-stat-grid--3" aria-hidden="true">
            <div class="mm-stat mm-stat--skeleton"><span class="mm-shimmer-line mm-shimmer-line--n"></span><span class="mm-shimmer-line mm-shimmer-line--l"></span></div>
            <div class="mm-stat mm-stat--skeleton"><span class="mm-shimmer-line mm-shimmer-line--n"></span><span class="mm-shimmer-line mm-shimmer-line--l"></span></div>
            <div class="mm-stat mm-stat--skeleton"><span class="mm-shimmer-line mm-shimmer-line--n"></span><span class="mm-shimmer-line mm-shimmer-line--l"></span></div>
          </div>
        </template>
        <p v-else-if="retirementFailed" class="empty-hint" role="status">
          // could not load the retirement queue
        </p>
        <div v-else class="mm-stat-grid mm-stat-grid--3">
          <div class="mm-stat">
            <div class="n">{{ retirementScoped }}</div>
            <div class="l">to revisit</div>
          </div>
          <div class="mm-stat">
            <div class="n">{{ retirementTrashed }}</div>
            <div class="l">retired</div>
          </div>
          <div class="mm-stat">
            <div class="n">{{ retirementSignals }}</div>
            <div class="l">reasons</div>
          </div>
        </div>
      </div>

      <!-- Review: the same shape as the memory map's sidebar — stats, a search,
           then chips that both report and filter. The kind filter used to be a
           segmented control in the panel header while this column sat empty,
           which put the queue's controls somewhere different from every other
           memory view's. -->
      <div v-else-if="mode === 'proposals'" class="mm-sidebar-scroll">
        <template v-if="proposals.loading">
          <h3>Suggested memories</h3>
          <div class="mm-loading-heading" role="status" aria-live="polite">
            <span class="history-loading-spinner" aria-hidden="true"></span>
            <span>Loading proposals…</span>
          </div>
          <div class="mm-stat-grid mm-stat-grid--3" aria-hidden="true">
            <div class="mm-stat mm-stat--skeleton"><span class="mm-shimmer-line mm-shimmer-line--n"></span><span class="mm-shimmer-line mm-shimmer-line--l"></span></div>
            <div class="mm-stat mm-stat--skeleton"><span class="mm-shimmer-line mm-shimmer-line--n"></span><span class="mm-shimmer-line mm-shimmer-line--l"></span></div>
            <div class="mm-stat mm-stat--skeleton"><span class="mm-shimmer-line mm-shimmer-line--n"></span><span class="mm-shimmer-line mm-shimmer-line--l"></span></div>
          </div>
          <div class="mm-search">
            <input type="text" placeholder="Search proposals…" autocomplete="off" disabled />
          </div>
          <div class="mm-skeleton-block" aria-hidden="true">
            <span class="mm-shimmer-line" style="width: 40%; margin-bottom: 10px;"></span>
            <span class="mm-shimmer-line" style="width: 100%; height: 28px; margin-bottom: 6px;"></span>
            <span class="mm-shimmer-line" style="width: 92%; height: 28px; margin-bottom: 6px;"></span>
            <span class="mm-shimmer-line" style="width: 88%; height: 28px;"></span>
          </div>
        </template>
        <template v-else>
          <h3>Suggested memories</h3>
          <div class="mm-stat-grid mm-stat-grid--3">
            <div class="mm-stat">
              <div class="n">{{ reviewVisible }}</div>
              <div class="l">of {{ reviewScoped }} shown</div>
            </div>
            <div class="mm-stat">
              <div class="n">{{ proposals.selected.size }}</div>
              <div class="l">selected</div>
            </div>
            <div class="mm-stat">
              <div class="n">{{ reviewElsewhere }}</div>
              <div class="l">other workspaces</div>
            </div>
          </div>

          <div class="mm-search">
            <input
              v-model="proposals.search"
              type="text"
              placeholder="Search proposals…"
              autocomplete="off"
            />
          </div>

        <div class="mm-row-between">
          <h3>Kinds</h3>
          <button type="button" class="mm-link" @click="proposals.resetFilters()">reset</button>
        </div>
        <div class="mm-link-list">
          <div
            class="mm-link-item"
            :class="{ off: proposals.kindFilter !== 'all' }"
            @click="proposals.kindFilter = 'all'"
          >
            <span class="label">all</span>
            <span class="cnt">{{ reviewScoped }}</span>
          </div>
          <div
            v-for="k in reviewKinds"
            :key="k.kind"
            class="mm-link-item"
            :class="{ off: proposals.kindFilter !== k.kind }"
            :title="`Show only ${reviewKindLabel(k.kind)} proposals`"
            @click="proposals.kindFilter = k.kind"
          >
            <span class="label">{{ reviewKindLabel(k.kind) }}</span>
            <span class="cnt">{{ k.count }}</span>
          </div>
        </div>
        </template>
      </div>

      <div v-if="mode === 'memory'" class="mm-sidebar-scroll">
        <template v-if="mm.loading">
          <h3>Vault</h3>
          <div class="mm-loading-heading" role="status" aria-live="polite">
            <span class="history-loading-spinner" aria-hidden="true"></span>
            <span>Loading vault graph…</span>
          </div>
          <div class="mm-stat-grid" aria-hidden="true">
            <div class="mm-stat mm-stat--skeleton"><span class="mm-shimmer-line mm-shimmer-line--n"></span><span class="mm-shimmer-line mm-shimmer-line--l"></span></div>
            <div class="mm-stat mm-stat--skeleton"><span class="mm-shimmer-line mm-shimmer-line--n"></span><span class="mm-shimmer-line mm-shimmer-line--l"></span></div>
          </div>
          <div class="mm-search">
            <input type="text" placeholder="Search notes, tags…" autocomplete="off" disabled />
          </div>
          <div class="mm-skeleton-block" aria-hidden="true">
            <span class="mm-shimmer-line" style="width: 40%; margin-bottom: 10px;"></span>
            <span class="mm-shimmer-line" style="width: 100%; height: 28px; margin-bottom: 6px;"></span>
            <span class="mm-shimmer-line" style="width: 92%; height: 28px; margin-bottom: 6px;"></span>
            <span class="mm-shimmer-line" style="width: 88%; height: 28px;"></span>
          </div>
        </template>
        <template v-else>
          <h3>Vault</h3>
          <!-- Two tiles: "notes shown" and "total" were separate tiles showing
               the same number whenever nothing was filtered, so the total moved
               into the sublabel. Orphans left the grid entirely — as a bare
               number it was not actionable, and it is now a list. -->
          <div class="mm-stat-grid">
            <div class="mm-stat">
              <div class="n">{{ mm.visibleNodes.length }}</div>
              <div class="l">of {{ mm.nodes.length }} shown</div>
            </div>
            <div class="mm-stat"><div class="n">{{ mm.visibleEdgeCount }}</div><div class="l">links</div></div>
          </div>

          <!-- Workspace guide (AGENTS.md; CLAUDE.md pre-migration) — the only file every chat loads.
               Surfaced here because the vault graph hides it (it is not a vault note)
               yet its bounded regions budget every session. -->
          <div class="guide-card" :class="{ 'guide-card--over': guideOverCap }">
            <div class="guide-card-head">
              <div class="guide-card-title">
                <span class="guide-card-icon" aria-hidden="true">◆</span>
                {{ guidePathLabel }}
                <span v-if="guideOverCap" class="guide-card-badge guide-card-badge--warn" title="A bounded region is over its advisory cap">over cap</span>
                <span v-else-if="guideLoading" class="guide-card-badge">loading…</span>
              </div>
              <div class="guide-card-actions">
                <button type="button" class="guide-card-btn" :disabled="guideLoading || !!guideError" @click="openGuideFile" :title="`Open ${guidePathLabel}`">Open</button>
                <button type="button" class="guide-card-btn guide-card-btn--primary" :disabled="!canDiscussGuide" @click="discussGuide" title="Start a chat about this guide">Discuss</button>
              </div>
            </div>
            <div v-if="guideError" class="guide-card-error">{{ guideError }}</div>
            <template v-else-if="guideStats">
              <div class="guide-card-regions">
                <div v-for="r in guideStats.regions" :key="r.key" class="guide-region">
                  <div class="guide-region-head">
                    <span class="guide-region-name">{{ r.label }}</span>
                    <span class="guide-region-count" :class="{ 'guide-region-count--warn': r.overCap }">{{ r.usedChars }} / {{ r.charLimit }} chars</span>
                    <span class="guide-region-tokens" :title="`${r.usedChars} chars ≈ ${r.tokens} tokens`">≈ {{ r.tokens }} tokens</span>
                  </div>
                  <div class="guide-region-bar" :class="{ 'guide-region-bar--warn': r.overCap, 'guide-region-bar--high': !r.overCap && r.pct >= 80 }" :title="`${r.pct}% of cap`">
                    <span :style="{ width: Math.min(100, r.pct) + '%' }"></span>
                  </div>
                  <div class="guide-region-meta">
                    {{ r.entryCount }} {{ r.entryCount === 1 ? 'entry' : 'entries' }} · {{ r.pct }}%
                    <span v-if="r.expiredCount"> · {{ r.expiredCount }} expired</span>
                    <span v-if="r.malformedCount" class="guide-region-meta--warn"> · {{ r.malformedCount }} malformed tag</span>
                  </div>
                </div>
              </div>
              <div class="guide-card-foot">
                <span class="guide-card-foot-info" :title="guideContent ? `${guideContent.length} chars on disk` : ''">
                  {{ guideContent ? `${guideContent.length.toLocaleString()} chars` : '' }} · {{ guideStats.totalTokens }} tokens total
                </span>
              </div>
            </template>
            <template v-else-if="!guideLoading">
              <div class="guide-card-hint">No guide file found for this workspace.</div>
            </template>
          </div>

          <div class="mm-search">
            <input v-model="mm.search" type="text" placeholder="Search notes, tags…" autocomplete="off" />
          </div>

        <div class="mm-row-between">
          <h3>Categories</h3>
          <button type="button" class="mm-link" @click="mm.resetCategories()">reset</button>
        </div>
        <div class="mm-chip-row">
          <div
            v-for="cat in mm.categoryList"
            :key="cat.key"
            class="mm-chip"
            :class="{ off: !mm.activeCats.has(cat.key) }"
            @click="mm.toggleCategory(cat.key)"
          >
            <span class="dot" :style="{ background: cat.color }" />
            <span class="label">{{ cat.label }}</span>
            <span class="cnt">{{ cat.count }}</span>
            <button type="button" class="only" @click.stop="mm.isolateCategory(cat.key)">only</button>
          </div>
        </div>

        <template v-if="mm.mostConnected.length">
          <h3>Most connected</h3>
          <div class="mm-link-list">
            <div v-for="n in mm.mostConnected" :key="n.id" class="mm-link-item" @click="mm.requestFocus(n.id)">
              <span class="dot" :style="{ background: categoryColorFor(catKeyFor(n)) }" />
              <span class="label">{{ n.title }}</span>
              <span class="cnt">{{ n.degree }}</span>
            </div>
          </div>
        </template>

        <!-- The gap list: notes nothing links to. This is the actionable half
             of the old "orphaned" tile — each entry is either worth linking or
             worth deleting, and a count told you neither. -->
        <template v-if="mm.orphanNotes.length">
          <div class="mm-row-between">
            <h3>Unlinked ({{ mm.orphanNotes.length }})</h3>
            <div class="mm-row-actions">
              <button
                type="button"
                class="mm-link"
                :class="{ 'mm-link--active': mm.orphanFilter === 'only' }"
                :title="mm.orphanFilter === 'only' ? 'Show all notes in graph' : 'Show only unlinked notes in graph'"
                @click="mm.toggleOnlyOrphans()"
              >{{ mm.orphanFilter === 'only' ? 'show all' : 'only' }}</button>
              <span class="mm-sep" aria-hidden="true">·</span>
              <button
                type="button"
                class="mm-link"
                :title="mm.orphanFilter === 'hide' ? 'Show unlinked notes in graph' : 'Hide unlinked notes from graph'"
                @click="mm.toggleHideOrphans()"
              >{{ mm.orphanFilter === 'hide' ? 'show in graph' : 'hide in graph' }}</button>
            </div>
          </div>
          <div class="mm-link-list">
            <div
              v-for="n in mm.orphanNotes.slice(0, orphanLimit)"
              :key="n.id"
              class="mm-link-item"
              title="No note links to this one"
              @click="mm.requestFocus(n.id)"
            >
              <span class="dot" :style="{ background: categoryColorFor(catKeyFor(n)) }" />
              <span class="label">{{ n.title }}</span>
            </div>
          </div>
          <button
            v-if="mm.orphanNotes.length > orphanLimit"
            type="button"
            class="mm-link"
            @click="orphanLimit += 20"
          >show {{ Math.min(20, mm.orphanNotes.length - orphanLimit) }} more</button>
        </template>

        <!-- Aging notes: facts nobody has verified within their type's
             horizon. The actionable counterpart of "Recently written" — the
             daily curation routine reviews this same list and may resolve an
             entry, but a failed or disabled run leaves it for the user. -->
        <template v-if="mm.staleNotes.length">
          <div class="mm-row-between">
            <h3>Needs review ({{ mm.staleNotes.length }})</h3>
          </div>
          <p class="mm-hint">
            Daily Memory curation checks these notes too: it re-verifies, updates,
            corrects, or removes them. They stay here until you or that run
            resolves them.
          </p>
          <button
            type="button"
            class="mm-link mm-link--block"
            @click="openRetirementReview()"
          >Open retirement review →</button>
          <div class="mm-link-list">
            <div
              v-for="n in mm.staleNotes.slice(0, staleLimit)"
              :key="n.id"
              class="mm-link-item"
              :title="`Unverified for ${n.ageDays ?? '?'} days — click to open it in the map`"
              @click="mm.requestFocus(n.id)"
            >
              <span class="dot mm-dot--stale" />
              <span class="label">{{ n.title }}</span>
              <span class="cnt">{{ mm.ageLabelOf(n) }}</span>
            </div>
          </div>
          <button
            v-if="mm.staleNotes.length > staleLimit"
            type="button"
            class="mm-link"
            @click="staleLimit += 20"
          >show {{ Math.min(20, mm.staleNotes.length - staleLimit) }} more</button>
        </template>

        <!-- Entry points into the graph: the note you last touched is almost
             always the one you opened the map about. -->
        <template v-if="mm.recentNotes.length">
          <h3>Recently written</h3>
          <div class="mm-link-list">
            <div
              v-for="n in mm.recentNotes"
              :key="n.id"
              class="mm-link-item"
              :class="{ current: mm.selectedId === n.id }"
              title="Centre the map here"
              @click="mm.requestFocus(n.id)"
            >
              <span class="dot" :style="{ background: categoryColorFor(catKeyFor(n)) }" />
              <span class="label">{{ n.title }}</span>
            </div>
          </div>
        </template>

        <h3>Path finder</h3>
        <p class="mm-hint">{{ mm.pathHint }}</p>
        <button v-if="mm.pathStart || mm.pathEnd" type="button" class="mm-link" @click="mm.resetPath()">clear path</button>
        </template>
      </div>
    </template>

    <template v-if="!collapsed && (!mode || mode === 'chat' || mode === 'project')">
      <!-- Scrollable area for chats/projects -->
      <div class="chats-scroll-area">
        <template v-if="!store.bootstrapped && store.projects.length === 0">
          <div class="project-list" role="status" aria-live="polite" aria-label="Loading chats">
            <div v-for="i in 3" :key="i" class="project-group">
              <div class="project-header"><span class="mm-shimmer-line" style="width: 46%; height: 11px;"></span></div>
              <div class="chat-list">
                <div class="chat-item" style="pointer-events: none;"><span class="mm-shimmer-line" style="width: 68%; height: 10px;"></span></div>
                <div class="chat-item" style="pointer-events: none;"><span class="mm-shimmer-line" style="width: 58%; height: 10px;"></span></div>
              </div>
            </div>
          </div>
        </template>
        <template v-else>
          <!-- Recent chats (max 5) -->
          <!-- Recent moved to the home-screen "jump back in" grid (HomeRecentChats). -->
        <div v-if="false" class="recent-section">
          <div class="recent-label">recent</div>
          <div class="recent-items">
            <button
              type="button"
              v-for="chat in store.recentChats"
              :key="'recent-' + chat.chat_id"
              class="recent-item"
              :class="{
                active: chat.chat_id === store.activeChatId,
                remote: chat.local === false,
              }"
              @click="chat.local !== false && selectChat(chat.chat_id)"
              :disabled="chat.local === false"
              :title="chat.local === false ? 'This chat lives on another instance' : ''"
            >
              <span
                v-if="chat.title_status === 'pending'"
                class="title-shimmer"
                aria-label="Generating title"
                title="Generating title..."
              />
              <span
                v-else
                class="recent-title"
                :class="{ 'chat-title--unread': store.chatUnread(chat.chat_id) > 0 }"
              >{{ chat.title }}</span>
              <ChatSignals
                :chat-id="chat.chat_id"
                density="row"
                :hue="colorForChat(chat)"
              />
              <span v-if="chat.local === false" class="remote-chip">remote</span>
              <span class="recent-project" v-if="store.projectFor(chat.chat_id)?.name">
                {{ store.projectFor(chat.chat_id)?.name }}
              </span>
            </button>
          </div>
        </div>

        <!-- Project list -->
        <h2 v-if="store.workspaceProjects.length" class="sidebar-list-label">Projects</h2>
        <div class="project-list">
          <div
            v-for="project in store.workspaceProjects"
            :key="project.project_id"
            class="project-group"
          >
            <DropdownMenuRoot
              :open="projectMenu === project.project_id"
              :modal="false"
              @update:open="onProjectMenuOpenChange(project.project_id, $event)"
            >
              <div
                class="project-header"
                :class="{
                  'is-system': project.is_auto,
                  'drag-over': isDragOverProject(project),
                  'dragging': dragProjectId === project.project_id,
                }"
                :draggable="isDraggable(project)"
                @dragstart="onProjectDragStart(project, $event)"
                @dragover.prevent="onProjectDragOver(project)"
                @drop.prevent="onProjectDrop(project)"
                @dragend="onProjectDragEnd"
                @contextmenu.prevent="openProjectMenuAt($event, project)"
              >
                <button
                  type="button"
                  class="project-icon"
                  @click="toggleProject(project.project_id)"
                  :title="expandedProjects.has(project.project_id) ? 'Collapse' : 'Expand'"
                  :aria-label="`${expandedProjects.has(project.project_id) ? 'Collapse' : 'Expand'} ${project.name}`"
                  :aria-expanded="expandedProjects.has(project.project_id)"
                >{{ expandedProjects.has(project.project_id) ? '▾' : '▸' }}</button>
                <button
                  type="button"
                  class="project-name"
                  :data-workspace-color="colorForProject(project.workspace)"
                  v-if="editingProject !== project.project_id"
                  @click="openProject(project.project_id)"
                  title="Open project page"
                >
                  {{ project.name }}
                  <span v-if="project.is_auto" class="system-chip" title="Auto-managed project">auto</span>
                  <span
                    v-if="store.projectNeedsInput(project.project_id) > 0"
                    class="rollup-needs-dot"
                    title="A chat in this project needs your answer"
                    aria-label="A chat in this project needs your answer"
                  />
                  <span
                    v-else-if="store.projectIsStreaming(project.project_id)"
                    class="rollup-ring"
                    title="A chat in this project is working"
                    aria-label="A chat in this project is working"
                  ><span class="rollup-ring-core" aria-hidden="true" /></span>
                  <span
                    v-if="store.projectUnread(project.project_id) > 0"
                    class="badge"
                    :title="`${store.projectUnread(project.project_id)} unread chats`"
                    :aria-label="`${store.projectUnread(project.project_id)} unread chats`"
                  >{{ store.projectUnread(project.project_id) }}</span>
                </button>
                <input
                  v-else
                  class="edit-input"
                  :value="project.name"
                  @keyup.enter="finishEditProject($event, project.project_id)"
                  @keyup.escape="editingProject = null"
                  @blur="finishEditProject($event, project.project_id)"
                  ref="editInput"
                  autofocus
                />
                <DropdownMenuTrigger
                  v-if="!project.is_auto"
                  as-child
                  :disabled="editingProject === project.project_id"
                >
                  <button
                    class="project-actions-btn"
                    aria-label="Project actions"
                    title="Rename or delete project"
                    @click.stop
                    @pointerdown="useProjectActionAnchor"
                    @focus="useProjectActionAnchor"
                  >&middot;&middot;&middot;</button>
                </DropdownMenuTrigger>
                <button
                  class="add-chat-btn"
                  :class="{ 'add-chat-btn--creating': store.creatingChatProjectIds[project.project_id] }"
                  :disabled="store.creatingChatProjectIds[project.project_id]"
                  aria-haspopup="dialog"
                  @click.stop="chooseNewChat(project.workspace, project.project_id)"
                  title="New chat"
                  :aria-label="`New chat in ${project.name}`"
                >{{ store.creatingChatProjectIds[project.project_id] ? '...' : '+' }}</button>
              </div>

              <DropdownMenuPortal>
                <DropdownMenuContent
                  class="context-menu"
                  :reference="projectMenuReference"
                  :side="projectMenuFromContext ? 'right' : 'bottom'"
                  :align="projectMenuFromContext ? 'start' : 'end'"
                  :side-offset="projectMenuFromContext ? 2 : 4"
                  :collision-padding="8"
                  :position-strategy="'fixed'"
                  :update-position-strategy="'always'"
                >
                  <DropdownMenuItem as-child>
                    <button @click="startEditProject(project.project_id)">Rename</button>
                  </DropdownMenuItem>
                  <DropdownMenuItem v-if="!project.vault_folder" as-child>
                    <button @click="confirmDeleteProject(project.project_id)">Delete</button>
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenuPortal>
            </DropdownMenuRoot>

            <!-- Chats in project -->
            <div v-if="expandedProjects.has(project.project_id)" class="chat-list">
              <template
                v-for="chat in store.projectChats(project.project_id)"
                :key="chat.chat_id"
              >
                <DropdownMenuRoot
                  :open="chatMenu === chat.chat_id"
                  :modal="false"
                  @update:open="onChatMenuOpenChange(chat.chat_id, $event)"
                >
                  <div
                    class="chat-item"
                    :class="{
                      active: chat.chat_id === store.activeChatId && !activeSubagentId,
                      remote: chat.local === false,
                      dragging: dragChatId === chat.chat_id,
                    }"
                    :draggable="chat.local !== false"
                    @click="chat.local !== false && selectChat(chat.chat_id)"
                    @keydown.enter.self.prevent="chat.local !== false && selectChat(chat.chat_id)"
                    @keydown.space.self.prevent="chat.local !== false && selectChat(chat.chat_id)"
                    @dragstart="onChatDragStart(chat, $event)"
                    @dragend="onChatDragEnd"
                    @contextmenu.prevent="openChatMenuAt($event, chat.chat_id)"
                    role="link"
                    :tabindex="chat.local === false ? -1 : 0"
                    :aria-disabled="chat.local === false"
                    :title="chat.local === false ? 'This chat lives on another instance' : 'Drag to move to another project'"
                  >
                    <button
                      v-if="subagentsFor(chat.chat_id).length"
                      type="button"
                      class="subagent-toggle"
                      :aria-expanded="subagentsExpanded(chat.chat_id)"
                      :aria-label="(subagentsExpanded(chat.chat_id) ? 'Collapse' : 'Expand') + ' subagents for ' + chat.title"
                      :title="(subagentsExpanded(chat.chat_id) ? 'Collapse' : 'Expand') + ' subagents'"
                      @click.stop="toggleSubagents(chat.chat_id)"
                    >{{ subagentsExpanded(chat.chat_id) ? '▾' : '▸' }}</button>
                    <span
                      v-if="chat.title_status === 'pending'"
                      class="title-shimmer"
                      aria-label="Generating title"
                      title="Generating title..."
                    />
                    <span
                      v-else
                      class="chat-title"
                      :class="{ 'chat-title--unread': store.chatUnread(chat.chat_id) > 0 }"
                    >{{ chat.title }}</span>
                    <ChatSignals
                      :chat-id="chat.chat_id"
                      density="row"
                      :hue="colorForChat(chat)"
                    />
                    <span v-if="chat.local === false" class="remote-chip">remote</span>
                    <DropdownMenuTrigger as-child>
                      <button
                        class="chat-actions-btn"
                        aria-label="Chat actions"
                        title="Copy ID, rename, move, archive, delete"
                        @click.stop
                        @pointerdown="useChatActionAnchor"
                        @focus="useChatActionAnchor"
                      >&middot;&middot;&middot;</button>
                    </DropdownMenuTrigger>
                  </div>

                  <DropdownMenuPortal>
                    <DropdownMenuContent
                      class="context-menu"
                      :reference="chatMenuReference"
                      :side="chatMenuFromContext ? 'right' : 'bottom'"
                      :align="chatMenuFromContext ? 'start' : 'end'"
                      :side-offset="chatMenuFromContext ? 2 : 4"
                      :collision-padding="8"
                      :position-strategy="'fixed'"
                      :update-position-strategy="'always'"
                      aria-label="Chat actions"
                    >
                      <DropdownMenuItem as-child>
                        <button @click="copyChatId(chatMenu!)">Copy chat ID</button>
                      </DropdownMenuItem>
                      <DropdownMenuItem as-child>
                        <button @click="startRenameChat(chatMenu!)">Rename</button>
                      </DropdownMenuItem>
                      <DropdownMenuSub
                        v-if="moveTargets.length"
                        v-model:open="moveSubmenu"
                      >
                        <DropdownMenuSubTrigger as-child>
                          <button>Move to...</button>
                        </DropdownMenuSubTrigger>
                        <DropdownMenuSubContent
                          class="context-menu"
                          side="right"
                          align="start"
                          :side-offset="4"
                          :collision-padding="8"
                          :position-strategy="'fixed'"
                          :update-position-strategy="'always'"
                        >
                          <DropdownMenuItem as-child>
                            <button
                              v-for="target in moveTargets"
                              :key="target.project_id"
                              @click="doMoveChat(target.project_id)"
                            >{{ target.name }}</button>
                          </DropdownMenuItem>
                          <DropdownMenuItem as-child @select.prevent>
                            <button class="context-menu-back" @click="moveSubmenu = false">← Back</button>
                          </DropdownMenuItem>
                        </DropdownMenuSubContent>
                      </DropdownMenuSub>
                      <DropdownMenuItem v-if="chatMenuChat?.retry?.status === 'pending'" as-child>
                        <button @click="stopRetry(chatMenu!)">Stop trying</button>
                      </DropdownMenuItem>
                      <DropdownMenuItem v-else as-child>
                        <button @click="setRetry(chatMenu!)">Set to retry</button>
                      </DropdownMenuItem>
                      <DropdownMenuItem v-if="chatMenuChat && !chatMenuChat.archived" as-child>
                        <button @click="doMarkUnread(chatMenu!)">Mark unread</button>
                      </DropdownMenuItem>
                      <DropdownMenuItem as-child>
                        <button @click="doArchiveChat(chatMenu!)">
                          {{ ARCHIVE_MENU_LABEL }}
                        </button>
                      </DropdownMenuItem>
                      <DropdownMenuItem as-child>
                        <button @click="confirmDeleteChat(chatMenu!)">Delete</button>
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenuPortal>
                </DropdownMenuRoot>

                <!-- Subagents this chat has working right now. They are not
                     chats: the row opens a read-only view of the agent's own
                     transcript, and it disappears when the agent finishes
                     (the completed transcript stays in the chat's Activity
                     trace). -->
                <template v-if="subagentsExpanded(chat.chat_id)">
                  <RouterLink
                    v-for="sub in subagentsFor(chat.chat_id)"
                    :key="sub.agent_id"
                    class="chat-item subagent-item"
                    :class="{ active: isActiveSubagent(chat.chat_id, sub.agent_id) }"
                    :to="subagentPath(chat.chat_id, sub.agent_id)"
                    :title="subagentLabel(sub) + ' — running in ' + chat.title"
                    @click="emit('chat-selected')"
                  >
                    <span class="subagent-mark" aria-hidden="true">&#8627;</span>
                    <span class="chat-title">{{ subagentLabel(sub) }}</span>
                    <span
                      v-if="sub.subagent_type"
                      class="subagent-chip"
                    >{{ sub.subagent_type }}</span>
                    <span
                      class="subagent-spinner"
                      role="img"
                      aria-label="Working"
                      title="Working"
                    />
                  </RouterLink>
                </template>
              </template>
            </div>
          </div>
        </div>
        </template>
      </div>

      <!-- Add project button + archived-projects entry point -->
      <div class="sidebar-footer">
        <button class="add-project-btn" @click="addProject">+ New Project</button>
        <button
          class="archive-btn"
          @click="openArchive"
          title="Completed projects"
          aria-label="Completed projects"
        >
          <!-- Archive box: lid over a bin, the conventional "archived" glyph -->
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
            <rect x="3" y="4" width="18" height="4" />
            <path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8" />
            <line x1="10" y1="12" x2="14" y2="12" />
          </svg>
        </button>
      </div>
    </template>
  </aside>

  <!-- Completed (archived) projects dialog -->
  <div v-if="archiveOpen" class="modal-overlay" @click.self="archiveOpen = false">
    <div class="modal modal--archive">
      <h3>completed projects</h3>
      <p class="archive-hint">
        Click a project to open its canonical doc. Restoring moves it back to active; old chats stay archived.
      </p>
      <div v-if="loadingCompleted" class="archive-empty">Loading...</div>
      <div v-else-if="!completedProjects.length" class="archive-empty">
        // no completed projects in {{ store.activeWorkspace }}
      </div>
      <div v-else class="archive-list">
        <div v-for="cp in completedProjects" :key="cp.stem" class="archive-item">
          <button
            type="button"
            class="archive-name"
            :class="{ 'archive-name--clickable': cp.vault_doc_path }"
            :title="cp.vault_doc_path ? 'Open canonical doc' : (cp.context || cp.name)"
            :disabled="!cp.vault_doc_path"
            @click="openCompletedDoc(cp)"
          >{{ cp.name }}</button>
          <button
            class="btn-small archive-restore"
            :disabled="restoringStem === cp.stem"
            @click="doRestore(cp)"
          >{{ restoringStem === cp.stem ? '...' : 'Restore' }}</button>
        </div>
      </div>
      <div class="modal-actions">
        <button @click="archiveOpen = false">Close</button>
      </div>
    </div>
  </div>

  <!-- Rename chat dialog -->
  <div v-if="renamingChat" class="modal-overlay" @click.self="renamingChat = null">
    <div class="modal">
      <h3>rename chat</h3>
      <input v-model="renameValue" @keyup.enter="doRenameChat" autofocus />
      <div class="modal-actions">
        <button @click="renamingChat = null">Cancel</button>
        <button class="btn-primary" @click="doRenameChat">Save</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuRoot,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from 'reka-ui'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { errorMessage } from '../lib/errorMessage'
import { useTaskStore } from '../stores/tasks'
import { useHousekeepingStore } from '../stores/housekeeping'
import { useFileViewerStore } from '../stores/fileViewer'
import { useMemoryMapStore, categoryColorFor, catKeyFor } from '../stores/memoryMap'
import { useProposalsStore } from '../stores/proposals'
import { useVaultReviewStore } from '../stores/vaultReview'
import ChatSignals from './ChatSignals.vue'
import BrandMark from './BrandMark.vue'
import { isApplePlatform, isDesktopApp } from '../lib/desktop'
import { scheduleInWorkspace } from '../lib/automationWorkspace'
import { colorForWorkspace } from '../lib/workspaceColors'
import { ARCHIVE_CONFIRM_MESSAGE, ARCHIVE_MENU_LABEL } from '../lib/archiveCopy'
import { askConfirm } from '../lib/confirm'
import { workspaceLabel } from '../lib/workspaceLabel'
import { kindLabel as reviewKindLabel } from '../lib/proposalKinds'
import { askPrompt } from '../lib/prompt'
import { writeClipboard } from '../lib/codeCopy'
import { startFileDiscussion } from '../lib/fileDiscussion'
import { openNewChatPicker } from '../lib/newChat'

const props = defineProps<{ collapsed: boolean; mode?: 'chat' | 'project' | 'schedules' | 'settings' | 'memory' | 'proposals' }>()
const emit = defineEmits<{ toggle: []; 'chat-selected': []; 'new-schedule': [] }>()

const store = useProjectStore()
const taskStore = useTaskStore()
const housekeeping = useHousekeepingStore()
const fileViewer = useFileViewerStore()
const mm = useMemoryMapStore()
const proposals = useProposalsStore()
const vaultReview = useVaultReviewStore()

// Review-queue figures for the sidebar. Scoped counts come from the store so
// they use the same workspace rule as the list — a chip that disagreed with the
// rows under it would be worse than no chip.
// Retirement counts mirror `retirementCount`/`trashCount` in MemoryMapView:
// the store holds one workspace at a time, so a load for another workspace
// must read as zero here rather than as the previous workspace's queue.
const retirementLoaded = computed(() => vaultReview.loadedWorkspace === store.activeWorkspace)
// `retirementLoaded` alone collapsed three states into one number: the queue
// not fetched yet, a fetch that failed (the store swallows the error and
// leaves `loadedWorkspace` where it was), and a queue that really is empty.
// The first two rendered a hard 0/0/0 beside a panel saying "Loading
// candidates…" — the stats-vs-rows contradiction this column exists to avoid.
// The Proposals column above solves it with a skeleton; so does this one.
const retirementPending = computed(() => !retirementLoaded.value && !vaultReview.error)
const retirementFailed = computed(() => !retirementLoaded.value && Boolean(vaultReview.error))
const retirementScoped = computed(() => (retirementLoaded.value ? vaultReview.candidates.length : 0))
const retirementTrashed = computed(() => (retirementLoaded.value ? vaultReview.trashed.length : 0))
// Distinct reasons across the queue, not a sum: one note flagged unlinked and
// weak_provenance is two signals on one row, and the number is there to say
// what kind of work the queue holds.
const retirementSignals = computed(() => {
  if (!retirementLoaded.value) return 0
  const seen = new Set<string>()
  for (const c of vaultReview.candidates) for (const s of c.signals) seen.add(s)
  return seen.size
})

const reviewScoped = computed(() => proposals.scopedRows(store.activeWorkspace).length)
// What the Review button promises: everything in this workspace still waiting
// on a decision, across both of its queues. Retired notes and the decision
// ledger are records, not work, so neither counts here.
const decisionsWaiting = computed(() => reviewScoped.value + retirementScoped.value)
const reviewVisible = computed(() => proposals.visibleRows(store.activeWorkspace).length)
const reviewElsewhere = computed(() => proposals.rows.length - reviewScoped.value)
const reviewKinds = computed(() => proposals.kindCounts(store.activeWorkspace))
const hasBlockingHousekeeping = computed(() => housekeeping.actions.some(action => action.blocking))
const settingsNeedsAttention = computed(() => Boolean(store.packageStatus?.update_available || hasBlockingHousekeeping.value))

onMounted(() => {
  // These signals are needed by the rail even when the home or review pane has
  // never been opened. Both store initializers are idempotent.
  housekeeping.init()
  void proposals.ensureLoaded()
  void vaultReview.ensureLoaded(store.activeWorkspace)
  document.addEventListener('click', closeWorkspaceMenuOnOutside)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', closeWorkspaceMenuOnOutside)
})

// The unlinked list is the one section that can run to hundreds of entries on a
// real vault, so it grows on demand rather than pushing every other section off
// the bottom of the sidebar.
const orphanLimit = ref(8)
const staleLimit = ref(8)

// ---------- workspace guide card (AGENTS.md; CLAUDE.md pre-migration) ----------
const GUIDE_DEFAULTS: Record<string, { label: string; limit: number }> = {
  memory: { label: 'Agent memory', limit: 3000 },
  profile: { label: 'User profile', limit: 1375 },
}
const guideContent = ref('')
const guideLoading = ref(false)
const guideError = ref('')
const guideResolvedPath = ref('') // actual file that existed: AGENTS.md, or a legacy CLAUDE.md
const guidePathLabel = computed(() => guideResolvedPath.value || 'AGENTS.md')
const GUIDE_REGION_RE: Record<string, RegExp> = {
  memory: /<!--\s*ciao:memory:start(?:\s+cap=(\d+))?\s*-->([\s\S]*?)<!--\s*ciao:memory:end\s*-->/i,
  profile: /<!--\s*ciao:profile:start(?:\s+cap=(\d+))?\s*-->([\s\S]*?)<!--\s*ciao:profile:end\s*-->/i,
}
function parseEntriesForRegion(raw: string): string[] {
  const headingStripped = raw.replace(/^\s*##\s*(Agent memory|User profile)\s*\n?/, '')
  const parts = headingStripped.split(/\n?§\n?/)
  return parts.map(p => p.trim()).filter(Boolean)
}
function serializeLen(entries: string[]): number {
  if (!entries.length) return 0
  return entries.join('\n§\n').length + 1 // +1 trailing \n mirrors python serialize_entries
}
function tokensFor(chars: number): number { return Math.ceil(chars / 4) || 0 }
function expirationInfo(entry: string): { expired: boolean; malformed: boolean } {
  const hasPrefix = /\[expires\s*:/i.test(entry)
  const m = entry.match(/\[expires:\s*([^\]]*)\]/i)
  if (!m) return { expired: false, malformed: hasPrefix }
  const raw = m[1].trim()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) return { expired: false, malformed: true }
  // Reject impossible dates (e.g. 2026-02-30): JS normalizes them to a later
  // day, so Number.isNaN alone would report them as valid. Round-trip the
  // parsed year/month/day back to the original string to agree with the
  // backend validator, which rejects these as malformed.
  const [y, mo, da] = raw.split('-').map(Number)
  const d = new Date(y, mo - 1, da)
  const roundTrips = d.getFullYear() === y && d.getMonth() === mo - 1 && d.getDate() === da
  if (!roundTrips) return { expired: false, malformed: true }
  const today = new Date(); today.setHours(0,0,0,0)
  return { expired: d < today, malformed: false }
}
const guideStats = computed(() => {
  const content = guideContent.value
  if (!content) return null
  const regions: Array<{
    key: string; label: string; usedChars: number; charLimit: number; pct: number;
    tokens: number; entryCount: number; expiredCount: number; malformedCount: number; overCap: boolean
  }> = []
  let totalChars = 0
  for (const key of ['memory', 'profile'] as const) {
    const re = GUIDE_REGION_RE[key]
    const match = content.match(re)
    let cap = GUIDE_DEFAULTS[key].limit
    let body = ''
    if (match) {
      if (match[1]) { const n = Number(match[1]); if (Number.isFinite(n)) cap = n }
      body = match[2] || ''
    }
    const entries = body ? parseEntriesForRegion(body) : []
    const used = serializeLen(entries)
    totalChars += used
    let expired = 0, malformed = 0
    for (const e of entries) { const info = expirationInfo(e); if (info.expired) expired++; if (info.malformed) malformed++ }
    const pct = cap ? Math.round((used / cap) * 100 * 10) / 10 : 0
    regions.push({
      key, label: GUIDE_DEFAULTS[key].label,
      usedChars: used, charLimit: cap, pct, tokens: tokensFor(used),
      entryCount: entries.length, expiredCount: expired, malformedCount: malformed,
      overCap: used > cap,
    })
  }
  return { regions, totalTokens: tokensFor(content.length), totalChars: content.length }
})
const guideOverCap = computed(() => !!guideStats.value?.regions.some(r => r.overCap))
const canDiscussGuide = computed(() => !!guideResolvedPath.value && !guideLoading.value && !guideError.value)
let guideFetchSeq = 0
async function fetchGuide(): Promise<void> {
  const seq = ++guideFetchSeq
  guideLoading.value = true
  guideError.value = ''
  // AGENTS.md is the workspace guide; CLAUDE.md is only what an install that
  // has not run the guide migration still has (ciao/workspace_guide.py), so it
  // is tried second and will stop appearing once installs have upgraded.
  //
  // After the workspace re-root migration each guide lives under
  // `<workspace>/AGENTS.md`, so a bare basename would let /api/workspace-file's
  // fuzzy lookup silently resolve to the lexicographically-first workspace's
  // guide. Try the workspace-qualified path first (retained for Open/Discuss/
  // pin), then fall back to the bare basename for installs that have not
  // re-rooted (guide still at the install root).
  const ws = store.activeWorkspace
  const qualified = [`${ws}/AGENTS.md`, `${ws}/CLAUDE.md`]
  const bare = ['AGENTS.md', 'CLAUDE.md']
  let lastError = ''
  let qualifiedErrored = false
  for (const candidate of [...qualified, ...bare]) {
    // A bare basename can fuzzy-resolve to a DIFFERENT workspace's guide
    // (routes_helpers._resolve_workspace_path anchors relative paths to the
    // primary root), so it is only a legitimate fallback when every
    // workspace-qualified probe genuinely 404'd. If one of them errored we
    // do not know whether this workspace has a guide, and showing another
    // one's — with Open/Discuss/pin acting on it — is worse than showing
    // nothing.
    if (bare.includes(candidate) && qualifiedErrored) break
    try {
      // `exact=1`: no fuzzy fallback. Without it, asking for
      // `<ws>/AGENTS.md` on a workspace that has no guide yet
      // filename-matches another workspace's and returns it with a 200,
      // so the card would render someone else's guide as this one's.
      const resp = await fetch(`/api/workspace-file?exact=1&path=${encodeURIComponent(candidate)}`, { credentials: 'same-origin' })
      if (seq !== guideFetchSeq) return
      if (resp.status === 404) continue
      // Keep trying the remaining candidates rather than giving up on the
      // first non-404: a transient 503 (the engine restarting) on the first
      // name used to blank the card even though a later name would have
      // served it. The error is only shown if every candidate fails.
      if (!resp.ok) {
        lastError = `Failed to load ${candidate} (HTTP ${resp.status})`
        if (qualified.includes(candidate)) qualifiedErrored = true
        continue
      }
      const text = await resp.text()
      if (seq !== guideFetchSeq) return
      guideContent.value = text
      guideResolvedPath.value = candidate
      guideError.value = ''
      guideLoading.value = false
      return
    } catch (e) {
      if (qualified.includes(candidate)) qualifiedErrored = true
      if (seq === guideFetchSeq) { guideError.value = e instanceof Error ? e.message : String(e) }
    }
  }
  if (seq !== guideFetchSeq) return
  guideContent.value = ''
  // Every candidate 404'd (no guide yet) or errored. Surface the last real
  // error if there was one; a plain "not found" stays silent, because a
  // workspace with no guide yet is an ordinary state, not a failure.
  if (!guideError.value) guideError.value = lastError
  guideResolvedPath.value = ''
  guideLoading.value = false
}
watch(() => store.activeWorkspace, () => { void fetchGuide() }, { immediate: true })
function openGuideFile(): void {
  if (!guideResolvedPath.value) return
  void fileViewer.open(guideResolvedPath.value)
}
async function discussGuide(): Promise<void> {
  if (!guideResolvedPath.value) return
  const path = guideResolvedPath.value
  // Reuse the generic file-discuss flow (creates a chat and pins the guide).
  await discussFileInChat(path, `Let's review the workspace guide \`${path}\`. Help me audit it — what should we trim, clarify, or promote from the bounded regions?`)
}
async function discussFileInChat(path: string, prompt?: string): Promise<void> {
  await startFileDiscussion(store, { path, seed: prompt || `Let's discuss the file \`${path}\`.` })
}
// Expose for template's generic file discuss (also used by FileViewerModal/PinnedFilePanel via a shared helper fallback)
// and for the guide card's "Discuss" button.
const route = useRoute()
const router = useRouter()

/** The memory page's Memory/Review switcher. Review is the /proposals
 * route; graph and list are both /memory, so only those two need a push.
 *
 * A graph/list choice is also remembered in `mapView`, so the Memory button
 * returns to the drawing that was on screen rather than resetting to graph. */
function setMemoryView(next: 'graph' | 'list' | 'review') {
  mm.view = next
  if (next !== 'review') mm.mapView = next
  const target = next === 'review' ? '/proposals' : '/memory'
  if (route.path !== target) void router.push(target)
}

/** The "Needs review" list lands directly on the retirement queue. */
function openRetirementReview() {
  mm.reviewTab = 'retirement'
  // Always the queue, never the trash: this link means "show me what is
  // waiting", and the sub-tab could be left on Trash from a previous visit.
  mm.retirementTab = 'candidates'
  setMemoryView('review')
}

function promptTitle(prompt: string): string {
  const first = prompt.split('\n')[0].trim()
  return first.length > 36 ? first.slice(0, 33) + '...' : first
}

// With a single workspace the toggle is pure noise — hide it and let the
// content fill the space.
const hasMultipleWorkspaces = computed(() => store.workspaceOptions.length > 1)
// Settings tabs, in rail order. /settings is the General tab (route kept as-is).
const SETTINGS_NAV = [
  { to: '/settings', label: 'General' },
  { to: '/settings/workspaces', label: 'Workspaces' },
  { to: '/settings/models', label: 'Models & providers' },
  { to: '/settings/skills', label: 'Skills' },
  { to: '/settings/subagents', label: 'Subagents' },
  { to: '/settings/commands', label: 'Commands' },
  { to: '/settings/mcp', label: 'MCP servers' },
  { to: '/settings/automations', label: 'Automations' },
  { to: '/settings/notifications', label: 'Notifications' },
] as const
// The chord ChatLayout binds for New chat: Cmd+T in the desktop shell (the
// browser keeps Cmd+T for itself), Option/Alt+N everywhere else.
const newChatShortcut = isDesktopApp() ? '⌘T' : isApplePlatform() ? '⌥N' : 'Alt+N'
const newChatKeyshortcuts = isDesktopApp() ? 'Meta+T' : 'Alt+N'
const workspaceScopeOpen = ref(false)
const workspaceScopeEl = ref<HTMLElement | null>(null)
const workspaceScopeTrigger = ref<HTMLButtonElement | null>(null)
const workspaceScopeMenu = ref<HTMLElement | null>(null)

// Review + retirement work waiting in a workspace, used only as the scope
// trigger's attention badge. `retirementScoped` is zero for other workspaces,
// so their count is the proposals queue alone.
function workspaceActionCount(workspace: string): number {
  const retirement = vaultReview.loadedWorkspace === workspace ? vaultReview.candidates.length : 0
  return proposals.scopedRows(workspace).length + retirement + missedCountFor(workspace)
}

function openWorkspaceMenu(): void {
  if (!hasMultipleWorkspaces.value) return
  workspaceScopeOpen.value = true
  void nextTick(() => {
    workspaceScopeMenu.value
      ?.querySelector<HTMLButtonElement>('[role="menuitem"]')
      ?.focus()
  })
}

function closeWorkspaceMenu(restoreFocus = false): void {
  if (!workspaceScopeOpen.value) return
  workspaceScopeOpen.value = false
  if (restoreFocus) void nextTick(() => workspaceScopeTrigger.value?.focus())
}

function toggleWorkspaceMenu(): void {
  if (!hasMultipleWorkspaces.value) return
  if (workspaceScopeOpen.value) closeWorkspaceMenu(true)
  else openWorkspaceMenu()
}

function selectWorkspaceScope(workspace: string): void {
  closeWorkspaceMenu(true)
  if (props.mode === 'schedules') {
    void selectAutomationWorkspace(workspace)
    return
  }
  void store.switchWorkspace(workspace, { transition: false })
}

function onWorkspaceMenuKeydown(event: KeyboardEvent): void {
  const items = Array.from(workspaceScopeMenu.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
  if (!items.length) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    closeWorkspaceMenu(true)
    return
  }
  if (event.key === 'Tab') {
    closeWorkspaceMenu(false)
    return
  }
  const current = items.indexOf(document.activeElement as HTMLButtonElement)
  let next = current
  if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = items.length - 1
  else if (current < 0) next = event.key === 'ArrowUp' ? items.length - 1 : 0
  else if (event.key === 'ArrowDown') next = (current + 1) % items.length
  else if (event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length
  else return
  event.preventDefault()
  items[next]?.focus()
}

function closeWorkspaceMenuOnOutside(event: MouseEvent): void {
  if (!workspaceScopeOpen.value) return
  const target = event.target as Node | null
  if (target && workspaceScopeEl.value?.contains(target)) return
  closeWorkspaceMenu(false)
}

// Schedule list split: one-offs first (sorted by datetime), then recurring.
const workspaceSchedules = computed(() =>
  taskStore.schedules.filter(s => scheduleInWorkspace(
    s,
    store.activeWorkspace,
    store.chats,
    store.projects,
  )),
)

/**
 * The badge in a row's time slot: paused, cadence, or fire time.
 *
 * An interval entry has no time of day, so showing `daily_time_utc` left the
 * slot blank and the row unreadable. Its cadence goes there instead.
 */
function cadenceBadge(s: Schedule): string {
  if (!s.enabled) return 'off'
  if (s.frequency === 'interval') return `${s.interval_minutes}m`
  if (s.frequency === 'manual') return '·'
  return s.daily_time_utc
}

const oneOffSchedules = computed(() => {
  return workspaceSchedules.value
    .filter(s => s.frequency === 'once')
    .slice()
    .sort((a, b) => {
      const ka = `${a.run_at_date || ''} ${a.daily_time_utc || ''}`
      const kb = `${b.run_at_date || ''} ${b.daily_time_utc || ''}`
      return ka.localeCompare(kb)
    })
})
const userRoutines = computed(() =>
  workspaceSchedules.value.filter(s => s.frequency !== 'once' && s.scope !== 'system'),
)
const systemAutomations = computed(() =>
  workspaceSchedules.value.filter(s => s.frequency !== 'once' && s.scope === 'system'),
)

// ── Keyboard navigation for the schedules list (mirrors HomeRecentChats) ──────
const schedulesListEl = ref<HTMLElement | null>(null)

function focusableSchedules(): HTMLElement[] {
  const root = schedulesListEl.value
  if (!root) return []
  return Array.from(root.querySelectorAll<HTMLElement>('.schedule-item:not([aria-disabled="true"])'))
}

function focusScheduleElement(element: HTMLElement) {
  element.focus()
  element.scrollIntoView({ block: 'nearest' })
}

function clamp(value: number, lower: number, upper: number): number {
  return Math.min(upper, Math.max(lower, value))
}

function onArrow(key: string): boolean {
  if (props.collapsed) return false
  const items = focusableSchedules()
  if (!items.length) return false
  const active = document.activeElement as HTMLElement | null
  let index = items.indexOf(active as HTMLElement)
  if (index < 0) {
    // Start from the active route, or the first item. This mirrors the
    // homepage's anchorLane logic but for a flat list.
    const activeLink = rootActiveScheduleElement(items)
    if (activeLink) {
      index = items.indexOf(activeLink)
    } else {
      items[0]?.focus()
      items[0]?.scrollIntoView({ block: 'nearest' })
      return true
    }
  }
  const delta = key === 'ArrowDown' || key === 'ArrowRight' ? 1 : key === 'ArrowUp' || key === 'ArrowLeft' ? -1 : 0
  if (!delta) return false
  const nextIndex = clamp(index + delta, 0, items.length - 1)
  if (nextIndex === index) return true
  focusScheduleElement(items[nextIndex])
  return true
}

function rootActiveScheduleElement(items: HTMLElement[]): HTMLElement | null {
  // The router-link for the current /schedules/:id carries .active (or
  // .router-link-active). Prefer it so arrows start where the user is.
  const active = schedulesListEl.value?.querySelector<HTMLElement>('.schedule-item.active')
  if (active && items.includes(active)) return active
  return null
}

defineExpose({ onArrow })

async function selectAutomationWorkspace(workspace: string) {
  if (store.activeWorkspace !== workspace) {
    await store.switchWorkspace(workspace, { transition: false })
  }
  if (props.mode === 'schedules') await router.push('/schedules')
}

function missedCountFor(workspace: string): number {
  return taskStore.schedules.filter(
    // Same arguments as `workspaceSchedules`, or the badge counts a different
    // set than the list below it: an entry with no stored workspace (every
    // chat-bound one created before the stamp landed) resolves only through
    // the chat/project fallback.
    s => s.enabled && s.missed && scheduleInWorkspace(
      s, workspace, store.chats, store.projects,
    ),
  ).length
}

import type { ChatInfo, ProjectInfo, RunningSubagent, Schedule } from '../lib/types'
import { bareAgentId, shortAgentId, subagentPath } from '../lib/subagentIds'
function openProject(projectId: string) {
  router.push(`/project/${projectId}`)
  emit('chat-selected') // collapse sidebar on mobile
}

const expandedProjects = reactive(new Set<string>())
// Subagent groups are open by default: a row only exists while its agent is
// working, so hiding it behind a closed disclosure would defeat the point.
// Deliberately component-local, matching the project disclosure state above
// and the chat context disclosure in ChatPanel.
const collapsedSubagentParents = reactive(new Set<string>())
const projectMenu = ref<string | null>(null)
const chatMenu = ref<string | null>(null)
type MenuReference = HTMLElement | { getBoundingClientRect: () => DOMRect }
const projectMenuAnchor = ref<MenuReference | null>(null)
const chatMenuAnchor = ref<MenuReference | null>(null)
const projectMenuFromContext = ref(false)
const chatMenuFromContext = ref(false)
const projectMenuReference = computed(() => projectMenuAnchor.value ?? undefined)
const chatMenuReference = computed(() => chatMenuAnchor.value ?? undefined)
const moveSubmenu = ref(false)
const editingProject = ref<string | null>(null)
const renamingChat = ref<string | null>(null)
const renameValue = ref('')
// One definition, in the store: this used to OR the sources by hand and so
// missed background runs entirely when they were added.
const isAnyChatWorking = computed(() => store.anyChatBusy)

const hasAutomationWarning = computed(() => {
  return taskStore.schedules.some(s => {
    // `missing-chat` is set at the same moment the entry is disabled (the
    // dispatcher found neither a target chat nor a project to re-home into),
    // so gating it on `enabled` hid the one failure that cannot fix itself.
    if (s.last_status === 'missing-chat') return true
    return s.enabled && (s.missed || s.last_status === 'error')
  })
})
// Destination projects for "Move to..." — same workspace as the chat,
// excluding the chat's current project. Backend rejects cross-workspace moves.
const chatMenuChat = computed<ChatInfo | null>(() => {
  const cid = chatMenu.value
  if (!cid) return null
  return store.chats.find(c => c.chat_id === cid) || null
})

const moveTargets = computed<ProjectInfo[]>(() => {
  const cid = chatMenu.value
  if (!cid) return []
  const chat = store.chats.find(c => c.chat_id === cid)
  if (!chat) return []
  return store.workspaceProjects
    .filter(p => p.project_id !== chat.project_id)
    .slice()
    .sort((a, b) => {
      // Pin "General" first, then alphabetical.
      if (a.name === 'General') return -1
      if (b.name === 'General') return 1
      return a.name.localeCompare(b.name)
    })
})

function contextReference(event: MouseEvent): MenuReference {
  const x = event.clientX
  const y = event.clientY
  return {
    getBoundingClientRect: () => ({
      x,
      y,
      left: x,
      right: x,
      top: y,
      bottom: y,
      width: 0,
      height: 0,
      toJSON: () => ({}),
    } as DOMRect),
  }
}

function onProjectMenuOpenChange(projectId: string, nextOpen: boolean): void {
  if (nextOpen) {
    projectMenu.value = projectId
    return
  }
  if (projectMenu.value === projectId) {
    projectMenu.value = null
    projectMenuAnchor.value = null
    projectMenuFromContext.value = false
  }
}

function onChatMenuOpenChange(chatId: string, nextOpen: boolean): void {
  if (nextOpen) {
    chatMenu.value = chatId
    return
  }
  if (chatMenu.value === chatId) closeChatMenus()
}

function useProjectActionAnchor(): void {
  projectMenuAnchor.value = null
  projectMenuFromContext.value = false
}

function useChatActionAnchor(): void {
  chatMenuAnchor.value = null
  chatMenuFromContext.value = false
}

function openProjectMenuAt(event: MouseEvent, project: ProjectInfo): void {
  if (project.is_auto) {
    closeProjectMenu()
    return
  }
  if (projectMenu.value === project.project_id) {
    closeProjectMenu()
    return
  }
  projectMenuAnchor.value = contextReference(event)
  projectMenuFromContext.value = true
  projectMenu.value = project.project_id
}

function openChatMenuAt(event: MouseEvent, chatId: string): void {
  if (chatMenu.value === chatId) {
    closeChatMenus()
    return
  }
  chatMenuAnchor.value = contextReference(event)
  chatMenuFromContext.value = true
  chatMenu.value = chatId
}

function closeProjectMenu(): void {
  projectMenu.value = null
  projectMenuAnchor.value = null
  projectMenuFromContext.value = false
}

function closeChatMenus(): void {
  chatMenu.value = null
  chatMenuAnchor.value = null
  chatMenuFromContext.value = false
  moveSubmenu.value = false
}

// Reset the submenu whenever the active chat menu changes (open, close,
// switch chats), so re-opening always starts at the top-level menu.
watch(chatMenu, () => { moveSubmenu.value = false })

async function doMoveChat(targetProjectId: string) {
  const cid = chatMenu.value
  closeChatMenus()
  if (!cid) return
  await moveChatToProject(cid, targetProjectId)
}

async function moveChatToProject(chatId: string, targetProjectId: string) {
  try {
    await store.moveChat(chatId, targetProjectId)
    expandedProjects.add(targetProjectId)
  } catch (e) {
    store.pushErrorToast('Could not move chat', `${errorMessage(e)}`)
  }
}

// Auto-expand all projects when they load (and keep new ones expanded)
watch(() => store.workspaceProjects, (projects) => {
  for (const p of projects) {
    expandedProjects.add(p.project_id)
  }
}, { immediate: true })

// Opening a subagent view must never leave its row hidden behind a collapsed
// disclosure — the route is reachable from the chat's Activity trace too.
watch(() => route.params.agentId, (agentId) => {
  const chatId = route.params.chatId as string
  if (agentId && chatId) collapsedSubagentParents.delete(chatId)
}, { immediate: true })

watch(() => store.activeChatId, (chatId) => {
  if (!chatId) return
  const project = store.projectFor(chatId)
  if (project) {
    expandedProjects.add(project.project_id)
  }
}, { immediate: true })

function selectChat(chatId: string) {
  store.switchChat(chatId)
  emit('chat-selected')
}

// ── Drag-to-reorder projects ──────────────────────────────────────────────
// General is auto-managed and pinned to the top (order 0) by the server, so
// it isn't draggable; everything else can be dragged into a new order.
const dragProjectId = ref<string | null>(null)
const dragChatId = ref<string | null>(null)
const dragChatProjectId = ref<string | null>(null)
const dragOverProjectId = ref<string | null>(null)

function isDraggable(project: ProjectInfo): boolean {
  return project.name !== 'General' && !project.is_auto
}

function onProjectDragStart(project: ProjectInfo, event: DragEvent) {
  if (!isDraggable(project)) return
  dragChatId.value = null
  dragChatProjectId.value = null
  dragProjectId.value = project.project_id
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    // Firefox requires data to be set for the drag to start.
    event.dataTransfer.setData('text/plain', project.project_id)
  }
}

function onChatDragStart(chat: ChatInfo, event: DragEvent) {
  if (chat.local === false) return
  dragProjectId.value = null
  dragChatId.value = chat.chat_id
  dragChatProjectId.value = chat.project_id
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('application/x-ciaobot-chat', chat.chat_id)
    // Keep a text payload for browsers that do not expose custom MIME types.
    event.dataTransfer.setData('text/plain', chat.chat_id)
  }
}

function onProjectDragOver(project: ProjectInfo) {
  if (dragChatId.value) {
    if (dragChatProjectId.value === project.project_id) {
      dragOverProjectId.value = null
      return
    }
    dragOverProjectId.value = project.project_id
    return
  }
  if (dragProjectId.value && dragProjectId.value !== project.project_id) {
    dragOverProjectId.value = project.project_id
  }
}

function onProjectDrop(target: ProjectInfo) {
  const draggedChatId = dragChatId.value
  const draggedChatProjectId = dragChatProjectId.value
  if (draggedChatId) {
    onChatDragEnd()
    if (!draggedChatProjectId || draggedChatProjectId === target.project_id) return
    void moveChatToProject(draggedChatId, target.project_id)
    return
  }
  const draggedId = dragProjectId.value
  onProjectDragEnd()
  if (!draggedId || draggedId === target.project_id) return
  const ids = store.workspaceProjects.map(p => p.project_id)
  const from = ids.indexOf(draggedId)
  const to = ids.indexOf(target.project_id)
  if (from < 0 || to < 0 || from === to) return
  ids.splice(to, 0, ids.splice(from, 1)[0])
  void store.reorderProjects(ids)
}

function onProjectDragEnd() {
  dragProjectId.value = null
  dragOverProjectId.value = null
}

function onChatDragEnd() {
  dragChatId.value = null
  dragChatProjectId.value = null
  dragOverProjectId.value = null
}

function isDragOverProject(project: ProjectInfo): boolean {
  if (dragOverProjectId.value !== project.project_id) return false
  return dragProjectId.value !== project.project_id || dragChatProjectId.value !== project.project_id
}

function toggleProject(id: string) {
  if (expandedProjects.has(id)) {
    expandedProjects.delete(id)
  } else {
    expandedProjects.add(id)
  }
}

function subagentsFor(chatId: string): RunningSubagent[] {
  return store.runningSubagentsFor(chatId)
}

function subagentsExpanded(chatId: string): boolean {
  return !collapsedSubagentParents.has(chatId)
}

function toggleSubagents(chatId: string) {
  if (subagentsExpanded(chatId)) {
    collapsedSubagentParents.add(chatId)
  } else {
    collapsedSubagentParents.delete(chatId)
  }
}

const activeSubagentId = computed(() => (route.params.agentId as string) || '')

function isActiveSubagent(chatId: string, agentId: string): boolean {
  return (
    route.params.chatId === chatId
    && activeSubagentId.value === bareAgentId(agentId)
  )
}

function subagentLabel(sub: RunningSubagent): string {
  return (sub.description || '').trim() || shortAgentId(sub.agent_id)
}

// `window.prompt` cannot be used here: wry's WKUIDelegate never shows it, so in
// the desktop app it returned null and this button did nothing at all. See
// lib/prompt.
async function addProject() {
  const name = await askPrompt('Project name', { title: 'New project' })
  if (!name) return
  try {
    const p = await store.createProject(name)
    expandedProjects.add(p.project_id)
  } catch (e) {
    // A failed create used to reject silently, leaving the same "nothing
    // happened" symptom the missing dialog caused. `alert` is also a no-op in
    // the desktop webview, so surface it as a toast.
    store.pushErrorToast('Could not create project', errorMessage(e))
  }
}


function workspaceShortcut(name: string): string {
  const index = store.workspaceOptions.findIndex(workspace => workspace.name === name) + 1
  return index >= 1 && index <= 9 ? String(index) : ''
}

function colorForChat(chat: { project_id: string }) {
  const project = store.projects.find(item => item.project_id === chat.project_id)
  return colorForWorkspace(store.workspaceOptions.find(item => item.name === project?.workspace))
}

function colorForProject(workspace: string) {
  return colorForWorkspace(store.workspaceOptions.find(item => item.name === workspace))
}

// ── Completed (archived) projects ──────────────────────────────────────
type CompletedProject = { stem: string; name: string; context: string; workspace: string; vault_doc_path?: string }
const archiveOpen = ref(false)
const loadingCompleted = ref(false)
const completedProjects = ref<CompletedProject[]>([])
const restoringStem = ref<string | null>(null)

async function openArchive() {
  archiveOpen.value = true
  loadingCompleted.value = true
  try {
    completedProjects.value = await store.fetchCompletedProjects()
  } catch (e) {
    store.pushErrorToast('Could not load completed projects', `${errorMessage(e)}`)
    archiveOpen.value = false
  } finally {
    loadingCompleted.value = false
  }
}

function openCompletedDoc(cp: CompletedProject) {
  if (!cp.vault_doc_path) return
  void fileViewer.open(cp.vault_doc_path)
}

async function doRestore(cp: CompletedProject) {
  if (restoringStem.value) return
  restoringStem.value = cp.stem
  try {
    const restored = await store.restoreProject(cp.workspace, cp.stem)
    completedProjects.value = completedProjects.value.filter(p => p.stem !== cp.stem)
    if (restored) expandedProjects.add(restored.project_id)
    if (!completedProjects.value.length) archiveOpen.value = false
  } catch (e) {
    store.pushErrorToast('Could not restore project', `${errorMessage(e)}`)
  } finally {
    restoringStem.value = null
  }
}

function startEditProject(id: string) {
  editingProject.value = id
  closeProjectMenu()
}

async function finishEditProject(event: Event, id: string) {
  const input = event.target as HTMLInputElement
  const name = input.value.trim()
  if (name) {
    await store.updateProject(id, { name })
  }
  editingProject.value = null
}

async function confirmDeleteProject(id: string) {
  closeProjectMenu()
  if (!await askConfirm('Delete this project and archive all its chats?', {
    title: 'Delete project',
    confirmLabel: 'Delete project',
    destructive: true,
  })) return
  await store.deleteProject(id)
}

// One shared project picker for every visible New action. The project-local "+"
// preselects its own project; the global New chat above the tree opens the same
// dialog on the active workspace.
async function chooseNewChat(workspace: string, preferredProjectId?: string) {
  const projectId = await openNewChatPicker({ workspace, projectId: preferredProjectId })
  if (!projectId) return
  expandedProjects.add(projectId)
  await store.newChatInProject(projectId)
}

function startRenameChat(chatId: string) {
  closeChatMenus()
  const chat = store.chats.find(c => c.chat_id === chatId)
  renameValue.value = chat?.title || ''
  renamingChat.value = chatId
}

async function doRenameChat() {
  if (renamingChat.value && renameValue.value.trim()) {
    await store.renameChat(renamingChat.value, renameValue.value.trim())
  }
  renamingChat.value = null
}

async function copyChatId(chatId: string) {
  closeChatMenus()
  try {
    const copied = await writeClipboard(chatId)
    if (!copied) throw new Error('copy failed')
    store.pushToast({
      chat_id: chatId,
      title: 'Chat ID copied',
      body: chatId,
    })
  } catch (e) {
    store.pushErrorToast('Could not copy chat ID', `${errorMessage(e)}`)
  }
}

async function doMarkUnread(chatId: string) {
  closeChatMenus()
  await store.markUnread(chatId)
}

async function doArchiveChat(chatId: string) {
  closeChatMenus()
  // This path never asked for confirmation, unlike the chat header's archive
  // button, so archiving from the sidebar menu was a single misclick.
  if (!await askConfirm(ARCHIVE_CONFIRM_MESSAGE, {
    title: 'Archive chat',
    confirmLabel: 'Archive',
  })) return
  try {
    await store.archiveChat(chatId)
  } catch {
    // archiveChat reconnected the sockets and raised an error toast already;
    // swallow the rejection so it is not an unhandled one.
  }
}

async function setRetry(chatId: string) {
  closeChatMenus()
  await store.loadMessages(chatId)
  const msgs = store.messages[chatId] || []
  const lastUser = [...msgs].reverse().find(m => m.role === 'user')
  const text = lastUser?.content?.trim()
  if (!text) {
    // Not `alert`: it is a no-op in the desktop webview, so the menu entry
    // looked broken rather than unavailable.
    store.pushToast({
      chat_id: '',
      title: 'Nothing to retry',
      body: 'Open the chat or send a message first — there is no user turn to retry.',
    })
    return
  }
  await store.setChatRetry(chatId, text, lastUser?.images)
}

async function stopRetry(chatId: string) {
  closeChatMenus()
  await store.stopChatRetry(chatId)
}

async function confirmDeleteChat(chatId: string) {
  closeChatMenus()
  if (!await askConfirm('Delete this chat permanently? It cannot be recovered.', {
    title: 'Delete chat',
    confirmLabel: 'Delete chat',
    destructive: true,
  })) return
  await store.deleteChat(chatId)
}
</script>

<style scoped>
.sidebar {
  /* Fallback only: ChatLayout sets the real width inline (drag-resizable,
     remembered per user). Matches DEFAULT_SIDEBAR_WIDTH so the two agree
     before that inline style applies. */
  width: 340px;
  min-width: 340px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  /* Project list scrolls internally; keeping overflow off the outer
     sidebar prevents double-scroll and keeps the footer fixed. */
  overflow: hidden;
  transition: width 0.2s var(--ease), min-width 0.2s var(--ease), transform 0.22s var(--ease);
  padding-top: var(--safe-top);
  padding-left: var(--safe-left);
  padding-bottom: var(--safe-bottom);
}

.sidebar.collapsed {
  width: 40px;
  min-width: 40px;
}

.sidebar-header {
  display: flex;
  align-items: center;
  gap: 8px;
  /* Keep the collapsed rail aligned with the pane header. */
  height: 61px;
  flex-shrink: 0;
  padding: 8px;
  border-bottom: 1px solid var(--border);
}

/* Expanded: prototype A's stacked rail top - brand row, workspace scope,
   New chat, then the destinations as a labelled list. */
.sidebar-header--expanded {
  flex-direction: column;
  align-items: stretch;
  gap: 0;
  height: auto;
  padding: 10px 12px 12px;
}

.sidebar-brand-row {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}

.sidebar-header--expanded .sidebar-brand-row {
  margin: 0 0 10px 4px;
}

.sidebar-header--expanded .toggle-btn {
  margin-left: auto;
}

.sidebar-brand-glyph {
  display: grid;
  place-items: center;
  flex: 0 0 22px;
  width: 22px;
  height: 22px;
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  color: var(--accent);
  font: 700 16px/1 var(--font-sans);
}

.sidebar-brand-row :deep(.sidebar-brand) {
  min-height: 30px;
}

/* Key hints: the chord or digit that reaches this control. */
.sidebar-keycap {
  display: inline-grid;
  place-items: center;
  flex: 0 0 auto;
  min-width: 19px;
  height: 19px;
  padding: 0 4px;
  box-sizing: border-box;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-xs);
  color: var(--fg3);
  font: 700 10px/1 var(--font-mono);
}

.toggle-btn {
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  transition: color 120ms var(--ease), background 120ms var(--ease);
}
.toggle-btn:hover { color: var(--fg); }
.toggle-btn:active { transform: scale(0.94); }
.toggle-btn--collapsed svg { transform: scaleX(-1); }

/* Pulsing dot used inline next to project / chat names to signal activity.
   A breathing scale+opacity pulse reads as "alive" at a glance, unlike a
   thin two-tone ring spin which is too subtle at this size to notice. */
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

.workspace-status-dot,
.rollup-needs-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--accent);
}

/* Working, at rollup level. Same treatment as the chat transcript's own
   activity pulse and ChatSignals: a solid accent dot with a halo plus an
   expanding ring. The outlined ring this replaced was nearly invisible at this
   size against the sidebar ground. */
.rollup-ring,
.workspace-status-ring {
  position: relative;
  display: inline-flex;
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 4px var(--accent);
  animation: ciao-pulse 1.1s ease-in-out infinite;
}

.rollup-ring::before,
.workspace-status-ring::before {
  content: "";
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  background: var(--accent);
  opacity: 0.45;
  animation: ciao-ring 1.1s ease-out infinite;
  pointer-events: none;
}

/* The inner core is now the dot itself, so the nested span is decorative. */
.workspace-status-core,
.rollup-ring-core {
  display: none;
}

@keyframes ciao-ring {
  0% { transform: scale(0.6); opacity: 0.45; }
  100% { transform: scale(1.6); opacity: 0; }
}

/* Scrollable area for chats and projects */
.chats-scroll-area {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  padding: 8px 8px 12px;
}

/* Recent chats section above the project list. */
.recent-section {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  margin-bottom: 10px;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.recent-label {
  display: flex;
  align-items: center;
  padding: 6px 10px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  font-size: var(--text-sm);
  color: var(--fg2);
  font-weight: 600;
  letter-spacing: 0.3px;
}

.recent-items {
  display: flex;
  flex-direction: column;
}

.recent-item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-height: var(--touch);
  padding: 6px 10px;
  cursor: pointer;
  font-size: var(--text-base);
  color: var(--fg2);
  overflow: hidden;
  white-space: nowrap;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
  border-left: 0;
  border-right: 0;
  border-top: 0;
  background: transparent;
  text-align: left;
}

.recent-item:last-child {
  border-bottom: none;
}

.recent-item:hover {
  background: var(--bg3);
  color: var(--fg);
}

.recent-item.active {
  background: var(--bg3);
  color: var(--fg);
  border-left: 2px solid var(--accent);
  padding-left: 8px;
}

.recent-item.remote,
.chat-item.remote {
  opacity: 0.5;
  cursor: default;
}

.remote-chip {
  display: inline-flex;
  align-items: center;
  height: 14px;
  padding: 0 5px;
  border-radius: 4px;
  background: var(--bg3);
  color: var(--fg2);
  font-size: calc(9px * var(--font-scale));
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  flex-shrink: 0;
}

.recent-title {
  flex-shrink: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent-project {
  font-size: calc(10px * var(--font-scale));
  color: var(--fg3);
  background: var(--bg);
  padding: 1px 5px;
  border-radius: 4px;
  flex-shrink: 0;
  max-width: 80px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sidebar-new-chat {
  display: flex;
  align-items: center;
  gap: 9px;
  box-sizing: border-box;
  width: 100%;
  min-height: 40px;
  margin: 10px 0 0;
  padding: 0 11px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elev);
  color: var(--fg);
  font: inherit;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
  transition: border-color 120ms var(--ease), background 120ms var(--ease);
}

.sidebar-new-chat:hover {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 13%, var(--bg2));
}

.sidebar-new-chat-plus {
  color: var(--accent);
  font-size: 20px;
  line-height: 1;
}

.sidebar-new-chat .sidebar-keycap {
  margin-left: auto;
}

/* The destinations, as a labelled vertical list. They used to be four icons
   squeezed into the header row beside the workspace scope and New chat, which
   left no room for either at the default sidebar width. */
.nav-links {
  display: grid;
  gap: 3px;
  margin: 12px 0 0;
  min-width: 0;
}

.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  min-height: 38px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: 7px;
  color: var(--fg2);
  text-decoration: none;
  transition: color 120ms var(--ease), background 120ms var(--ease), border-color 120ms var(--ease);
}

/* Static, so the badge inside it anchors to the row and lands at the right
   edge (prototype A's nav-badge) instead of covering the glyph. */
.nav-item-icon {
  position: static;
  display: inline-flex;
  width: 18px;
  height: 18px;
  flex: 0 0 18px;
  align-items: center;
  justify-content: center;
}

/* Persistent system-state signal, not a count: a pulsing dot reads better than
   a numeral for update and blocking-housekeeping warnings. */
.nav-item-badge {
  position: absolute;
  top: calc(50% - 4px);
  right: 12px;
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--accent, #4c8bf5);
  box-shadow: 0 0 0 2px var(--bg2);
  animation: nav-item-badge-pulse 2s ease-in-out infinite;
}

.nav-item-badge--warning {
  background: var(--warning);
}

.nav-item-badge--static {
  animation: none;
}

@keyframes nav-item-badge-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.55; transform: scale(0.85); }
}

/* Coarse pointers get the full 44px target on every rail row. */
@media (pointer: coarse) {
  .nav-item,
  .sidebar-new-chat { min-height: var(--touch); }
}

/* aria-hidden because .nav-item already carries the full aria-label. */
.nav-item-label {
  min-width: 0;
  overflow: hidden;
  color: inherit;
  font-size: var(--text-base);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nav-item:hover {
  color: var(--fg);
  background: var(--bg-elev);
}

.nav-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.nav-item--working svg {
  animation: pulse-working 2.2s infinite ease-in-out;
  color: var(--accent);
}

.nav-item--warning svg {
  animation: pulse-warning 2.2s infinite ease-in-out;
  color: var(--warning);
}

@keyframes pulse-working {
  0%, 100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.55;
    transform: scale(0.92);
  }
}

@keyframes pulse-warning {
  0%, 100% {
    opacity: 1;
    transform: scale(1);
    filter: drop-shadow(0 0 0px var(--warning));
  }
  50% {
    opacity: 0.7;
    transform: scale(1.06);
    filter: drop-shadow(0 0 1px var(--warning));
  }
}

@media (prefers-reduced-motion: reduce) {
  .nav-item--working svg,
  .nav-item--warning svg {
    animation: none;
  }
}

.nav-item--active,
.nav-item--active:hover {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
  background: color-mix(in srgb, var(--accent) 13%, transparent);
  color: var(--fg);
}

.nav-item--active .nav-item-icon {
  color: var(--accent);
}

.workspace-toggle {
  display: flex;
  flex-wrap: wrap;
  padding: 8px;
  gap: 4px;
}

.workspace-toggle button {
  flex: 1 1 0;
  min-width: 0;
  min-height: var(--touch);
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg3);
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-sm);
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  white-space: nowrap;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), color 120ms var(--ease);
}

.workspace-toggle button:hover {
  background: var(--bg);
  border-color: var(--accent);
  color: var(--fg);
}

.workspace-toggle button.active {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 18%, var(--bg3));
}

/* Pending-proposal count inside the view switcher's Review segment. Same
   treatment as the workspace shortcut chip: small, bordered, quiet. */
.view-toggle .view-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg);
  color: var(--fg2);
  font-size: 10px;
  line-height: 1;
  font-weight: 700;
}

/* Keep workspace status markers visually separate from the workspace name,
   matching the spacing used by project-level state dots. */
.workspace-toggle button .workspace-status-dot,
.workspace-toggle button .workspace-status-ring {
  margin-left: var(--space-2);
}

/* Prototype A's side-section label. */
.sidebar-list-label {
  margin: 4px 0 0;
  padding: 6px 10px 5px;
  color: var(--fg3);
  font: 600 var(--text-xs)/1.2 var(--font-sans);
}

/* Flat rows rather than a bordered box per project: the tree keeps its
   structure (project header, its chats indented beneath) at list density. */
.project-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex-shrink: 0;
}

.project-group {
  border-radius: 7px;
}

.project-header {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px;
  border-radius: 7px;
  font-size: var(--text-base);
  color: var(--fg2);
  font-weight: 600;
  min-height: var(--touch);
}

.project-header:hover {
  background: var(--bg-elev);
  color: var(--fg);
}

/* Fine pointers take prototype A's 36px row. The header's controls keep their
   44px boxes (negative margins); only the row's own padding shrinks. */
@media (pointer: fine) {
  .project-header {
    min-height: 36px;
    padding-block: 2px;
  }

  .project-list .chat-item {
    min-height: 36px;
  }
}

/* Drag-to-reorder affordances. The header shows a grab cursor when draggable,
   dims while being dragged, and draws an accent line where a drop will land. */
.project-header[draggable="true"] {
  cursor: grab;
}
.project-header.dragging {
  opacity: 0.4;
  cursor: grabbing;
}
.project-header.drag-over {
  box-shadow: inset 0 2px 0 0 var(--accent);
  background: var(--bg3);
}

.project-header.is-system {
  opacity: 0.85;
}
.project-header.is-system .project-name {
  font-weight: 500;
  text-transform: none;
  letter-spacing: 0;
  color: var(--fg2);
}
.project-header.is-system:hover .project-name { color: var(--fg); }

.system-chip {
  display: inline-flex;
  align-items: center;
  height: 14px;
  padding: 0 5px;
  margin-left: 6px;
  border-radius: var(--radius-xs);
  background: var(--bg3);
  color: var(--fg2);
  font-size: calc(9px * var(--font-scale));
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  vertical-align: middle;
}

.project-icon {
  font-size: calc(10px * var(--font-scale));
  width: var(--touch);
  height: var(--touch);
  margin: -6px 0 -6px -10px;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: center;
  user-select: none;
}
.project-icon:hover { color: var(--fg); }

.project-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
  min-height: var(--touch);
  margin: -6px 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  text-transform: inherit;
  letter-spacing: inherit;
}
.project-name:hover { color: var(--fg); }

/* The project header is a text button, not a flex row, so the state marks sit
   flush against the last letter of the name. Space them the same 6px .badge
   already gives itself, and align them to the text rather than the baseline.
   Scoped here on purpose: the workspace pill puts the same marks in a flex row
   with a gap, where a margin would double up. */
.project-name .rollup-needs-dot,
.project-name .rollup-ring {
  margin-left: var(--space-2);
  vertical-align: middle;
}

.edit-input {
  flex: 1;
  font-size: var(--text-sm);
  padding: 2px 4px;
  background: var(--bg);
  border: 1px solid var(--accent);
  border-radius: 3px;
  color: var(--fg);
  font-family: var(--font);
}

.add-chat-btn {
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: calc(14px * var(--font-scale));
  width: var(--touch);
  height: var(--touch);
  margin: -6px -10px -6px 0;
  padding: 0;
  opacity: 0;
  transition: opacity 0.15s;
  min-width: var(--touch);
  text-align: center;
}

.project-header:hover .add-chat-btn {
  opacity: 1;
}
.add-chat-btn:focus-visible { opacity: 1; }

.add-chat-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

/* Project actions use the same 44px affordance as chat actions. The explicit
   trigger is what makes the context menu reachable by keyboard and touch;
   right-click still uses the same Reka menu at the pointer. */
.project-actions-btn {
  flex-shrink: 0;
  width: var(--touch);
  height: var(--touch);
  margin: -6px 0;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: calc(14px * var(--font-scale));
  line-height: 1;
  opacity: 0;
  transition: opacity 100ms var(--ease), background 100ms var(--ease);
}
.project-header:hover .project-actions-btn,
.project-actions-btn:focus-visible,
.project-actions-btn[data-state='open'] { opacity: 1; }
.project-actions-btn:hover { color: var(--fg); background: var(--bg3); }
@media (hover: none) { .project-actions-btn { opacity: 0.6; } }

.chat-item {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: var(--touch);
  padding: 0 4px 0 30px;
  border-radius: 7px;
  cursor: pointer;
  font-size: var(--text-base);
  color: var(--fg2);
  overflow: hidden;
  white-space: nowrap;
}

/* A subagent row is not a chat: it opens a read-only transcript, so it drops
   the actions menu and shifts right to read as owned by the chat above it.
   Indent is on padding rather than margin so the hover/active background
   still spans the full sidebar width. */
.chat-item.subagent-item {
  padding-left: 44px;
  text-decoration: none;
  color: var(--fg2);
}

.subagent-mark {
  flex: none;
  color: var(--fg3, var(--fg2));
  font-size: var(--text-sm, 0.85em);
  line-height: 1;
}

/* The agent's type ("Explore", "general-purpose"), when the CLI recorded one.
   Muted: the description is the row's subject, this only qualifies it. */
.subagent-chip {
  flex: none;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 9px;
  color: var(--fg3, var(--fg2));
  font-size: var(--text-xs, 0.75em);
  line-height: 16px;
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Live signal. Same pulse as the in-chat SubagentPanel spinner so the two
   surfaces read as one state. */
.subagent-spinner {
  flex: none;
  margin-left: auto;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent2, var(--accent));
  animation: subagent-row-pulse 1.1s ease-in-out infinite;
}

@keyframes subagent-row-pulse {
  0%, 100% { transform: scale(0.55); opacity: 0.35; }
  50% { transform: scale(1); opacity: 1; }
}

@media (prefers-reduced-motion: reduce) {
  .subagent-spinner { animation-duration: 2.2s; }
}

/* A chat's subagent disclosure sits inside the chat row. It uses the same
   44px hit area as the project disclosure while keeping the glyph compact, so
   collapsing a busy chat does not make the child rows unreachable on a
   touch device. */
.subagent-toggle {
  flex: 0 0 var(--touch);
  width: var(--touch);
  height: var(--touch);
  margin: 0 0 0 -14px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--fg3, var(--fg2));
  cursor: pointer;
  font: inherit;
  line-height: 1;
  text-align: center;
}
.subagent-toggle:hover { color: var(--fg); }
.subagent-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
  border-radius: var(--radius-sm);
}

/* Interval-automation marker on a chat row. Accent while the cadence is live,
   muted when it is paused, so "this chat re-runs itself" reads at a glance
   without competing with the streaming dot next to it. */
.badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 6px;
  margin-left: 6px;
  border-radius: 9px;
  background: var(--accent);
  color: var(--bg);
  font-size: var(--text-xs);
  font-weight: 700;
  line-height: 1;
  text-transform: none;
  letter-spacing: 0;
  vertical-align: middle;
}
.badge--missed {
  margin-left: 0;
  background: var(--warning);
  color: var(--bg);
}

.chat-item:hover {
  background: var(--bg-elev);
  color: var(--fg);
}

/* Chat rows can be dragged onto any project header to move them. The context
   menu remains available for keyboard and touch users. */
.chat-item[draggable="true"] {
  cursor: grab;
}
.chat-item.dragging {
  opacity: 0.4;
  cursor: grabbing;
}

.chat-item.active {
  background: var(--bg3);
  color: var(--fg);
  box-shadow: inset 2px 0 0 var(--accent);
}

.chat-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-title--unread {
  color: var(--fg);
  font-weight: 600;
}

.workspace-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* One workspace scope for the whole rail. */
.workspace-scope {
  position: relative;
  z-index: 40;
  margin: 0;
}

.workspace-scope-trigger,
.workspace-scope-option {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-height: var(--touch);
  color: var(--fg);
  font: inherit;
  text-align: left;
}

.workspace-scope-trigger {
  min-height: 48px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg-elev);
  cursor: pointer;
}

.workspace-scope-trigger:disabled {
  cursor: default;
  opacity: 1;
}

.workspace-scope-trigger:not(:disabled):hover,
.workspace-scope-trigger[aria-expanded="true"] {
  border-color: var(--border-strong);
  background: var(--bg3);
}

.workspace-scope-dot {
  width: 9px;
  height: 9px;
  flex: 0 0 9px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent);
}

.workspace-scope-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-sm);
  font-weight: 650;
}

.workspace-scope-trigger > svg {
  flex: none;
  color: var(--fg2);
}

.workspace-scope-menu {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  left: 0;
  z-index: 50;
  overflow: hidden;
  padding: var(--space-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  box-shadow: 0 1rem 2.5rem rgb(0 0 0 / 38%);
}

.workspace-scope-option {
  padding: 0 var(--space-2);
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  cursor: pointer;
}

.workspace-scope-option:hover,
.workspace-scope-option:focus-visible,
.workspace-scope-option.active {
  border-color: var(--border);
  background: var(--bg3);
}

.workspace-scope-option.active {
  color: var(--accent);
}

@media (prefers-reduced-motion: reduce) {
  .rollup-ring,
  .workspace-status-ring { animation: none; }
  .rollup-ring::before,
  .workspace-status-ring::before { animation: none; opacity: 0.3; }
}

/* Shimmer placeholder shown in the sidebar while the server auto-titles
   a brand new chat. The linear-gradient "sweep" is what the eye reads as
   "something is happening", similar to skeleton loaders elsewhere. */
.title-shimmer {
  flex: 1;
  min-width: 0;
  height: 12px;
  border-radius: 4px;
  background: linear-gradient(
    90deg,
    var(--bg2) 0%,
    var(--bg3) 50%,
    var(--bg2) 100%
  );
  background-size: 200% 100%;
  animation: title-shimmer-sweep 1.4s ease-in-out infinite;
}

@keyframes title-shimmer-sweep {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}

@media (prefers-reduced-motion: reduce) {
  .title-shimmer {
    animation: title-shimmer-pulse 1.8s ease-in-out infinite;
  }
  @keyframes title-shimmer-pulse {
    0%, 100% { opacity: 0.6; }
    50% { opacity: 1; }
  }
}

/* Three-dot chat actions. Hidden by default on desktop, fade in on row
   hover or when the menu is open. Always visible on touch devices so
   the entry point is discoverable without hover. */
.chat-actions-btn {
  flex-shrink: 0;
  margin-left: 2px;
  width: var(--touch);
  height: var(--touch);
  padding: 0;
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: calc(14px * var(--font-scale));
  line-height: 1;
  border-radius: 4px;
  opacity: 0;
  transition: opacity 100ms var(--ease), background 100ms var(--ease);
}
.chat-item:hover .chat-actions-btn,
.chat-item.active .chat-actions-btn { opacity: 1; }
.chat-actions-btn:hover {
  color: var(--fg);
  background: var(--bg2);
}
@media (hover: none) {
  .chat-actions-btn { opacity: 0.6; }
}


.sidebar-footer {
  /* Match the sidebar/pane headers: 44px controls + 8px pad + 1px border. */
  height: 61px;
  padding: 8px;
  border-top: 1px solid var(--border);
  display: flex;
  gap: 6px;
  align-items: center;
  flex-shrink: 0;
  box-sizing: border-box;
}

.add-project-btn,
.add-automation-btn {
  flex: 1;
  height: var(--touch);
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg3);
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-sm);
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), color 120ms var(--ease);
}

.add-project-btn:hover,
.add-automation-btn:hover {
  background: var(--bg);
  border-color: var(--accent);
  color: var(--fg);
}

.archive-btn {
  flex-shrink: 0;
  width: var(--touch);
  height: var(--touch);
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg3);
  color: var(--fg2);
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), color 120ms var(--ease);
}
.archive-btn:hover {
  background: var(--bg);
  border-color: var(--accent);
  color: var(--fg);
}

/* Completed-projects dialog */
.modal--archive {
  width: min(560px, calc(100vw - 32px));
  max-height: calc(100vh - 48px);
}
.archive-hint {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--fg2);
  line-height: 1.4;
}
.archive-empty {
  padding: 16px 4px;
  color: var(--fg2);
  font-size: var(--text-sm);
  text-align: center;
}
.archive-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: min(65vh, calc(100vh - 220px));
  overflow-y: auto;
}
.archive-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
}
.archive-item:hover { background: var(--bg3); }
.archive-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-sm);
  color: var(--fg);
  border: none;
  background: transparent;
  padding: 0;
  text-align: left;
  font-family: inherit;
}
.archive-name--clickable {
  cursor: pointer;
}
.archive-name--clickable:hover {
  color: var(--accent);
  text-decoration: underline;
}
.archive-name:disabled {
  cursor: default;
  opacity: 1;
}
.archive-restore { flex-shrink: 0; }

/* Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
}

.modal {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  width: 320px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.modal h3 {
  font-size: calc(14px * var(--font-scale));
  margin: 0;
}

.modal input {
  width: 100%;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.modal-actions button {
  padding: 6px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-base);
}

@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    top: 0; left: 0; bottom: 0;
    z-index: 50;
    width: 84vw;
    min-width: 0;
    max-width: 320px;
    box-shadow: 4px 0 24px rgba(0, 0, 0, 0.5);
    transform: translateX(0);
  }
  .sidebar.collapsed {
    transform: translateX(-100%);
    box-shadow: none;
    pointer-events: none;
  }
  .sidebar.collapsed .sidebar-header,
  .sidebar.collapsed .project-list,
  .sidebar.collapsed .sidebar-footer {
    visibility: hidden;
  }
  .add-chat-btn { opacity: 1; }
  .nav-item,
  .sidebar-new-chat { min-height: var(--touch); }
  /* No containment on the drawer's header. `container-type: inline-size` makes
     the element its own rasterization root, and here that root sits inside a
     `position: fixed` layer that the transform above keeps composited - the
     combination WebKit rasterizes soft, which is why the nav glyphs came out
     blurry on iPhone while the pills below them stayed sharp. Nothing is lost:
     the query it feeds only decides whether the active item keeps its label,
     and the `max-width: 900px` rule above already drops that label at every
     width this media query covers. The container query stays for the wide
     window where the sidebar is drag-resizable. */
  .sidebar-header { container-type: normal; }
  /* Same failure, second cause. `.touch-hit` opens an isolated stacking context
     with a `z-index: -1` pill behind it, and inside the drawer's composited
     layer WebKit renders each of those isolated subtrees into its own offscreen
     buffer - at 1x, not the device's 3x. The header's border and background are
     painted straight into the layer and stay crisp, which is why the blur ends
     exactly at the header's box while the workspace pills right below it (no
     isolation) are sharp. Dropping the isolation costs nothing here: the header
     declares no background of its own, so the pill still lands above the
     sidebar's background and below the glyphs. */
  .sidebar-header .touch-hit { isolation: auto; }
}

/* Schedules list in sidebar (schedules mode) */
.sidebar-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px 4px;
}
.sidebar-section-title {
  font-size: var(--text-xs);
  letter-spacing: 0.5px;
  color: var(--fg2);
  font-weight: 600;
}
.schedules-list {
  display: flex;
  /* Fills the space between the workspace toggle and the footer, so the create
     button pins to the bottom exactly like the chat sidebar's. min-height:0 is
     what lets it actually scroll inside a column flex parent. */
  flex: 1;
  min-height: 0;
  flex-direction: column;
  overflow-y: auto;
  padding: 8px 8px 12px;
  gap: 12px;
}

/* Grouped schedule sections: a sentence-case label over plain 36px rows,
   the same list language as the Projects tree - title on the left, the muted
   time or cadence on the right. */
.schedule-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex-shrink: 0;
}
.schedule-group-header {
  margin: 4px 0 0;
  padding: 6px 10px 5px;
  color: var(--fg3);
  font: 600 var(--text-xs)/1.2 var(--font-sans);
}
.schedule-group-items {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.schedule-item {
  display: flex;
  width: 100%;
  min-width: 0;
  min-height: 36px;
  box-sizing: border-box;
  align-items: center;
  gap: 8px;
  padding: 0 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-base);
  text-align: left;
  text-decoration: none;
  cursor: pointer;
  transition: background 120ms var(--ease), color 120ms var(--ease);
}
@media (pointer: coarse) {
  .schedule-item { min-height: var(--touch); }
}
.schedule-item:hover { background: var(--bg-elev); color: var(--fg); }
.schedule-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.schedule-item.active {
  background: var(--bg3);
  color: var(--fg);
  box-shadow: inset 2px 0 0 var(--accent);
}
.schedule-item .schedule-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.schedule-item .schedule-time {
  flex-shrink: 0;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}
.schedule-item--missed .schedule-time { color: var(--warning); }
.schedule-item--disabled .schedule-label { color: var(--fg3); }
.schedule-item .missed-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--warning);
  flex-shrink: 0;
}
.empty-hint {
  padding: 12px 16px;
  color: var(--fg2);
  font-size: var(--text-sm);
  text-align: center;
}

/* Settings sub-page navigation */
.settings-nav-list {
  display: flex;
  flex-direction: column;
  padding: 4px 12px 12px;
  gap: 2px;
}
.settings-nav-list .sidebar-list-label {
  margin: 0;
}
.settings-nav-item {
  display: flex;
  align-items: center;
  min-height: 36px;
  padding: 0 10px;
  border-radius: 7px;
  text-decoration: none;
  color: var(--fg2);
  font-size: var(--text-base);
  cursor: pointer;
  transition: background 120ms var(--ease), color 120ms var(--ease);
}
.settings-nav-item:hover {
  background: var(--bg-elev);
  color: var(--fg);
}
.settings-nav-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.settings-nav-item.active {
  background: var(--bg3);
  color: var(--fg);
}
@media (pointer: coarse) {
  .settings-nav-item { min-height: var(--touch); }
}

/* Memory Map sidebar (vault stats, search, categories, path finder) */
.mm-sidebar-scroll {
  overflow-y: auto;
  padding: var(--space-3);
  flex: 1;
  min-height: 0;
}
.mm-sidebar-scroll h3 {
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--fg3);
  margin: var(--space-4) 0 var(--space-2);
}
.mm-sidebar-scroll h3:first-child { margin-top: 0; }
/* A heading inside this row (e.g. "Categories" + reset) is a flex item, so
   its own margin-top shifts it within the row instead of gapping the row
   from whatever precedes it. Move that spacing onto the row itself and
   zero the heading's own margin so the two elements share one baseline. */
.mm-row-between {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-top: var(--space-4);
  margin-bottom: var(--space-2);
}
.mm-row-between h3 { margin: 0; }
.mm-row-between:first-child { margin-top: 0; }
.mm-row-actions { display: inline-flex; align-items: baseline; gap: 6px; }
.mm-sep { color: var(--fg3); font-size: var(--text-xs); }
.mm-link--active { color: var(--fg); font-weight: 600; }
.mm-link { background: none; border: none; color: var(--accent); font-size: var(--text-xs); cursor: pointer; padding: 0; }
/* A standalone entry point, not an inline action: full touch target. */
.mm-link--block { display: inline-flex; align-items: center; min-height: var(--touch); margin-bottom: var(--space-1); }
.mm-link--block:hover { text-decoration: underline; }
.mm-hint { color: var(--fg3); font-size: var(--text-xs); margin: 0; }

.mm-stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-2); }
.mm-stat-grid--3 { grid-template-columns: repeat(3, 1fr); }
.mm-stat { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 6px 8px; }
.mm-stat .n { font-size: var(--text-lg); font-weight: 600; }
.mm-stat .l { font-size: var(--text-xs); color: var(--fg3); }
.mm-stat--skeleton { display: flex; flex-direction: column; justify-content: center; gap: 0; min-height: 52px; }

.mm-loading-heading {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--fg2);
  font-size: var(--text-sm);
  margin-bottom: var(--space-3);
}
.mm-shimmer-line {
  display: block;
  height: 10px;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--bg2) 0%, var(--bg3) 50%, var(--bg2) 100%);
  background-size: 200% 100%;
  animation: title-shimmer-sweep 1.4s ease-in-out infinite;
}
.mm-shimmer-line--n { width: 56%; height: 16px; margin-bottom: 8px; }
.mm-shimmer-line--l { width: 78%; height: 8px; }
.mm-skeleton-block { margin-top: var(--space-3); }
@media (prefers-reduced-motion: reduce) {
  .mm-shimmer-line { animation: title-shimmer-pulse 1.8s ease-in-out infinite; }
}
.history-loading-spinner {
  width: 9px;
  height: 9px;
  flex: 0 0 auto;
  border: 2px solid color-mix(in srgb, var(--accent) 28%, transparent);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: history-loading-spin 0.8s linear infinite;
}
@keyframes history-loading-spin {
  to { transform: rotate(360deg); }
}

.mm-search { margin-top: var(--space-3); }
.mm-search input { width: 100%; font-size: var(--text-sm); }

/* Workspace guide card (AGENTS.md; CLAUDE.md pre-migration) — bounded memory health, always visible */
.guide-card {
  margin-top: var(--space-3);
  padding: 10px 10px 8px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.guide-card--over { border-color: color-mix(in srgb, var(--warning) 45%, var(--border)); }
.guide-card-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 8px;
}
.guide-card-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  overflow-wrap: anywhere;
  flex: 1 1 auto;
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--fg2);
  font-family: var(--font-mono);
}
.guide-card-icon { color: var(--accent); font-size: 10px; }
.guide-card-badge {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--fg3);
  background: var(--bg3);
  padding: 1px 6px;
  border-radius: var(--radius-pill);
}
.guide-card-badge--warn { background: color-mix(in srgb, var(--warning) 18%, transparent); color: var(--warning); }
.guide-card-actions { display: inline-flex; gap: 6px; flex-shrink: 0; }
.guide-card-btn {
  font-size: var(--text-xs);
  padding: 3px 8px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--fg2);
  cursor: pointer;
  font-family: var(--font);
  /* Touch-safe hit area: the visible 3px/8px padding is too small to tap
     reliably on the mobile sidebar, so guarantee a 44px minimum target. */
  min-width: var(--touch);
  min-height: var(--touch);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.guide-card-btn:disabled { opacity: 0.5; cursor: default; }
.guide-card-btn:hover:not(:disabled) { background: var(--bg3); color: var(--fg); }
.guide-card-btn--primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
.guide-card-btn--primary:hover:not(:disabled) { filter: brightness(1.08); color: var(--on-accent); }
.guide-card-error { color: var(--warning); font-size: var(--text-xs); }
.guide-card-hint { color: var(--fg3); font-size: var(--text-xs); }
.guide-card-regions { display: flex; flex-direction: column; gap: 10px; }
.guide-region-head {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex-wrap: wrap;
  font-size: var(--text-xs);
}
.guide-region-name { font-weight: 600; color: var(--fg2); flex: 1; }
.guide-region-count { color: var(--fg3); font-variant-numeric: tabular-nums; }
.guide-region-count--warn { color: var(--warning); font-weight: 600; }
.guide-region-tokens { color: var(--fg3); font-family: var(--font-mono); font-size: 11px; }
.guide-region-bar {
  height: 6px;
  border-radius: 3px;
  background: var(--bg3);
  overflow: hidden;
  margin-top: 4px;
}
.guide-region-bar > span {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: var(--accent);
  transition: width 200ms;
}
.guide-region-bar--high > span { background: #d6a600; }
.guide-region-bar--warn > span { background: var(--warning); }
.guide-region-meta { font-size: 11px; color: var(--fg3); margin-top: 3px; }
.guide-region-meta--warn { color: var(--warning); }
.guide-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
  font-size: var(--text-xs);
  color: var(--fg3);
}
.guide-card-foot-info { font-variant-numeric: tabular-nums; }

.mm-chip-row { display: flex; flex-direction: column; gap: 2px; }
.mm-chip {
  display: flex; align-items: center; gap: 7px; padding: 5px 6px; border-radius: var(--radius-sm);
  cursor: pointer; font-size: var(--text-sm); color: var(--fg2);
}
.mm-chip:hover { background: var(--bg3); }
.mm-chip.off { opacity: 0.35; }
.mm-chip .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.mm-chip .cnt { margin-left: auto; color: var(--fg3); font-variant-numeric: tabular-nums; }
.mm-chip .only {
  display: none; margin-left: auto; background: none; border: none; color: var(--accent);
  font-size: var(--text-xs); padding: 1px 4px; border-radius: 4px; cursor: pointer;
}
.mm-chip:hover .cnt { display: none; }
.mm-chip:hover .only { display: inline; }

.mm-link-list { display: flex; flex-direction: column; gap: 2px; }
.mm-link-item {
  display: flex; align-items: center; gap: 6px; padding: 5px 6px; border-radius: var(--radius-sm);
  cursor: pointer; font-size: var(--text-sm); color: var(--fg);
}
.mm-link-item:hover { background: var(--bg3); }
.mm-link-item .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
/* Aging note marker: the app warning token, not a category colour — age is
   not a type, and reusing a hue would lie about what the dot means. */
.mm-dot--stale { background: var(--warning, #ff9800); }
.mm-link-item .cnt { margin-left: auto; color: var(--fg3); }
/* Marks which note the map is currently centred on, so the recent list
   doubles as a "you are here" indicator rather than just a jump list. */
.mm-link-item.current { background: var(--bg3); color: var(--fg); }
/* The review queue's kind rows are a filter, so the SELECTED one is the solid
   one and the rest recede — the inverse of the memory chips, where every chip is
   on until you switch it off. */
.mm-link-item.off { opacity: 0.55; }
.mm-link-item:not(.off) { background: var(--bg3); color: var(--fg); }
.mm-link-item.current .label { font-weight: 600; }
</style>

<!-- Non-scoped: teleported context menus live outside this component's DOM -->
<style>
.context-menu-overlay {
  position: fixed;
  inset: 0;
  z-index: 200;
}

.context-menu {
  position: fixed;
  min-width: 150px;
  max-width: min(280px, calc(100vw - 16px));
  max-height: calc(100dvh - 16px);
  overflow-y: auto;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  z-index: 201;
  padding: 4px 0;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3);
  outline: none;
}

.context-menu button {
  display: block;
  width: 100%;
  min-height: var(--touch);
  text-align: left;
  padding: 6px 16px;
  border: none;
  background: none;
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-base);
}

.context-menu button:hover,
.context-menu button:focus-visible,
.context-menu [data-highlighted] {
  background: var(--bg3);
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.context-menu-label {
  padding: 6px 16px 4px;
  font-size: var(--text-xs);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.4px;
}

.context-menu-back {
  border-top: 1px solid var(--border) !important;
  margin-top: 4px;
  color: var(--fg2) !important;
  font-size: var(--text-sm) !important;
}
</style>
