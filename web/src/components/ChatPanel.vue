<template>
  <div class="chat-panel" @dragover.prevent="dragOver = true" @dragleave="dragOver = false" @drop.prevent="handleDrop" @click="handlePanelClick">
    <div v-if="dragOver" class="drop-overlay">Drop images to attach, or files to add their accessible path</div>

    <!-- Header. No page tag: the breadcrumb below already reads
         `workspace / project / title`, so a "chat" marker beside it would name
         what the breadcrumb and the transcript underneath it both already say.
         This is also the most crowded header in the app, so the room goes to the
         breadcrumb and the action icons instead. -->
    <!-- No brand mark here. This is the densest header in the app - breadcrumb,
         model picker, agent pill, archive - and the centred wordmark was
         squeezing the chat title down to a few characters. The breadcrumb
         already says where you are, and the mark is a click-to-reload
         shortcut available on every other view. -->
    <PaneHeader
      :brand="false"
      :active-bg-agents="store.activeBackgroundAgents"
      @open-sidebar="$emit('open-sidebar')"
    >
      <template #title>
        <div class="header-left">
          <!-- Same 18px stroke icon in the same 30px box as the trailing header
               actions, so the close control reads as one of them. It used to be
               a `&times;` character at 20px, which is a different size from
               every icon around it and sits on the text baseline rather than in
               a box - visibly out of line beside the breadcrumb. -->
          <button class="btn-icon close-btn" @click="$emit('close')" title="Close chat" aria-label="Close chat">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
          <div class="header-breadcrumb" ref="breadcrumbRef">
            <!-- Workspace and project are one thing - the scope the chat sits in -
                 so they share a wrapper. Wrapping them together is what lets the
                 narrow header put the scope on one quiet line above the title:
                 as three loose flex items they broke into three lines at three
                 different sizes, with the workspace the largest text in the
                 header and the title the smallest.
                 The workspace was only ever implied here, through --accent. This
                 is the screen where you are deepest inside one, so it says so -
                 with its number key, which is otherwise only discoverable in
                 the sidebar. -->
            <span v-if="hasScopeCrumb" class="breadcrumb-scope">
              <span
                v-if="workspaceCrumb"
                class="breadcrumb-workspace"
                :data-workspace-color="workspaceCrumb.color"
                :title="`Workspace ${workspaceCrumb.name} (press ${workspaceCrumb.key})`"
              >{{ workspaceCrumb.name }}</span>
              <span v-if="workspaceCrumb && projectCrumb" class="breadcrumb-separator">/</span>
              <span
                v-if="projectCrumb"
                class="breadcrumb-project"
                @click.stop="toggleContext"
                :class="{ active: showContext }"
              >{{ projectCrumb }}</span>
            </span>
            <input
              v-if="editingTitle"
              class="title-input"
              v-model="titleValue"
              @keyup.enter="saveTitle"
              @keyup.escape="editingTitle = false"
              @blur="saveTitle"
              @click.stop
              autofocus
            />
            <span v-else class="pane-title chat-title" @dblclick.stop="startEditTitle" @click.stop>{{ chat.title }}</span>
            <!-- Project context popup -->
            <div
              v-if="showContext"
              class="context-popup"
              @click.stop
            >
              <div class="context-popup-body">
                <div v-if="project?.vault_doc_path" class="context-popup-section">
                  <span class="label-eyebrow">Project</span>
                  <p v-if="project.context" class="context-description">{{ project.context }}</p>
                  <button
                    class="btn-small"
                    @click="fileViewer.open(project.vault_doc_path)"
                  >Open canonical doc</button>
                </div>
                <div v-else class="context-popup-section">
                  <span class="label-eyebrow">Project context</span>
                  <textarea
                    v-model="contextDraft"
                    class="context-textarea"
                    :placeholder="project ? 'No project context configured.' : ''"
                    :disabled="!project"
                    rows="3"
                  ></textarea>
                  <div class="context-edit-actions">
                    <span v-if="contextStatus" class="context-status" :class="contextStatus">{{ contextStatusLabel }}</span>
                    <button
                      class="btn-small"
                      :disabled="!contextDirty || contextSaving"
                      @click="saveContext"
                    >{{ contextSaving ? 'Saving...' : 'Save' }}</button>
                  </div>
                </div>
                <div v-if="showProjectFiles" class="context-popup-section">
                  <span class="label-eyebrow">Files ({{ projectFiles.length }})</span>
                  <div v-if="projectFilesLoading" class="context-files-status">Loading…</div>
                  <div v-else-if="projectFilesError" class="context-files-status error">{{ projectFilesError }}</div>
                  <div v-else-if="!projectFiles.length" class="context-files-status">// no files</div>
                  <div v-else class="context-files-list">
                    <div
                      v-for="f in projectFiles"
                      :key="f.path"
                      class="context-file-row"
                      @click="openProjectFile(f)"
                      :title="f.path"
                    >
                      <AppIcon class="context-file-icon" :name="f.kind === 'image' ? 'image' : f.kind === 'markdown' ? 'doc' : 'file'" />
                      <span class="context-file-name">{{ f.path }}</span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </template>
      <template #actions>
        <span
          v-if="store.activeBackgroundAgents > 0"
          class="bg-agents-pill"
          :title="`${store.activeBackgroundAgents} background agent${store.activeBackgroundAgents === 1 ? '' : 's'} running`"
        >
          <span class="bg-agents-dot" aria-hidden="true"></span>
          {{ store.activeBackgroundAgents }} agent{{ store.activeBackgroundAgents === 1 ? '' : 's' }}
        </span>
        <div class="model-picker-wrap" ref="modelPickerRef">
          <button
            class="model-picker-btn touch-hit mobile-only"
            :title="`${routingProviderLabel(activeBucket, chat.provider)} · ${chipModelLabel}${chipThinkingLabel ? ' · ' + chipThinkingLabel : ''}`"
            @click.stop="toggleModelPicker"
            aria-label="Model"
          >
            <AppIcon name="model" :size="18" />
          </button>
          <button
            v-if="chat.provider"
            type="button"
            class="model-picker-summary desktop-only"
            :title="`${routingProviderLabel(activeBucket, chat.provider)} · ${chipModelLabel}${chipThinkingLabel ? ' · ' + chipThinkingLabel : ''}`"
            @click.stop="toggleModelPicker"
          >{{ routingProviderLabel(activeBucket, chat.provider) }} · {{ chipModelLabel }}<template v-if="chipThinkingLabel"> · {{ chipThinkingLabel }}</template></button>
          <ModelSelector
            v-if="showModelPicker"
            triggerless
            :model-value="canonicalTier(activeModelId)"
            :active-models="activeModelHighlights"
            :sections="chatModelSections"
            :filter-section="capabilityPickerSection"
            placeholder="Model"
            placement="bottom-end"
            @select="selectModel"
            @close="showModelPicker = false"
          >
            <template #footer>
              <div
                v-if="showThinkingLevels"
                class="thinking-levels"
              >
                <span class="thinking-levels__label">Thinking</span>
                <div class="thinking-levels__chips">
                  <button
                    v-for="level in ['', ...filteredThinkingLevels]"
                    :key="level"
                    type="button"
                    class="thinking-chip"
                    :class="{ 'thinking-chip--active': (chat.thinking_level || '') === level }"
                    :aria-pressed="(chat.thinking_level || '') === level"
                    @click="selectThinking(level)"
                  >
                    {{ level || 'auto' }}
                  </button>
                </div>
              </div>
            </template>
          </ModelSelector>
        </div>
        <button
          class="archive-btn touch-hit"
          @click="doArchive"
          :title="ARCHIVE_ACTION_LABEL"
          :aria-label="ARCHIVE_ACTION_LABEL"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/></svg>
        </button>
      </template>
    </PaneHeader>

    <!-- Context bar: what this chat is attached to — its automations. These
         were sibling banner blocks, each a v-for, so a chat with all of them
         opened with its first message below the fold. Collapsed it is one line
         of counted chips; expanded it is the same detail rows with the same
         actions. -->
    <div v-if="contextRelations.length" class="ctx-bar" :class="{ 'ctx-bar--open': contextExpanded }">
      <button
        type="button"
        class="ctx-summary"
        :aria-expanded="contextExpanded"
        @click="contextExpanded = !contextExpanded"
      >
        <span class="ctx-chevron" aria-hidden="true">{{ contextExpanded ? '▾' : '▸' }}</span>
        <span
          v-for="rel in contextRelations"
          :key="rel.key"
          class="ctx-chip"
        >
          <span v-if="rel.glyph" class="ctx-chip-glyph" :class="{ live: rel.live }" aria-hidden="true">{{ rel.glyph }}</span>
          {{ rel.label }}
        </span>
      </button>

      <div v-if="contextExpanded" class="ctx-detail">
        <div v-for="s in chatSchedules" :key="s.schedule_id" class="loop-banner-row">
          <!-- Interval entries keep the cycle glyph loops used; everything else
               keeps the clock, so the cadence reads before the text does. -->
          <span v-if="s.frequency === 'interval'" class="loop-banner-ico" aria-hidden="true">&#10227;</span>
          <AppIcon v-else class="loop-banner-ico" name="clock" :size="18" />
          <span class="loop-banner-text">
            <strong>{{ s.title || 'Automation' }}</strong>
            · {{ scheduleCadence(s) }}
            · {{ s.enabled ? 'enabled' : 'paused' }}<template v-if="s.last_status === 'busy'"> (waiting, chat busy)</template>
            <template v-if="s.enabled && scheduleCountdown(s)"> · next {{ scheduleCountdown(s) }}</template>
          </span>
          <button class="btn-small" @click="toggleScheduleEnabled(s)">{{ s.enabled ? 'Pause' : 'Resume' }}</button>
          <button class="btn-small" :disabled="scheduleRunningId === s.schedule_id" @click="runScheduleNow(s)">{{ scheduleRunningId === s.schedule_id ? 'Running…' : 'Run now' }}</button>
          <router-link :to="`/schedules/${s.schedule_id}`" class="btn-small loop-banner-manage">Manage</router-link>
        </div>
      </div>
    </div>

    <!-- Messages + comment sidebar -->
    <div class="chat-with-sidebar">
    <div class="messages" ref="messagesEl" :aria-busy="store.messageHistoryLoading" :style="{ overflowAnchor: isNearBottom ? 'none' : 'auto' }" @click="handleHighlightClick" @mouseover="onChatHighlightHover" @mouseout="onChatHighlightHoverOut">
      <div class="messages-content">
      <Transition name="history-loading">
        <!-- Placeholder for the transcript, in the transcript's own shape: a
             right-aligned user bubble, the left-aligned assistant bubbles with
             their accent edge, and a collapsed Activity row between them, all
             at the widths and radii the real rows use. The previous version
             was a single bordered card in the middle of the pane, so the
             reveal replaced one layout with a completely different one — the
             skeleton predicted nothing about what was coming. -->
        <div
          v-if="blockingHistoryLoad"
          class="history-skeleton-stack"
          role="status"
          aria-live="polite"
          aria-label="Loading conversation"
          aria-busy="true"
        >
          <div class="skel-msg skel-msg--assistant" aria-hidden="true">
            <span class="history-skeleton-line history-skeleton-line--long"></span>
            <span class="history-skeleton-line history-skeleton-line--medium"></span>
          </div>
          <div class="skel-msg skel-msg--user" aria-hidden="true">
            <span class="history-skeleton-line history-skeleton-line--wide"></span>
            <span class="history-skeleton-line history-skeleton-line--short"></span>
          </div>
          <div class="skel-trace" aria-hidden="true">
            <span class="skel-trace-chevron">&#9656;</span>
            <span class="history-skeleton-line history-skeleton-line--trace"></span>
          </div>
          <div class="skel-msg skel-msg--assistant" aria-hidden="true">
            <span class="history-skeleton-line history-skeleton-line--long"></span>
            <span class="history-skeleton-line history-skeleton-line--long"></span>
            <span class="history-skeleton-line history-skeleton-line--medium"></span>
            <span class="history-skeleton-line history-skeleton-line--short"></span>
          </div>
        </div>
      </Transition>
      <template v-if="!blockingHistoryLoad">
      <template v-for="(item, i) in renderItems" :key="item.key">
        <!-- Reasoning trace: intermediate assistant text + tool calls grouped -->
        <div v-if="item.kind === 'trace'" class="trace-block" :class="{ open: openTraces[i] }">
          <button
            type="button"
            class="trace-summary"
            :aria-expanded="Boolean(openTraces[i])"
            @click="toggleTrace(i)"
          >
            <span class="trace-chevron">{{ openTraces[i] ? '\u25BE' : '\u25B8' }}</span>
            <AppIcon class="trace-icon" name="activity" :size="14" />
            <span class="trace-label">Activity</span>
            <span class="trace-meta">
              <span
                v-for="part in traceSummaryMetaParts(item.steps, item.subs)"
                :key="part.key"
                :class="['trace-meta-part', `part-${part.key}`, { 'part-important': part.isImportant }]"
              >
                <span class="part-text-long">{{ part.text }}</span>
                <span class="part-text-short">{{ part.shortText || part.text }}</span>
              </span>
            </span>
            <span class="sr-only">, {{ openTraces[i] ? 'expanded' : 'collapsed' }}</span>
          </button>
          <div v-if="openTraces[i]" class="trace-body" @click="onTraceBodyClick(i, $event)">
            <template v-for="(step, j) in item.steps" :key="j">
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
                @click="openFileCard(step.file_path || step.content)"
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
                  @click.stop="toggleThinking"
                >
                  <span aria-hidden="true">{{ thinkingExpanded ? '\u25BE' : '\u25B8' }}</span>
                  <span>{{ thinkingExpanded ? 'Thinking' : 'Thinking (collapsed)' }}</span>
                </button>
                <div v-if="thinkingExpanded" class="trace-text trace-thinking">
                  <button
                    v-if="step.lazy && typeof step.i === 'number'"
                    type="button"
                    class="thinking-load"
                    @click.stop="expandLazyStep(step)"
                  >
                    Load full reasoning…
                  </button>
                  <div v-else v-html="renderMarkdown(step.content)"></div>
                </div>
              </div>
              <div v-else class="trace-text" v-html="renderMarkdown(step.content)"></div>
            </template>
            <SubagentPanel v-if="item.subs?.length" :subagents="item.subs" :chat-id="chat.chat_id" />
            <div v-if="item.outputs?.length" class="trace-files">
              <button
                v-for="(f, fi) in item.outputs"
                :key="fi"
                type="button"
                class="file-chip"
                @click.stop="openFileCard(f.file_path)"
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
        <!-- User message -->
        <div v-else-if="item.kind === 'user'" class="message-wrap user" :class="{ 'actions-tapped': tappedMessageKey === `user-${i}` }">
          <div class="message-row" @click="toggleMessageActions(`user-${i}`, $event)">
            <div class="message user" :data-msg-id="item.msg.timestamp ? `msg-${item.msg.timestamp}` : `msg-user-${i}`" :data-msg-index="i" data-msg-role="user">
              <div class="message-content">
                <div v-if="item.msg.images?.length" class="message-images">
                  <a
                    v-for="img in item.msg.images"
                    :key="img"
                    :href="img.startsWith('data:') ? img : `/api/images/${img}`"
                    target="_blank"
                    rel="noopener"
                    class="message-image-link"
                  >
                    <img :src="img.startsWith('data:') ? img : `/api/images/${img}`" :alt="img.startsWith('data:') ? 'image' : img" class="message-image" />
                  </a>
                </div>
                <div v-html="renderMarkdown(item.msg.content)"></div>
              </div>
              <div v-if="item.msg.timestamp || item.msg.unattended" class="message-meta">
                <!-- An automation's tick, not something the reader typed.
                     Without this the two are indistinguishable in the
                     transcript. -->
                <span
                  v-if="item.msg.unattended"
                  class="unattended-mark"
                  title="Sent automatically by an automation"
                >&#10227; auto</span>
                <span v-if="item.msg.timestamp">{{ formatTime(item.msg.timestamp) }}</span>
              </div>
            </div>
            <div v-if="item.msg.content?.trim()" class="message-actions">
              <button
                type="button"
                class="message-action-btn"
                :title="copiedMessageKey === `user-${i}` ? 'Copied' : 'Copy'"
                :aria-label="copiedMessageKey === `user-${i}` ? 'Copied' : 'Copy message'"
                @click="copyMessageText(item.msg.content, `user-${i}`)"
              >
                <svg v-if="copiedMessageKey === `user-${i}`" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
                <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              </button>
              <button
                type="button"
                class="message-action-btn"
                :class="{ 'message-action-btn--busy': speakLoadingKey === `user-${i}` }"
                :title="speakingMessageKey === `user-${i}` ? 'Stop' : 'Read aloud'"
                :aria-label="speakingMessageKey === `user-${i}` ? 'Stop reading' : 'Read message aloud'"
                :disabled="speakLoadingKey !== null && speakLoadingKey !== `user-${i}`"
                @click="speakMessage(item.msg.content, `user-${i}`)"
              >
                <svg v-if="speakingMessageKey === `user-${i}`" width="16" height="16" viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
                <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>
              </button>
            </div>
          </div>
          <p v-if="speakError?.key === `user-${i}`" class="speak-error">{{ speakError.message }}</p>
        </div>
        <!-- Final assistant message -->
        <div v-else-if="item.kind === 'assistant'" class="message-wrap assistant" :class="{ 'actions-tapped': tappedMessageKey === `assistant-${i}` }">
          <div class="message-row" @click="toggleMessageActions(`assistant-${i}`, $event)">
            <div class="message assistant" :class="{ error: item.msg.is_error }" :data-msg-id="item.msg.timestamp ? `msg-${item.msg.timestamp}` : `msg-asst-${i}`" :data-msg-index="i" data-msg-role="assistant">
              <div class="message-content" v-html="renderMarkdown(item.msg.content)"></div>
              <div v-if="item.msg.is_error" class="error-attribution" role="status">
                <span class="error-attribution-label">{{ classifyError(item.msg.content).label }}</span>
                <span>{{ classifyError(item.msg.content).copy }}</span>
              </div>
              <div v-if="item.outputs?.length" class="answer-outputs" role="group" aria-label="Outputs">
                <span class="answer-outputs-label">Outputs</span>
                <div class="answer-output-files">
                  <button
                    v-for="(f, fi) in item.outputs"
                    :key="fi"
                    type="button"
                    class="file-chip"
                    @click.stop="openFileCard(f.file_path)"
                    :title="f.file_path"
                  >
                    <AppIcon class="file-chip-icon" :name="fileCardIcon(f.file_path)" :size="14" />
                    <span class="file-chip-name">{{ fileCardBasename(f.file_path) }}</span>
                    <span v-if="f.action === 'created'" class="file-chip-action">new</span>
                    <span class="file-chip-open" aria-hidden="true">&#8599;</span>
                  </button>
                </div>
              </div>
              <!-- One footer per turn, on its last bubble. A turn can produce
                   several assistant bubbles, and the fields are spread across
                   them: the merged answer carries the model and the token
                   usage, while the completion time and duration are overlaid
                   onto the turn's last assistant row. Rendered per message that
                   read as the cost of the *first* bubble and left the reply the
                   user actually ends on unlabelled. `item.meta` is the whole
                   turn's footer, set only on the bubble that closes it. -->
              <div v-if="item.meta" class="message-meta">
                <span v-if="item.meta.timestamp">{{ formatTime(item.meta.timestamp) }}</span>
                <span v-if="item.meta.duration_ms"> &middot; {{ formatDuration(item.meta.duration_ms) }}</span>
                <span v-if="item.meta.effective_model"> &middot; {{ item.meta.effective_model }}</span>
                <span v-if="formatTokenUsage(item.meta.usage)" class="tokens-group">&nbsp;| <span v-html="formatTokenUsage(item.meta.usage)"></span></span>
              </div>
            </div>
            <div v-if="item.msg.content?.trim()" class="message-actions">
              <button
                type="button"
                class="message-action-btn"
                :title="copiedMessageKey === `assistant-${i}` ? 'Copied' : 'Copy'"
                :aria-label="copiedMessageKey === `assistant-${i}` ? 'Copied' : 'Copy message'"
                @click="copyMessageText(item.msg.content, `assistant-${i}`)"
              >
                <svg v-if="copiedMessageKey === `assistant-${i}`" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
                <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              </button>
              <button
                v-if="!item.msg.is_error"
                type="button"
                class="message-action-btn"
                :class="{ 'message-action-btn--busy': forkLoadingKey === `assistant-${i}` }"
                :title="forkLoadingKey === `assistant-${i}` ? 'Forking…' : 'Fork conversation from here'"
                aria-label="Fork conversation from here"
                :disabled="forkLoadingKey !== null"
                @click.stop="forkConversation(item.msg, `assistant-${i}`)"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <circle cx="6" cy="5" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="18" cy="18" r="2"/><path d="M6 7v4a6 6 0 0 0 6 6h4"/><path d="M8 5h4a6 6 0 0 1 6 6v5"/>
                </svg>
              </button>
              <button
                type="button"
                class="message-action-btn"
                :class="{ 'message-action-btn--busy': speakLoadingKey === `assistant-${i}` }"
                :title="speakingMessageKey === `assistant-${i}` ? 'Stop' : 'Read aloud'"
                :aria-label="speakingMessageKey === `assistant-${i}` ? 'Stop reading' : 'Read message aloud'"
                :disabled="speakLoadingKey !== null && speakLoadingKey !== `assistant-${i}`"
                @click="speakMessage(item.msg.content, `assistant-${i}`)"
              >
                <svg v-if="speakingMessageKey === `assistant-${i}`" width="16" height="16" viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
                <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>
              </button>
            </div>
          </div>
          <p v-if="speakError?.key === `assistant-${i}`" class="speak-error">{{ speakError.message }}</p>
          <div v-if="item.msg.is_error" class="error-actions">
            <button
              v-if="lastUserBefore(i)"
              class="retry-btn"
              @click="retryFromError(i)"
            >Retry</button>
            <button
              class="retry-btn fix-btn"
              @click="openFixChat(i)"
            >Fix this error</button>
          </div>
        </div>
        <!-- System message (errors, etc) -->
        <div v-else-if="item.kind === 'system'" class="message system" :data-msg-id="item.msg.timestamp ? `msg-${item.msg.timestamp}` : `msg-sys-${i}`" :data-msg-index="i" data-msg-role="system">
          <div class="message-content" v-html="renderMarkdown(item.msg.content)"></div>
          <div v-if="isErrorMsg(item.msg.content)" class="error-attribution" role="status">
            <span class="error-attribution-label">{{ classifyError(item.msg.content).label }}</span>
            <span>{{ classifyError(item.msg.content).copy }}</span>
          </div>
          <div v-if="isErrorMsg(item.msg.content)" class="error-actions">
            <button
              v-if="lastUserBefore(i)"
              class="retry-btn"
              @click="retryFromError(i)"
            >Retry</button>
            <button
              class="retry-btn fix-btn"
              @click="openFixChat(i)"
            >Fix this error</button>
          </div>
        </div>
      </template>
      </template>

      <!-- Ephemeral orientation aid shown after reopening a chat. Keep it
           after the rendered transcript so it reads as the latest message,
           while the tag makes it clear that it is generated context rather
           than a reply. -->
      <div v-if="reentrySummary" class="message-wrap assistant reentry-summary-wrap">
        <div class="message-row">
          <div class="message assistant reentry-summary-message" role="status" aria-label="Apple Intelligence summary">
            <div class="reentry-summary-header">
              <span class="reentry-summary-badge">Summary</span>
              <span class="reentry-summary-source">Apple Intelligence</span>
            </div>
            <div class="message-content" v-html="renderMarkdown(reentrySummary)"></div>
          </div>
        </div>
      </div>

      <div
        v-if="store.hostConnectionUnavailable"
        class="host-connection-card"
        role="alert"
      >
        <div class="host-connection-main">
          <span class="host-connection-spinner" aria-hidden="true"></span>
          <div>
            <div class="host-connection-title">Can’t reach the host</div>
            <div class="host-connection-meta">
              Ciaobot is trying to reconnect. You can keep waiting, or make this device the host.
            </div>
            <div v-if="hostHandoverError" class="host-connection-error">
              {{ hostHandoverError }}
            </div>
          </div>
        </div>
        <button
          type="button"
          class="btn-small host-connection-action"
          :disabled="becomingHost"
          @click="disconnectAndBecomeHost"
        >
          {{ becomingHost ? 'Becoming host…' : 'Disconnect and become host' }}
        </button>
      </div>

      <div v-if="chat.retry?.status === 'pending' && !store.isStreaming" class="retry-card">
        <div class="retry-card-main">
          <AppIcon class="retry-card-icon" name="clock" :size="18" />
          <div>
            <div class="retry-card-title">Retrying this turn every hour</div>
            <div class="retry-card-meta">
              <span v-if="chat.retry.next_at">Next try {{ formatRetryTime(chat.retry.next_at) }}</span>
              <span v-if="chat.retry.attempts"> · {{ chat.retry.attempts }} attempt{{ chat.retry.attempts === 1 ? '' : 's' }}</span>
            </div>
          </div>
        </div>
        <div class="retry-card-actions">
          <button class="btn-small" :disabled="store.isStreaming" @click="openHandoverPicker">Continue with...</button>
          <button class="btn-small" :disabled="store.isStreaming" @click="tryRetryNow">Try now</button>
          <button class="btn-small" @click="stopRetry">Stop trying</button>
        </div>
      </div>

      <!-- Live reasoning trace: shown from the moment streaming starts.
           All in-progress content (tool calls, intermediate text, and current
           streaming text) stays inside this block. The final answer bubble
           only appears after the result event. -->
      <div v-if="store.isStreaming" class="trace-block live" :class="{ open: liveTraceOpen }">
        <button
          type="button"
          class="trace-summary"
          :aria-expanded="liveTraceOpen"
          @click="toggleLiveTrace"
        >
          <span class="trace-chevron">{{ liveTraceOpen ? '\u25BE' : '\u25B8' }}</span>
          <span class="activity-spinner"></span>
          <span class="trace-label">{{ liveTraceLabel }}</span>
          <span v-if="liveTraceMetaParts.length" class="trace-meta">
            <span
              v-for="part in liveTraceMetaParts"
              :key="part.key"
              :class="['trace-meta-part', `part-${part.key}`, { 'part-important': part.isImportant }]"
            >
              <span class="part-text-long">{{ part.text }}</span>
              <span class="part-text-short">{{ part.shortText || part.text }}</span>
            </span>
          </span>
          <span class="sr-only">, {{ liveTraceOpen ? 'expanded' : 'collapsed' }}</span>
        </button>
        <div
          v-if="liveTraceOpen && (store.currentTimeline.length || store.currentStreamingText || store.currentStreamingThinking || liveSubagents.length)"
          class="trace-body"
          @click="onLiveTraceBodyClick"
        >
          <template v-for="(entry, j) in store.currentTimeline" :key="j">
            <div v-if="entry.kind === 'tool'" class="trace-tools">
              <div
                v-for="(line, k) in activityLines(entry.content)"
                :key="k"
                class="activity-line"
                :class="{ subagent: isSubagentLine(line) }"
                v-html="renderActivityLine(line)"
              ></div>
            </div>
            <button
              v-else-if="entry.kind === 'filecard'"
              type="button"
              class="file-card"
              @click="openFileCard(entry.file_path)"
              :title="entry.file_path"
            >
              <AppIcon class="file-card-icon" :name="fileCardIcon(entry.file_path)" :size="18" />
              <span class="file-card-main">
                <span class="file-card-name">{{ fileCardBasename(entry.file_path) }}</span>
                <span class="file-card-meta">
                  <span class="file-card-action">{{ entry.action }}</span>
                  <span v-if="fileCardDirname(entry.file_path)" class="file-card-dir"> · {{ fileCardDirname(entry.file_path) }}</span>
                </span>
              </span>
              <span class="file-card-chevron" aria-hidden="true">&#8599;</span>
            </button>
            <div v-else-if="entry.kind === 'thinking'" class="thinking-block">
              <button
                type="button"
                class="thinking-toggle"
                :aria-expanded="thinkingExpanded"
                @click.stop="toggleThinking"
              >
                <span aria-hidden="true">{{ thinkingExpanded ? '\u25BE' : '\u25B8' }}</span>
                <span>{{ thinkingExpanded ? 'Thinking' : 'Thinking (collapsed)' }}</span>
              </button>
              <div v-if="thinkingExpanded" class="trace-text trace-thinking" v-html="renderMarkdown(entry.content)"></div>
            </div>
            <div
              v-else-if="entry.kind === 'status'"
              class="trace-text trace-status"
              v-html="renderMarkdown(entry.content)"
            ></div>
            <div v-else class="trace-text" v-html="renderMarkdown(entry.content)"></div>
          </template>
          <div v-if="store.currentStreamingThinking" class="thinking-block">
            <button
              type="button"
              class="thinking-toggle"
              :aria-expanded="thinkingExpanded"
              @click.stop="toggleThinking"
            >
              <span aria-hidden="true">{{ thinkingExpanded ? '\u25BE' : '\u25B8' }}</span>
              <span>{{ thinkingExpanded ? 'Thinking' : 'Thinking (collapsed)' }}</span>
            </button>
            <div v-if="thinkingExpanded" class="trace-text trace-thinking trace-streaming" v-html="renderMarkdown(store.currentStreamingThinking)"></div>
          </div>
          <div v-if="store.currentStreamingText" class="trace-text trace-streaming" v-html="renderMarkdown(store.currentStreamingText)"></div>
          <!-- Subagents for the in-flight turn nest in the live trace -->
          <SubagentPanel v-if="liveSubagents.length" :subagents="liveSubagents" :chat-id="chat.chat_id" />
        </div>
      </div>

      <div ref="scrollAnchor"></div>

      <!-- Floating "Comment" pill is teleported to body so it isn't clipped by
           .messages (position: relative + overflow-y: auto). -->
      <Teleport to="body">
        <button
          v-if="selectionAnchor"
          class="chat-comment-trigger"
          :style="{ top: selectionAnchor.top + 'px', left: selectionAnchor.left + 'px' }"
          @mousedown.prevent
          @click="openCommentForSelection()"
          type="button"
          title="Comment on this selection"
        >
          <AppIcon class="chat-comment-trigger-icon" name="comment" />
          Comment
        </button>

      </Teleport>
      <CommentComposePopover
        ref="commentComposeDraftRef"
        :anchor="commentDraft && draftAnchor ? draftAnchor : null"
        v-model="composeText"
        :images="commentDraftImages"
        @cancel="cancelChatComment"
        @save="saveChatComment"
        @upload="handleDraftImageUpload"
        @remove-image="removeDraftImage"
      />
      <!-- Same popover, editing an existing comment. Anchored to the chip that
           opened it rather than to a selection, since the text it annotates may
           be scrolled out of view. -->
      <CommentComposePopover
        ref="commentComposeEditRef"
        :anchor="editingChatCommentId ? chipEditAnchor : null"
        v-model="editingChatCommentText"
        :images="editingChatCommentImages"
        @cancel="cancelEditChatComment"
        @save="editingChatCommentId && saveEditChatComment(editingChatCommentId)"
        @upload="editingChatCommentId && handleEditImageUpload($event, editingChatCommentId)"
        @remove-image="removeEditImage"
      />
      <!-- Read popover for a comment highlight. Owns its own state so hovering
           a highlight doesn't re-render the transcript; see the component. -->
      <ChatCommentPopover
        ref="commentPopover"
        :comments="store.pendingChatComments"
        :draft-id="DRAFT_COMMENT_ID"
        @edit="openEditFromChatPopover"
        @delete="deleteChatComment"
      />
      </div>
    </div>
      <!-- Scroll-to-bottom floats inside the scroll area so it tracks the
           composer height: .chat-with-sidebar ends at the top of the input
           bar, so bottom:12px stays 12px above the composer even when the
           textarea expands to 200px. Previously it was absolute to
           .chat-panel at bottom:72px and was overlapped by an expanding
           composer (see screenshot where the chevron sits mid-text). -->
      <button
        v-if="showScrollBtn"
        class="scroll-to-bottom-btn"
        @click="scrollToBottom"
        title="Scroll to bottom"
        aria-label="Scroll to bottom"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
      </button>
    </div>

    <!-- AskUserQuestion picker. The model paused mid-turn to ask the user
         a structured question; we render an interactive option list so the
         answer flows back as the next user message. The SDK's built-in CLI
         picker can't run headless, so this is the only path. -->
    <div v-if="questionCardVisible" class="question-card">
      <div class="question-card-header">
        <AppIcon class="question-card-icon" name="question" :size="18" />
        <span class="question-card-title">The model has a question</span>
        <button class="question-card-dismiss" @click="dismissQuestions" title="Dismiss">&times;</button>
      </div>
      <div
        v-for="(q, qi) in activeQuestions"
        :key="qi"
        class="question-block"
      >
        <div class="question-block-header">
          <span v-if="q.header" class="question-block-chip">{{ q.header }}</span>
          <span v-if="q.multiSelect" class="question-block-multi">multi-select</span>
        </div>
        <div v-if="q.question || !q.header" class="question-block-prompt">
          {{ questionPromptLabel(q, qi) }}
        </div>
        <div class="question-options">
          <button
            v-for="(opt, oi) in q.options"
            :key="opt.label"
            type="button"
            class="question-option"
            :class="{ selected: isQuestionOptionSelected(qi, opt.label) }"
            :aria-keyshortcuts="questionOptionShortcut(qi, oi) || undefined"
            @click="toggleQuestionOption(qi, opt.label, q.multiSelect)"
          >
            <span class="question-option-main">
              <!-- Keyboard hint, not part of the label: only rendered where the
                   digit actually works (first question, first nine options). -->
              <span
                v-if="questionOptionShortcut(qi, oi)"
                class="question-option-key"
                aria-hidden="true"
              >{{ questionOptionShortcut(qi, oi) }}</span>
              <span class="question-option-text">
                <span class="question-option-label">{{ opt.label }}</span>
                <span v-if="opt.description" class="question-option-desc">{{ opt.description }}</span>
              </span>
            </span>
          </button>
        </div>
        <input
          v-if="q.allowOther"
          :type="q.isSecret ? 'password' : 'text'"
          class="question-other"
          placeholder="Other (free text)"
          :value="questionAnswers[qi]?.other || ''"
          @input="ensureAnswer(qi).other = ($event.target as HTMLInputElement).value"
        />
      </div>
      <div class="question-card-actions">
        <button class="btn-sm" type="button" @click="dismissQuestions">Cancel</button>
        <button class="btn-sm primary" type="button" :disabled="!allQuestionsAnswered" @click="submitQuestionAnswers">Send answer</button>
      </div>
    </div>

    <!-- Image-capability question. The server paused before dispatch because
         the selected model can't see images. The card shows the full
         provider-filtered ModelSelector (vision-capable models only) with the
         current model visible but disabled. Picking one switches the chat and
         re-dispatches the turn with the image; Cancel (or the 30s timeout)
         closes the turn with a system bubble. -->
    <div v-if="activeCapabilityQuestions.length" class="question-card capability-card">
      <div class="question-card-header">
        <span class="question-card-icon">&#128444;</span>
        <span class="question-card-title">This model can't see images</span>
        <span class="capability-countdown">{{ capabilityRemaining(activeCapabilityQuestions[0]) }}s</span>
      </div>
      <div
        v-for="q in activeCapabilityQuestions"
        :key="q.request_id"
        class="question-block capability-picker-block"
      >
        <ModelSelector
          :model-value="q.current_model"
          :sections="capabilitySectionsFor(q)"
          :disabled="capabilityExpired(q)"
          :active-models="[q.current_model]"
          searchable
          placeholder="Pick a vision model..."
          @select="(val) => handleCapabilityPickerSelect(q, val)"
        />
      </div>
      <div class="question-card-actions">
        <button
          class="btn-sm"
          type="button"
          :disabled="capabilityExpired(activeCapabilityQuestions[0])"
          @click="cancelCapability(activeCapabilityQuestions[0])"
        >Cancel</button>
      </div>
    </div>

    <!-- Pending Auto-mode permission prompts. Shown above queued/input so
         the user can't miss them. Each prompt sticks until it's approved or
         denied; the server resolves still-open prompts on turn teardown.
         Only the first is expanded by default: a permission blocks a tool
         mid-turn, so it outranks a question, but several stacked cards used to
         push the transcript off screen. The rest are reachable via the dock
         strip below. -->
    <div v-if="pendingApprovals.length" class="permission-requests">
      <div
        v-for="p in visibleApprovals"
        :key="p.request_id"
        class="permission-card"
      >
        <div class="permission-header">
          <svg class="permission-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square" stroke-linejoin="miter"><path d="M12 3l7 3.5v5c0 4.5-3 7.75-7 9-4-1.25-7-4.5-7-9v-5L12 3z"/><path d="M12 8v5"/><path d="M12 16h.01"/></svg>
          <span class="permission-tool">{{ p.tool_name }}</span>
          <span v-if="permissionReason(p)" class="permission-message">{{ permissionReason(p) }}</span>
        </div>
        <!-- Flat argument objects (the common case: MCP tools, Skill calls)
             render as labelled rows, so a long `prompt` value reads as prose
             instead of a JSON wall that gets clipped by the scroll box and
             looks like the request failed to parse. Nested payloads and bare
             strings fall back to the raw text. -->
        <dl v-if="permissionArgs(p.tool_input)" class="permission-args">
          <template v-for="f in permissionArgs(p.tool_input)" :key="f.key">
            <dt>{{ f.key }}</dt>
            <dd>{{ f.value }}</dd>
          </template>
        </dl>
        <pre v-else-if="p.tool_input" class="permission-input">{{ formatToolInput(p.tool_input) }}</pre>
        <div class="permission-actions">
          <button
            class="btn-deny"
            :aria-keyshortcuts="permissionShortcut('deny') || undefined"
            @click="store.respondPermission(chat.chat_id, p.request_id, false, 'User denied')"
          ><span v-if="permissionShortcut('deny')" class="permission-key" aria-hidden="true">{{ permissionShortcut('deny') }}</span>Deny</button>
          <button
            class="btn-approve"
            :aria-keyshortcuts="permissionShortcut('approve') || undefined"
            @click="store.respondPermission(chat.chat_id, p.request_id, true)"
          ><span v-if="permissionShortcut('approve')" class="permission-key" aria-hidden="true">{{ permissionShortcut('approve') }}</span>Approve</button>
        </div>
      </div>
    </div>

    <!-- Queued messages (sent while a response was already streaming). -->
    <div v-if="store.currentQueued.length && (!dockPrimary || dockExpanded)" class="queued-messages">
      <div
        v-for="(q, i) in store.currentQueued"
        :key="q.id || i"
        class="queued-chip"
        title="Will be sent when current response finishes"
      >
        <span class="queued-label">Queued</span>
        <div class="queued-body">
          <div v-if="q.images?.length" class="queued-images">
            <img v-for="img in q.images" :key="img" :src="`/api/images/${img}`" :alt="img" class="queued-image-thumb" />
          </div>
          <template v-if="editingQueueId === q.id">
            <textarea
              v-model="editingQueueText"
              class="queued-edit-input"
              rows="2"
              @keydown.enter.prevent="saveEditQueue(chat.chat_id, q.id)"
              @keydown.esc="cancelEditQueue"
            />
          </template>
          <span v-else class="queued-text">{{ q.text }}</span>
        </div>
        <div class="queued-actions">
          <button
            class="queued-action"
            :disabled="i === 0"
            title="Move up"
            @click="store.reorderQueued(chat.chat_id, i, i - 1)"
          >▲</button>
          <button
            class="queued-action"
            :disabled="i === store.currentQueued.length - 1"
            title="Move down"
            @click="store.reorderQueued(chat.chat_id, i, i + 1)"
          >▼</button>
          <button
            v-if="editingQueueId !== q.id"
            class="queued-action"
            title="Edit"
            @click="startEditQueue(q)"
          >✎</button>
          <template v-else>
            <button class="queued-action" title="Save" @click="saveEditQueue(chat.chat_id, q.id)">✓</button>
            <button class="queued-action" title="Cancel" @click="cancelEditQueue">✕</button>
          </template>
          <button class="queued-remove" @click="store.removeQueued(chat.chat_id, i)" title="Remove">&times;</button>
        </div>
      </div>
    </div>

    <!-- Staged attachments. Images, chat comments and file comments share one
         lifecycle (staged here, sent with the next message, cleared on send),
         so they share one row above the input. A chip is a summary; clicking a
         chat-comment chip opens an edit popover anchored to it. -->
    <div
      v-if="store.pendingImages.length || store.pendingChatComments.length || store.pendingComments.length"
      class="pending-attachments"
    >
      <span v-for="(ref, i) in store.pendingImages" :key="`img-${ref}`" class="image-preview">
        <img :src="`/api/images/${ref}`" :alt="ref" class="image-preview-thumb" />
        <button class="image-ref-chip" @click="insertImageRef(i + 1)" title="Insert reference at cursor">[Image {{ i + 1 }}]</button>
        <button class="image-preview-remove" @click="removePendingImage(i)" title="Remove">&times;</button>
      </span>
      <span
        v-for="c in store.pendingChatComments"
        :key="`cc-${c.id}`"
        class="comment-chip"
        :class="{ 'is-editing': editingChatCommentId === c.id }"
      >
        <AppIcon class="comment-chip-icon" name="comment" :size="14" />
        <button
          type="button"
          class="comment-chip-body"
          @click.stop.prevent="openChatCommentChip(c.id, $event)"
          :title="`${c.selection}\n\n${c.comment}`"
        >
          <span class="comment-chip-quote">"{{ truncate(c.selection, 40) }}"</span>
          <span class="comment-chip-note">{{ truncate(c.comment, 40) }}</span>
        </button>
        <button class="comment-chip-remove" @click.stop.prevent="deleteChatComment(c.id)" title="Remove">&times;</button>
      </span>
      <span v-for="c in store.pendingComments" :key="`fc-${c.id}`" class="comment-chip">
        <AppIcon class="comment-chip-icon" name="doc" :size="14" />
        <button
          type="button"
          class="comment-chip-body"
          @click.stop.prevent="openFileCommentChip(c)"
          :title="`${c.path}\n\n${c.selection}\n\n${c.comment}`"
        >
          <span class="comment-chip-file">
            {{ fileCardBasename(c.path) }}
            <span v-if="formatCommentLocation(c)" class="comment-chip-line">· {{ formatCommentLocation(c) }}</span>
          </span>
          <span class="comment-chip-note">{{ truncate(c.comment, 40) }}</span>
        </button>
        <button class="comment-chip-remove" @click="store.removePendingComment(c.id)" title="Remove">&times;</button>
      </span>
    </div>

    <!-- Dock strip: one counted line for everything not expanded above, so the
         dock never grows past one card plus this. Replaces the standalone
         background-agents bar, which was a third rendering of a count the
         header pill already shows. Nothing here is unreachable — the strip is a
         disclosure. -->
    <!-- Rendered whenever there is anything to collapse OR anything already
         expanded, so the disclosure works in both directions. Gating purely on
         dockDeferred made the strip unmount itself on click: every deferred
         entry except background agents disappears once dockExpanded is true. -->
    <div v-if="dockStripVisible" class="dock-strip-wrap">
      <!-- aria-live sits on an inner span: an explicit role="status" on the
           button would override its implicit button role and drop
           aria-expanded, so assistive tech would stop treating it as a
           control. -->
      <button
        type="button"
        class="dock-strip"
        :aria-expanded="dockExpanded"
        @click="dockExpanded = !dockExpanded"
      >
        <span class="dock-chevron" aria-hidden="true">{{ dockExpanded ? '▾' : '▸' }}</span>
        <span class="dock-strip-items" role="status" aria-live="polite">
          <span
            v-for="item in dockDeferred"
            :key="item.key"
            class="dock-pill"
            :class="{ 'dock-pill--blocking': item.blocking }"
          >{{ item.label }}</span>
          <span v-if="dockExpanded && !dockDeferred.length" class="dock-pill">collapse</span>
        </span>
      </button>
    </div>

    <!-- Running-agent links, revealed by expanding the dock's "N agent(s)
         running" pill above. The pill itself sits inside a <button> (can't
         nest a link there), so the actual links live in this sibling block,
         same disclosure pattern as queued-messages/questions below. -->
    <div v-if="dockExpanded && dockAgentsPillShown && dockRunningAgents.length" class="dock-agent-links">
      <router-link
        v-for="sub in dockRunningAgents"
        :key="sub.agent_id"
        class="dock-agent-link"
        :to="subagentPath(chat.chat_id, sub.agent_id)"
      >{{ sub.description || shortAgentId(sub.agent_id) }}</router-link>
    </div>

    <!-- @-mention picker (textarea version: inserts plain backend-facing text) -->
    <div v-if="showMentionPicker" class="commands-picker mention-picker" role="listbox" aria-label="Mentions">
      <div
        v-for="(item, i) in filteredMentions"
        :key="`${item.kind}:${item.insertText}`"
        class="commands-picker-row mention-picker-row"
        :class="{ active: i === mentionHighlightIdx }"
        role="option"
        :aria-selected="i === mentionHighlightIdx"
        @mousedown.prevent="mentionPicker.select(item)"
        @mouseenter="mentionHighlightIdx = i"
      >
        <div class="commands-picker-head">
          <span class="mention-picker-kind">{{ item.kind }}</span>
          <span class="commands-picker-name" :title="`@${item.insertText}`">@{{ item.label }}</span>
        </div>
        <div class="commands-picker-desc">{{ item.description }}</div>
      </div>
    </div>

    <!-- Slash-command picker. Skills can be picked from any slash token;
         Ciao-owned commands remain start-of-message only. -->
    <div v-if="showCommandsPicker" class="commands-picker" role="listbox" aria-label="Slash commands">
      <div
        v-for="(cmd, i) in filteredCommands"
        :key="cmd.name"
        class="commands-picker-row"
        :class="{ active: i === commandHighlightIdx }"
        role="option"
        :aria-selected="i === commandHighlightIdx"
        @mousedown.prevent="applyCommand(cmd)"
        @mouseenter="commandHighlightIdx = i"
      >
        <div class="commands-picker-head">
          <span v-if="cmd.source === 'skill'" class="commands-picker-kind">skill</span>
          <span class="commands-picker-name">/{{ cmd.name }}</span>
          <span v-if="cmd.argument_hint" class="commands-picker-hint">{{ cmd.argument_hint }}</span>
        </div>
        <div v-if="cmd.description" class="commands-picker-desc">{{ cmd.description }}</div>
      </div>
    </div>

    <div class="input-bar" :class="{ disabled: chat.archived }">
      <template v-if="chat.archived">
        <div class="archived-notice">
          <div class="archived-notice-row">
            <span>This chat is archived.</span>
            <button class="btn-sm primary continue-chat-btn" @click="continueChat" :disabled="isContinuing">
              {{ isContinuing ? 'Continuing...' : 'Continue in new chat' }}
            </button>
          </div>
          <!-- What Ciaobot took from this conversation. Runs as a live line
               while the pipeline works, then settles and stays: the archived
               chat is the permanent record of what was learned from it, and
               nothing else in the app ever reported this. -->
          <p
            v-if="archiveTidying"
            class="archived-postprocess"
            aria-live="polite"
          >
            <span class="archived-postprocess-dot" aria-hidden="true" />
            {{ archiveTidyLabel }}…
          </p>
          <p
            v-else-if="archiveTidySummary"
            class="archived-postprocess"
            :class="{ failed: archiveTidyFailed }"
          >{{ archiveTidySummary }}</p>
        </div>
      </template>
      <template v-else>
        <textarea
          ref="inputEl"
          v-model="inputText"
          class="chat-input"
          :placeholder="inputPlaceholder"
          rows="1"
          @keydown="handleKeydown"
          @input="handleInput"
          @paste="handlePaste"
          @focus="handleInputFocus"
          @click="refreshComposerPickers"
        ></textarea>
        <div class="input-actions">
          <!-- Voice recording is allowed during streaming too: the user's
               transcript becomes a queued follow-up, same as typed text. -->
          <VoiceRecorder v-if="!transcribing" ref="voiceRecorderRef" @recorded="handleVoice" @error="handleVoiceError" />
          <span v-else class="voice-transcribing" title="Transcribing...">
            <span class="transcribe-spinner"></span>
          </span>
          <label class="image-btn" title="Upload images" aria-label="Upload images">
            <input type="file" accept="image/*" multiple hidden @change="handleFileSelect" />
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
          </label>
          <!-- While streaming: empty composer → stop; any draft → queue. -->
          <button
            class="send-btn"
            :class="{ 'is-stop': showStopAction }"
            :disabled="!showStopAction && !canSend"
            :title="primaryActionTitle"
            :aria-label="primaryActionLabel"
            @click="primaryAction"
          >
            <span v-if="showStopAction" class="stop-icon" aria-hidden="true">&#9632;</span>
            <span v-else class="send-glyph">{{ store.isStreaming ? '»' : '↵' }}</span>
          </button>
        </div>
      </template>
    </div>

  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useProjectStore } from '../stores/projects'
