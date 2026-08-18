<template>
  <aside class="sidebar" :class="{ collapsed }" v-bind="$attrs">
    <div class="sidebar-header">
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
      <template v-if="!collapsed">
        <!-- The wordmark used to sit here, between the toggle and these icons.
             It is `BrandMark` in the pane header now, where it is centred and
             does not have to share the sidebar's width. -->
        <div class="nav-links">
          <router-link
            to="/"
            class="nav-item touch-hit"
            :class="{
              'nav-item--active': mode === 'chat' || mode === 'project',
              'nav-item--working': isAnyChatWorking
            }"
            title="chats"
            :aria-label="isAnyChatWorking ? 'chats (assistant is working)' : 'chats'"
          >
            <!-- Stacked message lines: sharper, more "log-window" than a speech bubble -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
              <rect x="3" y="4" width="18" height="14" />
              <line x1="6" y1="9" x2="14" y2="9" />
              <line x1="6" y1="13" x2="18" y2="13" />
              <polyline points="8 18 8 21 11 18" />
            </svg>
                      <span class="nav-item-label" aria-hidden="true">chats</span>
          </router-link>
          <router-link
            to="/schedules"
            class="nav-item touch-hit"
            :class="{
              'nav-item--active': mode === 'schedules',
              'nav-item--warning': hasAutomationWarning
            }"
            title="automations"
            :aria-label="hasAutomationWarning ? 'automations (attention required)' : 'automations'"
          >
            <!-- Clock face with hour markers: more diagrammatic than calendar grid -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
              <rect x="3" y="3" width="18" height="18" />
              <line x1="12" y1="3" x2="12" y2="5" />
              <line x1="12" y1="19" x2="12" y2="21" />
              <line x1="3" y1="12" x2="5" y2="12" />
              <line x1="19" y1="12" x2="21" y2="12" />
              <polyline points="12 8 12 12 15 14" />
            </svg>
                      <span class="nav-item-label" aria-hidden="true">automations</span>
          </router-link>
          <router-link
            to="/memory"
            class="nav-item touch-hit"
            :class="{ 'nav-item--active': mode === 'memory' }"
            title="memory"
            aria-label="memory"
          >
            <!-- Brain: the only rail icon built from organic curves rather than
                 rectilinear primitives, so it uses round caps/joins like the
                 voice icons elsewhere instead of the rail's usual square/miter. -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5h1a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2Z" />
              <path d="M15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5h-1a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z" />
              <line x1="12" y1="6" x2="12" y2="18" />
            </svg>
                      <span class="nav-item-label" aria-hidden="true">memory</span>
          </router-link>
          <!-- mode, not active-class: every settings tab is its own route
               (/settings/providers, /settings/models, ...) and none of them match
               the /settings record, so active-class left this item inactive on
               nearly every settings page - and with it the label collapsed. The
               sibling links already key off mode for the same reason. -->
          <router-link
            to="/settings"
            class="nav-item touch-hit"
            :class="{ 'nav-item--active': mode === 'settings' }"
            title="settings"
            aria-label="settings"
          >
            <!-- Sliders / equalizer: more direct than a gear, mono-grid friendly -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="square" stroke-linejoin="miter" aria-hidden="true">
              <line x1="4" y1="7" x2="20" y2="7" />
              <line x1="4" y1="12" x2="20" y2="12" />
              <line x1="4" y1="17" x2="20" y2="17" />
              <rect x="14" y="5" width="4" height="4" fill="currentColor" />
              <rect x="7" y="10" width="4" height="4" fill="currentColor" />
              <rect x="15" y="15" width="4" height="4" fill="currentColor" />
            </svg>
                      <span class="nav-item-label" aria-hidden="true">settings</span>
          </router-link>
          <NotificationBell class="sidebar-bell" />
        </div>
      </template>
    </div>

    <!-- No section title here: the nav pill above already names this view, and
         the create action sits in the footer like the chat sidebar's, so both
         modes put "make a new one" in the same place. -->
    <template v-if="!collapsed && (mode === 'schedules')">
      <div v-if="hasMultipleWorkspaces" class="workspace-toggle">
        <button
          v-for="workspace in store.workspaceOptions"
          :key="workspace.name"
          :class="{ active: store.activeWorkspace === workspace.name }"
          :aria-pressed="store.activeWorkspace === workspace.name"
          :aria-keyshortcuts="workspaceShortcut(workspace.name) || undefined"
          :data-workspace-color="colorForWorkspace(workspace)"
          :title="workspaceShortcut(workspace.name) ? `Switch to ${workspaceLabel(workspace.name)} (${workspaceShortcut(workspace.name)})` : undefined"
          @click="selectAutomationWorkspace(workspace.name)"
        >
          <span v-if="workspaceShortcut(workspace.name)" class="workspace-shortcut" aria-hidden="true">{{ workspaceShortcut(workspace.name) }}</span>
          <!-- Wrapped, not a bare text node: the buttons are nowrap so a long
               workspace name needs a shrinkable element to ellipse inside, or it
               overflows into its neighbour. The button's title carries the full
               name. -->
          <span class="workspace-name">{{ workspaceLabel(workspace.name) }}</span>
          <span
            v-if="missedCountFor(workspace.name) > 0"
            class="badge badge--missed"
            :title="`${missedCountFor(workspace.name)} missed`"
            :aria-label="`${missedCountFor(workspace.name)} missed`"
          >{{ missedCountFor(workspace.name) }}</span>
        </button>
      </div>
      <div class="schedules-list">
        <div v-if="workspaceSchedules.length === 0 && workspaceLoops.length === 0" class="empty-hint">// no automations in this workspace</div>

        <template v-if="oneOffSchedules.length">
          <div class="schedule-group schedule-group--once">
            <div class="schedule-group-header">
              <span>One-offs <span class="schedule-group-hint">delete after run</span></span>
              <span class="schedule-group-count">{{ oneOffSchedules.length }}</span>
            </div>
            <div class="schedule-group-items">
              <router-link
                v-for="s in oneOffSchedules"
                :key="s.schedule_id"
                :to="`/schedules/${s.schedule_id}`"
                class="schedule-item schedule-item--once"
                :class="{ 'schedule-item--missed': s.missed, 'schedule-item--disabled': !s.enabled }"
                active-class="active"
              >
                <span class="schedule-time">{{ s.run_at_date?.slice(5) }} {{ s.daily_time_utc }}</span>
                <span class="schedule-label">{{ s.title || promptTitle(s.prompt) }}</span>
                <span v-if="s.missed" class="missed-dot" title="Expected to run but didn't"></span>
              </router-link>
            </div>
          </div>
        </template>

        <template v-if="userRoutines.length">
          <div class="schedule-group">
            <div class="schedule-group-header">
              <span>Custom Routines</span>
              <span class="schedule-group-count">{{ userRoutines.length }}</span>
            </div>
            <div class="schedule-group-items">
              <router-link
                v-for="s in userRoutines"
                :key="s.schedule_id"
                :to="`/schedules/${s.schedule_id}`"
                class="schedule-item"
                :class="{ 'schedule-item--missed': s.missed, 'schedule-item--disabled': !s.enabled }"
                active-class="active"
              >
                <span class="schedule-time">{{ !s.enabled ? 'off' : s.frequency === 'manual' ? '·' : s.daily_time_utc }}</span>
                <span class="schedule-label">{{ s.title || promptTitle(s.prompt) }}</span>
                <span v-if="s.missed" class="missed-dot" title="Expected to run but didn't"></span>
              </router-link>
            </div>
          </div>
        </template>

        <template v-if="userLoops.length">
          <div class="schedule-group">
            <div class="schedule-group-header">
              <span>Custom Loops</span>
              <span class="schedule-group-count">{{ userLoops.length }}</span>
            </div>
            <div class="schedule-group-items">
              <router-link
                v-for="l in userLoops"
                :key="l.loop_id"
                :to="`/schedules/${l.loop_id}`"
                class="schedule-item"
                :class="{ 'schedule-item--disabled': !l.running }"
                active-class="active"
              >
                <span class="schedule-time">{{ l.running ? `${l.interval_minutes}m` : 'off' }}</span>
                <span class="schedule-label">{{ l.title || promptTitle(l.prompt) }}</span>
                <span
                  v-if="l.running && l.web_chat_id && store.isChatStreaming(l.web_chat_id)"
                  class="spinner-dot"
                  title="This loop is working"
                />
              </router-link>
            </div>
          </div>
        </template>

        <template v-if="systemAutomations.length">
          <div class="schedule-group schedule-group--system">
            <div class="schedule-group-header">
              <span>System Routines</span>
              <span class="schedule-group-count">{{ systemAutomations.length }}</span>
            </div>
            <div class="schedule-group-items">
              <router-link
                v-for="s in systemAutomations"
                :key="s.schedule_id"
                :to="`/schedules/${s.schedule_id}`"
                class="schedule-item"
                :class="{ 'schedule-item--missed': s.missed, 'schedule-item--disabled': !s.enabled }"
                active-class="active"
              >
                <span class="schedule-time">{{ !s.enabled ? 'off' : s.frequency === 'manual' ? '·' : s.daily_time_utc }}</span>
                <span class="schedule-label">{{ s.title || promptTitle(s.prompt) }}</span>
                <span v-if="s.missed" class="missed-dot" title="Expected to run but didn't"></span>
              </router-link>
            </div>
          </div>
        </template>

        <template v-if="systemLoops.length">
          <div class="schedule-group schedule-group--system">
            <div class="schedule-group-header">
              <span>System Loops</span>
              <span class="schedule-group-count">{{ systemLoops.length }}</span>
            </div>
            <div class="schedule-group-items">
              <router-link
                v-for="l in systemLoops"
                :key="l.loop_id"
                :to="`/schedules/${l.loop_id}`"
                class="schedule-item"
                :class="{ 'schedule-item--disabled': !l.running }"
                active-class="active"
              >
                <span class="schedule-time">{{ l.running ? `${l.interval_minutes}m` : 'off' }}</span>
                <span class="schedule-label">{{ l.title || promptTitle(l.prompt) }}</span>
                <span
                  v-if="l.running && l.web_chat_id && store.isChatStreaming(l.web_chat_id)"
                  class="spinner-dot"
                  title="This loop is working"
                />
              </router-link>
            </div>
          </div>
        </template>
      </div>

      <div class="sidebar-footer">
        <button class="add-automation-btn" @click="emit('new-schedule')">+ New Automation</button>
      </div>
    </template>

    <template v-if="!collapsed && mode === 'settings'">
      <div class="sidebar-section-header">
        <span class="sidebar-section-title">settings</span>
      </div>
      <div class="settings-nav-list">
        <router-link
          to="/settings"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings' }"
        >
          home
        </router-link>
        <router-link
          to="/settings/providers"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/providers' }"
        >
          providers
        </router-link>
        <router-link
          to="/settings/workspaces"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/workspaces' }"
        >
          workspaces
        </router-link>
        <router-link
          to="/settings/models"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/models' }"
        >
          models
        </router-link>
        <router-link
          to="/settings/context"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/context' }"
        >
          context
        </router-link>
        <router-link
          to="/settings/skills"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/skills' }"
        >
          skills
        </router-link>
        <router-link
          to="/settings/subagents"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/subagents' }"
        >
          subagents
        </router-link>
        <router-link
          to="/settings/commands"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/commands' }"
        >
          commands
        </router-link>
        <router-link
          to="/settings/mcp"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/mcp' }"
        >
          mcp
        </router-link>
        <router-link
          to="/settings/automations"
          class="settings-nav-item"
          :class="{ active: route.path === '/settings/automations' }"
        >
          automations
        </router-link>
      </div>
    </template>

    <template v-if="!collapsed && mode === 'memory'">
      <div v-if="hasMultipleWorkspaces" class="workspace-toggle">
        <button
          v-for="workspace in store.workspaceOptions"
          :key="workspace.name"
          :class="{ active: store.activeWorkspace === workspace.name }"
          :aria-pressed="store.activeWorkspace === workspace.name"
          :aria-keyshortcuts="workspaceShortcut(workspace.name) || undefined"
          :data-workspace-color="colorForWorkspace(workspace)"
          :title="workspaceShortcut(workspace.name) ? `Switch to ${workspaceLabel(workspace.name)} (${workspaceShortcut(workspace.name)})` : undefined"
          @click="store.switchWorkspace(workspace.name, { transition: false })"
        >
          <span v-if="workspaceShortcut(workspace.name)" class="workspace-shortcut" aria-hidden="true">{{ workspaceShortcut(workspace.name) }}</span>
          <span class="workspace-name">{{ workspaceLabel(workspace.name) }}</span>
        </button>
      </div>

      <div class="mm-sidebar-scroll">
        <h3>Vault</h3>
        <div class="mm-stat-grid">
          <div class="mm-stat"><div class="n">{{ mm.visibleNodes.length }}</div><div class="l">notes shown</div></div>
          <div class="mm-stat"><div class="n">{{ mm.visibleEdgeCount }}</div><div class="l">links</div></div>
          <div class="mm-stat"><div class="n">{{ mm.orphanCount }}</div><div class="l">orphaned</div></div>
          <div class="mm-stat"><div class="n">{{ mm.nodes.length }}</div><div class="l">total</div></div>
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

        <h3>Path finder</h3>
        <p class="mm-hint">{{ mm.pathHint }}</p>
        <button v-if="mm.pathStart || mm.pathEnd" type="button" class="mm-link" @click="mm.resetPath()">clear path</button>
      </div>
    </template>

    <template v-if="!collapsed && (!mode || mode === 'chat' || mode === 'project')">
      <!-- Workspace toggle -->
      <div v-if="hasMultipleWorkspaces" class="workspace-toggle">
        <button
          v-for="workspace in store.workspaceOptions"
          :key="workspace.name"
          :class="{ active: store.activeWorkspace === workspace.name }"
          :aria-pressed="store.activeWorkspace === workspace.name"
          :aria-keyshortcuts="workspaceShortcut(workspace.name) || undefined"
          :data-workspace-color="colorForWorkspace(workspace)"
          :title="workspaceShortcut(workspace.name) ? `Switch to ${workspaceLabel(workspace.name)} (${workspaceShortcut(workspace.name)})` : undefined"
          @click="store.switchWorkspace(workspace.name)"
        >
          <!-- Shortcut badge comes from workspaceShortcut(), which also backs the
               aria-keyshortcuts on this button and returns '' past the 9th
               workspace. Marks follow the signal grammar: needs-you outranks
               working, and unread is a separate count. -->
          <span v-if="workspaceShortcut(workspace.name)" class="workspace-shortcut" aria-hidden="true">{{ workspaceShortcut(workspace.name) }}</span>
          <span class="workspace-name">{{ workspaceLabel(workspace.name) }}</span>
          <span
            v-if="store.workspaceNeedsInput(workspace.name) > 0"
            class="workspace-status-dot"
            title="A chat needs your answer"
            aria-label="A chat needs your answer"
          />
          <span
            v-else-if="store.workspaceIsStreaming(workspace.name)"
            class="workspace-status-ring"
            title="A chat is working"
            aria-label="A chat is working"
          ><span class="workspace-status-core" aria-hidden="true" /></span>
          <span
            v-if="store.workspaceUnread(workspace.name) > 0"
            class="badge"
            :title="`${store.workspaceUnread(workspace.name)} unread chats`"
            :aria-label="`${store.workspaceUnread(workspace.name)} unread chats`"
          >{{ store.workspaceUnread(workspace.name) }}</span>
        </button>
      </div>

      <!-- Scrollable area for chats/projects -->
      <div class="chats-scroll-area">
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
        <div class="project-list">
          <div
            v-for="project in store.workspaceProjects"
            :key="project.project_id"
            class="project-group"
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
              @contextmenu.prevent="toggleProjectMenu($event, project)"
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
              <button
                class="add-chat-btn"
                :class="{ 'add-chat-btn--creating': store.creatingChatProjectIds[project.project_id] }"
                :disabled="store.creatingChatProjectIds[project.project_id]"
                @click.stop="addChat(project.project_id)"
                title="New chat"
                :aria-label="`New chat in ${project.name}`"
              >{{ store.creatingChatProjectIds[project.project_id] ? '...' : '+' }}</button>
            </div>

            <!-- Context menu (suppressed for system projects) - teleported to body -->
            <Teleport to="body">
              <div
                v-if="projectMenu === project.project_id && !project.is_auto"
                class="context-menu-overlay"
                @click.self="projectMenu = null"
              >
                <div
                  class="context-menu"
                  :style="{ top: projectMenuPos.top + 'px', left: projectMenuPos.left + 'px' }"
                  @mouseleave="projectMenu = null"
                >
                  <button @click="startEditProject(project.project_id)">Rename</button>
                  <button
                    v-if="!project.vault_folder"
                    @click="confirmDeleteProject(project.project_id)"
                  >Delete</button>
                </div>
              </div>
            </Teleport>

            <!-- Chats in project -->
            <div v-if="expandedProjects.has(project.project_id)" class="chat-list">
              <template
                v-for="{ chat, isDelegate } in store.projectChatRows(project.project_id)"
                :key="chat.chat_id"
              >
                <div
                  v-if="!isDelegate || subchatsExpanded(chat.spawned_from_chat_id || '')"
                  class="chat-item"
                  :class="{
                    active: chat.chat_id === store.activeChatId,
                    remote: chat.local === false,
                    delegate: isDelegate,
                    dragging: dragChatId === chat.chat_id,
                  }"
                  :draggable="chat.local !== false"
                  @click="chat.local !== false && selectChat(chat.chat_id)"
                  @keydown.enter.self.prevent="chat.local !== false && selectChat(chat.chat_id)"
                  @keydown.space.self.prevent="chat.local !== false && selectChat(chat.chat_id)"
                  @dragstart="onChatDragStart(chat, $event)"
                  @dragend="onChatDragEnd"
                  @contextmenu.prevent="toggleChatMenu($event, chat.chat_id)"
                  role="link"
                  :tabindex="chat.local === false ? -1 : 0"
                  :aria-disabled="chat.local === false"
                  :title="chat.local === false ? 'This chat lives on another instance' : 'Drag to move to another project'"
                >
                  <span
                    v-if="isDelegate"
                    class="delegate-mark"
                    title="Delegate: spawned by this chat's supervisor"
                    aria-label="Delegate chat"
                  >&#8627;</span>
                  <button
                    v-else-if="visibleSubchatCount(project.project_id, chat.chat_id) > 0"
                    type="button"
                    class="subchat-toggle"
                    :aria-expanded="subchatsExpanded(chat.chat_id)"
                    :aria-label="(subchatsExpanded(chat.chat_id) ? 'Collapse' : 'Expand') + ' subchats for ' + chat.title"
                    :title="(subchatsExpanded(chat.chat_id) ? 'Collapse' : 'Expand') + ' subchats'"
                    @click.stop="toggleSubchats(chat.chat_id)"
                  >{{ subchatsExpanded(chat.chat_id) ? '▾' : '▸' }}</button>
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
                  <button
                    class="chat-actions-btn"
                    aria-label="Chat actions"
                    title="Copy ID, rename, move, archive, delete"
                    @click.stop="toggleChatMenu($event, chat.chat_id)"
                  >&middot;&middot;&middot;</button>
                </div>
              </template>

              <!-- Chat context menu - teleported to body -->
              <Teleport to="body">
                <div
                  v-if="chatMenu && store.projectChats(project.project_id).some(c => c.chat_id === chatMenu)"
                  class="context-menu-overlay"
                  @click.self="closeChatMenus()"
                >
                  <div
                    class="context-menu"
                    :style="{ top: chatMenuPos.top + 'px', left: chatMenuPos.left + 'px' }"
                  >
                    <template v-if="!moveSubmenu">
                      <button @click="copyChatId(chatMenu!)">Copy chat ID</button>
                      <button @click="startRenameChat(chatMenu!)">Rename</button>
                      <button v-if="moveTargets.length" @click="openMoveSubmenu()">Move to...</button>
                      <button v-if="chatMenuChat?.retry?.status === 'pending'" @click="stopRetry(chatMenu!)">Stop trying</button>
                      <button v-else @click="setRetry(chatMenu!)">Set to retry</button>
                      <button @click="doArchiveChat(chatMenu!)">
                        {{ archiveMenuLabel(chatMenu!) }}
                      </button>
                      <button @click="confirmDeleteChat(chatMenu!)">Delete</button>
                    </template>
                    <template v-else>
                      <div class="context-menu-label">Move to project</div>
                      <button
                        v-for="target in moveTargets"
                        :key="target.project_id"
                        @click="doMoveChat(target.project_id)"
                      >{{ target.name }}</button>
                      <button class="context-menu-back" @click="moveSubmenu = false">← Back</button>
                    </template>
                  </div>
                </div>
              </Teleport>
            </div>
          </div>
        </div>
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
import { ref, reactive, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { errorMessage } from '../lib/errorMessage'
import { useTaskStore } from '../stores/tasks'
import { useFileViewerStore } from '../stores/fileViewer'
import { useMemoryMapStore, categoryColorFor, catKeyFor } from '../stores/memoryMap'
import NotificationBell from './NotificationBell.vue'
import ChatSignals from './ChatSignals.vue'
import { loopInWorkspace, scheduleInWorkspace } from '../lib/automationWorkspace'
import { colorForWorkspace } from '../lib/workspaceColors'
import { archiveMenuLabel as menuLabel, archiveConfirmMessage } from '../lib/archiveCopy'
import { askConfirm } from '../lib/confirm'
import { workspaceLabel } from '../lib/workspaceLabel'
import { askPrompt } from '../lib/prompt'

const props = defineProps<{ collapsed: boolean; mode?: 'chat' | 'project' | 'schedules' | 'settings' | 'memory' }>()
const emit = defineEmits<{ toggle: []; 'chat-selected': []; 'new-schedule': [] }>()

const store = useProjectStore()
const taskStore = useTaskStore()
const fileViewer = useFileViewerStore()
const mm = useMemoryMapStore()
const route = useRoute()
const router = useRouter()

function promptTitle(prompt: string): string {
  const first = prompt.split('\n')[0].trim()
  return first.length > 36 ? first.slice(0, 33) + '...' : first
}

// With a single workspace the toggle is pure noise — hide it and let the
// content fill the space.
const hasMultipleWorkspaces = computed(() => store.workspaceOptions.length > 1)

// Schedule list split: one-offs first (sorted by datetime), then recurring.
const workspaceSchedules = computed(() =>
  taskStore.schedules.filter(s => scheduleInWorkspace(s, store.activeWorkspace)),
)
const workspaceLoops = computed(() =>
  taskStore.loops.filter(l => loopInWorkspace(
    l,
    store.activeWorkspace,
    store.chats,
    store.projects,
  )),
)

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
const userLoops = computed(() =>
  workspaceLoops.value.filter(l => l.scope !== 'system'),
)
const systemLoops = computed(() =>
  workspaceLoops.value.filter(l => l.scope === 'system'),
)

async function selectAutomationWorkspace(workspace: string) {
  if (store.activeWorkspace !== workspace) {
    await store.switchWorkspace(workspace, { transition: false })
  }
  if (props.mode === 'schedules') await router.push('/schedules')
}

function missedCountFor(workspace: string): number {
  return taskStore.schedules.filter(
    s => s.enabled && s.missed && scheduleInWorkspace(s, workspace),
  ).length
}

import type { ChatInfo, ProjectInfo } from '../lib/types'
function openProject(projectId: string) {
  router.push(`/project/${projectId}`)
  emit('chat-selected') // collapse sidebar on mobile
}

const expandedProjects = reactive(new Set<string>())
// Keep supervisor groups open by default so the existing sidebar remains
// unchanged until the user explicitly collapses one. This is deliberately
// component-local, matching the project disclosure state above and the chat
// context disclosure in ChatPanel.
const collapsedSubchatParents = reactive(new Set<string>())
const projectMenu = ref<string | null>(null)
const chatMenu = ref<string | null>(null)
const chatMenuPos = ref<{ top: number; left: number }>({ top: 0, left: 0 })
const projectMenuPos = ref<{ top: number; left: number }>({ top: 0, left: 0 })
const moveSubmenu = ref(false)
const editingProject = ref<string | null>(null)
const renamingChat = ref<string | null>(null)
const renameValue = ref('')
const isAnyChatWorking = computed(() => {
  return Object.values(store.streaming).some(Boolean) ||
         Object.values(store.projectStreaming).some(Boolean) ||
         Object.values(store.backgroundAgents).some(val => val > 0)
})

const hasAutomationWarning = computed(() => {
  return taskStore.schedules.some(s => s.enabled && s.missed) ||
         taskStore.loops.some(l => l.running && (l.last_status === 'error' || l.last_status === 'missing-chat'))
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

function menuPosition(rect: DOMRect, menuHeight = 184): { top: number; left: number } {
  const top = rect.bottom + 4
  const left = Math.max(8, rect.right - 160)
  // If the menu would overflow the viewport bottom, flip it above the trigger
  if (top + menuHeight > window.innerHeight) {
    return { top: Math.max(8, rect.top - menuHeight - 4), left }
  }
  return { top, left }
}

function toggleChatMenu(event: MouseEvent, chatId: string) {
  if (chatMenu.value === chatId) {
    chatMenu.value = null
    return
  }
  const btn = event.currentTarget as HTMLElement
  const rect = btn.getBoundingClientRect()
  chatMenuPos.value = menuPosition(rect)
  chatMenu.value = chatId
}

function toggleProjectMenu(event: MouseEvent, project: ProjectInfo) {
  if (project.is_auto) { projectMenu.value = null; return }
  if (projectMenu.value === project.project_id) {
    projectMenu.value = null
    return
  }
  const el = event.currentTarget as HTMLElement
  const rect = el.getBoundingClientRect()
  projectMenuPos.value = menuPosition(rect, 80)
  projectMenu.value = project.project_id
}

function openMoveSubmenu() {
  moveSubmenu.value = true
}

function closeChatMenus() {
  chatMenu.value = null
  moveSubmenu.value = false
}

function activeSubchatCount(chatId: string): number {
  return store.activeDelegatesFor(chatId).length
}

function archiveMenuLabel(chatId: string): string {
  return menuLabel(activeSubchatCount(chatId))
}

// Subchats working right now. Archiving stops them instead of waiting, so the
// confirm dialog names them first. Background agents count as working: they
// outlive their turn, so a subchat with no live turn can still be busy.
function busySubchatCount(chatId: string): number {
  return store.activeDelegatesFor(chatId).filter(
    d => store.isChatStreaming(d.chat_id) || store.chatHasBackgroundAgents(d.chat_id),
  ).length
}

function archiveConfirmation(chatId: string): string {
  return archiveConfirmMessage(activeSubchatCount(chatId), busySubchatCount(chatId))
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

watch(() => store.activeChatId, (chatId) => {
  if (!chatId) return
  const project = store.projectFor(chatId)
  if (project) {
    expandedProjects.add(project.project_id)
  }
  const chat = store.chats.find(c => c.chat_id === chatId)
  if (chat?.spawned_from_chat_id) {
    collapsedSubchatParents.delete(chat.spawned_from_chat_id)
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

function visibleSubchatCount(projectId: string, chatId: string): number {
  return store.projectChatGroups(projectId)
    .find(group => group.chat.chat_id === chatId)?.delegates.length || 0
}

function subchatsExpanded(chatId: string): boolean {
  return !collapsedSubchatParents.has(chatId)
}

function toggleSubchats(chatId: string) {
  if (subchatsExpanded(chatId)) {
    collapsedSubchatParents.add(chatId)
  } else {
    collapsedSubchatParents.delete(chatId)
  }
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
  projectMenu.value = null
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
  projectMenu.value = null
  if (!await askConfirm('Delete this project and archive all its chats?', {
    title: 'Delete project',
    confirmLabel: 'Delete project',
    destructive: true,
  })) return
  await store.deleteProject(id)
}

async function addChat(projectId: string) {
  expandedProjects.add(projectId)
  await store.createChat(projectId)
}

function startRenameChat(chatId: string) {
  chatMenu.value = null
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
    await navigator.clipboard.writeText(chatId)
    store.pushToast({
      chat_id: chatId,
      title: 'Chat ID copied',
      body: chatId,
    })
  } catch (e) {
    store.pushErrorToast('Could not copy chat ID', `${errorMessage(e)}`)
  }
}

async function doArchiveChat(chatId: string) {
  chatMenu.value = null
  // This path never asked for confirmation, unlike the chat header's archive
  // button, so archiving from the sidebar menu was a single misclick.
  if (!await askConfirm(archiveConfirmation(chatId), {
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
  chatMenu.value = null
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
  chatMenu.value = null
  await store.stopChatRetry(chatId)
}

async function confirmDeleteChat(chatId: string) {
  chatMenu.value = null
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
  /* Queried below so the nav label answers to the width it actually has -
     the sidebar is drag-resizable and remembers its width per user, so a
     viewport media query cannot know whether "automations" fits. */
  container-type: inline-size;
  /* Keep the collapsed rail aligned with the expanded nav and pane headers. */
  height: 61px;
  flex-shrink: 0;
  padding: 8px;
  border-bottom: 1px solid var(--border);
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

.nav-links {
  display: flex;
  align-items: center;
  /* The wordmark used to take the middle of this row, leaving these four icons
     huddled at a 4px gap against the right edge. It is in the pane header now, and
     the ~90px it gives back is spent here: the icons spread across a 200px strip,
     so each 30px glyph gets ~27px of air and its 44px touch target no longer
     overlaps its neighbour's. The glyphs stay 30px, because the pane header sizes
     its own icons to match the sidebar - see the note there.
     `space-between` over a capped basis rather than a fixed gap, because that
     degrades in both directions: a sidebar dragged out to 500px does not fling the
     icons to the far edge (the strip stops at 200px), and one dragged down to its
     180px minimum packs them back to the --space-1 floor instead of overflowing
     the rail. The strip is narrower on mobile, where the bell hides - see below. */
  /* Sized to content now rather than a fixed strip: the active item carries an
     expanding label, so the row's width depends on which page you are on. */
  flex: 0 1 auto;
  justify-content: flex-end;
  /* Wider than the old icon-only 4px: the active item now ends in text, and a
     4px gap between a word and the next glyph reads as a collision. */
  gap: var(--space-2);
  margin-left: auto;
  min-width: 0;
}

.nav-item {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  /* No gap: the label carries its own leading space instead. A collapsed label
     is max-width:0, but a flex gap is reserved whether or not the item beside
     it has width - so on an icons-only row the 4px still counted, pushing the
     glyph 2px left of the pill's centre and leaving twice as much air on its
     right. As padding on the label it disappears with the label, because
     border-box folds it into that max-width:0. */
  gap: 0;
  /* min-width, not width: the active item grows to fit its label. */
  min-width: 30px;
  height: 30px;
  /* Padding is left to .touch-hit, uniformly. Trading the inline half down to
     var(--space-1) packed the rail by 6px per item, but .touch-hit paints its
     pill by insetting that padding on every side: at 4px the highlight landed
     3px *inside* the glyph, clipping the icon instead of padding it, and the
     matching negative margin shrank each item's footprint to 24px. Uniform
     padding restores the 30px control with a 44px touch target, and matches
     the bell beside it, which never overrode this. */
  border-radius: var(--radius-sm);
  position: relative;
  isolation: isolate;
  color: var(--fg2);
  text-decoration: none;
  transition: color 120ms var(--ease);
}

/* The page you are on names itself, next to its own icon, instead of a separate
   tag elsewhere in the window. Inactive items stay glyph-only, so the row reads
   as one selected item among icons rather than a list of words. Collapsed with
   max-width so it animates, and aria-hidden because .nav-item already carries a
   full aria-label - otherwise the accessible name would read "automations
   automations". */
.nav-item-label {
  max-width: 0;
  overflow: hidden;
  /* The gap that used to live on .nav-item; see the note there. */
  padding-inline-start: var(--space-1);
  color: inherit;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.04em;
  white-space: nowrap;
  opacity: 0;
  transition: max-width 160ms var(--ease), opacity 120ms var(--ease);
}

.nav-item--active .nav-item-label {
  /* Room for the longest label ("automations", 11 characters) plus a little, so
     it is never clipped mid-word. At 9ch it read as "automatio" hard against the
     next glyph. */
  max-width: 13ch;
  opacity: 1;
}

/* Below this the rail cannot hold a full label and every glyph at once, so trade
   the label away rather than the icons. */
@media (max-width: 900px) {
  .nav-item--active .nav-item-label { max-width: 0; opacity: 0; }
}

/* The row's contents need roughly: toggle (44) + active item with label (~118)
   + two bare items (88) + bell (44) + gaps (24) = 318. Under that the flex row
   shrinks the only thing that can give - the label - and "automations" rendered
   as "automation" jammed against the pill edge. Drop the label instead of
   clipping a word in half; the icon and its tooltip still say what it is.
   Keyed to the header's own width, so a user who drags the sidebar narrow (or
   kept a width saved from before it grew) gets the icons-only row.

   A container query resolves against the container's *content* box, so this
   compares against 318 with the header's own 16px padding already excluded -
   not against the sidebar's outer width. At the 340px default the header
   measures 340 - 1 (sidebar border) - 16 (its padding) - any --safe-left inset
   = 323 on a desktop window, which clears it. On a device with a left inset the
   headroom shrinks and the label hides earlier, which is the intended
   degradation rather than a clipped word.

   Known limit: the cap above is in ch and this threshold is in px, so at a
   large --font-scale the label can outgrow the budget and clip again. Fixing
   that properly needs measurement rather than a breakpoint. */
@container (max-width: 317px) {
  .nav-item--active .nav-item-label { max-width: 0; opacity: 0; }
}

.nav-item:hover {
  color: var(--fg);
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
  color: var(--accent);
}

.nav-item--active::before {
  background: var(--bg3);
}

.sidebar-bell :deep(.bell-btn) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  position: relative;
  isolation: isolate;
  color: var(--fg2);
  background: none;
  border: none;
  cursor: pointer;
  transition: color 120ms var(--ease);
}
.sidebar-bell :deep(.bell-btn) svg {
  width: 18px;
  height: 18px;
}
.sidebar-bell :deep(.bell-btn:hover) {
  color: var(--fg);
}
.sidebar-bell :deep(.bell-btn.has-unread) {
  color: var(--accent);
}
.sidebar-bell :deep(.bell-badge) {
  top: 4px;
  right: 4px;
  font-size: calc(10px * var(--font-scale));
  min-width: 14px;
  height: 14px;
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

.workspace-shortcut {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 16px;
  padding: 0 3px;
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  color: var(--fg3);
  font-size: 10px;
  line-height: 1;
  font-weight: 700;
  flex: 0 0 auto;
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

/* Keep workspace status markers visually separate from the workspace name,
   matching the spacing used by project-level state dots. */
.workspace-toggle button .workspace-status-dot,
.workspace-toggle button .workspace-status-ring {
  margin-left: var(--space-2);
}

.project-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  flex-shrink: 0;
}

.project-group {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.project-header {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px;
  font-size: var(--text-sm);
  color: var(--fg2);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  background: var(--bg2);
  min-height: var(--touch);
}

.project-header:hover {
  background: var(--bg3);
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

.project-group:has(.chat-list) .project-header {
  border-bottom: 1px solid var(--border);
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

.chat-item {
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: var(--touch);
  padding: 0 4px 0 20px;
  cursor: pointer;
  font-size: var(--text-base);
  color: var(--fg2);
  overflow: hidden;
  white-space: nowrap;
  border-bottom: 1px solid var(--border);
}

.chat-item:last-child {
  border-bottom: none;
}

/* A delegate is a real chat, so it keeps the full row (streaming dot, unread
   badge, actions menu) and only shifts right to read as owned by the chat
   above it. Indent is on padding rather than margin so the hover/active
   background still spans the full sidebar width. */
.chat-item.delegate {
  padding-left: 34px;
}

.delegate-mark {
  flex: none;
  color: var(--fg3, var(--fg2));
  font-size: var(--text-sm, 0.85em);
  line-height: 1;
}

/* A supervisor's disclosure sits inside the parent chat row. It uses the same
   44px hit area as the project disclosure while keeping the glyph compact, so
   collapsing a busy supervisor does not make the child rows unreachable on a
   touch device. */
.subchat-toggle {
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
.subchat-toggle:hover { color: var(--fg); }
.subchat-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
  border-radius: var(--radius-sm);
}

/* Loop marker on a chat row. Accent while the cadence is live, muted when the
   loop exists but is stopped, so "this chat re-runs itself" reads at a glance
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
  background: var(--bg3);
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
  border-left: 2px solid var(--accent);
  padding-left: 18px;
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

.workspace-shortcut {
  flex: 0 0 auto;
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 700;
}

.workspace-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
  .sidebar-bell { display: none; }
  /* Three icons here instead of four (the pane header carries the bell on mobile),
     so the strip narrows to match. Left at 200px it would space three glyphs
     40px apart and read as scattered rather than spread. */
  .nav-links { flex-basis: 150px; }
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
}

/* Grouped schedule sections */
.schedule-group {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  margin-bottom: 10px;
  flex-shrink: 0;
}
.schedule-group--once {
  border-left: 2px solid var(--accent);
}
.schedule-group--system {
  border-left: 2px solid var(--accent2);
}
.schedule-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--fg2);
  font-weight: 600;
}
.schedule-group-hint {
  font-weight: 400;
  text-transform: none;
  letter-spacing: 0;
  color: var(--fg2);
  opacity: 0.7;
  font-size: calc(10px * var(--font-scale));
  margin-left: 4px;
}
.schedule-group-count {
  font-size: calc(10px * var(--font-scale));
  background: var(--bg3);
  padding: 1px 5px;
  border-radius: 999px;
  color: var(--fg2);
  min-width: 16px;
  text-align: center;
}
.schedule-group-items {
  display: flex;
  flex-direction: column;
}
.schedule-group-items .schedule-item {
  border-radius: 0;
}
.schedule-item--once .schedule-time {
  color: var(--accent, #ff5566);
  font-weight: 600;
}
.schedule-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  text-decoration: none;
  color: var(--fg2);
  font-size: var(--text-base);
  cursor: pointer;
  min-height: var(--touch);
}
.schedule-item:hover { background: var(--bg3); color: var(--fg); }
.schedule-item.active {
  background: var(--bg3);
  color: var(--fg);
  font-weight: 500;
  border-left: 2px solid var(--accent);
  padding-left: 8px;
}
.schedule-item .schedule-time {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  color: var(--fg2);
  flex-shrink: 0;
  font-size: var(--text-base);
}
.schedule-item .schedule-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.schedule-item--missed .schedule-time { color: var(--warning); }
.schedule-item--disabled { opacity: 0.5; }
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
  padding: 8px;
  gap: 2px;
}
.settings-nav-item {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  text-decoration: none;
  color: var(--fg2);
  font-size: var(--text-base);
  cursor: pointer;
  transition: background 120ms var(--ease), color 120ms var(--ease);
}
.settings-nav-item:hover {
  background: var(--bg);
  color: var(--fg);
}
.settings-nav-item.active {
  background: var(--bg3);
  color: var(--fg);
  border-right: 2px solid var(--accent);
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
}
.mm-row-between h3 { margin: 0; }
.mm-link { background: none; border: none; color: var(--accent); font-size: var(--text-xs); cursor: pointer; padding: 0; }
.mm-hint { color: var(--fg3); font-size: var(--text-xs); margin: 0; }

.mm-stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-2); }
.mm-stat { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 6px 8px; }
.mm-stat .n { font-size: var(--text-lg); font-weight: 600; }
.mm-stat .l { font-size: var(--text-xs); color: var(--fg3); }

.mm-search { margin-top: var(--space-3); }
.mm-search input { width: 100%; font-size: var(--text-sm); }

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
.mm-link-item .cnt { margin-left: auto; color: var(--fg3); }
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
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  z-index: 201;
  padding: 4px 0;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3);
}

.context-menu button {
  display: block;
  width: 100%;
  text-align: left;
  padding: 6px 16px;
  border: none;
  background: none;
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-base);
}

.context-menu button:hover {
  background: var(--bg3);
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