import { errorMessage, apiErrorMessage } from '../lib/errorMessage'
import {
  isPostprocessing,
  postprocessFailed,
  postprocessLabel,
  postprocessSummary,
} from '../lib/postprocessView'
import { useFileViewerStore } from '../stores/fileViewer'
import VoiceRecorder from './VoiceRecorder.vue'
// Subagent transcripts carry `turn_index` (the user turn that dispatched
// them, parsed server-side from the session JSONL), so each panel anchors
// under the turn that spawned its agents.
import SubagentPanel from './SubagentPanel.vue'
import { api } from '../lib/api'
import { askConfirm } from '../lib/confirm'
import { formatAttachedFilePath, nativeAbsoluteFilePath } from '../lib/chatAttachments'
import { readChatDraft, readSentPromptHistory, recordSentPrompt, writeChatDraft } from '../lib/chatDrafts'
import type { AgentAssetsResponse, CommandsResponse, RuntimeProvider, Schedule, ModelsResponse, ChatMessage, SlashCommand, SubagentTranscript } from '../lib/types'
import { useTaskStore } from '../stores/tasks'
import PaneHeader from './PaneHeader.vue'
import ModelSelector from './ModelSelector.vue'
import { colorForWorkspace } from '../lib/workspaceColors'
import { ARCHIVE_ACTION_LABEL, ARCHIVE_CONFIRM_MESSAGE } from '../lib/archiveCopy'
import AppIcon, { type AppIconName } from './AppIcon.vue'
import { linkifyText } from '../lib/filePaths'
import { sectionsFromModelsResponse } from '../lib/modelSections'
import { renderMarkdown as renderSafeMarkdown } from '../lib/safeMarkdown'
import { handleCodeCopyClick, writeClipboard } from '../lib/codeCopy'
import { classifyError } from '../lib/errorAttribution'
import { formatTime, formatDuration } from '../lib/time'
import { buildTurnParts, collectTraceOutputs, findFinalAnswerIndex, formatTokenUsage, traceSummaryMetaParts, type TraceOutput } from '../lib/chatActivity'
import { buildForkSnapshot } from '../lib/chatFork'
import { formatCommentLocation, type ChatCommentAnchor } from '../lib/commentContext'
import {
  cleanCommentSelection,
  commentTextMatches,
  commentTextOccurrenceIndex,
  escapeCssAttrValue,
  highlightCommentText,
} from '../lib/commentHighlight'
import { clampAnchorLeft, clampAnchorTop } from '../lib/popoverAnchor'
import {
  useMentionPicker,
  type MentionAgent,
  type MentionChat,
  type MentionFile,
  type MentionProject,
} from '../composables/useMentionPicker'
import { useThinkingPreference } from '../composables/useThinkingPreference'
import { useReentrySummaryPreference } from '../composables/useReentrySummaryPreference'
import { useTypeToComment } from '../composables/useTypeToComment'
import ChatCommentPopover from './ChatCommentPopover.vue'
import CommentComposePopover from './CommentComposePopover.vue'
import { subagentPath, shortAgentId } from '../lib/subagentIds'

/** The footer facts for one turn: when it landed, how long it took, which
 *  model answered and what it cost. Collected across the turn's assistant
 *  messages and rendered once, on the turn's last bubble. */
type TurnMeta = {
  timestamp?: string
  duration_ms?: number
  effective_model?: string
  usage?: Record<string, string>
}

type RenderItemInput =
  | { kind: 'user'; msg: ChatMessage; turnIndex?: number }
  | { kind: 'assistant'; msg: ChatMessage; outputs?: TraceOutput[]; turnIndex?: number; meta?: TurnMeta }
  | { kind: 'system'; msg: ChatMessage }
  | { kind: 'trace'; steps: ChatMessage[]; subs?: SubagentTranscript[]; outputs?: TraceOutput[]; turnIndex?: number }

type RenderItem = RenderItemInput & { key: string }

function renderItemKey(item: RenderItemInput): string {
  switch (item.kind) {
    case 'user':
    case 'assistant':
    case 'system': {
      const m = item.msg
      const ts = m.timestamp || ''
      const content = (m.content || '').slice(0, 60)
      const tool = m.tool_name || ''
      const phase = m.phase || ''
      const file = m.file_path || ''
      const images = (m.images || []).length
      const error = m.is_error ? '1' : '0'
      return `${item.kind}:${ts}:${tool}:${phase}:${file}:${content}:${images}:${error}`
    }
    case 'trace': {
      const firstTs = item.steps[0]?.timestamp || ''
      const lastTs = item.steps[item.steps.length - 1]?.timestamp || ''
      const stepSig = item.steps
        .map(s => `${s.role}:${s.tool_name || ''}:${s.phase || ''}:${(s.content || '').slice(0, 40)}:${s.file_path || ''}:${s.timestamp || ''}`)
        .join('|')
      return `trace:${item.turnIndex ?? 'x'}:${item.steps.length}:${firstTs}:${lastTs}:${stepSig.slice(0, 200)}`
    }
  }
}

function withKey<T extends RenderItemInput>(item: T): T & { key: string } {
  return { ...item, key: renderItemKey(item) }
}

/** Defensive dedup: if two RenderItems would get the same key, disambiguate by
 *  appending a running counter. This should not happen for well-formed history,
 *  but it protects against duplicate server entries or hash collisions. */
function dedupeRenderItemKeys(items: RenderItem[]): RenderItem[] {
  const seen = new Map<string, number>()
  return items.map((item) => {
    let key = item.key
    let count = seen.get(key) || 0
    if (count > 0) {
      key = `${item.key}:dup:${count}`
    }
    seen.set(item.key, count + 1)
    return { ...item, key }
  })
}


const emit = defineEmits<{ close: [], 'open-sidebar': [] }>()

const store = useProjectStore()
const fileViewer = useFileViewerStore()
const { thinkingExpanded, toggleThinking } = useThinkingPreference()
const { reentrySummaryEnabled } = useReentrySummaryPreference()
const draftChatId = store.activeChatId
const inputText = ref(readChatDraft(draftChatId))
const inputRevision = ref(0)
const inputEl = ref<HTMLTextAreaElement>()
const promptHistoryIndex = ref(-1)
const promptHistoryDraft = ref('')
let settingPromptHistoryText = false
const isContinuing = ref(false)
const becomingHost = ref(false)
const hostHandoverError = ref('')

// ChatLayout keys this panel by chat id, so each instance owns one draft.
// Persist synchronously to avoid losing the last keystroke when switching
// chats immediately after typing.
watch(inputText, (text) => {
  inputRevision.value += 1
  if (!settingPromptHistoryText) {
    promptHistoryIndex.value = -1
    promptHistoryDraft.value = ''
    writeChatDraft(draftChatId, text, undefined, {
      projectId: chat.value?.project_id,
      workspace: store.activeWorkspace,
    })
  }
}, { flush: 'sync' })

function promptHistory(): string[] {
  return readSentPromptHistory(draftChatId)
}

function setPromptHistoryText(text: string): void {
  settingPromptHistoryText = true
  inputText.value = text
  settingPromptHistoryText = false
  nextTick(() => autoResize())
}

function handlePromptHistoryKey(e: KeyboardEvent): boolean {
  if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return false
  const history = promptHistory()
  if (!history.length) return false

  if (e.key === 'ArrowUp') {
    if (promptHistoryIndex.value < 0 && inputText.value.trim() !== '') return false
    e.preventDefault()
    if (promptHistoryIndex.value < 0) promptHistoryDraft.value = inputText.value
    promptHistoryIndex.value = promptHistoryIndex.value < 0
      ? history.length - 1
      : Math.max(0, promptHistoryIndex.value - 1)
    setPromptHistoryText(history[promptHistoryIndex.value])
    return true
  }

  if (promptHistoryIndex.value < 0) return false
  e.preventDefault()
  if (promptHistoryIndex.value >= history.length - 1) {
    promptHistoryIndex.value = -1
    setPromptHistoryText(promptHistoryDraft.value)
    promptHistoryDraft.value = ''
  } else {
    promptHistoryIndex.value += 1
    setPromptHistoryText(history[promptHistoryIndex.value])
  }
  return true
}

async function disconnectAndBecomeHost() {
  if (becomingHost.value) return
  const confirmed = await askConfirm(
    'Disconnect from the unreachable host and make this device the host? Changes that exist only on the other host may not be synced.',
    {
      title: 'Become host on this device?',
      confirmLabel: 'Disconnect and become host',
    },
  )
  if (!confirmed) return

  becomingHost.value = true
  hostHandoverError.value = ''
  try {
    const result = await api.post<{ ok: boolean }>('/api/node/handover', { force: true })
    if (!result.ok) throw new Error('Could not make this device the host')
    window.location.assign('/')
  } catch (e) {
    hostHandoverError.value = apiErrorMessage(e, 'Could not make this device the host')
    becomingHost.value = false
  }
}

// Ticks once a second while streaming so the live elapsed-time label in the
// "Working..." trace meta advances.
const nowTs = ref(Date.now())
let clockTimer: ReturnType<typeof setInterval> | null = null
watch(() => store.isStreaming, (streaming) => {
  if (streaming && !clockTimer) {
    nowTs.value = Date.now()
    clockTimer = setInterval(() => { nowTs.value = Date.now() }, 1000)
  } else if (!streaming && clockTimer) {
    clearInterval(clockTimer)
    clockTimer = null
  }
}, { immediate: true })

const canSend = computed(() =>
  !!(inputText.value.trim()
    || store.pendingImages.length
    || store.pendingComments.length
    || store.pendingChatComments.length),
)
// Empty composer while a turn is in flight → stop; otherwise the same
// button queues/sends the draft.
const showStopAction = computed(() => store.isStreaming && !canSend.value)
const primaryActionTitle = computed(() => {
  if (showStopAction.value) return 'Stop'
  if (store.isStreaming) return 'Queue message (sends when current turn finishes)'
  return 'Send'
})
const primaryActionLabel = computed(() => {
  if (showStopAction.value) return 'Stop generation'
  if (store.isStreaming) return 'Queue message'
  return 'Send message'
})
function primaryAction() {
  if (showStopAction.value) {
    store.stopChat(chat.value.chat_id)
    return
  }
  send()
}

// Slash-command picker: populated once on mount from /api/commands.
const slashCommands = ref<SlashCommand[]>([])
const commandHighlightIdx = ref(0)

interface SlashCommandTrigger {
  start: number
  end: number
  query: string
}

/** Find a slash token immediately before the textarea caret. */
function findSlashCommandTrigger(text: string, cursor: number): SlashCommandTrigger | null {
  const end = Math.max(0, Math.min(cursor, text.length))
  const beforeCaret = text.slice(0, end)
  const match = beforeCaret.match(/(?:^|\s)\/([^\s/]*)$/)
  if (!match) return null

  const start = end - match[0].length + match[0].lastIndexOf('/')
  return { start, end, query: match[1] || '' }
}

const slashCommandTrigger = ref<SlashCommandTrigger | null>(null)

function dismissSlashCommandPicker(): void {
  slashCommandTrigger.value = null
  commandHighlightIdx.value = 0
}

function refreshSlashCommandPicker(): void {
  const el = inputEl.value
  if (!el || el.selectionStart !== el.selectionEnd) {
    dismissSlashCommandPicker()
    return
  }
  slashCommandTrigger.value = findSlashCommandTrigger(inputText.value, el.selectionStart)
  commandHighlightIdx.value = 0
}

async function loadSlashCommands(): Promise<void> {
  try {
    const provider = encodeURIComponent(chat.value.provider || '')
    const workspace = encodeURIComponent(project.value?.workspace || store.activeWorkspace || '')
    const response = await api.get<CommandsResponse>(`/api/commands?provider=${provider}&workspace=${workspace}`)
    slashCommands.value = [
      ...(response.commands || []),
      ...(response.skills || []),
    ]
  } catch {
    // Asset discovery is offline: leave the picker empty rather than stale.
    slashCommands.value = []
  }
}

const filteredCommands = computed<SlashCommand[]>(() => {
  const active = slashCommandTrigger.value
  if (!active) return []
  const needle = active.query.toLowerCase()
  return slashCommands.value.filter(command =>
    command.name.toLowerCase().startsWith(needle),
  )
})

const showCommandsPicker = computed(() => filteredCommands.value.length > 0)

watch(filteredCommands, (list) => {
  if (commandHighlightIdx.value >= list.length) commandHighlightIdx.value = 0
})

function applyCommand(cmd: SlashCommand) {
  const active = slashCommandTrigger.value
  if (!active) return

  const before = inputText.value.slice(0, active.start)
  const after = inputText.value.slice(active.end)
  const token = `/${cmd.name}`
  // Keep the existing separator when this replaces a token in the middle of
  // a draft; otherwise leave room for command arguments as before.
  const suffix = cmd.argument_hint && !(after && /^\s/.test(after)) ? ' ' : ''
  inputText.value = before + token + suffix + after
  const cursor = before.length + token.length + suffix.length
  dismissSlashCommandPicker()
  nextTick(() => {
    const input = inputEl.value
    if (!input) return
    input.setSelectionRange(cursor, cursor)
    input.focus()
    autoResize()
  })
}

watch(inputText, () => {
  if (!slashCommandTrigger.value) return
  const el = inputEl.value
  const current = el && el.selectionStart === el.selectionEnd
    ? findSlashCommandTrigger(inputText.value, el.selectionStart)
    : null
  if (current) slashCommandTrigger.value = current
  else dismissSlashCommandPicker()
})
const messagesEl = ref<HTMLElement>()
const scrollAnchor = ref<HTMLElement>()
const editingTitle = ref(false)
const titleValue = ref('')
const dragOver = ref(false)
const chat = computed(() => store.activeChat!)
const reentrySummary = computed(() => {
  if (!reentrySummaryEnabled.value) return ''
  return store.reentrySummaries[chat.value.chat_id] || ''
})

// Post-archive pipeline, reported in the archived-chat footer. Reads through the
// chat record rather than a transient flag so the settled summary is still there
// when this chat is reopened weeks later.
const archivePostprocess = computed(() => store.chatPostprocess(chat.value.chat_id))
const archiveTidying = computed(() => isPostprocessing(archivePostprocess.value))
const archiveTidyLabel = computed(() => postprocessLabel(archivePostprocess.value))
const archiveTidySummary = computed(() => postprocessSummary(archivePostprocess.value))
const archiveTidyFailed = computed(() => postprocessFailed(archivePostprocess.value))
watch(() => chat.value.provider, () => {
  void loadSlashCommands()
})

// Inline editing state for queued messages. Keyed by queue entry id.
const editingQueueId = ref<string | null>(null)
const editingQueueText = ref('')
// The edit UI only edits text, so hold the entry's images and re-send them on
// save — otherwise the backend clears attachments it resolves from an empty list.
const editingQueueImages = ref<string[] | undefined>(undefined)

function startEditQueue(entry: { id: string; text: string; images?: string[] }) {
  editingQueueId.value = entry.id
  editingQueueText.value = entry.text
  editingQueueImages.value = entry.images ? [...entry.images] : undefined
}

function cancelEditQueue() {
  editingQueueId.value = null
  editingQueueText.value = ''
  editingQueueImages.value = undefined
}

function saveEditQueue(chatId: string, entryId: string) {
  const text = editingQueueText.value.trim()
  if (!text) return
  store.editQueued(chatId, entryId, text, editingQueueImages.value)
  cancelEditQueue()
}

const taskStore = useTaskStore()
// Automations linked to this chat: either the chat carries a schedule_id
// backlink (project schedules, stamped at creation) or the automation pins
// this chat via web_chat_id (fixed-chat schedules, and every interval entry
// that replaced a loop). Durable across runs because each run stamps the
// backlink on the chat.
const chatSchedules = computed(() => {
  const cid = chat.value?.chat_id
  const sid = chat.value?.schedule_id
  return taskStore.schedules.filter(s =>
    (sid && s.schedule_id === sid) || (cid && s.web_chat_id === cid),
  )
})

// ── Context bar ─────────────────────────────────────────────────────
// One counted chip per relation, so the v-for banner blocks can never again
// push the transcript below the fold. Detail rows live behind the disclosure.
const contextExpanded = ref(false)

interface ContextRelation {
  key: string
  label: string
  glyph?: string
  live?: boolean
}

const contextRelations = computed<ContextRelation[]>(() => {
  const rels: ContextRelation[] = []
  // Interval entries get their own chip with the cycle glyph: "this chat
  // re-runs itself" is a different fact from "something fires here at 09:00",
  // and collapsing them into one count hid it.
  const intervals = chatSchedules.value.filter(s => s.frequency === 'interval')
  const timed = chatSchedules.value.filter(s => s.frequency !== 'interval')
  if (intervals.length) {
    const label = intervals.length === 1
      ? `every ${intervals[0].interval_minutes}m`
      : `${intervals.length} interval runs`
    rels.push({
      key: 'intervals',
      label,
      glyph: '↻',
      live: intervals.some(s => s.enabled),
    })
  }
  if (timed.length) {
    const label = timed.length === 1
      ? `scheduled ${scheduleCadence(timed[0])}`
      : `${timed.length} schedules`
    rels.push({ key: 'schedules', label })
  }
  return rels
})

// ── Action dock ─────────────────────────────────────────────────────
// Six independent v-if blocks used to stack between the transcript and the
// composer with nothing distinguishing blocking items from status. Now: exactly
// one blocking item expanded by precedence, everything else on one counted
// strip that expands.
//
// Precedence: a permission blocks a tool mid-turn, a question blocks the turn's
// end, so the permission is the tighter deadline and wins.
//
// Staged attachments and the slash-command picker deliberately stay outside the
// dock: both are immediate feedback for something the user just did, and hiding
// them behind a disclosure would make the composer feel unresponsive.
const dockExpanded = ref(false)

const dockPrimary = computed<'permission' | 'question' | null>(() => {
  if (pendingApprovals.value.length) return 'permission'
  if (activeQuestions.value.length) return 'question'
  return null
})

const visibleApprovals = computed(() =>
  dockExpanded.value ? pendingApprovals.value : pendingApprovals.value.slice(0, 1),
)

interface DockItem {
  key: string
  label: string
  blocking?: boolean
}

// Anything to collapse, or anything already expanded that needs a way back.
const dockStripVisible = computed(() =>
  dockDeferred.value.length > 0
  || (dockExpanded.value && (pendingApprovals.value.length > 1
    || activeQuestions.value.length > 0
    || store.currentQueued.length > 0)),
)

// Whether the strip carries the "N agents running" pill. The links block below
// is that pill's disclosure, so both read the same condition — otherwise an
// archived chat with lingering rows renders links no pill ever announced.
const dockAgentsPillShown = computed(
  () => store.activeBackgroundAgents > 0 && !chat.value?.archived,
)

const dockRunsPillShown = computed(
  () => store.activeBackgroundRuns > 0 && !chat.value?.archived,
)

const dockDeferred = computed<DockItem[]>(() => {
  const items: DockItem[] = []
  const extraApprovals = pendingApprovals.value.length - 1
  if (!dockExpanded.value && extraApprovals > 0) {
    items.push({ key: 'permissions', label: `${extraApprovals} more permission${extraApprovals === 1 ? '' : 's'}`, blocking: true })
  }
  if (!dockExpanded.value && dockPrimary.value === 'permission' && activeQuestions.value.length) {
    items.push({ key: 'question', label: 'a question waiting', blocking: true })
  }
  if (!dockExpanded.value && dockPrimary.value && store.currentQueued.length) {
    const n = store.currentQueued.length
    items.push({ key: 'queued', label: `${n} queued` })
  }
  if (dockAgentsPillShown.value) {
    const n = store.activeBackgroundAgents
    items.push({ key: 'agents', label: `${n} agent${n === 1 ? '' : 's'} running` })
  }
  // Tracked `background_run_start` commands. A separate pill, not folded into
  // the agents one: these have no transcript to open, so the count is all
  // there is to show, and the wording has to stay honest about that. Shown
  // even while the chat is idle — that quiet gap is exactly when the user
  // has no other sign the command is still going.
  if (dockRunsPillShown.value) {
    const n = store.activeBackgroundRuns
    items.push({ key: 'runs', label: `${n} background run${n === 1 ? '' : 's'}` })
  }
  return items
})

// Backs the "N agent(s) running" dock pill's expanded state: the pill itself
// only shows a count (it lives inside the strip's toggle button, which can't
// nest a link), so expanding it reveals these as a separate block of links.
const dockRunningAgents = computed(() =>
  chat.value ? store.runningSubagentsFor(chat.value.chat_id) : [],
)

onMounted(() => {
  taskStore.fetchSchedules().catch(() => {})
})

// Lightweight 30-second tick powering the "next in Xm" countdown in the
// automation banner. Only runs while this chat has a live automation.
const loopNow = ref(Date.now())
let loopTick: ReturnType<typeof setInterval> | null = null
watch(chatSchedules, (scheds) => {
  const hasScheduled = scheds.some(s => s.enabled && s.next_run)
  if (hasScheduled && !loopTick) {
    loopNow.value = Date.now()
    loopTick = setInterval(() => { loopNow.value = Date.now() }, 30_000)
  } else if (!hasScheduled && loopTick) {
    clearInterval(loopTick)
    loopTick = null
  }
}, { immediate: true })
onBeforeUnmount(() => { if (loopTick) clearInterval(loopTick) })

// ── Automation banner helpers ──
const scheduleRunningId = ref<string | null>(null)
function scheduleCadence(s: Schedule): string {
  const time = s.daily_time_utc ? s.daily_time_utc.slice(0, 5) : ''
  switch (s.frequency) {
    case 'daily': return time ? `daily ${time}` : 'daily'
    case 'weekly': {
      const days = s.days_of_week && s.days_of_week.length
        ? s.days_of_week.map(d => d.charAt(0).toUpperCase() + d.slice(1)).join('/')
        : ''
      return days ? `weekly ${days}${time ? ' ' + time : ''}` : 'weekly'
    }
    case 'monthly': return s.day_of_month ? `monthly on day ${s.day_of_month}` : 'monthly'
    case 'once': return s.run_at_date ? `once ${s.run_at_date}` : 'once'
    case 'interval': return `every ${s.interval_minutes}m`
    case 'manual': return 'manual'
    default: return s.frequency
  }
}
function scheduleCountdown(s: Schedule): string {
  if (!s.next_run) return ''
  const diffMs = new Date(s.next_run).getTime() - loopNow.value
  if (diffMs <= 0) return 'soon'
  const mins = Math.ceil(diffMs / 60_000)
  if (mins < 60) return `in ${mins}m`
  const hrs = Math.floor(mins / 60)
  const rm = mins % 60
  return rm ? `in ${hrs}h ${rm}m` : `in ${hrs}h`
}
async function runScheduleNow(s: Schedule) {
  scheduleRunningId.value = s.schedule_id
  try {
    await taskStore.runScheduleNow(s.schedule_id)
  } catch {
    // An interval run into a chat that is already streaming is refused rather
    // than queued. Nothing to surface here beyond clearing the button.
  } finally {
    scheduleRunningId.value = null
  }
}

async function toggleScheduleEnabled(s: Schedule) {
  await taskStore.updateSchedule(s.schedule_id, { enabled: !s.enabled })
}
const project = computed(() => store.activeProject)

// Workspace crumb: name, accent and the 1-9 shortcut that selects it. The index
// comes from workspaceOptions so it always matches the sidebar pill and the home
// lane badge for the same workspace.
const workspaceCrumb = computed(() => {
  const name = project.value?.workspace
  if (!name) return null
  const index = store.workspaceOptions.findIndex(w => w.name === name)
  if (index < 0) return null
  return {
    name: name.split(/[-_\s]+/).filter(Boolean).join(' '),
    color: colorForWorkspace(store.workspaceOptions[index]),
    key: index < 9 ? String(index + 1) : '',
  }
})
// 'General' is the implicit project every workspace has, so naming it in the
// breadcrumb would say nothing.
const projectCrumb = computed(() => {
  const name = project.value?.name
  return name && name !== 'General' ? name : null
})
// Whether there is a scope line at all - it decides both the wrapper and the
// separator that joins the scope to the chat title.
const hasScopeCrumb = computed(() => !!workspaceCrumb.value || !!projectCrumb.value)
const models = ref<string[]>(['haiku', 'sonnet', 'opus', 'fable'])
const providerModels = ref<Record<string, string[]>>({})
const providerDefaults = ref<Record<string, string>>({})
const modelsResponse = ref<ModelsResponse | null>(null)

const thinkingLevels = ref<Record<string, string[]>>({})

const openTraces = ref<Record<number, boolean>>({})
const liveTraceOpen = ref(false)
const copiedMessageKey = ref<string | null>(null)
const forkLoadingKey = ref<string | null>(null)
// On touch devices there is no hover, so a tap on the bubble reveals the
// per-message action icons. Holds the key of the message whose actions are open.
const tappedMessageKey = ref<string | null>(null)

// Touch: tap a message to toggle its action icons. Ignored on hover-capable
// devices (they use hover) and when the tap targets a link/button or a text
// selection is in progress.
function toggleMessageActions(key: string, e: MouseEvent): void {
  if (!window.matchMedia('(hover: none)').matches) return
  const target = e.target as HTMLElement | null
  if (target?.closest('a, button, input, textarea')) return
  if (window.getSelection()?.toString()) return
  tappedMessageKey.value = tappedMessageKey.value === key ? null : key
}
const transcribing = ref(false)
const voiceRecorderRef = ref<InstanceType<typeof VoiceRecorder> | null>(null)
const commentComposeDraftRef = ref<InstanceType<typeof CommentComposePopover> | null>(null)
const commentComposeEditRef = ref<InstanceType<typeof CommentComposePopover> | null>(null)
const isNearBottom = ref(true)
let messagesResizeObserver: ResizeObserver | null = null
const showScrollBtn = computed(() => Boolean(messagesEl.value && store.activeMessages.length > 0 && !isNearBottom.value))
const showModelPicker = ref(false)
const modelPickerRef = ref<HTMLElement>()
const showContext = ref(false)
const contextDraft = ref('')
const contextSaving = ref(false)
const contextStatus = ref<'' | 'saved' | 'error'>('')
const breadcrumbRef = ref<HTMLElement>()

watch(
  () => [project.value?.project_id, project.value?.context, showContext.value] as const,
  ([_id, ctx, open]) => {
    if (open) contextDraft.value = ctx || ''
  },
  { immediate: true }
)

const contextDirty = computed(() => (project.value?.context || '') !== contextDraft.value)
const contextStatusLabel = computed(() => {
  if (contextStatus.value === 'saved') return 'Saved'
  if (contextStatus.value === 'error') return 'Error'
  return ''
})

async function saveContext() {
  if (!project.value || !contextDirty.value) return
  contextSaving.value = true
  contextStatus.value = ''
  try {
    await store.updateProject(project.value.project_id, { context: contextDraft.value })
    contextStatus.value = 'saved'
    setTimeout(() => { if (contextStatus.value === 'saved') contextStatus.value = '' }, 2000)
  } catch {
    contextStatus.value = 'error'
  } finally {
    contextSaving.value = false
  }
}

function toggleContext() {
  showContext.value = !showContext.value
}

// Close popup when clicking outside
function onDocumentClick(e: MouseEvent) {
  if (!showContext.value) return
  const target = e.target as HTMLElement
  if (breadcrumbRef.value && !breadcrumbRef.value.contains(target)) {
    showContext.value = false
  }
}
watch(showContext, (open) => {
  if (open) {
    window.addEventListener('click', onDocumentClick, { once: true })
  }
})

interface ContextProjectFile {
  path: string
  vault_path: string
  kind: 'markdown' | 'image' | 'text' | 'binary'
  size: number
  mtime: string
}
const projectFiles = ref<ContextProjectFile[]>([])
const projectFilesLoading = ref(false)
const projectFilesError = ref('')
const showProjectFiles = computed(() => Boolean(project.value?.vault_folder))
const mentionAgents = ref<MentionAgent[]>([])
const mentionChats = computed<MentionChat[]>(() => {
  const activeProjects = new Map(store.projects.map(item => [item.project_id, item]))
  return store.chats
    .filter(chatItem => !chatItem.archived && chatItem.local !== false && activeProjects.has(chatItem.project_id))
    .map(chatItem => ({
      chat_id: chatItem.chat_id,
      title: chatItem.title,
      project_id: chatItem.project_id,
      project_name: activeProjects.get(chatItem.project_id)?.name,
      workspace: activeProjects.get(chatItem.project_id)?.workspace,
      archived: chatItem.archived,
      local: chatItem.local,
    }))
})
const mentionProjects = computed<MentionProject[]>(() => store.projects.map(projectItem => ({
  project_id: projectItem.project_id,
  name: projectItem.name,
  workspace: projectItem.workspace,
})))
const mentionFiles = computed<MentionFile[]>(() => projectFiles.value.map(file => ({
  path: file.path,
  vault_path: file.vault_path,
})))
const mentionPicker = useMentionPicker({
  draft: inputText,
  input: inputEl,
  files: mentionFiles,
  agents: mentionAgents,
  chats: mentionChats,
  projects: mentionProjects,
})
const filteredMentions = mentionPicker.filteredItems
const mentionHighlightIdx = mentionPicker.highlightIndex
const showMentionPicker = mentionPicker.showPicker

function refreshComposerPickers(): void {
  mentionPicker.refresh()
  refreshSlashCommandPicker()
}

async function loadProjectFiles() {
  if (!project.value || !project.value.vault_folder) {
    projectFiles.value = []
    projectFilesError.value = ''
    return
  }
  projectFilesLoading.value = true
  projectFilesError.value = ''
  try {
    const resp = await fetch(`/api/projects/${project.value.project_id}/files`, {
      credentials: 'same-origin',
    })
    if (resp.ok) {
      projectFiles.value = await resp.json()
    } else {
      projectFiles.value = []
      projectFilesError.value = `Couldn't load files (HTTP ${resp.status}).`
    }
  } catch (e) {
    projectFiles.value = []
    projectFilesError.value = e instanceof Error ? e.message : String(e)
  } finally {
    projectFilesLoading.value = false
  }
}

function openProjectFile(f: ContextProjectFile): void {
  const isDoc = f.kind === 'markdown' || f.kind === 'text' || /\.(pdf|pptx)$/i.test(f.vault_path)
  if (f.kind === 'image') {
    fileViewer.openImage(f.vault_path)
  } else if (isDoc) {
    fileViewer.open(f.vault_path)
  } else {
    const url = `/api/workspace-binary?path=${encodeURIComponent(f.vault_path)}`
    window.open(url, '_blank')
  }
}

watch(
  () => [showContext.value, project.value?.project_id, project.value?.vault_folder] as const,
  ([open]) => { if (open || project.value?.vault_folder) loadProjectFiles() },
  { immediate: true }
)

async function loadMentionAgents(): Promise<void> {
  try {
    const response = await api.get<AgentAssetsResponse>('/api/agent-assets')
    mentionAgents.value = Array.isArray(response.subagents) ? response.subagents : []
  } catch {
    // Mentions are an enhancement; an unavailable asset catalog leaves files usable.
    mentionAgents.value = []
  }
}

// Deduped list of files the agent has written/edited in this chat. Most
// recent occurrence wins for action label; count shows how many times the
// same path was touched.
type TouchedFile = { file_path: string; action: string; count: number; index: number }
const touchedFiles = computed<TouchedFile[]>(() => {
  const byPath = new Map<string, TouchedFile>()
  const msgs = store.activeMessages
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i]
    if (m.tool_name !== '_filecard') continue
    const fp = m.file_path || m.content
    if (!fp) continue
    const existing = byPath.get(fp)
    if (existing) {
      existing.action = m.action || existing.action
      existing.count += 1
      existing.index = i
    } else {
      byPath.set(fp, { file_path: fp, action: m.action || 'touched', count: 1, index: i })
    }
  }
  // Most recent first.
  return Array.from(byPath.values()).sort((a, b) => b.index - a.index)
})

type ProviderKey = RuntimeProvider
// One bucket per runtime provider: each owns its own auth and its own catalog,
// so the provider *is* the route. (This used to also enumerate the env-injected
// Ollama/OpenRouter/custom routes that ran through the Claude runner.)
type BucketKey = ProviderKey
type TierAlias = 'haiku' | 'sonnet' | 'opus' | 'fable'

const BUCKET_DEFS: { key: BucketKey; label: string; provider: ProviderKey }[] = [
  { key: 'claude', label: 'Claude', provider: 'claude' },
  { key: 'opencode', label: 'opencode', provider: 'opencode' },
]

// ModelSelector names the Claude section 'anthropic' (the models are Anthropic's,
// the runner is Claude Code), so the two vocabularies need one hop between them.
const SECTION_BY_BUCKET: Record<BucketKey, string> = {
  claude: 'anthropic',
  opencode: 'opencode',
}


function toggleTrace(i: number) {
  openTraces.value = { ...openTraces.value, [i]: !openTraces.value[i] }
}

function toggleLiveTrace() {
  liveTraceOpen.value = !liveTraceOpen.value
}

// Click on an expanded activity/thinking bubble body collapses it, unless the
// click landed on an interactive descendant (a link, file card/chip, subagent
// panel, inline code, copy button, or anything stopPropagation-bearing).
// Selection drags that happen to end inside the body are ignored so users
// can highlight text without accidentally closing the bubble.
function isInteractiveTraceChild(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false
  if (target.closest('a')) return true
  if (target.closest('button, [role="button"]')) return true
  if (target.closest('.file-card, .file-chip')) return true
  if (target.closest('.subagent-panel')) return true
  if (target.closest('code, pre, kbd')) return true
  return false
}

function onTraceBodyClick(i: number, e: MouseEvent): void {
  if (e.defaultPrevented) return
  if (isInteractiveTraceChild(e.target)) return
  if (window.getSelection()?.toString()) return
  if (!openTraces.value[i]) return
  openTraces.value = { ...openTraces.value, [i]: false }
}

function onLiveTraceBodyClick(e: MouseEvent): void {
  if (e.defaultPrevented) return
  if (isInteractiveTraceChild(e.target)) return
  if (window.getSelection()?.toString()) return
  if (!liveTraceOpen.value) return
  liveTraceOpen.value = false
}

function checkScroll() {
  const el = messagesEl.value
  if (!el) return
  const threshold = 4
  isNearBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
  onChatScrollReanchor()
  // Paginated history: near the top of a windowed timeline, pull the older
  // page in. The store's loadingOlder flag debounces repeat fires.
  if (el.scrollTop <= 80) {
    const chatId = store.activeChatId
    if (chatId && store.canLoadOlder(chatId) && !store.isLoadingOlder(chatId)) {
      void loadOlderAnchored()
    }
  }
}

// Prepend an older history page while keeping the viewport anchored to the
// rows the user is looking at: compensate for the height the prepend adds.
async function loadOlderAnchored() {
  const el = messagesEl.value
  const chatId = store.activeChatId
  if (!el || !chatId) return
  const prevHeight = el.scrollHeight
  await store.loadOlderMessages(chatId)
  await nextTick()
  if (messagesEl.value === el) el.scrollTop += el.scrollHeight - prevHeight
}

function expandLazyStep(step: { i?: number }): void {
  const chatId = store.activeChatId
  if (!chatId || typeof step.i !== 'number') return
  void store.expandMessagePart(chatId, step.i)
}

function scrollToBottom() {
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
  isNearBottom.value = true
}

// ---------- opening a chat lands at the bottom ----------
// Setting scrollTop once when a chat opens is not enough, which is why a chat
// used to appear scrolled to the top and then travel down on its own a moment
// later. The height of the transcript keeps changing for a few hundred
// milliseconds after its rows first paint — the history fetch resolves after
// the skeleton has already been measured, images and avatars load, the
// subagent fetch adds trace blocks, the composer resizes — and each of those
// growths leaves the viewport where it was, i.e. at the top. The
// stick-to-bottom watcher only catches up on the *next* change, so the
// correction was always visible as a scroll.
//
// Pinning the scroll to the bottom on every frame for a short window after
// the open makes the first frame the user sees the bottom, and keeps it there
// while the height settles. It costs one rAF per frame for well under a
// second, and only while a chat is being opened.
const OPEN_PIN_MS = 700
let pinUntil = 0
let pinRafId = 0

function pinToBottom(ms = OPEN_PIN_MS) {
  pinUntil = performance.now() + ms
  if (pinRafId) return
  const step = () => {
    pinRafId = 0
    const el = messagesEl.value
    if (!el) {
      pinUntil = 0
      return
    }
    el.scrollTop = el.scrollHeight
    isNearBottom.value = true
    if (performance.now() < pinUntil) pinRafId = requestAnimationFrame(step)
    else checkScroll()
  }
  pinRafId = requestAnimationFrame(step)
}

/** The user scrolling during the settle window wins immediately — a pin that
 * fought a deliberate scroll would be worse than the jump it removes. */
function releasePin() {
  pinUntil = 0
  if (pinRafId) {
    cancelAnimationFrame(pinRafId)
    pinRafId = 0
  }
}

// Scroll after layout when we're already following the tail. Call checkScroll
// only after scrolling — running it first clears isNearBottom when new content
// just grew the list (user bubble, streaming indicator, etc.).
function stickToBottomIfNeeded() {
  if (!isNearBottom.value) return
  scrollToBottom()
}

const pendingApprovals = computed(() => {
  const id = store.activeChatId
  if (!id) return []
  return store.pendingPermissions[id] || []
})

// The backend's `message` field is almost always the templated
// "Approve use of {tool_name}?", which just repeats the tool-name badge shown
// right next to it. Only surface it when it carries something else.
function permissionReason(p: { tool_name: string; message: string }) {
  return p.message && p.message !== `Approve use of ${p.tool_name}?` ? p.message : ''
}

// tool_input is opaque server text — usually compact JSON, sometimes a bare
// shell command or reason sentence. Pretty-print only when it parses;
// otherwise fall back to showing it verbatim rather than guessing at structure.
function formatToolInput(raw: string) {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

// Split a tool input into `key: value` rows for the approval card. Returns
// null when the payload isn't a plain object (bare command string, array,
// truncated JSON) so the caller keeps the verbatim <pre> fallback. Nested
// values are re-serialized compactly — the card is a "what am I approving"
// glance, not a debugger.
function permissionArgs(raw: string): { key: string; value: string }[] | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
  const rows = Object.entries(parsed as Record<string, unknown>).map(([key, value]) => ({
    key,
    value:
      typeof value === 'string'
        ? value
        : value === null || value === undefined
          ? String(value)
          : typeof value === 'object'
            ? JSON.stringify(value)
            : String(value),
  }))
  return rows.length ? rows : null
}

// AskUserQuestion picker. The headless CLI can't render the SDK's built-in
// picker, so the model's tool call lands with an empty result; the PWA owns
// the actual UI here. `questionAnswers` holds the user's in-progress
// selections keyed by chat_id + question index; cleared along with
// `activeQuestions` when sendMessage fires.
const activeQuestions = computed(() => {
  const id = store.activeChatId
  if (!id) return []
  return store.activeQuestions[id] || []
})

// The card is only on screen when it wins the dock, or when the dock strip is
// expanded behind a permission. Named because the keyboard shortcuts below key
// off the same condition as the template: a hidden card must not eat digits.
const questionCardVisible = computed(() =>
  activeQuestions.value.length > 0 && (dockPrimary.value === 'question' || dockExpanded.value),
)

type QuestionAnswer = { selected: Set<string>; other: string }
const questionAnswers = ref<Record<number, QuestionAnswer>>({})

// Reset per-question selections whenever the active chat changes or the
// model fires a fresh AskUserQuestion. Watching the array reference catches
// both "new chat" and "new questions in same chat" without us touching the
// answers map by hand.
watch(activeQuestions, () => { questionAnswers.value = {} })

function ensureAnswer(i: number): QuestionAnswer {
  let a = questionAnswers.value[i]
  if (!a) {
    a = { selected: new Set<string>(), other: '' }
    questionAnswers.value[i] = a
  }
  return a
}

function toggleQuestionOption(i: number, label: string, multi: boolean) {
  const a = ensureAnswer(i)
  if (multi) {
    if (a.selected.has(label)) a.selected.delete(label)
    else a.selected.add(label)
  } else {
    a.selected.clear()
    a.selected.add(label)
  }
  // Force reactivity since Set mutations aren't tracked.
  questionAnswers.value = { ...questionAnswers.value, [i]: { ...a } }
}

function isQuestionOptionSelected(i: number, label: string): boolean {
  return questionAnswers.value[i]?.selected.has(label) ?? false
}

// Digit shortcuts cover the first question only -- the picker almost always
// carries one, and a second block would need a second digit row with no way to
// tell them apart. The badge is rendered from the same function so the hint can
// never claim a key that does nothing.
const MAX_QUESTION_SHORTCUTS = 9

function questionOptionShortcut(qi: number, oi: number): string {
  if (qi !== 0 || oi >= MAX_QUESTION_SHORTCUTS) return ''
  return String(oi + 1)
}

// 1 and 2 approve/deny the first pending permission card, matching the
// question-picker digits. Keys are shown on the buttons so the hint can never
// claim a key that does nothing; the badge is drawn from the same function.
function permissionShortcut(action: 'approve' | 'deny'): string {
  if (!pendingApprovals.value.length) return ''
  return action === 'approve' ? '2' : '1'
}

// Digit handling for the pending permission card. ChatLayout offers digits to
// the question card first (handleQuestionShortcut), so when a permission and a
// question are both up, the question wins the keys; otherwise the permission
// card gets 1 (deny) / 2 (approve) on the first card. Returning true means
// "eaten" and the layout preventDefaults.
function handlePermissionShortcut(e: KeyboardEvent): boolean {
  if (!pendingApprovals.value.length) return false
  const first = pendingApprovals.value[0]
  if (e.key === '1') {
    store.respondPermission(chat.value.chat_id, first.request_id, false, 'User denied')
    return true
  }
  if (e.key === '2') {
    store.respondPermission(chat.value.chat_id, first.request_id, true)
    return true
  }
  return false
}

// Keyboard handling for the open question card. ChatLayout owns the single
// window keydown listener (onUnreservedKeydown) and offers the key here first,
// the same way it offers arrows to the home grid; returning true means "eaten",
// and the layout then preventDefaults instead of running its own binding. That
// is what lets an open card outrank the 1-9 workspace switcher without either
// side growing a second listener.
//
// The caller has already screened out modifiers and text fields (composer and
// the card's own "Other" input both count as typing targets), so this only
// decides whether the card has a use for the key.
function handleQuestionShortcut(e: KeyboardEvent): boolean {
  if (!questionCardVisible.value) return false
  const q = activeQuestions.value[0]
  if (!q) return false

  if (e.key === 'Enter') {
    // Never steal Enter from a focused control: on Cancel/Send answer, or on
    // an option button reached by Tab, the native activation is what the user
    // is asking for. Enter only submits from "nowhere in particular", which is
    // where focus sits after picking with a digit.
    if (e.target instanceof HTMLElement && e.target.closest('button, a, [role="button"]')) return false
    // Multi-select is a collection, not a choice: digits toggle and the
    // explicit Send answer ends it, so Enter stays a no-op there.
    if (activeQuestions.value.some(other => other.multiSelect)) return false
    if (!allQuestionsAnswered.value) return false
    submitQuestionAnswers()
    return true
  }

  if (!/^[1-9]$/.test(e.key)) return false
  const opt = q.options[Number(e.key) - 1]
  if (!opt) return false
  toggleQuestionOption(0, opt.label, q.multiSelect)
  return true
}

// Block Send answer until every question has at least one option picked
// or non-empty "Other" text. Without this, tapping Send with no selection
// would route an empty answer through submitQuestionAnswers and the
// handler would label it "(no answer)" (line below), which is the bug this
// guard fixes.
const allQuestionsAnswered = computed(() => {
  const qs = activeQuestions.value
  if (!qs.length) return false
  for (let i = 0; i < qs.length; i++) {
    const a = questionAnswers.value[i]
    const picked = a && a.selected.size > 0
    const other = !!(a && a.other && a.other.trim())
    if (!picked && !other) return false
  }
  return true
})

// Prefer the model's header/question; fall back to "Question N" so an empty
// AskUserQuestion payload never renders as broken markdown (`****: …`) or a
// blank prompt above the option list.
function questionPromptLabel(
  q: { header?: string; question?: string },
  index: number,
): string {
  return (q.question || q.header || `Question ${index + 1}`).trim()
}

function submitQuestionAnswers() {
  if (!allQuestionsAnswered.value) return
  if (!chat.value || chat.value.archived) return
  const qs = activeQuestions.value
  if (!qs.length) return
  const lines: string[] = []
  const nativeAnswers: Record<string, string[]> = {}
  for (let i = 0; i < qs.length; i++) {
    const q = qs[i]
    const a = questionAnswers.value[i]
    const picked = a ? Array.from(a.selected) : []
    const other = (a?.other || '').trim()
    const parts: string[] = []
    if (picked.length) parts.push(...picked)
    if (other) parts.push(other)
    const answer = parts.length ? parts.join(', ') : '(no answer)'
    nativeAnswers[q.id] = parts
    lines.push(`**${questionPromptLabel(q, i)}**: ${answer}`)
  }
  const requestId = qs[0]?.requestId || ''
  if (requestId) {
    store.respondQuestion(chat.value.chat_id, requestId, nativeAnswers)
    questionAnswers.value = {}
    return
  }
  const text = lines.join('\n')
  // sendMessage clears activeQuestions for this chat automatically.
  store.sendMessage(chat.value.chat_id, text)
}

function dismissQuestions() {
  const id = store.activeChatId
  if (!id) return
  const requestId = activeQuestions.value[0]?.requestId || ''
  if (requestId) {
    // respondQuestion records the resolution itself before clearing.
    store.respondQuestion(id, requestId, {})
  } else {
    // Claude picker has no round-trip; remember it as resolved so a stale
    // server snapshot can't rebuild it after dismissal.
    store.markResolvedQuestion(id)
  }
  delete store.activeQuestions[id]
  questionAnswers.value = {}
}

// Image-capability question. The server paused before dispatch because the
// selected model can't see images; the card offers same-backend vision
// candidates, an "Open picker" escape hatch, and a Cancel. The countdown
// mirrors the server's 30s wait_for; when it hits zero the buttons disable
// and the server closes the turn with a system bubble.
const activeCapabilityQuestions = computed(() => {
  const id = store.activeChatId
  if (!id) return []
  return store.activeCapabilityQuestions[id] || []
})

const capabilityNow = ref(Date.now())
let capabilityTimer: number | undefined
watch(activeCapabilityQuestions, (qs) => {
  capabilityNow.value = Date.now()
  if (qs.length && capabilityTimer === undefined) {
    capabilityTimer = window.setInterval(() => { capabilityNow.value = Date.now() }, 1000)
  } else if (!qs.length && capabilityTimer !== undefined) {
    window.clearInterval(capabilityTimer)
    capabilityTimer = undefined
  }
}, { immediate: true })
onBeforeUnmount(() => {
  if (capabilityTimer !== undefined) window.clearInterval(capabilityTimer)
})

function capabilityRemaining(q: { opened_at: number; timeout_s: number }): number {
  const elapsed = Math.floor((capabilityNow.value - q.opened_at) / 1000)
  return Math.max(0, q.timeout_s - elapsed)
}

function capabilityExpired(q: { opened_at: number; timeout_s: number }): boolean {
  return capabilityRemaining(q) <= 0
}

function switchCapabilityModel(q: { request_id: string }, modelId: string) {
  if (!chat.value) return
  store.respondCapability(chat.value.chat_id, q.request_id, 'switch', modelId)
}

function handleCapabilityPickerSelect(q: { request_id: string }, val: string | string[]) {
  const modelId = Array.isArray(val) ? val[0] : val
  if (!modelId) return
  switchCapabilityModel(q, modelId)
}

function capabilitySectionsFor(q: { current_model: string; candidates: Array<{ id: string; label: string; disabled?: boolean; supports_vision?: boolean }> }): import('../lib/modelSections').ModelSection[] {
  const current = q.current_model
  const visionCandidates = q.candidates.filter(c => c.supports_vision && !c.disabled)
  const bucket = activeBucket.value
  const labelMap: Record<string, string> = {}
  for (const c of visionCandidates) labelMap[c.id] = c.label
  const sections: import('../lib/modelSections').ModelSection[] = []
  if (current) {
    sections.push({ key: 'current', label: 'Current model — can’t see images', models: [current], disabled: true })
  }
  if (visionCandidates.length) {
    const bucketSection = chatModelSections.value.find(s => s.key === capabilitySectionForBucket(bucket))
    const label = bucketSection?.label || bucket
    sections.push({
      key: capabilitySectionForBucket(bucket),
      label,
      models: visionCandidates.map(c => c.id),
      modelLabels: labelMap,
    })
  }
  return sections
}

function cancelCapability(q: { request_id: string }) {
  if (!chat.value) return
  store.respondCapability(chat.value.chat_id, q.request_id, 'cancel')
}

// Section key (ModelSelector) for a bucket, used to preselect the backend when
// the capability card opens the full picker.
function capabilitySectionForBucket(bucket: BucketKey): string {
  return SECTION_BY_BUCKET[bucket] || 'anthropic'
}

const capabilityPickerSection = ref('')

const activeProvider = computed<ProviderKey>(() => {
  return (chat.value?.provider as ProviderKey) || 'claude'
})

const activeBucket = computed<BucketKey>(() => {
  return (chat.value?.provider as BucketKey) || 'claude'
})

const chatModelSections = computed(() => {
  const baseSections = sectionsFromModelsResponse(modelsResponse.value)
  // Name the Anthropic section for its vendor so it reads as a peer of the
  // opencode section rather than as "the default".
  return baseSections.map(section => {
    if (section.key === 'anthropic') {
      return { ...section, label: 'Claude (Anthropic)' }
    }
    return section
  })
})

// The model id the chat is running on, as stored. A tier alias stays an alias
// here: the provider resolves it per turn against its own catalog, so there is
// no single concrete id to substitute.
const activeModelId = computed(() => {
  const stored = chat.value?.model?.trim()
  if (stored) return stored
  // Older OpenCode chats were created before the provider catalog was
  // persisted, so they can legitimately have an empty model. Keep the
  // header actionable by showing the provider's current default while the
  // user can still pick a concrete entry from the popover.
  const provider = chat.value?.provider || ''
  return providerDefaults.value[provider]
    || modelsResponse.value?.provider_models?.[provider]?.[0]
    || (provider === 'claude' ? modelsResponse.value?.default : '')
    || ''
})

const activeModelHighlights = computed(() => {
  const c = chat.value
  if (!c) return []
  const model = activeModelId.value
  if (!model) return []
  return [model]
})

const bucketLocked = computed(() => {
  const c = chat.value
  if (!c) return false
  // The SDK assigns ``session_id`` on the first turn, so any non-empty
  // value means the chat has history and the bucket is fixed. The
  // server enforces the same rule on PATCH; this just hides the choice
  // so the user doesn't try and get a 400 back.
  return Boolean(c.session_id) || store.activeMessages.length > 0
})

// Thinking levels are provider-native and may be narrowed per model when the
// catalog reports levels.
const filteredThinkingLevels = computed(() => {
  const model = chat.value?.model || ''
  const modelLevels = modelsResponse.value?.model_reasoning_levels?.[model]
  const levels = modelLevels?.length
    ? modelLevels
    : thinkingLevels.value[activeProvider.value] || []
  return levels
})

const showThinkingLevels = computed(() => {
  if (!filteredThinkingLevels.value.length) return false
  return true
})

// Thinking level for the header chip. An empty thinking_level means the user
// left it at the provider default ("auto"), which the chip does not report —
// only an explicitly chosen level gets a segment. "think:" keeps this segment
// apart from the mode segment, whose default value is also "auto".
const chipThinkingLabel = computed(() => {
  const level = (chat.value?.thinking_level || '').trim().toLowerCase()
  // "auto" is the provider default, not a user-selected tuning knob. It is
  // still available inside the picker, but repeating it in the compact header
  // chip made the default look like an explicit mode.
  return level && level !== 'auto' ? `think:${level}` : ''
})

// The provider is already the chip's first segment, so a model id that
// repeats it as a prefix ("opencode/deepseek-...") wastes the width budget.
// The tooltip keeps the full id.
const chipModelLabel = computed(() => {
  const model = activeModelId.value || ''
  const provider = chat.value?.provider || ''
  if (!model) return 'select model'
  return provider && model.startsWith(`${provider}/`)
    ? model.slice(provider.length + 1)
    : model
})

const inputPlaceholder = computed(() => {
  if (store.isStreaming) return 'Follow-up...'
  return 'message'
})

// ── Chat comment selection UX ─────────────────────────────────────────
type ChatCommentDraft = ChatCommentAnchor & {
  selection: string
  text: string
}
const selectionAnchor = ref<{ top: number; left: number } | null>(null)
const draftAnchor = ref<{ top: number; left: number } | null>(null)

const COMMENT_PILL_H = 44
const commentDraft = ref<ChatCommentDraft | null>(null)
const editingChatCommentId = ref<string | null>(null)
const editingChatCommentText = ref('')
const chipEditAnchor = ref<{ top: number; left: number } | null>(null)
const commentDraftImages = ref<string[]>([])
const editingChatCommentImages = ref<string[]>([])
const composeText = computed({
  get: () => commentDraft.value?.text ?? '',
  set: (v: string) => {
    if (commentDraft.value) commentDraft.value.text = v
  },
})
let lastChatSelectionText = ''
let lastChatSelectionRange: Range | null = null
let lastChatSelectionBubble: HTMLElement | null = null
let lastChatSelectionMsgId: string | undefined
let lastChatSelectionMsgIndex: number | undefined
let lastChatSelectionMsgRole: string | undefined
let lastChatSelectionOccurrenceIndex: number | undefined
let lastChatSelectionParagraphIndex: number | undefined
let draftBubbleEl: HTMLElement | null = null
const commentBubbleById = new Map<string, HTMLElement>()
const DRAFT_COMMENT_ID = '__draft__'

function truncate(s: string, n: number): string {
  if (!s) return ''
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

function getSelectionStartOffsetInElement(container: HTMLElement, range: Range): number {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT)
  let charPos = 0
  let node: Node | null
  while ((node = walker.nextNode())) {
    if (node === range.startContainer) {
      return charPos + range.startOffset
    }
    charPos += node.textContent?.length || 0
  }
  return -1
}

function computeOccurrenceIndex(contentEl: HTMLElement, range: Range, selection: string): number {
  const fullText = contentEl.textContent || ''
  const startOffset = getSelectionStartOffsetInElement(contentEl, range)
  if (startOffset === -1) return 0
  return commentTextOccurrenceIndex(fullText, selection, startOffset)
}

function computeParagraphIndex(contentEl: HTMLElement, range: Range): number {
  const blocks = contentEl.querySelectorAll('p, li, pre, blockquote, h1, h2, h3, h4, h5, h6')
  if (blocks.length > 0) {
    const idx = Array.from(blocks).findIndex(b => b.contains(range.startContainer))
    if (idx !== -1) return idx
  }
  const startOffset = getSelectionStartOffsetInElement(contentEl, range)
  if (startOffset > 0) {
    const textBefore = (contentEl.textContent || '').slice(0, startOffset)
    const doubleNewlines = textBefore.match(/\n\s*\n/g)
    return doubleNewlines ? doubleNewlines.length : 0
  }
  return 0
}

function updateChatSelectionAnchorFromRange(range: Range): void {
  const msgs = messagesEl.value
  if (!msgs) {
    selectionAnchor.value = null
    return
  }

  const rects = range.getClientRects()
  const endRect = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect()
  const msgsRect = msgs.getBoundingClientRect()
  const visible = endRect.bottom > msgsRect.top
    && endRect.top < msgsRect.bottom
    && endRect.right > msgsRect.left
    && endRect.left < msgsRect.right
  if (!visible) {
    selectionAnchor.value = null
    return
  }

  const popoverW = Math.min(420, window.innerWidth * 0.9)
  selectionAnchor.value = {
    top: clampAnchorTop(endRect.bottom + 2, COMMENT_PILL_H),
    left: clampAnchorLeft(endRect.right + 6, popoverW),
  }
}

const isProgrammaticScrolling = ref(false)
let programmaticScrollTimer: ReturnType<typeof setTimeout> | null = null

function onChatScrollReanchor(): void {
  if (isProgrammaticScrolling.value) return
  closeChatCommentPopover()
  if (commentDraft.value || !lastChatSelectionRange) return
  try {
    if (!lastChatSelectionRange.startContainer.isConnected) {
      lastChatSelectionRange = null
      selectionAnchor.value = null
      return
    }
    updateChatSelectionAnchorFromRange(lastChatSelectionRange)
  } catch {
    lastChatSelectionRange = null
    selectionAnchor.value = null
  }
}

function onChatSelectionChange(): void {
  if (commentDraft.value) return
  const sel = window.getSelection()
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
    lastChatSelectionRange = null
    selectionAnchor.value = null
    return
  }
  const range = sel.getRangeAt(0)
  // Only react to selections inside message bubbles
  const msgs = messagesEl.value
  if (!msgs || !msgs.contains(range.startContainer) || !msgs.contains(range.endContainer)) {
    lastChatSelectionRange = null
    selectionAnchor.value = null
    return
  }
  // Skip if the selection is inside an input/textarea
  const startEl = range.startContainer instanceof Element ? range.startContainer : range.startContainer.parentElement
  const endEl = range.endContainer instanceof Element ? range.endContainer : range.endContainer.parentElement
  if (startEl?.closest('textarea, input') || endEl?.closest('textarea, input')) {
    lastChatSelectionRange = null
    selectionAnchor.value = null
    return
  }
  // Find the bubble this selection lives in. Required so applyHighlights()
  // only wraps the matching text in the originating bubble, not every bubble
  // that happens to contain the same string.
  const bubble = startEl?.closest('.message') as HTMLElement | null
  if (!bubble) {
    lastChatSelectionRange = null
    selectionAnchor.value = null
    return
  }
  const text = cleanCommentSelection(sel.toString().trim())
  if (!text) {
    lastChatSelectionRange = null
    selectionAnchor.value = null
    return
  }
  lastChatSelectionText = text
  lastChatSelectionBubble = bubble
  lastChatSelectionRange = range.cloneRange()

  const contentEl = (bubble.querySelector('.message-content') || bubble) as HTMLElement
  lastChatSelectionMsgId = bubble.dataset.msgId
  lastChatSelectionMsgIndex = bubble.dataset.msgIndex ? parseInt(bubble.dataset.msgIndex, 10) : undefined
  lastChatSelectionMsgRole = bubble.dataset.msgRole
  lastChatSelectionOccurrenceIndex = computeOccurrenceIndex(contentEl, range, text)
  lastChatSelectionParagraphIndex = computeParagraphIndex(contentEl, range)

  updateChatSelectionAnchorFromRange(range)
}

function openCommentForSelection(initialText = ''): void {
  if (!selectionAnchor.value || !lastChatSelectionText) return
  closeChatCommentPopover()
  draftAnchor.value = { ...selectionAnchor.value }
  draftBubbleEl = lastChatSelectionBubble
  commentDraftImages.value = []
  commentDraft.value = {
    selection: lastChatSelectionText,
    text: initialText,
    messageId: lastChatSelectionMsgId,
    messageIndex: lastChatSelectionMsgIndex,
    messageRole: lastChatSelectionMsgRole,
    occurrenceIndex: lastChatSelectionOccurrenceIndex,
    paragraphIndex: lastChatSelectionParagraphIndex,
  }
  selectionAnchor.value = null
  lastChatSelectionRange = null
  window.getSelection()?.removeAllRanges()
  nextTick(() => applyHighlights())
}

function cancelChatComment(): void {
  commentDraft.value = null
  draftAnchor.value = null
  commentDraftImages.value = []
  draftBubbleEl = null
  lastChatSelectionText = ''
  lastChatSelectionBubble = null
  lastChatSelectionRange = null
  lastChatSelectionMsgId = undefined
  lastChatSelectionMsgIndex = undefined
  lastChatSelectionMsgRole = undefined
  lastChatSelectionOccurrenceIndex = undefined
  lastChatSelectionParagraphIndex = undefined
  nextTick(() => applyHighlights())
}

function saveChatComment(): void {
  const draft = commentDraft.value
  if (!draft) return
  const note = draft.text.trim()
  if (!note) return
  const id = store.addPendingChatComment({
    selection: draft.selection,
    comment: note,
    images: commentDraftImages.value.length ? commentDraftImages.value : undefined,
    messageId: draft.messageId,
    messageIndex: draft.messageIndex,
    messageRole: draft.messageRole,
    occurrenceIndex: draft.occurrenceIndex,
    paragraphIndex: draft.paragraphIndex,
  })
  if (draftBubbleEl) commentBubbleById.set(id, draftBubbleEl)
  draftBubbleEl = null
  commentDraft.value = null
  draftAnchor.value = null
  commentDraftImages.value = []
  lastChatSelectionText = ''
  lastChatSelectionBubble = null
  lastChatSelectionRange = null
  lastChatSelectionMsgId = undefined
  lastChatSelectionMsgIndex = undefined
  lastChatSelectionMsgRole = undefined
  lastChatSelectionOccurrenceIndex = undefined
  lastChatSelectionParagraphIndex = undefined
  nextTick(() => applyHighlights())
}

async function addDraftImages(files: File[]): Promise<void> {
  const chatId = store.activeChatId
  if (!chatId || !files.length) return
  try {
    const refs = await store.uploadImageRefs(chatId, files)
    commentDraftImages.value.push(...refs)
  } catch (err) {
    console.error('Comment image upload failed:', err)
  }
}

async function handleDraftImageUpload(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  if (!input.files?.length) return
  await addDraftImages(Array.from(input.files))
  input.value = ''
}

// Selecting transcript text and typing (or pasting, or hitting Cmd+D) opens the
// composer directly, so the "Comment" pill is a hint rather than a required
// click. Dictation has to wait for the popover to mount its recorder.
useTypeToComment({
  isActive: () => !!selectionAnchor.value && !commentDraft.value,
  open: (initialText: string) => openCommentForSelection(initialText),
  dictate: () => nextTick(() => commentComposeDraftRef.value?.toggleDictation()),
  addImages: (files: File[]) => addDraftImages(files),
})

function removeDraftImage(index: number): void {
  commentDraftImages.value.splice(index, 1)
}

// ── Edit / remove pending chat comments from the sidebar ─────────────
function startEditChatComment(c: { id: string; selection: string; comment: string; images?: string[] }): void {
  editingChatCommentId.value = c.id
  editingChatCommentText.value = c.comment
  editingChatCommentImages.value = c.images ? [...c.images] : []
}
function cancelEditChatComment(): void {
  editingChatCommentId.value = null
  editingChatCommentText.value = ''
  editingChatCommentImages.value = []
  chipEditAnchor.value = null
}
function saveEditChatComment(id: string): void {
  const text = editingChatCommentText.value.trim()
  if (!text) return
  store.updatePendingChatComment(id, text)
  // Sync images: remove existing ones that are gone, add new ones
  const existing = store.pendingChatComments.find(c => c.id === id)
  const existingImages = existing?.images || []
  const nextImages = editingChatCommentImages.value
  for (const img of existingImages) {
    if (!nextImages.includes(img)) store.removePendingChatCommentImage(id, img)
  }
  for (const img of nextImages) {
    if (!existingImages.includes(img)) store.addPendingChatCommentImage(id, img)
  }
  cancelEditChatComment()
}

async function handleEditImageUpload(e: Event, id: string): Promise<void> {
  const input = e.target as HTMLInputElement
  if (!input.files?.length) return
  const chatId = store.activeChatId
  if (!chatId) return
  try {
    const refs = await store.uploadImageRefs(chatId, Array.from(input.files))
    for (const ref of refs) {
      store.addPendingChatCommentImage(id, ref)
    }
    // Refresh local edit state from store
    const c = store.pendingChatComments.find(x => x.id === id)
    if (c?.images) editingChatCommentImages.value = [...c.images]
  } catch (err) {
    console.error('Comment image upload failed:', err)
  }
  input.value = ''
}

function removeEditImage(index: number): void {
  editingChatCommentImages.value.splice(index, 1)
}

function deleteChatComment(id: string): void {
  if (commentPopover.value?.openId === id) closeChatCommentPopover()
  store.removePendingChatComment(id)
  commentBubbleById.delete(id)
  if (editingChatCommentId.value === id) cancelEditChatComment()
  nextTick(() => applyHighlights())
}

// ── Highlight wrap / clear in message bubbles ────────────────────────
function clearHighlights(root: HTMLElement): void {
  const existing = root.querySelectorAll('.comment-highlight')
  for (const el of Array.from(existing)) {
    const parent = el.parentNode
    if (!parent) continue
    parent.replaceChild(document.createTextNode(el.textContent || ''), el)
    parent.normalize()
  }
}

function findBubbleForComment(root: HTMLElement, c: { id: string; selection: string; messageId?: string; messageIndex?: number; messageRole?: string }): HTMLElement | null {
  const stored = commentBubbleById.get(c.id)
  if (stored && root.contains(stored)) {
    const content = stored.querySelector('.message-content')
    const text = content?.textContent || ''
    if (commentTextMatches(text, c.selection)) {
      return stored
    }
  }

  if (c.messageId) {
    const escapedId = escapeCssAttrValue(c.messageId)
    const byId = root.querySelector(`.message[data-msg-id="${escapedId}"]`) as HTMLElement | null
    if (byId) return byId
  }

  if (c.messageIndex != null && c.messageIndex >= 0) {
    const byIndex = root.querySelector(`.message[data-msg-index="${c.messageIndex}"]`) as HTMLElement | null
    if (byIndex) return byIndex
  }

  const bubbles = root.querySelectorAll('.message')
  for (const bubble of Array.from(bubbles)) {
    const el = bubble as HTMLElement
    if (c.messageRole && el.dataset.msgRole && el.dataset.msgRole !== c.messageRole) {
      continue
    }
    const content = el.querySelector('.message-content')
    if (!content) continue
    const text = content.textContent || ''
    if (commentTextMatches(text, c.selection)) {
      return el
    }
  }
  return null
}

function applyHighlights(): void {
  const root = messagesEl.value
  if (!root) return
  clearHighlights(root)

  for (const c of store.pendingChatComments) {
    const bubble = findBubbleForComment(root, c)
    if (bubble) {
      highlightCommentText(bubble, c.selection, c.id, c.occurrenceIndex)
    } else {
      console.warn('Bubble not found for comment', c.id, c.selection.slice(0, 80))
    }
  }

  // Also highlight the in-progress draft selection so the user sees what
  // they're commenting on while they type, not only after saving.
  const draft = commentDraft.value
  if (draft && draftBubbleEl && root.contains(draftBubbleEl)) {
    highlightCommentText(draftBubbleEl, draft.selection, DRAFT_COMMENT_ID, draft.occurrenceIndex)
  }
}

// ── Click / hover sync between highlights, read popover, and sidebar ──
// The popover state lives in the child so hover never touches this component's
// render (the transcript is rendered inline here and is not virtualized). The
// handlers are read off the ref at event time, not at render time.
const commentPopover = ref<InstanceType<typeof ChatCommentPopover> | null>(null)

function onChatHighlightHover(e: MouseEvent): void {
  commentPopover.value?.onTargetOver(e)
}

function onChatHighlightHoverOut(e: MouseEvent): void {
  commentPopover.value?.onTargetOut(e)
}

function closeChatCommentPopover(): void {
  commentPopover.value?.close()
}

function handleHighlightClick(e: MouseEvent): void {
  const id = commentPopover.value?.pinFromEvent(e)
  if (!id) return
  e.stopPropagation()
}

// iOS Safari mishandles scrollIntoView on nested scrollable containers
// (it can scroll the wrong ancestor). Compute offsetTop relative to the
// scroll container and set scrollTop directly instead.
function offsetTopWithin(el: HTMLElement, root: HTMLElement): number {
  let top = 0
  let node: HTMLElement | null = el
  while (node && node !== root) {
    top += node.offsetTop
    node = node.offsetParent as HTMLElement | null
  }
  return top
}

function scrollToHighlight(id: string): void {
  const root = messagesEl.value
  if (!root) return
  const hl = root.querySelector(`.comment-highlight[data-comment-id="${id}"]`) as HTMLElement | null
  if (!hl) return
  const top = offsetTopWithin(hl, root) - (root.clientHeight - hl.offsetHeight) / 2
  root.scrollTo({ top: Math.max(0, top), behavior: 'smooth' })
}

// Click from a pending chat-comment chip: scroll the conversation to the
// highlighted text AND flash it briefly so the eye lands on the right span.
function jumpToCommentHighlight(id: string, onComplete?: () => void): void {
  isProgrammaticScrolling.value = true
  scrollToHighlight(id)
  const root = messagesEl.value
  if (!root) return
  const hl = root.querySelector(`.comment-highlight[data-comment-id="${id}"]`) as HTMLElement | null
  if (!hl) return
  hl.classList.remove('comment-highlight--pulse')
  // force reflow so re-adding the class restarts the animation
  void hl.offsetWidth
  hl.classList.add('comment-highlight--pulse')
  setTimeout(() => hl.classList.remove('comment-highlight--pulse'), 1200)

  if (programmaticScrollTimer) clearTimeout(programmaticScrollTimer)
  programmaticScrollTimer = setTimeout(() => {
    isProgrammaticScrolling.value = false
    programmaticScrollTimer = null
    if (onComplete) onComplete()
  }, 350)
}

function anchorFromElement(el: HTMLElement): { top: number; left: number } {
  const rect = el.getBoundingClientRect()
  return {
    top: clampAnchorTop(rect.bottom + 6, 80),
    left: clampAnchorLeft(rect.left, 280),
  }
}

function openEditFromChatPopover(c: { id: string; comment: string; images?: string[] }): void {
  const hl = messagesEl.value?.querySelector(`.comment-highlight[data-comment-id="${c.id}"]`) as HTMLElement | null
  const anchor = hl ? anchorFromElement(hl) : null
  chipEditAnchor.value = anchor
  const target = store.pendingChatComments.find(x => x.id === c.id)
  if (target) startEditChatComment(target)
}

// Clicking a pending comment chip scrolls to the commented text in the transcript,
// pulses the highlight, and pins the read popover over the highlight.
function openChatCommentChip(id: string, _e: MouseEvent): void {
  const c = store.pendingChatComments.find(x => x.id === id)
  if (!c) return
  cancelEditChatComment()

  jumpToCommentHighlight(id, () => {
    const rootEl = messagesEl.value
    if (!rootEl) return
    const hl = rootEl.querySelector(`.comment-highlight[data-comment-id="${id}"]`) as HTMLElement | null
    if (hl) {
      commentPopover.value?.show(id, hl, true)
    }
  })
}

// Sending the turn clears every pending comment, and switching chats swaps the
// bucket. Either way the chip the popover is anchored to is gone, so close it
// instead of leaving an editor floating over a comment that no longer exists.
watch(
  () => store.pendingChatComments.some(c => c.id === editingChatCommentId.value),
  (stillThere) => {
    if (editingChatCommentId.value && !stillThere) cancelEditChatComment()
  }
)

// File comments have no highlight in the transcript, so the chip opens the
// document at the commented line instead.
function openFileCommentChip(c: { id?: string; path: string; lineStart?: number | null }): void {
  if (!c.path) return
  const activePinKey = chat.value?.chat_id || store.activeChatId
  const pinnedPath = activePinKey ? store.pinnedFileFor(activePinKey) : ''
  if (pinnedPath && pinnedPath === c.path) {
    window.dispatchEvent(new CustomEvent('ciao:jump-pinned-comment', {
      detail: { id: c.id, line: c.lineStart }
    }))
  } else {
    fileViewer.open(c.path, c.lineStart ?? null, chat.value?.chat_id || '')
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('selectionchange', onChatSelectionChange)
}

// Opening a chat puts the cursor in the composer, so typing just works.
// ChatLayout keys this panel on chat_id, so it remounts per chat and this
// covers switching chats as well as opening one.
//
// Two deliberate exceptions:
//
//   A pending question or permission card. Its options are numbered on screen
//     and 1-9 picks one, but ChatLayout only offers a key to the card when
//     focus is NOT in a text field (see handleQuestionShortcut's contract).
//     Focusing here would turn "press 2" into typing "2", in exactly the chats
//     that are blocked waiting for that answer.
//
//   Narrow viewports. Focus is what raises the on-screen keyboard (see
//     handleInputFocus), so on a phone this would cover half the transcript on
//     every chat you tap into, and iOS zooms the page on focus besides.
const COMPOSER_FOCUS_MIN_WIDTH = 768

function focusComposerOnOpen(): void {
  if (window.innerWidth < COMPOSER_FOCUS_MIN_WIDTH) return
  if (questionCardVisible.value || pendingApprovals.value.length) return
  const el = inputEl.value
  if (!el) return
  el.focus()
  // A restored draft would otherwise take the caret at offset 0, so the next
  // keystroke would prepend to what the user already wrote.
  el.selectionStart = el.selectionEnd = el.value.length
}

onMounted(async () => {
  window.addEventListener('ciao:native-file-drag-enter', handleNativeFileDragEnter)
  window.addEventListener('ciao:native-file-drag-leave', handleNativeFileDragLeave)
  window.addEventListener('ciao:native-file-drop', handleNativeFileDrop)
  try {
    const r = await api.get<ModelsResponse>('/api/models')
    modelsResponse.value = r
    models.value = r.models
    providerModels.value = r.provider_models || {}
    providerDefaults.value = r.provider_defaults || {}
    thinkingLevels.value = r.thinking_levels || {}
  } catch { /* use defaults */ }
  await loadSlashCommands()
  await loadMentionAgents()
  notifyChatFocused(chat.value?.chat_id)
  messagesEl.value?.addEventListener('scroll', checkScroll, { passive: true })
  // Any hands-on scroll gesture releases the open-time bottom pin.
  messagesEl.value?.addEventListener('wheel', releasePin, { passive: true })
  messagesEl.value?.addEventListener('touchmove', releasePin, { passive: true })
  // Covers a scrollbar drag, which produces scroll events but no wheel.
  messagesEl.value?.addEventListener('pointerdown', releasePin, { passive: true })
  if (messagesEl.value && typeof ResizeObserver !== 'undefined') {
    messagesResizeObserver = new ResizeObserver(() => {
      stickToBottomIfNeeded()
      checkScroll()
    })
    messagesResizeObserver.observe(messagesEl.value)
  }
  nextTick(() => {
    autoResize()
    if (messagesEl.value) {
      messagesEl.value.scrollTop = messagesEl.value.scrollHeight
      pinToBottom()
    }
    focusComposerOnOpen()
  })
})

onBeforeUnmount(() => {
  writeChatDraft(
    draftChatId,
    promptHistoryIndex.value < 0 ? inputText.value : promptHistoryDraft.value,
    undefined,
    { projectId: chat.value?.project_id, workspace: store.activeWorkspace },
  )
  window.removeEventListener('ciao:native-file-drag-enter', handleNativeFileDragEnter)
  window.removeEventListener('ciao:native-file-drag-leave', handleNativeFileDragLeave)
  window.removeEventListener('ciao:native-file-drop', handleNativeFileDrop)
  commentPopover.value?.clearPendingClose()
  if (typeof document !== 'undefined') {
    document.removeEventListener('selectionchange', onChatSelectionChange)
  }
  messagesEl.value?.removeEventListener('scroll', checkScroll)
  messagesEl.value?.removeEventListener('wheel', releasePin)
  messagesEl.value?.removeEventListener('touchmove', releasePin)
  messagesEl.value?.removeEventListener('pointerdown', releasePin)
  releasePin()
  messagesResizeObserver?.disconnect()
  messagesResizeObserver = null
  if (clockTimer) { clearInterval(clockTimer); clockTimer = null }
})

// Tell the service worker which chat is in focus so it can clear the badge
function notifyChatFocused(chatId: string | undefined) {
  if (!chatId) return
  if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage({ type: 'chat-focused', chat_id: chatId })
  }
}

watch(() => chat.value?.chat_id, (id) => notifyChatFocused(id))

const knownFilePaths = computed(() => touchedFiles.value.map(f => f.file_path))

const mdCache = new Map<string, string>()
const MAX_MD_CACHE_SIZE = 500
// Rendered output depends on the known-file-path set (for linkification), so
// drop the cache whenever that set actually changes — keying on text alone is
// then safe, and avoids a stale render when paths change without the text.
watch(() => knownFilePaths.value.join('|'), () => mdCache.clear())

function renderMarkdown(text: string): string {
  if (!text) return ''
  const cached = mdCache.get(text)
  if (cached !== undefined) return cached

  const rendered = renderSafeMarkdown(text, knownFilePaths.value)
  if (mdCache.size >= MAX_MD_CACHE_SIZE) {
    // Plain FIFO eviction — fine for chat scroll-back.
    const firstKey = mdCache.keys().next().value
    if (firstKey !== undefined) mdCache.delete(firstKey)
  }
  mdCache.set(text, rendered)
  return rendered
}

function renderActivityLine(line: string): string {
  return linkifyText(line, knownFilePaths.value)
}

function activityLines(content: string): string[] {
  return content.split('\n').map(line => line.trim()).filter(Boolean)
}

async function copyMessageText(text: string, key: string): Promise<void> {
  const trimmed = text.trim()
  if (!trimmed) return
  const copied = await writeClipboard(trimmed)
  if (!copied) return
  copiedMessageKey.value = key
  setTimeout(() => {
    if (copiedMessageKey.value === key) copiedMessageKey.value = null
  }, 1500)
}

async function forkConversation(message: ChatMessage, key: string): Promise<void> {
  const sourceChatId = store.activeChatId
  if (!sourceChatId || forkLoadingKey.value) return
  const snapshot = buildForkSnapshot(store.activeMessages, message)
  if (!snapshot) {
    store.pushErrorToast(
      'Could not fork conversation',
      'This answer is no longer available as a fork point.',
    )
    return
  }
  forkLoadingKey.value = key
  try {
    await store.forkChat(sourceChatId, snapshot.messages, snapshot.turnIndex)
    await nextTick()
    inputEl.value?.focus()
  } catch (error) {
    store.pushErrorToast(
      'Could not fork conversation',
      error instanceof Error ? error.message : String(error),
    )
  } finally {
    forkLoadingKey.value = null
  }
}

// ── Read aloud ─────────────────────────────────────────────────────────
const speakingMessageKey = ref<string | null>(null)
const speakLoadingKey = ref<string | null>(null)
const speakError = ref<{ key: string; message: string } | null>(null)
let speakAudio: HTMLAudioElement | null = null

function stopSpeaking(): void {
  if (speakAudio) {
    speakAudio.pause()
    if (speakAudio.src.startsWith('blob:')) URL.revokeObjectURL(speakAudio.src)
    speakAudio = null
  }
  speakingMessageKey.value = null
}

async function speakMessage(text: string, key: string): Promise<void> {
  if (speakingMessageKey.value === key) {
    stopSpeaking()
    return
  }
  stopSpeaking()
  const chatId = store.activeChatId
  if (!chatId || !text.trim() || speakLoadingKey.value) return
  speakLoadingKey.value = key
  speakError.value = null
  try {
    const blob = await store.speakMessage(chatId, text)
    // The user may have started another playback while this one synthesized.
    stopSpeaking()
    const audio = new Audio(URL.createObjectURL(blob))
    speakAudio = audio
    speakingMessageKey.value = key
    audio.onended = audio.onerror = () => {
      if (speakAudio === audio) stopSpeaking()
    }
    await audio.play()
  } catch (e) {
    stopSpeaking()
    speakError.value = { key, message: e instanceof Error ? e.message : 'Speech failed' }
    setTimeout(() => {
      if (speakError.value?.key === key) speakError.value = null
    }, 6000)
  } finally {
    if (speakLoadingKey.value === key) speakLoadingKey.value = null
  }
}

onBeforeUnmount(stopSpeaking)

// Subagent activity lines are tagged with the leading turnstile arrow by the
// store's tool_use handler when an event arrives with parent_tool_use_id set.
// Used to indent and de-emphasize them so the trace reads "parent → subagent
// → parent" without the user mistaking subagent work for the parent's own.
function isSubagentLine(line: string): boolean {
  return line.trimStart().startsWith('↳')  // ↳
}

function handleFileLinkClick(e: MouseEvent): void {
  const target = e.target as HTMLElement | null
  if (!target) return
  const a = target.closest('a.file-link') as HTMLAnchorElement | null
  if (!a) return
  e.preventDefault()
  e.stopPropagation()
  const path = a.getAttribute('data-file-path') || ''
  const lineAttr = a.getAttribute('data-line')
  const line = lineAttr ? parseInt(lineAttr, 10) : null
  const cid = chat.value?.chat_id || ''
  if (_IMAGE_EXT_RE.test(path)) {
    fileViewer.openImage(path, cid)
  } else {
    fileViewer.open(path, Number.isFinite(line as number) ? line : null, cid)
  }
}

// One delegated listener at the panel root serves every clickable thing the
// markdown renderer emits. Code-block copy buttons live inside `v-html`
// output that is rebuilt on each streamed token, so they can only be reached
// by delegation — a per-button listener would be dropped on every re-render.
function handlePanelClick(e: MouseEvent): void {
  if (handleCodeCopyClick(e)) return
  handleFileLinkClick(e)
}

const liveTraceMetaParts = computed(() => {
  let toolCount = 0
  let textCount = 0
  let thinkingCount = 0
  let fileCount = 0
  for (const e of store.currentTimeline) {
    if (e.kind === 'tool') {
      toolCount += e.content.split('\n').filter(Boolean).length
    } else if (e.kind === 'thinking') {
      thinkingCount += 1
    } else if (e.kind === 'filecard') {
      fileCount += 1
    } else if (e.kind === 'text') {
      textCount += 1
    }
  }
  if (store.currentStreamingThinking) thinkingCount += 1
  if (store.currentStreamingText) textCount += 1
  const parts: { key: string; text: string; shortText?: string; isImportant?: boolean }[] = []
  if (thinkingCount) {
    parts.push({
      key: 'thoughts',
      text: `${thinkingCount} thought${thinkingCount === 1 ? '' : 's'}`,
      shortText: `${thinkingCount} th`
    })
  }
  if (textCount) {
    parts.push({
      key: 'notes',
      text: `${textCount} note${textCount === 1 ? '' : 's'}`,
      shortText: `${textCount} n`
    })
  }
  if (toolCount) {
    parts.push({
      key: 'tools',
      text: `${toolCount} tool call${toolCount === 1 ? '' : 's'}`,
      shortText: `${toolCount} tool${toolCount === 1 ? '' : 's'}`,
      isImportant: true
    })
  }
  if (fileCount) {
    parts.push({
      key: 'files',
      text: `${fileCount} file${fileCount === 1 ? '' : 's'}`,
      shortText: `${fileCount} f`
    })
  }
  // Live elapsed time: reads nowTs (ticks every second) against the turn's
  // start so the label counts up while the model works.
  const startedAt = store.currentStreamStartedAt
  if (startedAt) {
    const elapsed = nowTs.value - startedAt
    if (elapsed >= 0) {
      parts.push({
        key: 'duration',
        text: formatDuration(elapsed),
        isImportant: true
      })
    }
  }
  // Live token count: cumulative tokens reported so far this turn.
  const usage = store.currentLiveUsage
  if (usage) {
    if (usage.input > 0) {
      parts.push({
        key: 'tokens-in',
        text: `${formatTokens(usage.input)} in`,
        shortText: `${formatTokens(usage.input)} in`
      })
    }
    if (usage.output > 0) {
      parts.push({
        key: 'tokens-out',
        text: `${formatTokens(usage.output)} out`,
        shortText: `${formatTokens(usage.output)} out`
      })
    }
  }
  return parts
})

// Live trace label: "Working..." when real tool work or visible text is in
// progress, otherwise "Thinking..." while the model reasons.
const liveTraceLabel = computed(() => {
  if (store.currentTimeline.length || store.currentStreamingText) return 'Working...'
  return 'Thinking...'
})

// Compact token label: 1234 -> "1.2k", 1_200_000 -> "1.2M". Keeps the live
// trace meta short while the count grows.
function formatTokens(n: number): string {
  if (!isFinite(n) || n <= 0) return '0'
  if (n < 1000) return String(Math.round(n))
  if (n < 1_000_000) {
    const k = n / 1000
    return `${k < 10 ? k.toFixed(1) : Math.round(k)}k`
  }
  const m = n / 1_000_000
  return `${m < 10 ? m.toFixed(1) : Math.round(m)}M`
}



// Image extensions get routed through openImage so the binary streams
// directly instead of round-tripping through the text endpoint. Everything
// else (markdown, code, config, plain text) goes through `open`. Binary
// formats the viewer doesn't render (PDF, docx, xlsx, pptx, zip) fall
// through to `open`, which will 415 and show a clear error.
const _IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico|tiff?)$/i

function openFileCard(filePath: string): void {
  if (!filePath) return
  const cid = chat.value?.chat_id || ''
  if (_IMAGE_EXT_RE.test(filePath)) {
    fileViewer.openImage(filePath, cid)
  } else {
    fileViewer.open(filePath, null, cid)
  }
}

function fileCardBasename(filePath: string): string {
  if (!filePath) return ''
  const cleaned = filePath.replace(/[/\\]+$/, '')
  const slash = Math.max(cleaned.lastIndexOf('/'), cleaned.lastIndexOf('\\'))
  return slash >= 0 ? cleaned.slice(slash + 1) : cleaned
}

function fileCardDirname(filePath: string): string {
  if (!filePath) return ''
  const slash = Math.max(filePath.lastIndexOf('/'), filePath.lastIndexOf('\\'))
  return slash > 0 ? filePath.slice(0, slash) : ''
}

// Emoji cannot inherit currentColor, so file glyphs are SVG names now; see
// docs/DESIGN_SYSTEM.md rule S4.
function fileCardIcon(filePath: string): AppIconName {
  if (_IMAGE_EXT_RE.test(filePath)) return 'image'
  if (/\.(md|markdown|txt)$/i.test(filePath)) return 'doc'
  if (/\.(pdf|docx?|xlsx?|pptx?)$/i.test(filePath)) return 'doc'
  return 'file'
}

/** Fold a turn's footer facts into one record.
 *
 * The fields do not arrive together. The merged final-answer row carries
 * `effective_model` and `usage`; `_overlay_assistant_timings` puts `sent_at`
 * and `duration_ms` on whichever assistant row ends the turn; and which of
 * those becomes the turn's *last rendered* bubble depends on phase tagging.
 * Take the last non-empty value for each field so the footer is complete
 * wherever its parts came from.
 *
 * ``base`` carries what an earlier flush of the SAME turn already found: a
 * mid-turn system row (a client notice, an interrupt marker) splits one turn
 * across two flushes, and the halves rarely both carry every field.
 */
function turnMetaFrom(buffer: ChatMessage[], base: TurnMeta | null = null): TurnMeta | null {
  const meta: TurnMeta = { ...(base ?? {}) }
  for (const m of buffer) {
    if (m.role !== 'assistant') continue
    if (m.timestamp) meta.timestamp = m.timestamp
    if (m.duration_ms) meta.duration_ms = m.duration_ms
    if (m.effective_model) meta.effective_model = m.effective_model
    if (m.usage) meta.usage = m.usage
  }
  return Object.keys(meta).length ? meta : null
}

/** Put the turn's footer on the last assistant bubble it produced — once.
 *
 * Anything earlier is mid-turn: labelling it with the turn's cost read as the
 * price of that fragment, and left the reply the user actually ends on bare.
 * The walk clears the footer off every earlier bubble of the same turn rather
 * than only setting it on the last one, because a turn split by a mid-turn
 * system row flushes twice and the first flush has already attached one.
 */
function attachTurnMeta(items: RenderItem[], meta: TurnMeta | null): void {
  if (!meta) return
  let last: RenderItem | null = null
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i]
    if (item.kind === 'user') break
    if (item.kind !== 'assistant') continue
    if (last) delete item.meta
    else last = item
  }
  if (last && last.kind === 'assistant') last.meta = meta
}

const renderData = computed<{
  items: RenderItem[]
  liveSubs: SubagentTranscript[]
  liveStandaloneSubs: SubagentTranscript[]
}>(() => {
  const items: RenderItem[] = []
  let buffer: ChatMessage[] = []

  // Subagent transcripts grouped by the user turn that dispatched them.
  // Entries without a resolvable turn (older sessions, remote chats) attach
  // to the last turn so they stay visible.
  const subsByTurn = new Map<number, SubagentTranscript[]>()
  const unanchoredSubs: SubagentTranscript[] = []
  for (const sub of store.activeSubagents) {
    if (typeof sub.turn_index === 'number') {
      const list = subsByTurn.get(sub.turn_index) || []
      list.push(sub)
      subsByTurn.set(sub.turn_index, list)
    } else {
      unanchoredSubs.push(sub)
    }
  }
  let currentTurnIndex: number | null = null
  // The current turn's footer facts, carried across flushes of one turn.
  let turnMeta: TurnMeta | null = null

  const takeForegroundSubs = (turnIndex: number | null): SubagentTranscript[] => {
    if (turnIndex === null) return []
    const subs = subsByTurn.get(turnIndex)
    if (!subs?.length) return []
    subsByTurn.delete(turnIndex)
    return subs
  }

  const flushTurn = (isFinal = false) => {
    if (!buffer.length) return
    // Accumulated before any early return below, so a turn split across two
    // flushes still ends with one complete footer on its last bubble.
    turnMeta = turnMetaFrom(buffer, turnMeta)
    const turnOutputs = collectTraceOutputs(buffer)
    // Prefer the last non-progress assistant text as the final reply so Claude
    // "Now let me…" narration folds into Activity; fall back to the last text
    // so a short clarifying question still surfaces. Matches buildTurnParts.
    const finalIdx = findFinalAnswerIndex(buffer)
    const finalMsg = finalIdx >= 0 ? buffer[finalIdx] : null
    const trailing = finalIdx >= 0 ? buffer.slice(finalIdx + 1) : []

    // If the last assistant text is followed by `_thinking` blocks, the turn
    // was interrupted mid-thought. Fold everything into a trace so the user
    // doesn't see a standalone bubble that's actually mid-reasoning.
    //
    // BUT: trailing-only `_activity` (tool calls) does NOT mean the turn was
    // interrupted. A model commonly emits its final answer text and then runs
    // bookkeeping tools (TodoWrite, etc.) that produce no further user-facing
    // text. In that case the answer text is the real reply and must render as
    // a normal assistant bubble; the trailing tools just join the trace.

    const trailingHasThinking = trailing.some(m => m.tool_name === '_thinking')
    if (trailingHasThinking && finalMsg) {
      const traceSubs = takeForegroundSubs(currentTurnIndex)
      items.push(withKey({
        kind: 'trace',
        steps: buffer.slice(),
        turnIndex: currentTurnIndex ?? undefined,
        ...(traceSubs.length ? { subs: traceSubs } : {}),
        ...(turnOutputs.length ? { outputs: turnOutputs } : {}),
      }))
      buffer = []
      return
    }

    // The in-flight turn is already drawn by the live stream trace block
    // (`store.currentTimeline`), so drop its buffer rather than emitting a
    // second, static copy. Only the trailing turn is in flight — earlier turns
    // keep their trace even when they never produced an answer bubble (the
    // user hit Stop, or the turn ended in tool calls only).
    if (isFinal && store.isStreaming) {
      buffer = []
      return
    }

    const traceSubs = takeForegroundSubs(currentTurnIndex)
    // Substantive assistant text that appears BEFORE the final answer used to
    // be swallowed into the Activity trace (rendered italic, indistinguishable
    // from reasoning). Split the turn so each such block renders as its own
    // bubble, interleaved with the tool/thinking groups that ran between them,
    // in the order the model produced them. The final answer is appended below
    // with the turn's outputs attached.
    const turnItems: RenderItem[] = buildTurnParts(buffer, finalIdx).map((part) =>
      part.kind === 'assistant'
        ? withKey({ kind: 'assistant', msg: part.msg, turnIndex: currentTurnIndex ?? undefined })
        : withKey({ kind: 'trace', steps: part.steps, turnIndex: currentTurnIndex ?? undefined }),
    )
    // Foreground subagents and (when there's no answer bubble) file outputs
    // belong to the one Activity trace that sits right before the reply. Reuse
    // the trailing trace if there is one; otherwise mint an empty one so those
    // attachments still have a home adjacent to the answer.
    const needsHost =
      traceSubs.length > 0
      || (!finalMsg && turnOutputs.length > 0)
    const last = turnItems[turnItems.length - 1]
    let host = last && last.kind === 'trace' ? last : null
    if (!host && needsHost) {
      host = withKey({ kind: 'trace', steps: [], turnIndex: currentTurnIndex ?? undefined })
      turnItems.push(host)
    }
    if (host) {
      if (traceSubs.length) host.subs = traceSubs
      if (!finalMsg && turnOutputs.length) host.outputs = turnOutputs
    }
    for (const it of turnItems) items.push(it)
    if (finalMsg) {
      items.push(withKey({
        kind: 'assistant',
        msg: finalMsg,
        turnIndex: currentTurnIndex ?? undefined,
        ...(turnOutputs.length ? { outputs: turnOutputs } : {}),
      }))
    }
    attachTurnMeta(items, turnMeta)
    buffer = []
  }

  for (const msg of store.activeMessages) {
    if (msg.role === 'user') {
      flushTurn()
      currentTurnIndex = typeof msg.turn_index === 'number'
        ? msg.turn_index
        : currentTurnIndex === null ? 0 : currentTurnIndex + 1
      turnMeta = null
      items.push(withKey({ kind: 'user', msg, turnIndex: currentTurnIndex }))
    } else if (
      msg.role === 'system'
      && msg.tool_name !== '_activity'
      && msg.tool_name !== '_thinking'
      && msg.tool_name !== '_filecard'
    ) {
      flushTurn()
      items.push(withKey({ kind: 'system', msg }))
    } else {
      // assistant text, _activity tool block, _thinking note, or _filecard:
      // part of the current turn's trace.
      buffer.push(msg)
    }
  }
  flushTurn(true)
  // While streaming, subagents nest in the live trace.
  if (store.isStreaming) {
    const all = [...subsByTurn.values()].flat().concat(unanchoredSubs)
    return { items, liveSubs: all, liveStandaloneSubs: [] }
  }
  // Anything still unplaced (turn not in history yet, or no turn info):
  // attach anchored subagents to their actual user-turn's trace block so a
  // late-finishing agent doesn't drift to the most recent trace. Unanchored
  // leftovers still fall back to the last trace block, or a fresh block if
  // there is none.
  const leftovers = [...subsByTurn.values()].flat().concat(unanchoredSubs)
  if (leftovers.length) {
    const anchored = new Map<number, SubagentTranscript[]>()
    const unanchored: SubagentTranscript[] = []
    for (const sub of leftovers) {
      if (typeof sub.turn_index === 'number') {
        const list = anchored.get(sub.turn_index) || []
        list.push(sub)
        anchored.set(sub.turn_index, list)
      } else {
        unanchored.push(sub)
      }
    }

    // Attach each anchored group to the matching turn's trace block. If the
    // turn has no trace block (e.g. the model replied with plain text), mint
    // one right before the first assistant/system item of that turn.
    for (const [turnIdx, subs] of anchored) {
      let turnStart = -1
      for (let i = 0; i < items.length; i++) {
        const it = items[i]
        if (it.kind === 'user' && it.turnIndex === turnIdx) {
          turnStart = i
        }
      }
      if (turnStart === -1) {
        // Matching user turn isn't rendered yet; keep with unanchored fallback.
        unanchored.push(...subs)
        continue
      }
      let traceIdx = -1
      for (let i = turnStart + 1; i < items.length && items[i].kind !== 'user'; i++) {
        if (items[i].kind === 'trace') traceIdx = i
      }
      if (traceIdx >= 0) {
        const host = items[traceIdx]
        if (host.kind === 'trace') {
          host.subs = [...(host.subs || []), ...subs]
        }
      } else {
        let insertAt = turnStart + 1
        while (insertAt < items.length && items[insertAt].kind === 'user') insertAt++
        items.splice(insertAt, 0, withKey({ kind: 'trace', steps: [], subs, turnIndex: turnIdx }))
      }
    }

    if (unanchored.length) {
      let lastTrace: RenderItem | undefined
      for (let i = items.length - 1; i >= 0; i--) {
        if (items[i].kind === 'trace') { lastTrace = items[i]; break }
      }
      if (lastTrace && lastTrace.kind === 'trace') {
        lastTrace.subs = [...(lastTrace.subs || []), ...unanchored]
      } else {
        items.push(withKey({ kind: 'trace', steps: [], subs: unanchored }))
      }
    }
  }
  return { items: dedupeRenderItemKeys(items), liveSubs: [], liveStandaloneSubs: [] }
})

const renderItems = computed<RenderItem[]>(() => renderData.value.items)
const liveSubagents = computed<SubagentTranscript[]>(() => renderData.value.liveSubs)

// One consistent full-screen state for "this chat isn't ready to show yet" —
// previously this forked three ways depending on whether stale cached
// messages happened to already be in the store (full skeleton vs. a small
// "Updating conversation…" banner stacked above them) or whether the fetch
// ran with the loading flag suppressed at all (silent background refresh —
// see loadMessages' `background` option), which could leave an incomplete
// transcript on screen with no visible signal. Once a turn is actively
// streaming, the live Activity/typing UI is the better indicator, so that
// takes over instead of hiding the transcript.
const blockingHistoryLoad = computed(() => store.messageHistoryLoading && !store.isStreaming)

// Watcher: keep highlights in sync with the pending list and message DOM.
watch(
  () => [store.pendingChatComments.length, renderItems.value.length] as const,
  ([n]) => {
    if (n === 0) {
      commentBubbleById.clear()
      editingChatCommentId.value = null
    }
    nextTick(() => applyHighlights())
  },
  { flush: 'post' }
)

// Force-scroll to bottom when switching to a different chat.
watch(() => store.activeChatId, () => {
  isNearBottom.value = true
  // Both disclosures are per-chat state; carrying them across a switch meant a
  // chat you never expanded opened with its dock and context bar already open.
  dockExpanded.value = false
  contextExpanded.value = false
  nextTick(() => {
    if (messagesEl.value) messagesEl.value.scrollTop = messagesEl.value.scrollHeight
    // Hold the bottom while the incoming transcript's height settles, rather
    // than measuring it once against whatever was on screen at this instant
    // (usually the loading skeleton).
    pinToBottom()
  })
})

// The transcript replacing the loading skeleton is the moment the real height
// arrives, and it happens after the switch above has already scrolled. Pin
// again from there so the reveal itself lands at the bottom.
watch(blockingHistoryLoad, (loading, wasLoading) => {
  if (!loading && wasLoading) nextTick(() => pinToBottom())
})

// Auto-scroll only when the user is already near the bottom.
// Use direct scrollTop assignment instead of scrollIntoView — the latter
// can stop short of the absolute bottom with smooth scrolling, especially
// inside flex containers where the anchor is a zero-height child.
watch(
  () => [store.activeMessages.length, store.currentStreamingText, store.currentActivity.length, store.isStreaming],
  () => {
    nextTick(() => {
      stickToBottomIfNeeded()
      checkScroll()
    })
  },
  { deep: true }
)

function autoResize() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  // Floor at the shared touch target so an empty composer stays aligned
  // with the sidebar "+ New Project" row (both 44px inside 61px footers).
  const next = Math.min(Math.max(el.scrollHeight, 44), 200)
  el.style.height = next + 'px'
  const bar = el.closest('.input-bar')
  if (!bar) return
  const isTall = bar.classList.contains('tall')
  // Hysteresis: once tall, stay tall until the text shrinks by ~2 lines;
  // once short, stay short until it grows past the threshold. This stops
  // the buttons from flickering when typing hovers near the boundary.
  const enterTall = el.scrollHeight >= 120
  const leaveTall = el.scrollHeight < 80
  if (!isTall && enterTall) {
    bar.classList.add('tall')
  } else if (isTall && leaveTall) {
    bar.classList.remove('tall')
  }
}

function handleInput(): void {
  autoResize()
  refreshComposerPickers()
}

function handleKeydown(e: KeyboardEvent) {
  // Mention navigation uses the same keyboard-first picker contract as slash
  // commands, but only consumes keys while an @ token is active.
  if (mentionPicker.handleKeydown(e)) return

  // Slash-command picker navigation takes precedence over send/newline.
  if (showCommandsPicker.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      commandHighlightIdx.value = (commandHighlightIdx.value + 1) % filteredCommands.value.length
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      const n = filteredCommands.value.length
      commandHighlightIdx.value = (commandHighlightIdx.value - 1 + n) % n
      return
    }
    if (e.key === 'Escape') {
      e.preventDefault()
      // Claim the key: Esc now closes the chat even while typing, so without
      // this the same press would dismiss the picker AND close the chat.
      e.stopPropagation()
      // Dismiss the picker only — never the draft. The trigger is caret-local
      // now, so the picker opens mid-message ("…and then run /rev"): clearing
      // inputText here wiped the whole message, and the draft-sync watcher
      // persisted the empty string, so it could not be recovered.
      dismissSlashCommandPicker()
      return
    }
    if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey)) {
      e.preventDefault()
      const cmd = filteredCommands.value[commandHighlightIdx.value]
      if (cmd) applyCommand(cmd)
      return
    }
  }
  // Esc closes the chat from the composer. ChatLayout also binds Esc globally,
  // but that handler is gated on the route, so closing from here means it works
  // wherever the composer does. Losing the half-typed message is fine: drafts
  // are persisted per chat and restored when it reopens. The slash-command
  // picker above claims Esc first (and stops propagation) so it can dismiss
  // itself without also closing the chat.
  if (e.key === 'Escape') {
    e.preventDefault()
    // Claim it: ChatLayout's global handler would otherwise close the chat a
    // second time off the same press. Arrow keys already taught us what two
    // listeners on one key costs.
    e.stopPropagation()
    emit('close')
    return
  }

  // Recalling history is deliberately limited to the textarea's empty state
  // so ArrowUp/ArrowDown keep their normal cursor-navigation meaning while a
  // prompt is being edited. Once recall starts, the arrows walk that session's
  // bounded history and Down restores the draft that was present beforehand.
  if (handlePromptHistoryKey(e)) return

  // Cmd+Enter (mac) / Ctrl+Enter (linux/win) sends the message. Bare Enter
  // inserts a newline: this avoids accidental sends, especially on phones
  // where Enter is the default virtual-keyboard action. Mid-stream sends are
  // queued and flushed when the current turn finishes.
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault()
    send()
    return
  }
}

// Cmd/Ctrl+Enter offered by ChatLayout when the chord lands outside the
// composer. Attaching an image or a comment leaves focus on the control that
// closed, not the textarea, so the next press of the same chord -- the one that
// means "now send it" -- never reached handleKeydown, and a message that
// carried only attachments could not be sent from the keyboard at all. The
// caller has already screened out text fields, but the comment popovers'
// own Save/Cancel buttons are not text fields either -- while one is
// focused there, this must still decline so the button's own Enter
// activation runs instead of sending the unrelated composer draft.
function handleSendShortcut(): boolean {
  if (chat.value.archived || !canSend.value) return false
  if (commentDraft.value || editingChatCommentId.value) return false
  send()
  return true
}

function handleInputFocus() {
  refreshComposerPickers()
  if (window.innerWidth < 768 && messagesEl.value) {
    // Wait for the keyboard animation, then scroll messages to the bottom
    // so the latest content sits above the input. Use direct scrollTop
    // assignment instead of scrollIntoView, which can trigger unwanted
    // page-level scroll on iOS and conflict with visualViewport sizing.
    setTimeout(() => {
      const el = messagesEl.value
      if (el) el.scrollTop = el.scrollHeight
    }, 300)
  }
}

function send() {
  if (chat.value.archived) return
  const text = inputText.value.trim()
  const hasAttachments = store.pendingImages.length > 0 || store.pendingComments.length > 0 || store.pendingChatComments.length > 0
  if (!text && !hasAttachments) return
  if (text) recordSentPrompt(chat.value.chat_id, inputText.value)
  // Always "queue": when a response is in flight the backend buffers and
  // flushes on turn end; for a fresh turn this starts it.
  let sendText = text
  if (!text && !store.pendingComments.length && !store.pendingChatComments.length) {
    // Images-only with no text and no comments: emit numbered references so
    // the user bubble has content and the model gets explicit image indices.
    sendText = store.pendingImages.map((_, i) => `[Image ${i + 1}]`).join(' ')
  }
  // When any comments exist and there is no typed text, sendMessage builds
  // the composed content from the comment blocks, so we pass an empty string
  // here. The user sees the actual content in their bubble, not a placeholder.
  const sent = store.sendMessage(chat.value.chat_id, sendText, undefined, () => {
    writeChatDraft(chat.value.chat_id, '')
    inputText.value = ''
  })
  // If the send was deferred (chat WS is down), keep the text in the composer
  // and draft so the user doesn't lose it when the page updates/reloads.
  if (!sent) return
  // Sending implies following the reply: jump to the bottom even if the
  // user had scrolled up, so their bubble and the response are in view.
  // Double nextTick + rAF: the user bubble and streaming row render after
  // the first paint, so one-shot scroll can land above the true bottom.
  nextTick(() => {
    scrollToBottom()
    autoResize()
    nextTick(() => {
      scrollToBottom()
      requestAnimationFrame(() => scrollToBottom())
    })
  })
}



// Retry support: error messages are system bubbles whose content starts
// with "Error:" (set in stores/projects.ts error-event handler). If the
// prior user turn is still in the timeline, we can resend its text plus
// any images it carried — without draining the live composer.
function isErrorMsg(content: string): boolean {
  return typeof content === 'string' && content.startsWith('Error:')
}
function lastUserBefore(errorIdx: number): { text: string; images: string[] } | null {
  const items = renderItems.value
  for (let k = errorIdx - 1; k >= 0; k--) {
    const it = items[k]
    if (it.kind === 'user') {
      const images = Array.isArray(it.msg.images) ? [...it.msg.images] : []
      return { text: it.msg.content, images }
    }
  }
  return null
}
function retryFromError(errorIdx: number) {
  if (chat.value.archived) return
  const prior = lastUserBefore(errorIdx)
  if (!prior) return
  // Build a PreparedMessage so sendMessage sends the prior turn's image
  // refs alongside the text. We bypass prepareMessage (which would pull
  // from the live composer) and pass `prepared` to skip the
  // consumePreparedAttachments drain — a retry must not erase whatever
  // the user has currently staged for a fresh send.
  store.sendMessage(
    chat.value.chat_id,
    prior.text,
    { composed: prior.text, imageRefs: prior.images.length ? prior.images : undefined, fileComments: [], chatComments: [] },
  )
}

// Open a fresh chat in the General project seeded with this error + the last
// user turn, asking the agent to diagnose and fix it (or file a GitHub issue
// if the bug is in Ciaobot itself).
async function openFixChat(errorIdx: number) {
  const it = renderItems.value[errorIdx]
  const errorText = it && 'msg' in it ? it.msg.content : ''
  const prior = lastUserBefore(errorIdx)
  const context = prior ? prior.text : undefined
  try {
    await store.fixError({ errorText, context })
  } catch (e) {
    store.pushErrorToast('Could not open fix chat', `${(e as Error)?.message || e}`)
  }
}

function formatRetryTime(value: string): string {
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  return formatTime(d.toISOString())
}

async function tryRetryNow() {
  if (!chat.value || store.isStreaming) return
  await store.tryChatRetryNow(chat.value.chat_id)
}

async function stopRetry() {
  if (!chat.value) return
  await store.stopChatRetry(chat.value.chat_id)
}

function openHandoverPicker() {
  if (store.isStreaming) return
  showModelPicker.value = true
}

function startEditTitle() { titleValue.value = chat.value.title; editingTitle.value = true }
async function saveTitle() {
  if (titleValue.value.trim() && titleValue.value !== chat.value.title) {
    await store.renameChat(chat.value.chat_id, titleValue.value.trim())
  }
  editingTitle.value = false
}
function toggleModelPicker() {
  showModelPicker.value = !showModelPicker.value
}

function tierAlias(model: string): TierAlias | null {
  const normalized = model.trim().toLowerCase()
  return normalized === 'haiku' || normalized === 'sonnet' || normalized === 'opus' || normalized === 'fable'
    ? normalized
    : null
}

function canonicalTier(model: string): string {
  const alias = tierAlias(model)
  if (alias) return alias
  return model
}

// Render the vendor behind the chat as the operator in the brain chip. Falls
// back to the CLI provider when no bucket is set, so a chat in the legacy /
// auto state still shows something sensible instead of going blank.
function routingBucketLabel(bucket: string | undefined, provider: string): string {
  if (!bucket) return provider
  if (bucket === 'claude') return 'anthropic'
  return bucket
}

// Vendor name for the header chip, from the chat's provider.
function routingProviderLabel(bucket: string | undefined, provider: string): string {
  const lower = routingBucketLabel(bucket, provider)
  if (!lower) return ''
  if (lower === 'anthropic') return 'Anthropic'
  // Lower-case on purpose: that is how opencode brands itself.
  if (lower === 'opencode') return 'opencode'
  if (lower === 'claude') return 'Claude'
  return lower.charAt(0).toUpperCase() + lower.slice(1)
}

function bucketLabel(bucket: BucketKey): string {
  return BUCKET_DEFS.find((def) => def.key === bucket)?.label || 'Claude'
}

// Which provider owns a model id. Membership in a provider's catalog is
// authoritative; the `provider/model` shape is the fallback for an opencode id
// the catalog has not reported yet.
function bucketForSelectedModel(model: string): BucketKey {
  const response = modelsResponse.value
  if ((response?.opencode_models || []).includes(model)) return 'opencode'
  if (model.includes('/')) return 'opencode'
  return 'claude'
}


async function selectModel(value: string | string[], sectionKey = '') {
  const model = Array.isArray(value) ? value[0] : value
  if (!model || !chat.value) {
    showModelPicker.value = false
    return
  }
  const sectionBucket: Partial<Record<string, BucketKey>> = {
    anthropic: 'claude',
    opencode: 'opencode',
  }
  // Picking from the Anthropic section is an explicit handover to Claude Code,
  // never a tier change on whichever provider is active. To change tier while
  // staying on a provider, pick that provider's own model instead.
  const targetBucket = sectionBucket[sectionKey] || bucketForSelectedModel(model)
  const currentEntry = canonicalTier(chat.value.model)
  const sameModelAndRoute = canonicalTier(model) === currentEntry && targetBucket === activeBucket.value
  if (sameModelAndRoute) {
    showModelPicker.value = false
    return
  }
  // A handover is needed exactly when the provider changes: each runs its own
  // CLI with its own session, so the new one has never seen this chat.
  const targetRoute = targetBucket
  const currentRoute = activeBucket.value
  const updates: {
    provider: ProviderKey
    model: string
    thinking_level?: string
  } = {
    provider: (BUCKET_DEFS.find(def => def.key === targetBucket)?.provider || 'claude') as ProviderKey,
    model,
  }
  const targetLevels = modelsResponse.value?.model_reasoning_levels?.[model]
  if (
    chat.value.thinking_level
    && targetLevels
    && !targetLevels.includes(chat.value.thinking_level)
  ) {
    updates.thinking_level = ''
  }
  if (bucketLocked.value && targetRoute !== currentRoute) {
    const ok = await askConfirm(
      `Hand over this chat to ${bucketLabel(targetBucket)} / ${model}? The same visible chat will continue with a fresh provider session.`,
      {
        title: 'Hand over chat',
        confirmLabel: 'Hand over',
      },
    )
    if (!ok) return
    // Picking a model from the pending-retry card's "Continue with..." is
    // the whole point of switching model there: fire the retry immediately
    // on the new provider instead of leaving it parked for a manual "Try now".
    const hadPendingRetry = chat.value.retry?.status === 'pending'
    const chatId = chat.value.chat_id
    await store.handoverChat(chatId, updates)
    showModelPicker.value = false
    if (hadPendingRetry) {
      await store.tryChatRetryNow(chatId)
    }
    return
  }
  await store.updateChat(chat.value.chat_id, updates)
  showModelPicker.value = false
}

async function selectThinking(level: string) {
  // '' = provider default. Safe mid-chat: it never invalidates the
  // provider session, so no handover is involved.
  await store.updateChat(chat.value.chat_id, { thinking_level: level })
  showModelPicker.value = false
}

/* Close picker on click outside or Escape */
watch(showModelPicker, (open) => {
  if (!open) {
    // A capability-card "Open picker" filtered the sections; drop the filter
    // so the next normal open shows the full list again.
    capabilityPickerSection.value = ''
    return
  }
  const clickHandler = (e: MouseEvent) => {
    if (modelPickerRef.value && !modelPickerRef.value.contains(e.target as Node)) {
      showModelPicker.value = false
    }
  }
  const keyHandler = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      showModelPicker.value = false
    }
  }
  setTimeout(() => {
    window.addEventListener('click', clickHandler, { once: true })
    window.addEventListener('keydown', keyHandler, { once: true })
  }, 0)
})

async function doArchive() {
  if (!await askConfirm(ARCHIVE_CONFIRM_MESSAGE, {
    title: 'Archive chat',
    confirmLabel: 'Archive',
  })) return
  try {
    await store.archiveChat(chat.value.chat_id)
  } catch {
    // archiveChat already reconnected the sockets and raised an error toast.
    // Keep the pane open so the still-live chat stays reachable.
    return
  }
  // Notify ChatLayout so it closes the chat pane.
  emit('close')
}

async function continueChat() {
  if (!chat.value) return
  isContinuing.value = true
  try {
    await store.continueArchivedChat(chat.value.chat_id)
  } catch (e) {
    console.error('Failed to continue archived chat:', e)
    store.pushErrorToast('Could not continue chat', `${errorMessage(e)}`)
  } finally {
    isContinuing.value = false
  }
}
async function handleVoice(blob: Blob) {
  transcribing.value = true
  try {
    const text = await store.transcribeVoice(chat.value.chat_id, blob)
    if (text.trim()) {
      inputText.value = text
      nextTick(autoResize)
      inputEl.value?.focus()
    }
  } catch (e) {
    console.error('Voice error:', e)
    store.pushErrorToast('Voice transcription failed', `${errorMessage(e)}`)
  } finally {
    transcribing.value = false
  }
}

function handleVoiceError(message: string) {
  store.pushErrorToast('Voice dictation unavailable', message)
}
async function handleFileSelect(e: Event) { const input = e.target as HTMLInputElement; if (!input.files?.length) return; await store.uploadImages(chat.value.chat_id, Array.from(input.files)); input.value = '' }

type DroppedProjectFile = {
  path: string
  vault_path: string
  absolute_path?: string
  original_path?: string | null
  markdown_path?: string | null
}

type ProjectUploadResult = {
  saved?: DroppedProjectFile[]
  errors?: { filename: string; error: string }[]
  error?: string
}

type NativeFileDropDetail = {
  grantId?: string
  paths?: string[]
  error?: string
}

type NativeFileDropResult = {
  paths?: string[]
  attachments?: { original_path?: string | null; markdown_path?: string | null }[]
  image_refs?: string[]
  errors?: { filename: string; error: string }[]
  error?: string
}

async function importNativeFileDrop(detail: NativeFileDropDetail): Promise<void> {
  dragOver.value = false
  if (detail.error || !detail.grantId) {
    store.pushErrorToast(
      'Could not attach file',
      detail.error || 'The native file-drop grant was missing.',
    )
    return
  }
  try {
    const response = await fetch('/api/desktop-drop', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        grant_id: detail.grantId,
        project_id: project.value?.project_id || '',
        chat_id: chat.value.chat_id,
      }),
    })
    const result = await response.json().catch(() => ({})) as NativeFileDropResult
    if (!response.ok) {
      throw new Error(result.error || `Native file import failed (HTTP ${response.status})`)
    }
    for (const failure of result.errors || []) {
      store.pushErrorToast(`Could not attach ${failure.filename}`, failure.error)
    }
    const paths = (result.attachments || []).flatMap((entry) =>
      [entry.original_path, entry.markdown_path].filter((path): path is string => Boolean(path)))
    paths.push(...(result.paths || []))
    if (paths.length) {
      insertTextAtCursor(paths.map(formatAttachedFilePath).join(' '))
    }
    store.addPendingImageRefs(chat.value.chat_id, result.image_refs || [])
  } catch (error) {
    store.pushErrorToast(
      'Could not attach file',
      error instanceof Error ? error.message : String(error),
    )
  }
}

function handleNativeFileDragEnter(): void {
  dragOver.value = true
}

function handleNativeFileDragLeave(): void {
  dragOver.value = false
}

function handleNativeFileDrop(event: Event): void {
  const detail = (event as CustomEvent<NativeFileDropDetail>).detail || {}
  void importNativeFileDrop(detail)
}

async function localDropNeedsUpload(): Promise<boolean> {
  try {
    // This endpoint is deliberately handled by the local node instead of the
    // client proxy, so it reveals whether the browser and agent are on
    // different computers.
    const response = await fetch('/api/startup-status', {
      credentials: 'same-origin',
    })
    if (!response.ok) return true
    const role = String((await response.json()).node_role || '')
    return role === 'client' || role === 'standby'
  } catch {
    // Uploading is the safe fallback: a local-only path would be unusable if
    // this browser turns out to be connected to a remote host.
    return true
  }
}

async function uploadDroppedProjectFiles(files: File[]): Promise<string[]> {
  if (!project.value?.vault_folder) {
    throw new Error('This project has no folder for uploaded files.')
  }
  const form = new FormData()
  files.forEach((file, index) => form.append(`file${index}`, file, file.name))
  const response = await fetch(`/api/chats/${chat.value.chat_id}/attachments`, {
    method: 'POST',
    credentials: 'same-origin',
    body: form,
  })
  const result = await response.json().catch(() => ({})) as ProjectUploadResult
  if (!response.ok) {
    throw new Error(result.error || `Upload failed (HTTP ${response.status})`)
  }
  for (const failure of result.errors || []) {
    store.pushErrorToast(`Could not attach ${failure.filename}`, failure.error)
  }
  return (result.saved || []).flatMap((file) => {
    const paths = [file.original_path, file.markdown_path]
    return (paths.some(Boolean) ? paths : [file.absolute_path || file.vault_path])
      .filter((path): path is string => Boolean(path))
  })
}

async function handleDrop(e: DragEvent) {
  dragOver.value = false
  const dt = e.dataTransfer
  if (!dt) return

  // Capture DataTransfer contents synchronously; browsers may invalidate the
  // drag store once this event handler yields to the startup-status request.
  const files: File[] = []
  const folders: { name: string; file: File | null }[] = []
  const items = Array.from(dt.items || [])
  if (items.length) {
    for (const item of items) {
      if (item.kind !== 'file') continue
      const entry = (item as DataTransferItem & {
        webkitGetAsEntry?: () => { isDirectory?: boolean; name?: string } | null
      }).webkitGetAsEntry?.()
      if (entry?.isDirectory) {
        folders.push({ name: entry.name || 'folder', file: item.getAsFile() })
        continue
      }
      const file = item.getAsFile()
      if (file) files.push(file)
    }
  } else {
    files.push(...Array.from(dt.files || []))
  }

  const imageFiles = files.filter(file => file.type.startsWith('image/'))
  const regularFiles = files.filter(file => !file.type.startsWith('image/'))
  const paths: string[] = []
  const needsUpload = regularFiles.length || folders.length
    ? await localDropNeedsUpload()
    : false

  const unavailableFolders: string[] = []
  for (const folder of folders) {
    const nativePath = needsUpload || !folder.file
      ? null
      : nativeAbsoluteFilePath(folder.file)
    if (nativePath) paths.push(nativePath)
    else unavailableFolders.push(folder.name)
  }
  if (unavailableFolders.length) {
    store.pushErrorToast(
      'Could not attach folder',
      'Drop individual files instead; remote clients and sandboxed browsers cannot expose an absolute folder path.',
    )
  }

  if (regularFiles.length) {
    const uploadFiles: File[] = []
    for (const file of regularFiles) {
      const nativePath = needsUpload ? null : nativeAbsoluteFilePath(file)
      if (nativePath) paths.push(nativePath)
      else uploadFiles.push(file)
    }
    if (uploadFiles.length) {
      try {
        paths.push(...await uploadDroppedProjectFiles(uploadFiles))
      } catch (error) {
        store.pushErrorToast(
          'Could not attach file',
          error instanceof Error ? error.message : String(error),
        )
      }
    }
  }

  if (paths.length) {
    insertTextAtCursor(paths.map(formatAttachedFilePath).join(' '))
  }
  if (imageFiles.length) await store.uploadImages(chat.value.chat_id, imageFiles)
}
async function handlePaste(e: ClipboardEvent) { const items = Array.from(e.clipboardData?.items || []).filter(i => i.type.startsWith('image/')); if (items.length) { e.preventDefault(); await store.uploadImages(chat.value.chat_id, items.map(i => i.getAsFile()).filter(Boolean) as File[]) } }
function removePendingImage(index: number) { store.removePendingImage(index) }

function insertTextAtCursor(token: string) {
  const el = inputEl.value
  if (!el) return
  const start = el.selectionStart ?? 0
  const end = el.selectionEnd ?? 0
  const before = inputText.value.slice(0, start)
  const after = inputText.value.slice(end)
  // Add a leading space if we're appending to existing text and the token
  // isn't at the start or already preceded by whitespace.
  const prefix = start > 0 && !/\s$/.test(before) ? ' ' : ''
  // Add a trailing space so the user can keep typing.
  const suffix = ' '
  inputText.value = before + prefix + token + suffix + after
  nextTick(() => {
    const pos = start + prefix.length + token.length + suffix.length
    el.selectionStart = el.selectionEnd = pos
    el.focus()
  })
}

function insertImageRef(n: number) {
  insertTextAtCursor(`[Image ${n}]`)
}

// Cmd+D toggles a voice recording from the composer: first press starts,
// second press stops (same as the on-screen mic/stop button).
// When a comment compose popover is open, the shortcut is routed there instead
// of the main chat composer.
function toggleDictation() {
  if (commentComposeDraftRef.value) {
    commentComposeDraftRef.value.toggleDictation()
    return
  }
  if (commentComposeEditRef.value) {
    commentComposeEditRef.value.toggleDictation()
    return
  }
  voiceRecorderRef.value?.toggleRecording()
}

// Cmd+Backspace mirrors the header archive button (including its confirm
// dialog). Fires even while a text field is focused: that is the point of the
// binding, and the confirm dialog is what makes it safe.
function archiveActiveChat() {
  if (!chat.value || chat.value.archived) return
  void doArchive()
}

// Expose app-level shortcuts to the layout, which owns the global keydown.
defineExpose({ toggleDictation, toggleModelPicker, archiveActiveChat, handleQuestionShortcut, handlePermissionShortcut, handleSendShortcut })
</script>

<style scoped>
.chat-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
  min-height: 0;
}

.drop-overlay {
  position: absolute;
  inset: 0;
  background: rgba(233, 69, 96, 0.1);
  border: 2px dashed var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent);
  font-size: 18px;
  z-index: 10;
  pointer-events: none;
}

/* Scroll-to-bottom float button — centered inside .chat-with-sidebar
   (which ends at the top of the composer) so bottom:12px stays 12px
   above the composer even when the textarea expands. Previously it was
   absolute to .chat-panel at bottom:72px and was overlapped by a
   tall composer. */
.scroll-to-bottom-btn {
  position: absolute;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: var(--bg3);
  color: var(--fg);
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.3);
  z-index: 10;
  transition: opacity 0.2s ease, transform 0.15s ease;
}
.scroll-to-bottom-btn:hover {
  background: var(--bg2);
  border-color: var(--fg2);
}
.scroll-to-bottom-btn:active {
  transform: translateX(-50%) scale(0.92);
}

/* Header */
/* Doubled class = specificity (0,2,0), so these beat a plain single-class
   `display` on the same element regardless of source order. Written as one
   class they lost to .model-picker-btn / .model-picker-summary further down the
   stylesheet (equal specificity, later wins), which showed the brain button on
   desktop and the summary pill on mobile — both at once. */
.desktop-only.desktop-only { display: inline-flex; }
.mobile-only.mobile-only { display: none; }
@media (max-width: 768px) {
  .desktop-only.desktop-only { display: none; }
  .mobile-only.mobile-only { display: inline-flex; }
}

.header-left {
  display: flex;
  /* The close button is a 30px box, the breadcrumb is a one- or two-line block
     of text: centring the two keeps the icon on the optical middle of whichever
     the breadcrumb turns out to be. */
  align-items: center;
  gap: 4px;
  min-width: 0;
  text-align: left;
}

/* Sizing, hover fill and touch padding all come from PaneHeader's shared
   .btn-icon rules, so this only sets the resting colour: the same quiet --fg3
   as the trailing actions, since closing the chat is not the errand you came
   here for. */
.close-btn { color: var(--fg3); }
.close-btn:hover { color: var(--fg); }

.header-breadcrumb {
  display: flex;
  align-items: center;
  column-gap: 6px;
  min-width: 0;
  flex: 1;
  position: relative;
  /* Two rows: the scope claims a full-width line, the title takes the next. */
  flex-wrap: wrap;
  row-gap: 0;
}

/* Workspace and project together: where this chat lives, as a quiet eyebrow
   above the title rather than a third of one shared line. Sharing the line cost
   the title most of its width — this is the densest header in the app, and a
   chat title is the one string in it nothing else can stand in for, so it was
   the string that ellipsed. Stacked, the title gets the full row and the scope
   still reads as scope, now by size and colour instead of by position.
   Not a flex row itself but inline text, so the whole scope ellipses as one
   string when the header runs out of room instead of each crumb truncating on
   its own. */
.breadcrumb-scope {
  flex: 1 1 100%;
  font-size: var(--text-xs);
  line-height: 1.35;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* No divider between scope and title: they no longer share a line, and the
   line break is the separator. */

/* Hue is workspace identity, so the crumb is tinted by data-workspace-color
   rather than inheriting whatever the active accent happens to be. */
.breadcrumb-workspace {
  color: var(--accent);
}

@media (max-width: 600px) {
  /* The chat title is what matters on a phone; the crumb is orientation. */
  .breadcrumb-workspace { display: none; }
  .breadcrumb-workspace + .breadcrumb-separator { display: none; }
  /* A chat in the implicit General project has no project crumb, so hiding the
     workspace empties the scope. Drop the wrapper rather than leave a blank
     eyebrow line above the title. */
  .breadcrumb-scope:not(:has(.breadcrumb-project)) {
    display: none;
  }
}

.breadcrumb-project {
  color: var(--fg2);
  cursor: pointer;
  transition: color 120ms var(--ease);
}

.breadcrumb-project:hover {
  color: var(--accent);
}

.breadcrumb-separator {
  color: var(--fg3);
  user-select: none;
  /* Inside the scope the separator is inline text, so it needs its own breathing
     room; the one that joins the scope to the title is a flex item and gets it
     from the row's column-gap. */
  margin: 0 0.3em;
  flex-shrink: 0;
}
/* Compact project context popup, positioned below the breadcrumb.
   Replaces the old inline panel that pushed messages down. */
.context-popup {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  z-index: 100;
  min-width: 280px;
  max-width: 360px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  padding: 12px;
}

.context-popup-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 360px;
  overflow-y: auto;
}

.context-popup-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.context-popup .context-description {
  font-size: var(--text-sm);
  color: var(--fg2);
  line-height: 1.45;
  margin: 0;
  white-space: pre-wrap;
}

.context-popup .context-textarea {
  width: 100%;
  resize: vertical;
  font-size: var(--text-sm);
  padding: 6px 8px;
  min-height: 60px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--fg);
  font-family: var(--font);
}

.context-popup .context-edit-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.context-popup .context-status {
  font-size: var(--text-xs);
  color: var(--fg2);
}
.context-popup .context-status.saved { color: var(--success); }
.context-popup .context-status.error { color: var(--error); }

.context-popup .context-files-status {
  font-size: 12px;
  color: var(--fg2);
  padding: 4px 0;
}
.context-popup .context-files-status.error { color: var(--error); }

.context-popup .context-files-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
  font-size: 12px;
  padding-right: 4px;
}
.context-popup .context-file-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 6px;
  border-radius: 4px;
  cursor: pointer;
  color: var(--fg);
}
.context-popup .context-file-row:hover {
  background: var(--bg);
}
.context-popup .context-file-icon {
  flex-shrink: 0;
  font-size: 12px;
  width: 14px;
  text-align: center;
}
.context-popup .context-file-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 768px) {
  .context-popup {
    left: 0;
    right: auto;
    min-width: 260px;
    max-width: calc(100vw - 24px);
  }
}

.chat-title {
  cursor: pointer;
}

.title-input {
  font-weight: 600;
  /* Same step as the title it replaces, so renaming does not resize the text
     under the cursor. The literal 14px also ignored the font-scale setting. */
  font-size: var(--text-lg);
  /* Its own row, like the title it replaces, so renaming does not pull the
     title back up beside the scope. */
  align-self: center;
  background: var(--bg);
  border: 1px solid var(--accent);
  border-radius: 4px;
  color: var(--fg);
  padding: 2px 6px;
  font-family: var(--font);
  flex: 1 1 100%;
  min-width: 120px;
  width: 100%;
  box-sizing: border-box;
}

/* Messages: outer scroll container; inner content uses min-height:100% +
   flex-end so short chats sit above the composer without breaking scroll. */
.messages {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
  padding: 12px calc(12px + var(--safe-right)) 20px calc(12px + var(--safe-left));
  min-height: 0;
  position: relative;
}
.messages-content {
  display: flex;
  flex-direction: column;
  gap: 8px;
  /* Pin the transcript to the bottom when it's shorter than the viewport, but
     collapse to 0 and scroll normally when it overflows. `margin-top: auto`
     is resolution-independent — unlike `min-height: 100%`, which resolved
     against `.messages` (no explicit height) to 0 in some engines and left
     short/streaming chats stuck at the top with dead space below. */
  margin-top: auto;
}

/* The stack sits where the transcript sits: bottom-pinned, full width, same
   8px row gap as .messages-content, so the rows the skeleton draws are in the
   place the real rows will occupy. */
.history-skeleton-stack {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  margin-top: auto;
  animation: history-loading-enter 220ms ease-out both;
}

/* Geometry copied from .message.user / .message.assistant deliberately: the
   whole point is that the placeholder occupies the same box as the row that
   replaces it. */
.skel-msg {
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 12px 14px;
  min-width: 0;
}

.skel-msg--assistant {
  align-self: stretch;
  margin-right: 48px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-left: 3px solid color-mix(in srgb, var(--accent) 45%, transparent);
  border-radius: 4px 14px 14px 14px;
}

.skel-msg--user {
  align-self: flex-end;
  width: min(62%, calc(100% - 48px));
  margin-left: 48px;
  background: color-mix(in srgb, var(--accent2) 12%, var(--bg3));
  border: 1px solid var(--border-strong);
  border-radius: 14px 14px 2px 14px;
}

:root.theme-light .skel-msg--user {
  background: color-mix(in srgb, var(--accent2) 8%, var(--bg3));
  border-color: var(--border);
}

/* Matches a collapsed trace row: dashed, 98% wide, accent2 edge. */
.skel-trace {
  align-self: flex-start;
  display: flex;
  align-items: center;
  gap: 8px;
  width: 98%;
  padding: 9px 12px;
  border: 1px dashed var(--border);
  border-left: 3px solid color-mix(in srgb, var(--accent2) 45%, transparent);
  border-radius: var(--radius);
  opacity: 0.85;
}

.skel-trace-chevron {
  color: var(--fg3);
  font-size: 9px;
  line-height: 1;
}

@media (hover: hover) and (pointer: fine) {
  .skel-msg--assistant { margin-right: 32px; }
  .skel-msg--user { margin-left: 32px; }
}

/* history-loading-inline / spinner removed — skeleton is self-explanatory */

.history-skeleton-line {
  display: block;
  height: 10px;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--bg2) 0%, var(--bg3) 50%, var(--bg2) 100%);
  background-size: 200% 100%;
  animation: history-skeleton-sweep 1.4s ease-in-out infinite;
}

.history-skeleton-line--wide { width: 84%; }
.history-skeleton-line--long { width: 92%; }
.history-skeleton-line--medium { width: 68%; }
.history-skeleton-line--short { width: 42%; }
.history-skeleton-line--trace { width: 96px; height: 9px; }

.history-loading-enter-active,
.history-loading-leave-active {
  transition: opacity 180ms ease, transform 180ms ease;
}

.history-loading-enter-from,
.history-loading-leave-to {
  opacity: 0;
  transform: translateY(6px);
}

@keyframes history-loading-enter {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}

@keyframes history-loading-spin {
  to { transform: rotate(360deg); }
}

@keyframes history-skeleton-sweep {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}

@media (prefers-reduced-motion: reduce) {
  .history-skeleton-stack,
  .history-skeleton-line {
    animation: none;
  }
  .history-skeleton-stack { opacity: 0.9; }
}

.message-wrap {
  display: flex;
  flex-direction: column;
  width: 100%;
  max-width: 100%;
  min-width: 0;
}

.message-wrap.user {
  align-self: flex-end;
}

.message-wrap.assistant {
  align-self: flex-start;
}

.message-row {
  display: flex;
  align-items: flex-start;
  gap: 2px;
  min-width: 0;
  width: 100%;
  max-width: 100%;
  position: relative;
}

.message-wrap.user .message-row {
  flex-direction: row-reverse;
}

.message {
  flex: 1;
  max-width: 100%;
  padding: 10px 14px;
  font-size: var(--text-base);
  line-height: 1.5;
  word-break: break-word;
  min-width: 0;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.message.user {
  background: color-mix(in srgb, var(--accent2) 12%, var(--bg3));
  border: 1px solid var(--border-strong);
  border-radius: 14px 14px 2px 14px;
  color: var(--fg);
  margin-left: 48px;
}

:root.theme-light .message.user {
  background: color-mix(in srgb, var(--accent2) 8%, var(--bg3));
  border-color: var(--border);
}

.message.assistant {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 4px 14px 14px 14px;
  line-height: 1.6;
  margin-right: 48px;
}

@media (hover: hover) and (pointer: fine) {
  .message.user {
    margin-left: 32px;
  }
  .message.assistant {
    margin-right: 32px;
  }
}

.message-actions {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  gap: 4px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s;
  position: absolute;
  bottom: 0;
}

.message-wrap.user .message-actions {
  left: 2px;
}

.message-wrap.assistant .message-actions {
  right: 2px;
}

@media (hover: hover) and (pointer: fine) {
  .message-wrap.user .message-actions {
    left: 4px;
  }
  .message-wrap.assistant .message-actions {
    right: 4px;
  }
}

.message-wrap:focus-within .message-actions,
.message-wrap.actions-tapped .message-actions {
  opacity: 1;
  pointer-events: auto;
}

@media (hover: hover) {
  .message-wrap:hover .message-actions {
    opacity: 1;
    pointer-events: auto;
  }
}

.message-action-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: content-box;
  --message-action-visual: 28px;
  width: var(--message-action-visual);
  height: var(--message-action-visual);
  min-width: var(--message-action-visual);
  min-height: var(--message-action-visual);
  padding: calc((var(--touch, 44px) - var(--message-action-visual)) / 2);
  margin: calc((var(--message-action-visual) - var(--touch, 44px)) / 2);
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--fg2);
  cursor: pointer;
  position: relative;
  isolation: isolate;
  transition: color 0.12s;
}

.message-action-btn::before {
  content: '';
  position: absolute;
  inset: calc((var(--touch, 44px) - var(--message-action-visual)) / 2);
  z-index: -1;
  border-radius: 6px;
  background: transparent;
  pointer-events: none;
  transition: background 0.12s;
}

.message-action-btn svg {
  width: 14px;
  height: 14px;
}

.message-action-btn:hover {
  color: var(--fg);
}

.message-action-btn:hover::before {
  background: color-mix(in srgb, var(--fg) 8%, transparent);
}

.message-action-btn:active {
  transform: scale(0.95);
}

@media (hover: hover) and (pointer: fine) {
  .message-action-btn {
    --message-action-visual: 24px;
    padding: 0;
    margin: 0;
  }

  .message-action-btn::before {
    inset: 0;
  }
}

.message-action-btn--busy {
  animation: speak-pulse 1s ease-in-out infinite;
}

@keyframes speak-pulse {
  50% { opacity: 0.35; }
}

.speak-error {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--error);
}

.message.assistant.error {
  border-color: var(--error);
}

.message.streaming {
  border-color: var(--accent);
}

.message.system {
  align-self: center;
  color: var(--fg2);
  font-size: var(--text-sm);
  width: 98%;
  max-width: 98%;
}

.retry-btn {
  margin-top: 6px;
  padding: 4px 12px;
  background: var(--bg3);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-family: var(--font);
  font-size: 11px;
  cursor: pointer;
}
.retry-btn:hover { background: var(--border-strong); border-color: var(--fg2); }
.retry-btn:active { transform: scale(0.97); }
.error-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.error-attribution {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: baseline;
  margin-top: 8px;
  color: var(--fg2);
  font-size: var(--text-xs);
  line-height: 1.4;
}

.error-attribution-label {
  color: var(--error);
  font-weight: 600;
}
.fix-btn { color: var(--accent); border-color: var(--accent); }
.fix-btn:hover { background: var(--accent); color: var(--bg); border-color: var(--accent); }

.host-connection-card {
  align-self: center;
  width: min(680px, 90%);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3);
  background: rgba(255, 152, 0, 0.08);
  border: 1px solid rgba(255, 152, 0, 0.34);
  border-radius: var(--radius);
  color: var(--fg);
}

.host-connection-main {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  min-width: 0;
}

.host-connection-spinner {
  width: 14px;
  height: 14px;
  margin-top: 2px;
  flex: 0 0 auto;
  border: 2px solid rgba(255, 152, 0, 0.28);
  border-top-color: var(--warning);
  border-radius: 50%;
  animation: host-connection-spin 0.9s linear infinite;
}

@keyframes host-connection-spin {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .host-connection-spinner {
    animation: none;
    border-color: var(--warning);
  }
}

.host-connection-title {
  font-size: var(--text-sm);
  font-weight: 700;
}

.host-connection-meta,
.host-connection-error {
  margin-top: 2px;
  color: var(--fg2);
  font-size: var(--text-xs);
}

.host-connection-error {
  color: var(--error);
}

.host-connection-action {
  min-height: var(--touch);
  flex: 0 0 auto;
  color: var(--warning);
  border-color: var(--warning);
}

.retry-card {
  align-self: center;
  width: min(680px, 90%);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3);
  background: rgba(255, 193, 7, 0.08);
  border: 1px solid rgba(255, 193, 7, 0.28);
  border-radius: var(--radius);
  color: var(--fg);
}

.retry-card-main {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}

.retry-card-icon { flex-shrink: 0; }
.retry-card-title { font-size: var(--text-sm); font-weight: 700; }
.retry-card-meta { color: var(--fg2); font-size: var(--text-xs); margin-top: 2px; }
.retry-card-actions { display: flex; gap: var(--space-2); flex-shrink: 0; }

@media (max-width: 640px) {
  .host-connection-card { align-items: stretch; flex-direction: column; }
  .host-connection-action { align-self: stretch; }
  .retry-card { align-items: stretch; flex-direction: column; }
  .retry-card-actions { justify-content: flex-end; }
}

/* Reasoning trace (intermediate assistant text + tool calls grouped) */
.trace-block {
  align-self: flex-start;
  width: 98%;
  max-width: 98%;
  background: transparent;
  border: 1px dashed var(--border);
  border-left: 3px solid var(--accent2);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  opacity: 0.85;
  overflow-wrap: break-word;
  word-break: break-word;
  min-width: 0;
}

.trace-block.live {
  border-color: var(--accent);
  opacity: 1;
}
.trace-block.live .trace-label { color: var(--accent); font-weight: 600; }


.trace-summary {
  padding: 8px 12px;
  min-height: var(--touch);
  width: 100%;
  border: 0;
  background: transparent;
  cursor: pointer;
  color: var(--fg2);
  user-select: none;
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: var(--text-sm);
  line-height: 1.4;
  font-family: inherit;
  text-align: left;
}

.trace-summary:hover { color: var(--fg); }
.trace-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.trace-chevron { font-size: calc(10px * var(--font-scale)); color: var(--fg2); flex-shrink: 0; }
.trace-icon { font-size: calc(14px * var(--font-scale)); flex-shrink: 0; }
.trace-label { color: var(--fg2); white-space: nowrap; flex-shrink: 0; }
.trace-meta {
  color: var(--fg2);
  opacity: 0.7;
  font-weight: 400;
  margin-left: auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-xs);
  display: flex;
  align-items: center;
}
.trace-meta-part {
  display: inline-flex;
  align-items: center;
  white-space: nowrap;
}
.trace-meta-part::after {
  content: "·";
  margin: 0 6px;
  opacity: 0.7;
}
.trace-meta-part:last-child::after {
  content: none;
}
.trace-meta-part .part-text-short {
  display: none;
}
@media (max-width: 640px) {
  .trace-meta-part.part-thoughts,
  .trace-meta-part.part-notes,
  .trace-meta-part.part-files,
  .trace-meta-part.part-subagents {
    display: none;
  }
  .trace-meta-part:not(:has(~ .trace-meta-part:not(.part-thoughts):not(.part-notes):not(.part-files):not(.part-subagents)))::after {
    content: none;
  }
  .trace-meta-part .part-text-long {
    display: none;
  }
  .trace-meta-part .part-text-short {
    display: inline;
  }
}

.trace-body {
  padding: 6px 12px 10px;
  border-top: 1px dashed var(--border);
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.trace-text {
  color: var(--fg2);
  font-style: italic;
  font-size: var(--text-sm);
  line-height: 1.45;
  min-width: 0;
  overflow-wrap: break-word;
}

.thinking-block {
  min-width: 0;
}

.thinking-toggle {
  min-height: var(--touch);
  padding: 4px 8px 4px 2px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 0;
  background: transparent;
  color: var(--fg2);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-xs);
  opacity: 0.85;
}

.thinking-load {
  border: 1px solid var(--border);
  background: transparent;
  color: var(--fg2);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-xs);
  padding: 4px 10px;
  border-radius: 8px;
}

.thinking-load:hover {
  color: var(--fg);
}

.thinking-toggle:hover { color: var(--fg); }
.thinking-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.trace-text :deep(a) {
  color: var(--accent);
  text-decoration: underline;
}
.trace-text :deep(a:hover) {
  color: var(--accent-strong);
}

.trace-text :deep(pre) {
  max-width: 100%;
  overflow-x: auto;
}

.trace-text :deep(p) { margin: 2px 0; }
/* Re-establish list indent — the global `*` reset nukes browser defaults,
   and without padding-left the outside list-style markers render past the
   trace block's left border. */
.trace-text :deep(ul),
.trace-text :deep(ol) {
  padding-left: 22px;
  margin: 2px 0;
  list-style-position: outside;
}
.trace-text :deep(li) { padding-left: 2px; }

/* Thinking-block styling. Visually distinct from regular intermediate
   text so it reads as "model reasoning" rather than "draft answer". */
.trace-thinking {
  opacity: 0.7;
  border-left: 2px solid var(--fg2);
  padding-left: 8px;
  margin-left: 2px;
}

/* Transient status ticks (e.g. compaction) — one live line, dimmer than
   regular trace text, no border since it's not a block of reasoning. */
.trace-status {
  opacity: 0.6;
}

.trace-tools {
  background: var(--bg);
  border-radius: 4px;
  padding: 4px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}


/* Inline file card. Rendered inside the activity trace whenever the agent
   calls Write/Edit/MultiEdit/NotebookEdit. Tapping opens the FileViewerModal
   for that path (security-checked server-side by /api/workspace-file). */
/* Always-visible output chips sit below a final answer. Interrupted turns
   retain the chips inside Activity so file touches do not disappear. */
.trace-files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 12px 10px;
}
.answer-outputs {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
.answer-outputs-label {
  color: var(--fg2);
  font-size: var(--text-xs);
  font-weight: 600;
}
.answer-output-files {
  display: flex;
  flex: 1 1 auto;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}
.file-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  min-height: var(--touch);
  padding: 3px 8px;
  font-size: var(--text-xs);
  color: var(--fg);
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 999px;
  cursor: pointer;
}
.file-chip:hover {
  border-color: var(--accent);
}
.file-chip-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.file-chip-action {
  flex-shrink: 0;
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 600;
  text-transform: lowercase;
}
.file-chip-open {
  color: var(--fg3);
  flex-shrink: 0;
}

.file-card {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  max-width: 100%;
  padding: 8px 10px;
  margin: 2px 0;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  cursor: pointer;
  text-align: left;
  color: inherit;
  font: inherit;
  transition: background 0.12s, border-color 0.12s;
  min-width: 0;
}

.file-card:hover {
  background: var(--bg2);
  border-color: var(--accent2);
}

.file-card:active {
  background: var(--bg2);
}

.file-card-icon {
  flex: 0 0 auto;
  font-size: var(--text-lg);
  line-height: 1;
}

.file-card-main {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.file-card-name {
  font-weight: 600;
  font-size: var(--text-base);
  color: var(--fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-card-meta {
  font-size: var(--text-xs);
  color: var(--fg2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-top: 1px;
}

.file-card-action {
  color: var(--accent);
}

.file-card-dir {
  color: var(--fg2);
}

.file-card-chevron {
  flex: 0 0 auto;
  color: var(--fg2);
  font-size: var(--text-base);
  line-height: 1;
  opacity: 0.7;
}

/* Activity blocks (live streaming) */
.activity-block {
  align-self: flex-start;
  width: 98%;
  max-width: 98%;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  overflow: hidden;
  min-width: 0;
}

.activity-block.live {
  border-color: var(--accent2);
}

.activity-summary {
  padding: 6px 10px;
  cursor: pointer;
  color: var(--fg2);
  user-select: none;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 6px;
}

.activity-summary::-webkit-details-marker { display: none; }
.activity-summary::before {
  content: '\25B8';
  font-size: calc(10px * var(--font-scale));
  transition: transform 0.15s;
}
details[open] > .activity-summary::before {
  transform: rotate(90deg);
}

.activity-summary-live {
  padding: 6px 10px;
  min-height: 32px;
  color: var(--accent);
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}

.activity-icon { font-size: var(--text-base); }

.activity-spinner {
  position: relative;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 4px var(--accent);
  animation: activity-pulse 1.1s ease-in-out infinite;
  flex-shrink: 0;
  /* The halo and the expanding ring paint ~3px beyond this element's box, so the
     row's 8px gap looked like ~5px and the dot read as touching the label. */
  margin-right: var(--space-1);
}

.activity-spinner::before {
  content: "";
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  background: var(--accent);
  opacity: 0.45;
  animation: activity-ring 1.1s ease-out infinite;
  pointer-events: none;
}

@keyframes activity-pulse {
  0%, 100% { transform: scale(0.55); opacity: 0.35; }
  50% { transform: scale(1); opacity: 1; }
}

@keyframes activity-ring {
  0%   { transform: scale(0.7); opacity: 0.55; }
  100% { transform: scale(2.2); opacity: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .activity-spinner { animation-duration: 2.2s; }
  .activity-spinner::before { animation-duration: 2.2s; }
}

@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

.activity-lines {
  padding: 4px 10px 6px;
  border-top: 1px solid var(--border);
}

.activity-line {
  padding: 2px 0;
  color: var(--fg2);
  font-family: var(--font);
  font-size: var(--text-sm);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.45;
}

/* Subagent activity (parent_tool_use_id was set on the WS event). Indented
   and dimmed so the trace reads as parent → subagent → parent without the
   reader having to parse who did what. The bracketed [Explore] / [general]
   tag at the start of the line carries the actual attribution. */
.activity-line.subagent {
  padding-left: 18px;
  opacity: 0.78;
  border-left: 2px solid var(--border);
  margin-left: 4px;
}

/* "N agents" header pill: background subagents still running after the
   parent turn ended (store.activeBackgroundAgents, fed by /ws/events). */
.bg-agents-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 9px;
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: 11px;
  color: var(--fg2);
  white-space: nowrap;
}

.bg-agents-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent2);
  animation: bg-agents-pulse 1.6s ease-in-out infinite;
}

@keyframes bg-agents-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}

/* Message content (markdown) */
.message-content {
  min-width: 0;
  overflow-wrap: break-word;
}
.message-content :deep(pre) {
  background: var(--bg);
  padding: 8px 12px;
  border-radius: var(--radius-sm, 6px);
  overflow-x: auto;
  margin: 6px 0;
  white-space: pre-wrap;
  max-width: 100%;
  font-family: var(--font-mono);
}

.message-content :deep(code) {
  font-family: var(--font-mono);
  font-size: 0.9em;
  padding: 1px 4px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--fg) 8%, transparent);
}

.message-content :deep(pre code) {
  padding: 0;
  background: transparent;
  font-size: var(--text-sm);
}

/* Fenced code blocks (lib/codeCopy.ts emits the wrapper + button). The rule is
   anchored on .chat-panel rather than .message-content so it also covers the
   code blocks inside activity traces and the streaming bubble. */
/* The button sits in its own row above the block rather than floating over it:
   on a phone-width block an overlay would cover the start of the code. */
.chat-panel :deep(.code-block) {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  min-width: 0;
  margin: 6px 0;
}

.chat-panel :deep(.code-block pre) {
  width: 100%;
  box-sizing: border-box;
  margin: 0;
}

.chat-panel :deep(.code-copy-btn) {
  position: relative;
  margin-bottom: var(--space-1);
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--fg2);
  font-family: var(--font);
  font-size: var(--text-xs);
  line-height: 1.2;
  cursor: pointer;
  user-select: none;
  /* Dimmed but always present: this PWA runs on phones, where :hover never
     fires and a hover-only control would be unreachable. */
  opacity: 0.6;
  transition: opacity 120ms var(--ease), background 120ms var(--ease), color 120ms var(--ease);
}

/* Touch: grow the chip and expand its hit area to a full touch target without
   moving anything around it. The expander is dropped for fine pointers, where
   it would only steal clicks from the text next to the chip. */
@media (hover: none) {
  .chat-panel :deep(.code-copy-btn) {
    padding: var(--space-2) var(--space-3);
  }

  .chat-panel :deep(.code-copy-btn::after) {
    content: '';
    position: absolute;
    inset: calc(-1 * var(--space-2));
    min-width: var(--touch);
  }
}

.chat-panel :deep(.code-copy-btn:hover),
.chat-panel :deep(.code-copy-btn:focus-visible) {
  opacity: 1;
  background: var(--bg3);
  color: var(--fg);
}

.chat-panel :deep(.code-copy-btn:active) {
  transform: scale(0.96);
}

.chat-panel :deep(.code-copy-btn[data-copy-state="copied"]) {
  opacity: 1;
  color: var(--success);
  border-color: color-mix(in srgb, var(--success) 45%, var(--border));
}

.chat-panel :deep(.code-copy-btn[data-copy-state="failed"]) {
  opacity: 1;
  color: var(--error);
  border-color: color-mix(in srgb, var(--error) 45%, var(--border));
}

.message-content :deep(:is(h1, h2, h3, h4)) {
  margin-top: 1.2em;
  margin-bottom: 0.4em;
  line-height: 1.35;
  font-weight: 700;
}
.message-content :deep(h1) { font-size: 1.5em; }
.message-content :deep(h2) { font-size: 1.25em; }
.message-content :deep(h3) { font-size: 1.1em; }

.message-content :deep(p) { margin: 4px 0; }
.message-content :deep(ul),
.message-content :deep(ol) {
  padding-left: 1.35em;
  margin: 4px 0;
  list-style-position: inside;
}
/* Collapse the leading/trailing margin of the first/last markdown block so
   the bubble padding isn't compounded by a paragraph margin. */
.message-content :deep(:first-child) { margin-top: 0; }
.message-content :deep(:last-child) { margin-bottom: 0; }
.message-content :deep(li) { padding-left: 0; }
.message-content :deep(a) {
  color: var(--accent);
  text-decoration: underline;
}
.message-content :deep(a:hover) {
  color: var(--accent-strong);
}
.message-content :deep(hr) {
  border: 0;
  border-top: 1px solid var(--border);
  margin: 1.25em 0;
}

/* Quoted "comment" context (see lib/commentContext.ts) rendered as a quote
   card in the user's own bubble. The same tags are the boundary the model
   reads; here they're just styled. */
.message-content :deep(user-comment-reference) {
  display: block;
  margin: 6px 0 10px;
}
.message-content :deep(reference-source) {
  display: block;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  color: var(--fg2);
  margin-bottom: 4px;
}
.message-content :deep(quoted-text) {
  display: block;
  white-space: pre-wrap;
  color: var(--fg2);
  font-style: italic;
  background: var(--bg);
  border-left: 3px solid var(--accent);
  border-radius: 6px;
  padding: 8px 12px;
  margin: 4px 0;
}
.message-content :deep(user-comment) {
  display: block;
  color: var(--fg);
  font-style: normal;
  font-weight: normal;
  margin-top: 8px;
}
.message-content :deep(quoted-text p:first-child),
.message-content :deep(user-comment p:first-child) {
  margin-top: 0;
}
.message-content :deep(quoted-text p:last-child),
.message-content :deep(user-comment p:last-child) {
  margin-bottom: 0;
}
.message-content :deep(quoted-text > br:first-child),
.message-content :deep(user-comment > br:first-child),
.message-content :deep(quoted-text > br:last-child),
.message-content :deep(user-comment > br:last-child) {
  display: none;
}
.message-content :deep(blockquote) {
  margin: 8px 0;
  padding: 8px 12px;
  border-left: 3px solid var(--accent);
  background: var(--bg);
  border-radius: 6px;
  color: var(--fg2);
  font-style: italic;
}

/* File-path links produced by linkifyHtml/linkifyText. Subtle dotted
   underline so they're discoverable but don't look like external URLs. */
.message-content :deep(a.file-link),
.activity-line :deep(a.file-link),
:deep(a.file-link) {
  color: inherit;
  text-decoration: underline dotted;
  text-underline-offset: 2px;
  cursor: pointer;
}
.message-content :deep(a.file-link:hover),
.activity-line :deep(a.file-link:hover),
:deep(a.file-link:hover) {
  color: var(--accent);
  text-decoration: underline solid;
}
.message-content :deep(.markdown-table-scroll) {
  width: fit-content;
  max-width: 100%;
  overflow-x: auto;
  margin: 8px 0;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  overscroll-behavior-inline: contain;
  -webkit-overflow-scrolling: touch;
}
.message-content :deep(.markdown-table-scroll:focus-visible) {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.message-content :deep(table) {
  min-width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  margin: 0;
  font-size: var(--text-sm);
}
.message-content :deep(th),
.message-content :deep(td) {
  padding: 7px 10px;
  border-right: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  vertical-align: top;
  word-break: normal;
  overflow-wrap: normal;
}
.message-content :deep(tr > :last-child) {
  border-right: 0;
}
.message-content :deep(tbody tr:last-child > td) {
  border-bottom: 0;
}
.message-content :deep(th) {
  background: var(--bg3);
  font-weight: 600;
  text-align: left;
}
.message-content :deep(tbody tr:nth-child(even) > td) {
  background: color-mix(in srgb, var(--fg) 2.5%, transparent);
}
.message-content :deep(tr > :first-child) {
  white-space: nowrap;
}
.message-content :deep(tbody tr > td:first-child) {
  font-weight: 600;
}

.message-meta {
  font-size: 10px;
  color: var(--fg2);
  margin-top: 6px;
  padding-top: 4px;
  border-top: 1px solid color-mix(in srgb, var(--fg2) 18%, transparent);
  line-height: 1.3;
}

.message.user .message-meta {
  text-align: right;
}

/* Marks a turn fired by an automation. Accent-coloured so it reads as a
   property of the message, not as part of the timestamp next to it. */
.unattended-mark {
  color: var(--accent);
  font-weight: 600;
  letter-spacing: 0.02em;
  margin-right: 6px;
}
.tokens-group {
  white-space: nowrap;
  display: inline-block;
}
.tokens-group :deep(.token-number) {
  color: var(--fg);
  font-weight: 500;
}
.tokens-group :deep(.context-pct) {
  color: var(--fg2);
  font-weight: 500;
}

.message-images { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.message-image-link { display: inline-block; line-height: 0; text-decoration: none; }
.message-image {
  max-height: 160px;
  max-width: 240px;
  border-radius: 6px;
  object-fit: cover;
  border: 1px solid var(--border);
  cursor: zoom-in;
  background: var(--bg);
}

/* Pending attachments row: images, chat comments and file comments all stage
   here above the input. The drawers hold the full text. */
.pending-attachments {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--bg2);
  border-top: 1px solid var(--border);
  flex-shrink: 0;
  /* Comments wrap onto new rows, so cap the row before a long comment session
     pushes the input off screen. */
  max-height: 25vh;
  overflow-y: auto;
}

.image-preview {
  position: relative;
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}

.image-preview-thumb {
  height: 56px;
  width: 56px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg);
}

.image-preview-remove {
  position: absolute;
  top: -6px;
  right: -6px;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: var(--bg3);
  color: var(--fg);
  font-size: 14px;
  line-height: 16px;
  cursor: pointer;
  box-shadow: 0 1px 3px rgba(0,0,0,0.3);
}

.image-ref-chip {
  padding: 2px 6px;
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: var(--fg2);
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  transition: background 120ms var(--ease), color 120ms var(--ease);
  line-height: 1;
}
.image-ref-chip:hover { background: var(--accent); color: var(--bg); border-color: var(--accent); }
.image-ref-chip:active { background: var(--accent2); }

.comment-chip {
  display: inline-flex;
  align-items: flex-start;
  gap: 6px;
  /* Shrinkable basis, not a fixed 220px: with flex-wrap the line break is
     decided on the base size, so fixed-width chips wrapped onto their own row
     as soon as the chat pane got narrow (pinned file panel open). A small
     basis plus grow lets two or three chips share one row and split the
     available width, capped so a single chip does not stretch. */
  flex: 1 1 140px;
  min-width: 0;
  max-width: 220px;
  height: 48px;
  padding: 6px 8px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.35;
  color: var(--fg);
  box-sizing: border-box;
}
/* The chip whose edit popover is open. */
.comment-chip.is-editing {
  border-color: var(--accent, #60a5fa);
  background: var(--bg2);
}
.comment-chip-icon { line-height: 1; padding-top: 1px; }
.comment-chip-body {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  min-width: 0;
  flex: 1;
  padding: 0;
  border: none;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.comment-chip-body > * { max-width: 100%; }
.comment-chip-file {
  font-weight: 600;
  font-size: 11px;
  color: var(--fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.comment-chip-line { color: var(--fg2); font-weight: 400; }
.comment-chip-quote {
  color: var(--fg2);
  font-style: italic;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.comment-chip-note {
  color: var(--fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.comment-chip-remove {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--fg2);
  font-size: 14px;
  line-height: 16px;
  cursor: pointer;
}
.comment-chip-remove:hover { background: var(--bg2); color: var(--fg); }


/* Dock strip: the single counted line for everything the dock defers. */
.dock-strip-wrap {
  flex-shrink: 0;
  border-top: 1px solid var(--border);
  background: var(--bg);
}

.dock-strip {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-height: var(--touch);
  padding: var(--space-2) var(--space-3);
  padding-left: calc(var(--space-3) + var(--safe-left));
  padding-right: calc(var(--space-3) + var(--safe-right));
  border: 0;
  background: none;
  color: var(--fg2);
  font-family: var(--font);
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
  flex-wrap: wrap;
}

.dock-strip:hover { background: var(--bg2); }

.dock-strip:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.dock-chevron { color: var(--fg3); flex-shrink: 0; }

.dock-strip-items {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  min-width: 0;
}

.dock-pill {
  padding: 2px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--fg2);
  font-size: var(--text-xs);
  white-space: nowrap;
}

/* Deferred blocking work is still the user's move, so it is marked — but it
   stays outlined rather than filled, because the filled treatment belongs to
   the one item actually expanded above. */
.dock-pill--blocking {
  border: 1px solid var(--warning);
  color: var(--warning);
  background: none;
}

.dock-agent-links {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: 0 var(--space-3) var(--space-2);
  padding-left: calc(var(--space-3) + var(--safe-left));
  padding-right: calc(var(--space-3) + var(--safe-right));
  background: var(--bg);
  border-top: 1px solid var(--border);
}

.dock-agent-link {
  padding: 2px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--accent);
  font-size: var(--text-xs);
  text-decoration: none;
  /* Dispatch descriptions are free text and routinely a full sentence; kept on
     one line, an unbounded chip pushes the composer into horizontal scroll on
     a phone. Clip instead. */
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dock-agent-link:hover { text-decoration: underline; }

.bg-agents-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  flex-shrink: 0;
  animation: bg-agents-pulse 1.4s ease-in-out infinite;
}

@keyframes bg-agents-pulse {
  0%, 100% { opacity: 0.35; }
  50% { opacity: 1; }
}


/* Voice transcribing spinner */
.voice-transcribing {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: var(--touch);
  min-height: var(--touch);
}

.transcribe-spinner {
  width: 18px;
  height: 18px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

/* Slash-command picker */
.commands-picker {
  max-height: 240px;
  overflow-y: auto;
  margin: 0 calc(12px + var(--safe-left));
  padding: 4px 0;
  border: 1px solid var(--border);
  border-bottom: none;
  border-radius: 8px 8px 0 0;
  background: var(--bg);
  font-size: 0.9rem;
}
.commands-picker-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 12px;
  cursor: pointer;
}
.commands-picker-row.active {
  background: var(--bg3);
}
.commands-picker-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  white-space: nowrap;
  overflow: hidden;
}
.commands-picker-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-weight: 600;
  flex-shrink: 0;
}
.commands-picker-kind {
  color: var(--accent2);
  font-size: 0.72em;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  flex-shrink: 0;
}
.commands-picker-hint {
  color: var(--fg3);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.85em;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.commands-picker-desc {
  color: var(--fg3);
  font-size: 0.85em;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.mention-picker-row {
  min-height: var(--touch);
  box-sizing: border-box;
  touch-action: manipulation;
}
.mention-picker-kind {
  color: var(--accent2);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.75em;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  flex-shrink: 0;
}
.mention-picker-row .commands-picker-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Input bar */
.input-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  /* Match sidebar-footer / pane headers: 44px controls + 8px pad + 1px border.
     Grows past 61px when the textarea becomes multi-line (.tall). */
  min-height: 61px;
  padding: 8px 12px;
  /* Do not add the bottom safe-area inset: in PWA standalone mode this
     would reserve ~34px below the input for the home indicator, which
     feels excessive. iOS still reserves the swipe-up gesture globally,
     so the input can sit flush 8px from the viewport bottom. */
  padding-left: calc(12px + var(--safe-left));
  padding-right: calc(12px + var(--safe-right));
  border-top: 1px solid var(--border);
  background: var(--bg);
  flex-shrink: 0;
  box-sizing: border-box;
}

.input-bar.tall {
  align-items: flex-end;
}

/* Buttons sit in a row at the bottom by default; once the textarea grows
   tall enough they stack vertically. */
.input-actions {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}

.input-bar.tall .input-actions {
  flex-direction: column;
}

/* Force the send button to match the square mic/attach buttons when
   stacked vertically in the input-actions container. */
.input-actions .send-btn {
  padding: 0;
  width: var(--touch);
  height: var(--touch);
}

.chat-input {
  flex: 1;
  resize: none;
  height: var(--touch);
  min-height: var(--touch);
  max-height: 200px;
  /* Textareas top-align text; symmetric padding optically centers one
     line inside the 44px touch target (14px × 1.25 line-height). */
  padding: 13px 12px;
  line-height: 1.25;
}

.archived-notice {
  flex: 1;
  color: var(--fg2);
  font-size: var(--text-base);
  text-align: center;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
}

.archived-notice-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  flex-wrap: wrap;
}

/* A footnote, not a component: no card, no border, no background. It reports
   work the user did not ask for and does not need to act on, so it stays in the
   muted register even once it has something to say. */
.archived-postprocess {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  margin: 0;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.5;
  color: var(--fg3);
  text-align: center;
  flex-wrap: wrap;
}

/* The single exception to the muted rule: a failed step is only ever visible
   here, so it is allowed to say so. */
.archived-postprocess.failed { color: var(--warning); }

.archived-postprocess-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--fg3);
  flex: 0 0 auto;
  animation: archived-postprocess-breathe 2.6s ease-in-out infinite;
}

@keyframes archived-postprocess-breathe {
  0%, 100% { opacity: 0.35; }
  50%      { opacity: 0.9; }
}

@media (prefers-reduced-motion: reduce) {
  .archived-postprocess-dot { animation: none; opacity: 0.75; }
}

.image-btn {
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: var(--touch);
  min-height: var(--touch);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--fg2);
  transition: background 120ms var(--ease), color 120ms var(--ease), border-color 120ms var(--ease);
}
.image-btn:hover { background: var(--bg3); color: var(--fg); border-color: var(--fg2); }
.image-btn:active { background: var(--bg2); }

.send-btn {
  min-width: var(--touch);
  min-height: var(--touch);
  padding: 0 16px;
  border: none;
  border-radius: var(--radius);
  cursor: pointer;
  font-family: var(--font);
  font-size: 16px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 120ms var(--ease), transform 120ms var(--ease);
  background: var(--accent);
  color: white;
}
.send-btn:hover { background: var(--accent-strong); }
.send-btn:active { transform: scale(0.96); }
.send-btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }
.send-btn.is-stop {
  background: var(--error);
  padding: 0;
  width: var(--touch);
  height: var(--touch);
}
.send-btn.is-stop:hover { background: var(--error); filter: brightness(1.08); }
.send-glyph {
  font-size: 20px;
  font-weight: 700;
  line-height: 1;
  display: inline-block;
  transform: translateY(-1px);
}
.stop-icon {
  font-size: 14px;
  line-height: 1;
  display: inline-block;
}

/* Queued message chips */
.queued-messages {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 12px;
  background: var(--bg2);
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}
.queued-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--bg3);
  border-radius: var(--radius);
  font-size: 13px;
  color: var(--fg2);
}
.queued-label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--accent);
  flex-shrink: 0;
}
.queued-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.queued-images {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.queued-image-thumb {
  height: 40px;
  width: 40px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg);
}
.queued-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queued-remove {
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
  padding: 2px 6px;
  border-radius: 4px;
  flex-shrink: 0;
}
.queued-remove:hover { color: var(--fg); background: var(--bg2); }
.queued-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}
.queued-action {
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
  padding: 4px 6px;
  border-radius: 4px;
  flex-shrink: 0;
}
.queued-action:hover:not(:disabled) { color: var(--fg); background: var(--bg2); }
.queued-action:disabled { opacity: 0.3; cursor: not-allowed; }
.queued-edit-input {
  width: 100%;
  background: var(--bg);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 6px 8px;
  font-family: var(--font);
  font-size: 13px;
  resize: vertical;
}

/* AskUserQuestion picker. Same docking pattern as the permission card so
   the model's structured question doesn't get lost in the trace. */
.question-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  padding-left: calc(12px + var(--safe-left));
  padding-right: calc(12px + var(--safe-right));
  background: var(--bg);
  border-top: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  flex-shrink: 0;
  max-height: 50vh;
  overflow-y: auto;
}
.question-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--fg);
}
.question-card-icon { font-size: 16px; color: var(--accent); }
.question-card-title { flex: 1 1 auto; }
.capability-countdown {
  flex: 0 0 auto;
  font-size: 12px;
  font-weight: 500;
  color: var(--fg2);
  font-variant-numeric: tabular-nums;
}
.question-card-dismiss {
  background: transparent;
  border: 0;
  color: var(--fg2);
  font-size: 18px;
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
}
.question-card-dismiss:hover { color: var(--fg); }

.question-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.question-block-header { display: flex; gap: 6px; align-items: center; }
.question-block-chip {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  background: var(--bg);
  border: 1px solid var(--border);
  padding: 1px 6px;
  border-radius: 3px;
}
.question-block-multi {
  font-size: 11px;
  color: var(--fg2);
  font-style: italic;
}
.question-block-prompt {
  font-size: 13px;
  color: var(--fg);
  line-height: 1.4;
}
.question-options {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.question-option {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  text-align: left;
  padding: 8px 10px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  font-family: var(--font);
  font-size: 13px;
  color: var(--fg);
  transition: background 120ms var(--ease), border-color 120ms var(--ease);
}
.question-option:hover { background: var(--bg2); }
.question-option.selected {
  background: var(--bg2);
  border-color: var(--accent);
  box-shadow: inset 2px 0 0 var(--accent);
}
.question-option-main {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
/* Keeps a wrapped description aligned under its label rather than under the
   badge. */
.question-option-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
/* Keyboard chip: low-emphasis on purpose so it reads as a hint next to the
   label rather than as numbering the model wrote. */
.question-option-key {
  flex: none;
  min-width: 18px;
  padding: 1px 5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.4;
  text-align: center;
  color: var(--fg2);
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 3px;
}
.question-option.selected .question-option-key {
  color: var(--accent);
  border-color: var(--accent);
}
.question-option-label { font-weight: 600; }
.question-option-desc { font-size: 12px; color: var(--fg2); line-height: 1.3; }
.question-other {
  width: 100%;
  padding: 6px 8px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-family: var(--font);
  font-size: 13px;
  color: var(--fg);
}
.question-other:focus { outline: 1px solid var(--accent); border-color: var(--accent); }
.question-card-actions { display: flex; justify-content: flex-end; gap: 8px; }

/* Pending Auto-mode permission prompts. Sticks above the input until the
   user answers. Chrome uses --warning (this is a "waiting on you" state,
   not an action) so --accent reads as a single, unambiguous signal on the
   Approve button rather than being smeared across the whole card. */
.permission-requests {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 12px;
  padding-left: calc(12px + var(--safe-left));
  padding-right: calc(12px + var(--safe-right));
  background: var(--bg2);
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}

.permission-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  background: var(--bg);
  border: 1px solid var(--warning);
  border-left: 3px solid var(--warning);
  border-radius: var(--radius);
  font-size: 13px;
  animation: permission-pulse 1.4s ease-out;
}

@keyframes permission-pulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--warning) 40%, transparent); }
  100% { box-shadow: 0 0 0 8px color-mix(in srgb, var(--warning) 0%, transparent); }
}

.permission-header {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  color: var(--fg);
  line-height: 1.4;
}

.permission-icon {
  width: 14px;
  height: 14px;
  color: var(--warning);
  flex-shrink: 0;
}

.permission-tool {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  font-weight: 600;
  color: var(--warning);
  background: var(--bg2);
  padding: 1px 6px;
  border-radius: 3px;
  flex-shrink: 0;
}

.permission-message {
  color: var(--fg2);
  font-size: 12px;
  flex: 1 1 auto;
  min-width: 0;
}

.permission-input {
  margin: 0;
  padding: 6px 8px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--fg2);
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

/* Labelled argument rows. Key column is sized to content and clamped so a
   long `prompt` value keeps most of the width; the whole block scrolls as one
   unit rather than each value clipping on its own. */
.permission-args {
  display: grid;
  grid-template-columns: minmax(0, max-content) minmax(0, 1fr);
  gap: 2px 10px;
  margin: 0;
  padding: 6px 8px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 11px;
  max-height: 220px;
  overflow: auto;
}

.permission-args dt {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: var(--fg2);
  opacity: 0.75;
  white-space: nowrap;
}

.permission-args dd {
  margin: 0;
  min-width: 0;
  color: var(--fg);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.permission-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.btn-approve, .btn-deny {
  min-height: 36px;
  padding: 8px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  font-family: var(--font);
  font-size: var(--text-sm);
  font-weight: 600;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), transform 120ms var(--ease);
}
/* Keyboard chip on the permission buttons, mirroring the question-option key
   hint. Low-emphasis so it reads as a shortcut, not as part of the label. */
.permission-key {
  display: inline-block;
  min-width: 16px;
  margin-right: 6px;
  padding: 0 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.5;
  text-align: center;
  color: var(--fg2);
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid currentColor;
  border-radius: 3px;
  opacity: 0.75;
}
.btn-approve .permission-key { color: rgba(255, 255, 255, 0.85); }
.btn-approve {
  background: var(--accent);
  color: white;
  border-color: var(--accent);
}
.btn-approve:hover { background: var(--accent-strong); }
.btn-approve:active { transform: scale(0.96); }
.btn-deny {
  background: var(--bg3);
  color: var(--fg);
}
.btn-deny:hover { background: var(--bg2); border-color: var(--fg2); }
.btn-deny:active { transform: scale(0.96); }

.model-picker-wrap {
  position: relative;
  display: inline-flex;
  align-items: center;
  height: 30px;
}

.model-picker-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: content-box;
  width: 30px;
  height: 30px;
  min-width: 30px;
  min-height: 30px;
  border-radius: 6px;
  color: var(--fg2);
  font-size: 18px;
  background: transparent;
  border: none;
  cursor: pointer;
}
.model-picker-btn:active { transform: scale(0.96); }

.model-picker-summary {
  display: inline-flex;
  align-items: center;
  margin-left: 4px;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-elev);
  color: var(--fg2);
  font-size: 11px;
  line-height: 1.4;
  font-family: var(--font);
  white-space: nowrap;
  /* Four segments now (provider · model · mode · thinking). The trailing
     segments are the very thing the chip exists to report, so the content is
     kept short (the model segment drops a provider-repeating prefix) and the
     budget sized for the four-segment shape. Still bounded so the pill cannot
     crowd out the chat title. */
  max-width: min(380px, calc(100vw - 200px));
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), color 120ms var(--ease);
}
.model-picker-summary:hover {
  background: var(--bg3);
  color: var(--fg);
  border-color: var(--border-strong);
}
.model-picker-summary:active { transform: scale(0.97); }

.thinking-levels {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.thinking-levels__label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: var(--fg2);
}

.thinking-levels__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.thinking-chip {
  display: inline-flex;
  align-items: center;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-elev);
  color: var(--fg);
  font: inherit;
  font-size: 11px;
  line-height: 1.4;
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease), color 120ms var(--ease);
}

.thinking-chip:hover {
  background: var(--bg3);
}

.thinking-chip--active {
  background: var(--accent);
  border-color: var(--accent);
  color: white;
}

.archive-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: content-box;
  width: 30px;
  height: 30px;
  min-width: 30px;
  min-height: 30px;
  border-radius: 6px;
  color: var(--fg2);
  background: transparent;
  border: none;
  cursor: pointer;
}
.archive-btn:hover { color: var(--fg); }
.archive-btn:active { transform: scale(0.96); }

.model-picker-dropdown {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  min-width: 240px;
  max-width: 320px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px;
  z-index: 100;
  box-shadow: 0 8px 24px rgba(0,0,0,0.35);
}

.picker-section {
  margin-bottom: 10px;
}
.picker-section:last-child {
  margin-bottom: 0;
}

.picker-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: var(--fg2);
  margin-bottom: 6px;
  padding: 0 4px;
}

.picker-pills {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.picker-pill {
  font-size: 12px;
  padding: 5px 12px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font);
  transition: background 120ms var(--ease);
}
.picker-pill:hover { background: var(--bg2); }
.picker-pill.active {
  background: var(--accent);
  color: white;
  border-color: var(--accent);
}
.picker-pill.handover {
  border-color: var(--accent2);
  color: var(--fg2);
}
.picker-pill.handover:hover {
  background: var(--bg2);
  color: var(--fg);
}
.picker-hint {
  margin: 6px 0 0;
  font-size: 11px;
  color: var(--fg2);
  opacity: 0.7;
}

/* Pinned-file split: when the chat column is narrow, reclaim horizontal
   space from gutter padding so bubbles stay readable without extra scroll. */
@container chat-split (max-width: 560px) {
  .messages {
    padding: 8px 6px 16px 6px;
  }
  .message-wrap { width: 96%; max-width: 96%; }
  .message-wrap.assistant { width: 100%; max-width: 100%; }
}
@container chat-split (max-width: 400px) {
  .messages {
    padding: 6px 4px 12px 4px;
  }
  .message-wrap,
  .message-wrap.assistant { width: 100%; max-width: 100%; }
  .message { padding: 8px 10px; }
}

@media (max-width: 768px) {
  /* Mobile header: spend the narrow width on the chat context, not on a
     giant one-line truncation. The project and title can wrap to two compact
     lines while the action buttons keep their safe tap targets. */
  :deep(.pane-header) {
    padding-left: calc(12px + var(--safe-left));
    padding-right: calc(12px + var(--safe-right));
  }
  :deep(.header-title) { text-align: left; min-width: 0; }
  .header-left { min-width: 0; }
  /* The scope/title stack is the base layout now, so this block only carries
     what a narrow header changes about it. */
  /* PaneHeader drops every pane title to --text-sm on narrow screens, which is
     right for a title competing with a page tag and a wordmark. This header has
     neither, and the title has its own full-width row, so it keeps body size and
     stays the anchor of the block. Two-line clamp is inherited from PaneHeader. */
  .header-breadcrumb .pane-title.chat-title {
    font-size: var(--text-base);
    line-height: 1.25;
  }
  /* Renaming keeps the layout it replaces: own row, same step as the title. */
  .title-input {
    flex: 1 1 100%;
    font-size: var(--text-base);
  }
  :deep(.header-actions) {
    flex-shrink: 0;
    gap: 6px;
  }
  .model-picker-wrap { min-width: 0; }
  .model-picker-dropdown {
    right: 0;
    min-width: auto;
    width: min(320px, calc(100vw - 24px));
    max-width: none;
  }
  .message-wrap { width: 96%; max-width: 96%; }
  .message-wrap.assistant { width: 100%; max-width: 100%; }
  .message { padding: 10px 14px; }
  /* Keep input and placeholder at the same size so the text doesn't jump
     when the user starts typing. 16px is the iOS auto-zoom floor: any
     smaller and Safari zooms the page on focus, which is worse than a
     slightly truncated placeholder. */
  .chat-input { font-size: 16px; padding: 12px 12px; line-height: 1.25; }
  .chat-input::placeholder { font-size: 16px; }
  /* Keep every composer action at the shared touch-target minimum.
     Preserve the 61px footer lock (44 + 8 + 8 + 1) used on desktop so the
     sidebar "+ New Project" row still lines up on coarse pointers. */
  .input-bar { min-height: 61px; padding-top: 8px; padding-bottom: 8px; }
  .chat-input { height: var(--touch); min-height: var(--touch); }
  .input-actions .send-btn {
    min-width: var(--touch);
    min-height: var(--touch);
    width: var(--touch);
    height: var(--touch);
    padding: 0;
  }
  .image-btn { min-height: var(--touch); min-width: var(--touch); }
  :deep(.voice-btn) { min-height: var(--touch); min-width: var(--touch); }
}

/* Chat comment selection trigger + composer */
/* Comment trigger pill. Shape and behaviour match the danger-red variant
 * used in FileViewerModal and PinnedFilePanel so the "Comment" affordance
 * looks the same regardless of where the user is in the app. */
.chat-comment-trigger {
  position: fixed;
  z-index: 5;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  font-size: var(--text-sm);
  font-weight: 600;
  color: white;
  background: var(--error);
  border: none;
  border-radius: 999px;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
  user-select: none;
}
.chat-comment-trigger:hover { filter: brightness(1.08); }
.chat-comment-trigger-icon { font-size: var(--text-sm); line-height: 1; }

.chat-comment-backdrop {
  position: fixed;
  inset: 0;
  z-index: 40;
  background: rgba(0, 0, 0, 0.32);
}
.btn-sm {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-sm);
  font-weight: 500;
  padding: 8px 16px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease);
}
.btn-sm:hover { background: var(--bg3); border-color: var(--fg2); }
.btn-sm.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
  font-weight: 600;
}
.btn-sm.primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Messages container takes the full pane; comment surfaces are popovers. */
.chat-with-sidebar {
  flex: 1;
  position: relative;
  display: flex;
  min-height: 0;
  overflow: hidden;
}
.chat-with-sidebar > .messages {
  flex: 1;
  min-width: 0;
}
/* Inline text highlights inside message bubbles. Use :deep() because
 * highlight spans are inserted via DOM manipulation in applyHighlights()
 * and don't carry Vue's scoped attribute. */
:deep(.comment-highlight) {
  background: rgba(234, 179, 8, 0.25);
  border-bottom: 2px solid rgba(234, 179, 8, 0.6);
  cursor: pointer;
  transition: background 0.15s;
  border-radius: 2px;
}
:deep(.comment-highlight:hover) {
  background: rgba(234, 179, 8, 0.4);
}
/* In-progress draft selection: brighter so it stands out while typing. */
:deep(.comment-highlight[data-comment-id="__draft__"]) {
  background: rgba(234, 179, 8, 0.45);
  border-bottom-color: rgba(234, 179, 8, 0.9);
}
/* Brief flash when navigated to from a pending-comment chip. */
:deep(.comment-highlight--pulse) {
  animation: comment-pulse 1.1s var(--ease) 1;
}
@keyframes comment-pulse {
  0%   { background: rgba(234, 179, 8, 0.25); box-shadow: 0 0 0 0 rgba(234, 179, 8, 0); }
  25%  { background: rgba(234, 179, 8, 0.7);  box-shadow: 0 0 0 6px rgba(234, 179, 8, 0.18); }
  100% { background: rgba(234, 179, 8, 0.25); box-shadow: 0 0 0 0 rgba(234, 179, 8, 0); }
}

/* ── Ephemeral re-entry summary ── */
.reentry-summary-message {
  background: color-mix(in srgb, var(--accent2) 12%, var(--bg2));
  border-color: color-mix(in srgb, var(--accent2) 45%, var(--border));
  border-left-color: var(--accent2);
}
.reentry-summary-header {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.reentry-summary-badge {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  padding: 2px 8px;
  border: 1px solid color-mix(in srgb, var(--accent2) 70%, var(--border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent2) 18%, transparent);
  color: var(--fg);
  font-family: var(--font);
  font-size: var(--text-xs);
  font-weight: 700;
  letter-spacing: 0.4px;
  line-height: 1.2;
  text-transform: uppercase;
}
.reentry-summary-source {
  color: var(--fg2);
  font-family: var(--font);
  font-size: var(--text-xs);
}
.reentry-summary-message .message-content {
  color: var(--fg);
}

/* ── Automation banner ── */
/* ── Context bar ─────────────────────────────────────────────────────
   Collapsed: one line of counted chips. Expanded: the detail rows, which
   keep the original .loop-banner-row layout and actions. */
.ctx-bar {
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
}
.ctx-summary {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  padding: var(--space-2) var(--space-4);
  border: 0;
  background: none;
  color: var(--fg2);
  font-family: var(--font);
  font-size: var(--text-xs);
  text-align: left;
  cursor: pointer;
  flex-wrap: wrap;
  min-height: var(--touch);
}
.ctx-summary:hover { background: var(--bg3); }
.ctx-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.ctx-chevron { color: var(--fg3); flex-shrink: 0; }
.ctx-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  padding: 2px var(--space-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  color: var(--fg2);
  white-space: nowrap;
}
.ctx-chip-glyph {
  color: var(--fg3);
  font-weight: 700;
  line-height: 1;
}
/* A live automation is the one thing here that is actively happening. */
.ctx-chip-glyph.live { color: var(--accent); }
.ctx-detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: 0 var(--space-4) var(--space-2);
}
.loop-banner-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
/* Explicit size: inheriting --text-sm left the glyph the same size as the
   banner text, where the title and Start/Stop buttons overpowered it. Matches
   the sidebar nav icons so the heartbeat reads at a glance. */
.loop-banner-ico { color: var(--accent); font-weight: 700; font-size: 18px; line-height: 1; flex-shrink: 0; }
.loop-banner-text {
  flex: 1;
  font-size: var(--text-sm);
  color: var(--fg2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.loop-banner-text strong { color: var(--fg); }

/* Chat-level unread is title weight everywhere; chatUnread() is binary so a
   digit could only ever read "1". */
.loop-banner-manage { text-decoration: none; }
</style>
