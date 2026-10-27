<template>
  <div ref="panelEl" class="chat-panel" :class="{ 'chat-panel--rail': railShown }" @dragover.prevent="dragOver = true" @dragleave="dragOver = false" @drop.prevent="handleDrop" @click="handlePanelClick">
    <div v-if="dragOver" class="drop-overlay">Drop images to attach, or files to add their accessible path</div>

    <!-- Header. No page tag: the project context control below and the chat
         title already name this surface, so a "chat" marker beside them would
         repeat what the transcript underneath already says. This is also the
         most crowded header in the app, so the room goes to those two pieces
         and the action icons. -->
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
          <div class="header-breadcrumb">
            <div class="chat-title-row">
              <!-- Same 18px stroke icon in the same 30px box as the trailing header
                   actions, so the close control reads as one of them. It used to be
                   a `&times;` character at 20px, which is a different size from
                   every icon around it and sits on the text baseline rather than in
                   a box - visibly out of line beside the breadcrumb. It sits in the
                   title row so it shares the title's vertical centre: with the
                   project on a second line under the title, a close button outside
                   the row centred itself against both lines and drifted below the
                   title it belongs to. -->
              <button class="btn-icon close-btn" @click="$emit('close')" title="Close chat" aria-label="Close chat">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <line x1="18" y1="6" x2="6" y2="18"/>
                  <line x1="6" y1="6" x2="18" y2="18"/>
                </svg>
              </button>
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
              <!-- While the engine names a brand new chat there is no title to
                   show yet, and the placeholder the engine hands over ("New
                   Chat") is not one either. The shared shimmer stands in for it
                   until the real name arrives, and the close control and the
                   code badge stay put meanwhile. -->
              <span
                v-else-if="chat.title_status === 'pending'"
                class="chat-title-shimmer"
                role="status"
                aria-label="Naming conversation"
                title="Naming conversation"
              />
              <span
                v-else
                class="pane-title chat-title"
                :title="chat.title"
                @dblclick.stop="startEditTitle"
                @click.stop
              >{{ chat.title }}</span>
              <button
                v-if="codeModeOn && !chat.archived && !editingTitle"
                type="button"
                class="chat-code-badge"
                aria-label="Code mode is on. Show repository details"
                title="Code mode is on"
                @click.stop="showDetails('repo')"
              ><span class="chat-code-badge-tag" aria-hidden="true">›_</span></button>
            </div>
          </div>
        </div>
      </template>
      <template #actions>
        <!-- On a narrow pane the Chat details opener sits here, beside Archive,
             so it never covers the transcript. On a wide pane it floats in the
             chat body instead (below). -->
        <button
          v-if="!isWidePane"
          ref="inspectorTrigger"
          type="button"
          class="btn-icon work-inspector-trigger work-inspector-trigger--header"
          :aria-expanded="inspectorOpen"
          aria-controls="chat-work-rail"
          aria-label="Show chat details"
          title="Show chat details"
          @click="toggleWorkDetails"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><line x1="12" y1="11" x2="12" y2="16" /><line x1="12" y1="8" x2="12.01" y2="8" /></svg>
        </button>
        <!-- Archive is the header's one action, so it gets words, not just a
             glyph. Chat details opens from the info tab and hides from its own heading. -->
        <button
          v-if="!chat.archived"
          type="button"
          class="btn-icon chat-archive-btn"
          :class="{ 'chat-archive-btn--icon': !isWidePane }"
          :title="ARCHIVE_ACTION_LABEL"
          :aria-label="ARCHIVE_ACTION_LABEL"
          @click="doArchive"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/></svg>
          <span class="chat-archive-label">Archive</span>
        </button>
      </template>
    </PaneHeader>

    <!-- Body: one page grid. The left column holds everything that belongs to
         the conversation (context bar, transcript, dock, composer) so they all
         share the header title's left edge; the rail on the right is Work
         details, shown in place on panes wide enough for it. -->
    <div class="chat-body">
    <!-- Chat details, when it is not showing: the rail is hidden on a wide
         pane, or the pane is too narrow for one and it opens as a drawer. -->
    <button
      v-if="isWidePane && !railShown"
      ref="inspectorTrigger"
      type="button"
      class="btn-icon work-inspector-trigger"
      :aria-expanded="false"
      aria-controls="chat-work-rail"
      aria-label="Show chat details"
      title="Show chat details"
      @click="toggleWorkDetails"
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><line x1="12" y1="11" x2="12" y2="16" /><line x1="12" y1="8" x2="12.01" y2="8" /></svg>
    </button>
    <div class="chat-column">
    <!-- What this chat is, when nothing else on the surface says it. A memory
         pass is an ordinary chat in a hidden project, so the transcript reads
         as a conversation that began mid-thought; this names the conversation
         it belongs to. Only when the Chat details rail is hidden — with the
         rail shown the same sentence sits at its top instead. -->
    <p v-if="memoryPassOrigin && !railShown && !inspectorOpen" class="chat-origin-note">
      <AppIcon class="chat-origin-note-icon" name="spark" :size="16" />
      <span>Memory insight for <button type="button" class="chat-origin-link" @click="openMemoryPassSource">{{ memoryPassOrigin.title }}</button>.</span>
    </p>

    <!-- Messages + comment sidebar -->
    <div class="chat-with-sidebar">
    <div class="messages" :class="{ 'messages--empty': !blockingHistoryLoad && transcriptEmpty && !inputText.trim() }" ref="messagesEl" :aria-busy="store.messageHistoryLoading" :style="{ overflowAnchor: isNearBottom ? 'none' : 'auto' }" @click="handleHighlightClick" @mouseover="onChatHighlightHover" @mouseout="onChatHighlightHoverOut">
      <!-- Selecting a message (click or Enter) outlines it and shows its
         actions, leaving the rest of the transcript readable; clicking the
         message again or pressing Esc puts it back. -->
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
      <section
        v-if="!blockingHistoryLoad && transcriptEmpty && !inputText.trim()"
        class="chat-empty-state"
        aria-labelledby="chat-empty-title"
      >
        <h2 id="chat-empty-title">Start with a request</h2>
        <p class="chat-empty-context">
          This chat gets {{ project?.name ? `${project.name}'s` : "the project's" }} context.
          <button
            type="button"
            class="chat-empty-knowledge"
            @click="openProjectKnowledge"
          >See what Ciao knows</button>
        </p>
        <div class="chat-empty-starters" aria-label="Suggested first requests">
          <button
            v-for="starter in starterPrompts"
            :key="starter"
            type="button"
            class="chat-empty-starter"
            @click="useStarterPrompt(starter)"
          >{{ starter }}</button>
        </div>
      </section>
      <template v-if="!blockingHistoryLoad">
      <template v-for="(item, i) in renderItems" :key="item.key">
        <!-- Reasoning trace: intermediate assistant text + tool calls grouped.
             Rendering lives in ChatTurnActivity; ChatPanel keeps the open/closed
             map, the markdown cache and the file-viewer wiring. -->
        <ChatTurnActivity
          v-if="item.kind === 'trace'"
          :steps="item.steps"
          :subs="item.subs"
          :outputs="item.outputs"
          :outputs-open="Boolean(openOutputs[i])"
          :outputs-id="`outputs-${i}`"
          :open="Boolean(openTraces[i])"
          :chat-id="chat.chat_id"
          :thinking-expanded="thinkingExpanded"
          :render-markdown="renderMarkdown"
          :render-activity-line="renderActivityLine"
          :duration-ms="turnDurationAfter(i)"
          @toggle="toggleTrace(i)"
          @toggle-thinking="toggleThinking"
          @body-click="onTraceBodyClick(i, $event)"
          @toggle-outputs="toggleOutputs(i)"
          @open-file="openFileCard"
          @expand-step="expandLazyStep"
        />
        <!-- A delegated chat's first message is the engine's prompt for the
             agent, so it reads as the task that was handed over, with the
             prompt itself one disclosure away. -->
        <div v-else-if="item.kind === 'user' && taskHandover && item.msg === taskHandover.msg" class="message-wrap task-handover-wrap">
          <TaskHandoverCard :handover="taskHandover.handover" :prompt="item.msg.content" :known-paths="knownFilePaths" />
        </div>
        <!-- A turn the engine wrote (a wake, an automation run, a resume, a webhook,
             an update task) or a request the user retried reads as a notice, not
             as something the reader typed. -->
        <div
          v-else-if="item.kind === 'user' && injectedPrompts.get(item.msg)"
          class="message-wrap injected-prompt-wrap"
          :data-msg-id="item.msg.timestamp ? `msg-${item.msg.timestamp}` : `msg-user-${i}`"
          :data-msg-index="i"
          data-msg-role="user"
        >
          <InjectedPromptCard
            :prompt="injectedPrompts.get(item.msg)!"
            :raw="item.msg.content"
            :timestamp="item.msg.timestamp"
            :chat-title="chat.title"
            :schedule="injectedSchedule"
            :known-paths="knownFilePaths"
          />
        </div>
        <!-- User message -->
        <div v-else-if="item.kind === 'user'" class="message-wrap user">
          <div class="message-row">
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
                <div v-html="renderUserMessage(item.msg.content)"></div>
              </div>
              <!-- An automation's tick, not something the reader typed.
                   Without this the two are indistinguishable in the
                   transcript. -->
              <div v-if="item.msg.steered" class="message-meta">
                <span
                  class="steered-mark"
                  title="Sent into the running reply"
                >steered</span>
              </div>
              <div v-if="item.msg.unattended && !item.msg.origin" class="message-meta">
                <span
                  class="unattended-mark"
                  title="Sent automatically by an automation"
                >&#10227; auto</span>
              </div>
            </div>
            <!-- Copy and the send time for a request, under the bubble and in
                 the flow, so a 44px touch target cannot reach into the turn
                 below. The row always takes its height and only fades in on
                 hover or focus, so revealing it never reflows the transcript. -->
            <div v-if="item.msg.content?.trim() || item.msg.timestamp" class="message-actions">
              <span v-if="item.msg.timestamp" class="message-meta">{{ formatTime(item.msg.timestamp) }}</span>
              <template v-if="item.msg.content?.trim()">
              <button
                type="button"
                class="message-action-btn"
                :title="copiedMessageKey === `user-${i}` ? 'Copied' : 'Copy'"
                :aria-label="copiedMessageKey === `user-${i}` ? 'Copied' : 'Copy message'"
                @click="copyMessageText(item.msg.content, `user-${i}`)"
              >
                <svg v-if="copiedMessageKey === `user-${i}`" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
                <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/></svg>
                <span>{{ copiedMessageKey === `user-${i}` ? 'Copied' : 'Copy' }}</span>
              </button>
              </template>
            </div>
          </div>
        </div>
        <!-- Final assistant message -->
        <div v-else-if="item.kind === 'assistant'" class="message-wrap assistant">
          <div class="message-row">
            <div class="message assistant" :class="{ error: item.msg.is_error }" :data-msg-id="item.msg.timestamp ? `msg-${item.msg.timestamp}` : `msg-asst-${i}`" :data-msg-index="i" data-msg-role="assistant">
              <div class="message-content" v-html="renderMarkdown(item.msg.content)"></div>
              <div v-if="item.msg.is_error" class="error-attribution" role="status">
                <span class="error-attribution-label">{{ classifyError(item.msg.content).label }}</span>
                <span>{{ classifyError(item.msg.content).copy }}</span>
              </div>
            </div>
            <!-- The reply's own footer: Outputs, Copy, Fork, and the turn's
                 facts. The row always takes its height; Copy, Fork and the
                 facts fade in on hover or focus (always shown on touch), so
                 revealing them never reflows the transcript. -->
            <div v-if="item.msg.content?.trim() || item.meta || item.outputs?.length" class="message-actions">
              <!-- Outputs: collapsed by default, same disclosure shape as the
                   Activity summary (native button, chevron, aria-expanded).
                   It leads the footer row, always visible, and its list
                   opens on a line of its own under the whole row. -->
              <div v-if="item.outputs?.length" class="answer-outputs answer-outputs--footer">
                <button
                  type="button"
                  class="outputs-summary"
                  :aria-expanded="Boolean(openOutputs[i])"
                  :aria-controls="`outputs-${i}`"
                  @click.stop="toggleOutputs(i)"
                >
                  <span class="outputs-chevron" aria-hidden="true">{{ openOutputs[i] ? '▾' : '▸' }}</span>
                  <span class="outputs-label">Outputs</span>
                  <span class="outputs-count">&middot; {{ item.outputs.length }} {{ item.outputs.length === 1 ? 'file' : 'files' }}</span>
                  <span class="sr-only">, {{ openOutputs[i] ? 'expanded' : 'collapsed' }}</span>
                </button>
                <ul v-if="openOutputs[i]" :id="`outputs-${i}`" class="outputs-list">
                  <li v-for="(f, fi) in item.outputs" :key="fi" class="outputs-row">
                    <button
                      type="button"
                      class="outputs-link"
                      @click.stop="openFileCard(f.file_path)"
                      :title="f.file_path"
                    >
                      <span class="outputs-name">{{ fileCardBasename(f.file_path) }}</span>
                      <span class="outputs-open" aria-hidden="true">&#8599;</span>
                    </button>
                    <span class="outputs-tag" :class="{ 'outputs-tag--new': outputActionTag(f.action) === 'new' }">{{ outputActionTag(f.action) }}</span>
                  </li>
                </ul>
              </div>
              <template v-if="item.msg.content?.trim()">
              <button
                type="button"
                class="message-action-btn"
                :title="copiedMessageKey === `assistant-${i}` ? 'Copied' : 'Copy'"
                :aria-label="copiedMessageKey === `assistant-${i}` ? 'Copied' : 'Copy message'"
                @click="copyMessageText(item.msg.content, `assistant-${i}`)"
              >
                <svg v-if="copiedMessageKey === `assistant-${i}`" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
                <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/></svg>
                <span>{{ copiedMessageKey === `assistant-${i}` ? 'Copied' : 'Copy' }}</span>
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
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 4-6 3-12 7"/></svg>
                <span>{{ forkLoadingKey === `assistant-${i}` ? 'Forking…' : 'Fork' }}</span>
              </button>
              </template>
              <!-- One footer per turn, on its last bubble: the merged answer
                   carries the model and token usage, the turn's last row the
                   completion time and duration. `item.meta` is set only on the
                   closing bubble. Two units rather than one run of separators:
                   when and what ran it, then what it cost. Cost is tokens in
                   and out; the context-window share stays in the rail, which
                   shows it against the window's size. -->
              <div v-if="item.meta" class="message-meta">
                <span v-if="turnMetaText(item.meta)">{{ turnMetaText(item.meta) }}</span>
                <span v-if="formatTokenUsage(item.meta.usage)" class="tokens-group" v-html="formatTokenUsage(item.meta.usage)"></span>
              </div>
            </div>
          </div>
          <div v-if="item.msg.is_error" class="error-actions">
            <button
              v-if="lastUserBefore(i)"
              class="retry-btn"
              @click="retryFromError(i)"
            >Retry</button>
            <!-- See errorBubbleAction for which fix a known error gets. -->
            <button
              v-if="errorBubbleAction(i, item.msg.content) === 'retry-hourly'"
              class="retry-btn fix-btn"
              @click="retryHourlyFromError(i)"
            >Retry every hour</button>
            <button
              v-else-if="errorBubbleAction(i, item.msg.content) === 'switch-model'"
              class="retry-btn fix-btn"
              :disabled="store.isStreaming"
              @click="openRecoveryPicker(i)"
            >Switch model</button>
            <button
              v-else-if="errorBubbleAction(i, item.msg.content) === 'fix'"
              class="retry-btn fix-btn"
              @click="openFixChat(i)"
            >Fix this error</button>
          </div>
          <ProviderSignInHint
            v-if="isProviderAuthError(item.msg.content) && lastUserBefore(i) && !chat.archived"
            :provider="chat.provider"
            action-label="Retry"
            :action-disabled="store.isStreaming"
            @action="retryFromError(i)"
          />
        </div>
        <!-- System message (errors, etc) -->
        <!-- A background shell the provider ran itself: the same card as a
             `ciao run` wake, drawn from a system row so it never counts as a turn. -->
        <div
          v-else-if="item.kind === 'system' && injectedPrompts.get(item.msg)"
          class="message-wrap injected-prompt-wrap"
          :data-msg-id="item.msg.timestamp ? `msg-${item.msg.timestamp}` : `msg-sys-${i}`"
          :data-msg-index="i"
          data-msg-role="system"
        >
          <InjectedPromptCard
            :prompt="injectedPrompts.get(item.msg)!"
            :raw="''"
            :timestamp="item.msg.timestamp"
            :chat-title="chat.title"
            :schedule="injectedSchedule"
            :known-paths="knownFilePaths"
          />
        </div>
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
            <!-- See errorBubbleAction for which fix a known error gets. -->
            <button
              v-if="errorBubbleAction(i, item.msg.content) === 'retry-hourly'"
              class="retry-btn fix-btn"
              @click="retryHourlyFromError(i)"
            >Retry every hour</button>
            <button
              v-else-if="errorBubbleAction(i, item.msg.content) === 'switch-model'"
              class="retry-btn fix-btn"
              :disabled="store.isStreaming"
              @click="openRecoveryPicker(i)"
            >Switch model</button>
            <button
              v-else-if="errorBubbleAction(i, item.msg.content) === 'fix'"
              class="retry-btn fix-btn"
              @click="openFixChat(i)"
            >Fix this error</button>
          </div>
          <ProviderSignInHint
            v-if="isProviderAuthError(item.msg.content) && lastUserBefore(i) && !chat.archived"
            :provider="chat.provider"
            action-label="Retry"
            :action-disabled="store.isStreaming"
            @action="retryFromError(i)"
          />
        </div>
      </template>
      </template>

      <div v-if="chat.retry?.status === 'pending' && !store.isStreaming" class="retry-card">
        <div class="retry-card-main">
          <AppIcon class="retry-card-icon" name="clock" :size="18" />
          <div>
            <div class="retry-card-title">{{ retryCardTitle }}</div>
            <div class="retry-card-meta">
              <span v-if="chat.retry.next_at">Next try {{ formatRetryTime(chat.retry.next_at) }}</span>
              <span v-if="chat.retry.attempts"> · {{ chat.retry.attempts }} attempt{{ chat.retry.attempts === 1 ? '' : 's' }}</span>
            </div>
          </div>
        </div>
        <div class="retry-card-actions">
          <button class="btn-small" :disabled="store.isStreaming" @click="openRecoveryPicker(null)">Continue with...</button>
          <button class="btn-small" :disabled="store.isStreaming" @click="tryRetryNow">Try now</button>
          <button class="btn-small" @click="stopRetry">Stop trying</button>
        </div>
        <ProviderSignInHint
          v-if="isProviderAuthError(chat.retry.last_error)"
          class="retry-card-hint"
          :provider="chat.provider"
          action-label="Try now"
          :action-disabled="store.isStreaming"
          @action="tryRetryNow"
        />
      </div>

      <!-- Live reasoning trace: shown from the moment streaming starts.
           All in-progress content (tool calls, intermediate text, and current
           streaming text) stays inside this block. The final answer bubble
           only appears after the result event. -->
      <!-- The live turn starts as one expandable activity line. Full steps stay
           in its timeline; the answer bubble appears only after the result. -->
      <div v-if="store.isStreaming" class="trace-block live" :class="{ open: liveTraceOpen }">
        <button
          type="button"
          class="trace-summary"
          :aria-expanded="liveTraceOpen"
          @click="toggleLiveTrace"
        >
          <svg class="trace-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
          <span class="activity-spinner"></span>
          <span class="trace-label">Working</span>
          <span class="live-preview" aria-live="polite" aria-atomic="true">
            <Transition name="live-step" mode="out-in">
              <span :key="liveStepIdentity" class="live-preview-text">{{ liveStepPreview }}</span>
            </Transition>
          </span>
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
          ref="liveTraceBodyEl"
          class="trace-body"
          @click="onLiveTraceBodyClick"
          @scroll.passive="onLiveBodyScroll"
        >
          <template v-for="(entry, j) in store.currentTimeline" :key="j">
            <div v-if="entry.kind === 'tool'" class="trace-step trace-step--tool">
              <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m5 8 4 4-4 4M11 16h8" /></svg></span>
              <div class="trace-tools">
                <div
                  v-for="(line, k) in activityLines(entry.content)"
                  :key="k"
                  class="activity-line"
                  :class="{ subagent: isSubagentLine(line), failed: entry.kind === 'tool' && entry.failed?.[k] === true }"
                  v-html="renderActivityLine(line)"
                ></div>
              </div>
            </div>
            <div v-else-if="entry.kind === 'filecard'" class="trace-step trace-step--file">
              <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h9l3 3v15H6z" /></svg></span>
              <button
                type="button"
                class="file-card"
                @click="openFileCard(entry.file_path)"
                :title="entry.file_path"
              >
                <AppIcon class="file-card-icon" :name="fileCardIcon(entry.file_path)" :size="16" />
                <span class="file-card-main">
                  <span class="file-card-name">{{ fileCardBasename(entry.file_path) }}</span>
                  <span class="file-card-meta">
                    <span class="file-card-action">{{ entry.action }}</span>
                    <span v-if="fileCardDirname(entry.file_path)" class="file-card-dir"> · {{ fileCardDirname(entry.file_path) }}</span>
                  </span>
                </span>
                <span class="file-card-chevron" aria-hidden="true">&#8599;</span>
              </button>
            </div>
            <div v-else-if="entry.kind === 'thinking'" class="trace-step trace-step--thought">
              <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9V16h7v-2.1A6 6 0 0 0 12 3z" /></svg></span>
              <div class="thinking-block">
                <button
                  type="button"
                  class="thinking-toggle"
                  :aria-expanded="thinkingExpanded"
                  @click.stop="toggleThinking"
                >
                  <span>{{ thinkingExpanded ? 'Thought' : 'Thought (collapsed)' }}</span>
                </button>
                <div v-if="thinkingExpanded" class="trace-text trace-thinking" v-html="renderMarkdown(entry.content)"></div>
              </div>
            </div>
            <div
              v-else-if="entry.kind === 'status'"
              class="trace-text trace-status"
              v-html="renderMarkdown(entry.content)"
            ></div>
            <div v-else class="trace-step trace-step--note">
              <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v11H9l-5 4z" /></svg></span>
              <div class="trace-text" v-html="renderMarkdown(entry.content)"></div>
            </div>
          </template>
          <div v-if="store.currentStreamingThinking" class="trace-step trace-step--thought trace-step--now">
            <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9V16h7v-2.1A6 6 0 0 0 12 3z" /></svg></span>
            <div class="thinking-block">
              <button
                type="button"
                class="thinking-toggle"
                :aria-expanded="thinkingExpanded"
                @click.stop="toggleThinking"
              >
                <span>{{ thinkingExpanded ? 'Thinking' : 'Thinking (collapsed)' }}</span>
              </button>
              <div v-if="thinkingExpanded" class="trace-text trace-thinking trace-streaming" v-html="renderMarkdown(store.currentStreamingThinking)"></div>
            </div>
          </div>
          <div v-if="store.currentStreamingText" class="trace-step trace-step--note trace-step--now">
            <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v11H9l-5 4z" /></svg></span>
            <div class="trace-text trace-streaming" v-html="renderMarkdown(store.currentStreamingText)"></div>
          </div>
          <!-- Subagents for the in-flight turn nest in the live trace -->
          <div v-if="liveSubagents.length" class="trace-step trace-step--subagent">
            <span class="trace-step-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="3" /><path d="M6 20a6 6 0 0 1 12 0" /></svg></span>
            <SubagentPanel :subagents="liveSubagents" :chat-id="chat.chat_id" />
          </div>
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
        <button class="question-card-dismiss" :disabled="questionSubmitting" @click="dismissQuestions" title="Dismiss">&times;</button>
      </div>
      <p v-if="activeQuestions[0]?.stale" class="question-card-note">The engine restarted. Your answer will be sent as a message.</p>
      <template v-for="(q, qi) in activeQuestions" :key="qi">
        <div v-if="isQuestionActive(qi, q)" class="question-block">
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
            :key="`${opt.value ?? opt.label}-${oi}`"
            type="button"
            class="question-option"
            :class="{ selected: isQuestionOptionSelected(qi, opt.value ?? opt.label) }"
            :aria-keyshortcuts="questionOptionShortcut(qi, oi) || undefined"
            :disabled="questionSubmitting"
            @blur="markQuestionTouched(qi)"
            @click="toggleQuestionOption(qi, opt.value ?? opt.label, q.multiSelect)"
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
          v-if="q.allowOther && isQuestionActive(qi, q)"
          :type="q.isSecret ? 'password' : questionInputType(q)"
          class="question-other"
          placeholder="Other (free text)"
          :disabled="questionSubmitting"
          :minlength="q.minLength"
          :maxlength="q.maxLength"
          :pattern="q.pattern"
          :min="q.minimum"
          :max="q.maximum"
          :step="q.type === 'integer' ? 1 : undefined"
          :aria-invalid="Boolean(questionValidationMessage(q, qi))"
          :value="questionAnswers[qi]?.other || ''"
          @blur="markQuestionTouched(qi)"
          @input="setQuestionOther(qi, ($event.target as HTMLInputElement).value)"
        />
        <p
          v-if="questionValidationMessage(q, qi)"
          class="question-validation"
        >{{ questionValidationMessage(q, qi) }}</p>
        </div>
      </template>
      <div v-if="questionError" class="question-card-error">{{ questionError }}</div>
      <div class="question-card-actions">
        <button class="btn-sm" type="button" :disabled="questionSubmitting" @click="dismissQuestions">Cancel</button>
        <button class="btn-sm primary" type="button" :disabled="questionSubmitting || !allQuestionsAnswered" @click="submitQuestionAnswers">
          {{ questionSubmitting ? 'Sending…' : 'Send answer' }}
        </button>
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
             :disabled="permissionSubmittingFor(p.request_id, p.session_id)"
            @click="store.respondPermission(chat.chat_id, p.request_id, false, 'User denied', p.session_id || '')"
          ><span v-if="permissionShortcut('deny')" class="permission-key" aria-hidden="true">{{ permissionShortcut('deny') }}</span>Deny</button>
          <button
            class="btn-approve"
            :aria-keyshortcuts="permissionShortcut('approve') || undefined"
             :disabled="permissionSubmittingFor(p.request_id, p.session_id)"
            @click="store.respondPermission(chat.chat_id, p.request_id, true, '', p.session_id || '')"
          ><span v-if="permissionShortcut('approve')" class="permission-key" aria-hidden="true">{{ permissionShortcut('approve') }}</span>Approve</button>
        </div>
        <div v-if="permissionErrorFor(p.request_id, p.session_id)" class="question-card-error">{{ permissionErrorFor(p.request_id, p.session_id) }}</div>
      </div>
    </div>

    <!-- The command the assistant suggested for this chat. A transient card
         above the queue and the composer, so a suggestion waiting to be
         reviewed is never buried under the transcript. It is shown, copied,
         opened or dismissed — never run. -->
    <TerminalOfferCard
      v-if="terminalOffer"
      :chat-id="chat.chat_id"
      :offer="terminalOffer"
    />

    <!-- Queued messages (sent while a response was already streaming). -->
    <div v-if="store.currentQueued.length && (!dockPrimary || dockExpanded)" class="queued-messages">
      <div v-if="steerAllIds.length >= 2" class="queued-head">
        <span class="queued-count">{{ store.currentQueued.length }} queued</span>
        <button
          type="button"
          class="queued-steer-all"
          :title="store.isStreaming ? 'Send these into the running reply as one message' : 'Send these now as one message'"
          @click="steerQueued($event, steerAllIds, 'steer')"
        >{{ store.isStreaming ? 'Steer all' : 'Send all now' }}</button>
      </div>
      <div
        v-for="(q, i) in store.currentQueued"
        :key="q.id || i"
        class="queued-chip"
        tabindex="-1"
        :data-queue-id="q.id"
        :class="{ 'is-steering': q.steering }"
        :title="queuedChipTitle(q)"
      >
        <span class="queued-steering">{{ q.steering ? 'Steering…' : '' }}</span>
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
        <div v-if="!q.steering" class="queued-controls">
          <div
            v-if="store.isStreaming && !q.waiting"
            class="queued-mode"
            role="group"
            :aria-label="`What “${queuedExcerpt(q.text)}” will do`"
          >
            <button
              type="button"
              class="queued-mode-option"
              :aria-pressed="q.delivery === 'queue'"
              title="Send as the next message when the reply finishes"
              @click="q.delivery === 'queue' || steerQueued($event, [q.id], 'queue')"
            >Queue</button>
            <button
              type="button"
              class="queued-mode-option"
              :aria-pressed="q.delivery === 'steer'"
              title="Send into the running reply now"
              @click="q.delivery === 'steer' || steerQueued($event, [q.id], 'steer')"
            >Steer</button>
          </div>
          <span v-else class="queued-label">Waiting</span>
          <div class="queued-actions">
            <button
              v-if="q.waiting || !store.isStreaming"
              type="button"
              class="queued-send-now"
              :title="store.isStreaming ? 'Send this into the running reply now' : 'Start a turn with this message now'"
              @click="steerQueued($event, [q.id], 'steer')"
            >{{ store.isStreaming ? 'Steer' : 'Send now' }}</button>
            <button
              class="queued-action"
              :disabled="i === 0"
              title="Move up"
              aria-label="Move up"
              @click="store.reorderQueued(chat.chat_id, i, i - 1)"
            ><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 15l6-6 6 6"/></svg></button>
            <button
              class="queued-action"
              :disabled="i === store.currentQueued.length - 1"
              title="Move down"
              aria-label="Move down"
              @click="store.reorderQueued(chat.chat_id, i, i + 1)"
            ><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button>
            <button
              v-if="editingQueueId !== q.id"
              class="queued-action"
              title="Edit"
              aria-label="Edit queued message"
              @click="startEditQueue(q)"
            ><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/></svg></button>
            <template v-else>
              <button class="queued-action" title="Save" aria-label="Save edit" @click="saveEditQueue(chat.chat_id, q.id)"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7"/></svg></button>
              <button class="queued-action" title="Cancel" aria-label="Cancel edit" @click="cancelEditQueue"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
            </template>
            <button class="queued-remove" @click="store.removeQueued(chat.chat_id, i)" title="Remove" aria-label="Remove queued message"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
          </div>
        </div>
      </div>
    </div>
    <!-- One live region for the whole queue, always mounted: a chip that
         appears already in the Steering state is announced by this text
         changing, not by a new element. -->
    <span class="queue-live sr-only" aria-live="polite">{{ steeringAnnouncement }}</span>
    <!-- Outside the queue block: the fallback reply usually empties it, and a
         collapsed dock hides it, but the notice must still be read. -->
    <p v-if="store.steerNotice[chat.chat_id]" class="steer-notice" role="status">{{ store.steerNotice[chat.chat_id] }}</p>

    <!-- Dock strip: one counted line for everything not expanded above, so the
         dock never grows past one card plus this. Rendered whenever there is
         anything to collapse OR anything already expanded, so the disclosure
         works in both directions. Background agents and runs are not here: they
         are the composer bar's activity cluster. -->
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
              {{ isContinuing ? 'Continuing...' : heldSettledTask ? 'Continue the task in a new chat' : 'Continue in new chat' }}
            </button>
          </div>
          <!-- The memory pass this chat spawned, if one did. The pass lives in
               a project the sidebar hides, so this is the durable way back to
               it. -->
          <button
            v-if="archiveMemoryPassChatId"
            class="btn-sm archive-memory-pass-btn"
            type="button"
            @click="openMemoryPass"
          >Open memory pass</button>
        </div>
      </template>
      <template v-else>
        <!-- The command surface: staged attachments, the prompt, then one bar
             with the model, attach and the forward action. Same
             shape as Home's composer so the two read as one control. -->
        <div class="composer-surface">
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
          <div
            class="input-actions composer-bar"
            ref="composerBarEl"
            tabindex="-1"
            role="group"
            aria-label="Message composer"
          >
            <!-- The model picker's trigger lives in the composer, where the next
                 message is written, as on Home. The selector itself (thinking
                 levels, Option+M) is unchanged; it opens upward from here. -->
            <div class="model-picker-wrap composer-model" ref="modelPickerRef">
              <button
                type="button"
                class="model-picker-summary composer-chip"
                :title="`${routingProviderLabel(activeBucket, chat.provider)} · ${chipModelLabel}${chipThinkingLabel ? ' · ' + chipThinkingLabel : ''}`"
                aria-haspopup="listbox"
                :aria-expanded="showModelPicker"
                @click.stop="toggleModelPicker"
              >
                <span class="composer-model-dot" aria-hidden="true"></span>
                <span class="composer-chip-label"><template v-if="chat.provider">{{ routingProviderLabel(activeBucket, chat.provider) }} · </template>{{ chipModelLabel }}<template v-if="chipThinkingLabel"> · {{ chipThinkingLabel }}</template></span>
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m7 9 5 5 5-5" /></svg>
              </button>
              <ModelSelector
                v-if="showModelPicker"
                triggerless
                :model-value="canonicalTier(activeModelId)"
                :active-models="activeModelHighlights"
                :sections="chatModelSections"
                :filter-section="capabilityPickerSection"
                placeholder="Model"
                placement="top-start"
                :labelled-by="isRecoveryPicker ? 'model-picker-recovery-title' : ''"
                :described-by="isRecoveryPicker ? 'model-picker-recovery-note' : ''"
                @select="selectModel"
                @close="showModelPicker = false"
              >
                <!-- Recovery only. Here a pick is not a model change but a
                     retry of the turn that failed, so the picker says so
                     before the user chooses. The thinking chips below it are
                     the current model's set: a pick applies and retries in
                     one go, so there is no moment where the chips could
                     follow a model the user has not chosen yet. The note
                     says so, and a staged level the chosen model does not
                     offer is dropped when the pick is applied. -->
                <template v-if="isRecoveryPicker" #header>
                  <div class="model-picker-recovery">
                    <p id="model-picker-recovery-title" class="model-picker-recovery__title">Choose a model to retry immediately</p>
                    <p id="model-picker-recovery-note" class="model-picker-recovery__note">
                      Choosing a model retries the failed request and may incur usage charges. A Thinking level you pick here applies to that retry, within the levels the current model offers.
                    </p>
                  </div>
                </template>
                <template #footer>
                  <div
                    v-if="showThinkingLevels"
                    class="thinking-levels"
                  >
                    <span id="thinking-levels-label" class="thinking-levels__label">Thinking</span>
                    <div class="thinking-levels__chips" role="group" aria-labelledby="thinking-levels-label">
                      <button
                        v-for="level in ['', ...filteredThinkingLevels]"
                        :key="level"
                        type="button"
                        class="thinking-chip"
                        :class="{ 'thinking-chip--active': thinkingChipLevel === level }"
                        :aria-pressed="thinkingChipLevel === level"
                        @click="selectThinking(level)"
                      >
                        {{ level ? level.charAt(0).toUpperCase() + level.slice(1) : 'Auto' }}
                      </button>
                    </div>
                  </div>
                </template>
              </ModelSelector>
            </div>
            <label class="image-btn" title="Upload images" aria-label="Upload images">
              <input type="file" accept="image/*" multiple hidden @change="handleFileSelect" />
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
            </label>
            <!-- Background work the turn left running: agents and command runs, right
                 against Send. Fills the bar's free space, so it is also the spacer. -->
            <ComposerActivity
              :chat-id="chat.chat_id"
              :agent-count="composerAgentCount"
              :agents="runningRows"
              :runs="chatRuns"
              :finished="store.finishedBackgroundRuns[chat.chat_id] ?? null"
              :now="nowTs"
              :stop-task="(taskId: string) => store.stopBackgroundTask(chat.chat_id, taskId)"
              @open-details="openRunDetails"
            />
            <span v-if="inputText.trim()" class="composer-kbd" aria-hidden="true"><kbd>{{ sendChordLabel }}</kbd> send</span>
            <button
              v-if="showMoveToBackground"
              ref="moveBtnEl"
              type="button"
              class="composer-chip composer-move-bg"
              :disabled="movingToBackground"
              aria-label="Move to background"
              title="Move the running command or subagent to the background. The reply ends now and the result arrives when it finishes."
              @click="moveToBackground"
            >
              <span class="composer-move-bg-long">Move to background</span>
              <span class="composer-move-bg-short" aria-hidden="true">Background</span>
            </button>
            <!-- While streaming: empty composer → stop; any draft → queue. -->
            <button
              class="send-btn"
              :class="{ 'is-stop': showStopAction }"
              :disabled="!showStopAction && !canSend"
              :title="primaryActionTitle"
              :aria-label="primaryActionLabel"
              @click="primaryAction"
            >
              <svg v-if="showStopAction" class="stop-icon" width="14" height="14" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" /></svg>
              <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 14-7-4 14-3-6z" /><path d="M12 13 19 5" /></svg>
            </button>
          </div>
        </div>
      </template>
    </div>
    </div>

    <!-- Chat details. One panel at every width: in place against the pane's
         right edge when there is room, and over the chat as a drawer when
         there is not — the same stacked sections either way, read by
         scrolling, with one close button. -->
    <aside
      v-if="railShown || drawerShown"
      id="chat-work-rail"
      ref="railEl"
      class="chat-rail"
      :class="{ 'chat-rail--drawer': drawerShown }"
      :role="drawerShown ? 'dialog' : undefined"
      :aria-modal="drawerShown ? 'true' : undefined"
      aria-labelledby="chat-work-rail-title"
    >
      <div class="chat-rail-head">
        <h2 id="chat-work-rail-title" class="rail-title">Chat details</h2>
        <button
          ref="railHideButton"
          type="button"
          class="btn-icon chat-rail-hide"
          aria-expanded="true"
          aria-controls="chat-work-rail"
          aria-label="Hide chat details"
          title="Close chat details"
          @click="closeDetails"
        ><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg></button>
      </div>
      <!-- Where this chat came from, above everything else: one line naming the
           automation that runs here, the board task a delegated chat works on,
           and — for the one app-owned chat — the conversation its memory pass
           is distilling. Its cadence and controls live on the automation's own
           page. -->
      <p v-for="s in chatSchedules" :key="`rail-sched-${s.schedule_id}`" class="chat-rail-origin">
        <AppIcon class="chat-rail-origin-icon" name="clock" :size="16" />
        <span>This chat comes from the automation <router-link :to="`/schedules/${s.schedule_id}`">{{ s.title || 'Automation' }}</router-link>.</span>
      </p>
      <TaskOriginNote :chat="chat" @marked-done="archiveAfterTaskDone" />
      <p v-if="memoryPassOrigin" class="chat-rail-origin">
        <AppIcon class="chat-rail-origin-icon" name="spark" :size="16" />
        <span>Memory insight for <button type="button" class="chat-origin-link" @click="openMemoryPassSource">{{ memoryPassOrigin.title }}</button>.</span>
      </p>
      <!-- The linked repository and its Code mode switch; the header's `code` badge opens here. -->
      <div v-if="project?.repo" ref="railRepoEl" class="chat-rail-repo" tabindex="-1">
        <ChatRepoSection
          :repo="project.repo"
          :location="chat.repo_location ?? null"
          :chat-id="chat.chat_id"
          :code-mode="codeModeOn"
          :code-mode-locked="codeModeLocked"
          :archived="!!chat.archived"
        />
      </div>
      <!-- The terminal is not a repository feature. A chat with no linked
           folder still needs a shell — a provider sign-in, an install, an MCP
           server that stopped answering — so every chat has a terminal row,
           outside the repository's block; the row is its own label. -->
      <div class="chat-rail-terminal">
        <BrowserTerminalButton :chat-id="chat.chat_id" variant="row" />
      </div>
      <!-- What the agent is given. The project also lives here on wide panes;
           the header only names it when this rail is hidden. -->
      <div ref="railContextEl" class="chat-rail-agent-context" tabindex="-1">
        <AgentContextSection
          :project="project"
          :code-mode="codeModeOn"
          :context-pct="contextPct"
          :compacting="compacting"
          :compact-disabled="compactDisabled"
          :reveal-key="contextRevealKey"
          @open-file="openInspectorFile"
          @compact="compactContext"
        />
      </div>
      <section
        v-if="runningSubagents.length || chatRuns.length"
        ref="railRunningEl"
        class="rail-section chat-rail-running"
        aria-labelledby="chat-rail-running"
        tabindex="-1"
      >
        <p id="chat-rail-running" class="rail-label">Running</p>
        <!-- Agents first: an agent has a transcript to open, a process only a log. -->
        <template v-if="runningSubagents.length">
          <p class="chat-rail-subhead">Agents</p>
          <div class="rail-list">
            <router-link
              v-for="sub in runningSubagents"
              :key="sub.agent_id"
              :to="subagentPath(chat.chat_id, sub.agent_id)"
              class="rail-item chat-rail-agent"
            >
              <ActivityGlyph kind="working" />
              <span class="chat-rail-subagent">{{ subagentLabel(sub) }}</span>
              <small v-if="sub.subagent_type">{{ sub.subagent_type }}</small>
              <span class="chat-rail-agent-arrow" aria-hidden="true">→</span>
            </router-link>
          </div>
        </template>
        <template v-if="chatRuns.length">
          <p class="chat-rail-subhead">Processes</p>
          <BackgroundRunRows :chat-id="chat.chat_id" :runs="chatRuns" :now="nowTs" />
        </template>
      </section>
      <section v-if="toolUsage.skills.length || toolUsage.mcp.length" class="rail-section" aria-labelledby="chat-rail-tools">
        <p id="chat-rail-tools" class="rail-label">Skills and MCP used</p>
        <div class="rail-kvs">
          <div v-for="tool in shownTools" :key="`${tool.kind}-${tool.name}`" class="rail-kv">
            <span class="chat-rail-tool" :title="tool.tools?.join(', ')"><small>{{ tool.kind === 'skill' ? 'Skill' : 'MCP' }}</small> {{ tool.name }}</span>
            <strong v-if="tool.count > 1" :title="`${tool.count} calls`">×{{ tool.count }}</strong>
          </div>
        </div>
        <button
          v-if="railTools.length > RAIL_LIST_MAX"
          type="button"
          class="chat-rail-more"
          :aria-expanded="railShowAll.tools"
          @click="railShowAll.tools = !railShowAll.tools"
        >{{ railShowAll.tools ? 'Show fewer' : `Show all ${railTools.length}` }}</button>
      </section>
      <section class="rail-section" aria-labelledby="chat-rail-files">
        <p id="chat-rail-files" class="rail-label">Files</p>
        <div v-if="inspectorOutputs.length" class="rail-list">
          <button
            v-for="output in shownOutputs"
            :key="`${output.action}:${output.file_path}`"
            type="button"
            class="rail-item chat-rail-file"
            :title="output.file_path"
            @click="openFileCard(output.file_path)"
          >
            <span>{{ fileCardBasename(output.file_path) }}</span>
            <small>{{ outputActionTag(output.action) }}<template v-if="shortDirname(output.file_path)"> · {{ shortDirname(output.file_path) }}</template></small>
          </button>
        </div>
        <button
          v-if="inspectorOutputs.length > RAIL_LIST_MAX"
          type="button"
          class="chat-rail-more"
          :aria-expanded="railShowAll.files"
          @click="railShowAll.files = !railShowAll.files"
        >{{ railShowAll.files ? 'Show fewer' : `Show all ${inspectorOutputs.length}` }}</button>
        <p v-if="!inspectorOutputs.length && !mentionedFiles.length" class="rail-note">None yet.</p>
        <template v-if="mentionedFiles.length">
          <p class="rail-label chat-rail-sublabel">Mentioned in replies</p>
          <div class="rail-list">
            <button
              v-for="path in shownMentioned"
              :key="`mentioned-${path}`"
              type="button"
              class="rail-item chat-rail-file"
              :title="path"
              @click="openFileCard(path)"
            >
              <span>{{ fileCardBasename(path) }}</span>
              <small>{{ shortDirname(path) || 'workspace' }}</small>
            </button>
          </div>
          <button
            v-if="railMentioned.length > RAIL_LIST_MAX"
            type="button"
            class="chat-rail-more"
            :aria-expanded="railShowAll.mentioned"
            @click="railShowAll.mentioned = !railShowAll.mentioned"
          >{{ railShowAll.mentioned ? 'Show fewer' : `Show all ${railMentioned.length}` }}</button>
        </template>
      </section>
    </aside>
    </div>

  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useProjectStore } from '../stores/projects'
import { errorMessage } from '../lib/errorMessage'
import { isApplePlatform } from '../lib/platform'
import { useKeyboardSettings } from '../composables/useKeyboardSettings'
import { memoryPassChatId, memoryPassSource } from '../lib/memoryPass'
import { useFileViewerStore } from '../stores/fileViewer'
import { useTerminalStore } from '../stores/terminal'
// Subagent transcripts carry `turn_index` (the user turn that dispatched
// them, parsed server-side from the session JSONL), so each panel anchors
// under the turn that spawned its agents.
import SubagentPanel from './SubagentPanel.vue'
import AgentContextSection from './AgentContextSection.vue'
import ChatTurnActivity from './ChatTurnActivity.vue'
import { api } from '../lib/api'
import { askConfirm } from '../lib/confirm'
import { recordSentPrompt } from '../lib/chatDrafts'
import { useModalFocus } from '../composables/useModalFocus'
import { useArchiveChat } from '../composables/useArchiveChat'
import type { AgentAssetsResponse, CommandsResponse, RuntimeProvider, ModelsResponse, ChatMessage, SlashCommand, SubagentTranscript } from '../lib/types'
import { useTaskStore } from '../stores/tasks'
import PaneHeader from './PaneHeader.vue'
import ChatRepoSection from './ChatRepoSection.vue'
import BrowserTerminalButton from './BrowserTerminalButton.vue'
import TerminalOfferCard from './TerminalOfferCard.vue'
import ModelSelector from './ModelSelector.vue'
import { ARCHIVE_ACTION_LABEL } from '../lib/archiveCopy'
import AppIcon from './AppIcon.vue'
import ProviderSignInHint from './ProviderSignInHint.vue'
import TaskOriginNote from './TaskOriginNote.vue'
import { useTaskSignalsStore } from '../stores/taskSignals'
import { isLiveAttemptState } from '../lib/taskBoard'
import type { TaskAttemptActionResponse } from '../lib/types'
import TaskHandoverCard from './TaskHandoverCard.vue'
import { parseTaskHandover, type TaskHandover } from '../lib/taskHandover'
import InjectedPromptCard from './InjectedPromptCard.vue'
import { parseInjectedPrompt, type InjectedPrompt } from '../lib/injectedPrompt'
import { linkifyText } from '../lib/filePaths'
import { isChatId } from '../lib/chatLinks'
import { sectionsFromModelsResponse } from '../lib/modelSections'
import { canOfferMoveToBackground, shouldRestoreFocus } from '../lib/moveToBackground'
import { renderMarkdown as renderSafeMarkdown, renderUserMarkdown as renderSafeUserMarkdown } from '../lib/safeMarkdown'
import { handleCodeCopyClick, writeClipboard } from '../lib/codeCopy'
import { handleOpenLocalFileClick } from '../lib/openWorkspaceFile'
import { classifyError, errorQuickAction, isProviderAuthError } from '../lib/errorAttribution'
import { formatTime, formatDuration } from '../lib/time'
import { isChatCodeModeOn, isCodeModeLocked } from '../lib/codeMode'
import BackgroundRunRows from './BackgroundRunRows.vue'
import ComposerActivity from './ComposerActivity.vue'
import ActivityGlyph from './ActivityGlyph.vue'
import {
  activityLines,
  buildTurnParts,
  collectToolUsage,
  collectTraceOutputs,
  fileCardBasename,
  fileCardDirname,
  fileCardIcon,
  findFinalAnswerIndex,
  isInterruptedTail,
  isPermissionActivityLine,
  formatTokenUsage,
  isImageFilePath,
  isScratchPath,
  isSubagentLine,
  liveToolStep,
  permissionActivityLine,
  mentionedFilePaths,
  mergeTraceOutputs,
  collapseOutputsByName,
  shortDirname,
  outputActionTag,
  type TraceOutput,
} from '../lib/chatActivity'
import { buildForkSnapshot } from '../lib/chatFork'
import { formatCommentLocation, type ChatCommentAnchor } from '../lib/commentContext'
import {
  cleanCommentSelection,
  commentTextMatches,
  commentTextOccurrenceIndex,
  escapeCssAttrValue,
  highlightCommentText,
} from '../lib/commentHighlight'
import { questionAnswerError, questionAnswerIsValid, questionIsActive, type ActiveQuestion } from '../lib/chatQuestions'
import { clampAnchorLeft, clampAnchorTop } from '../lib/popoverAnchor'
import {
  useMentionPicker,
  type MentionAgent,
  type MentionChat,
  type MentionFile,
  type MentionProject,
} from '../composables/useMentionPicker'
import { useThinkingPreference } from '../composables/useThinkingPreference'
import { useTypeToComment } from '../composables/useTypeToComment'
import { useChatComposer } from '../composables/useChatComposer'
import ChatCommentPopover from './ChatCommentPopover.vue'
import CommentComposePopover from './CommentComposePopover.vue'
import { isShellRow, subagentLabel, subagentPath } from '../lib/subagentIds'
import { handleAppLinkClick } from '../lib/appLinks'

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


// `filePinned`: a file is docked beside this chat (ChatLayout's split view).
const props = defineProps<{ filePinned?: boolean }>()
const emit = defineEmits<{ close: [], 'open-sidebar': [] }>()

const store = useProjectStore()
const fileViewer = useFileViewerStore()
const { thinkingExpanded, toggleThinking } = useThinkingPreference()
const draftChatId = store.activeChatId
// The composer, its persisted draft, prompt-history recall and every
// attachment path (paste, drop, native desktop drop, the image picker) live
// in useChatComposer. ChatPanel keeps the send path, the pickers and the
// scroll behaviour, and hands the composable the ids it needs as getters.
const composer = useChatComposer({
  draftChatId,
  chatId: () => chat.value?.chat_id || '',
  projectId: () => chat.value?.project_id,
  workspace: () => store.activeWorkspace,
  vaultFolder: () => project.value?.vault_folder,
  store,
})
const inputText = composer.draft
const inputEl = composer.input
const dragOver = composer.dragOver
const starterPrompts = [
  'Review the latest project notes and flag what needs a decision',
  'Turn the current research into a concise briefing',
]

async function useStarterPrompt(prompt: string) {
  if (inputText.value.trim()) return
  inputText.value = prompt
  await nextTick()
  autoResize()
  inputEl.value?.focus()
}
const {
  autoResize,
  handleDrop,
  handleFileSelect,
  handleNativeFileDragEnter,
  handleNativeFileDragLeave,
  handleNativeFileDrop,
  handlePaste,
  handlePromptHistoryKey,
  insertImageRef,
  removePendingImage,
} = composer
const isContinuing = ref(false)

// Ticks once a second while streaming or while a background run is going, so
// the live elapsed-time labels (the "Working..." trace meta, a run's age)
// advance.
const nowTs = ref(Date.now())
let clockTimer: ReturnType<typeof setInterval> | null = null
watch(() => store.isStreaming || store.activeBackgroundRuns.length > 0, (streaming) => {
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
const moveBtnEl = ref<HTMLButtonElement | null>(null)
const composerBarEl = ref<HTMLElement | null>(null)
const movingToBackground = ref(false)
const showMoveToBackground = computed(() =>
  store.isStreaming && canOfferMoveToBackground(store.activeBackgroundableCalls, nowTs.value),
)
async function moveToBackground() {
  if (movingToBackground.value || !chat.value) return
  const chipEl = moveBtnEl.value
  const hadFocus = document.activeElement === chipEl
  movingToBackground.value = true
  try {
    await store.moveChatToBackground(chat.value.chat_id)
  } finally {
    movingToBackground.value = false
  }
  // The chip goes away the moment the turn ends, so focus has to be handed
  // somewhere that is still there — but only when it is still ours, which
  // `shouldRestoreFocus` decides.
  if (hadFocus) {
    const active = document.activeElement
    if (shouldRestoreFocus(chipEl, active)) {
      // Never the send/stop button. It is a single element in three states —
      // enabled Stop while the turn runs, enabled Send once a draft exists,
      // disabled Send with an empty draft — and the move we just asked for ends
      // the turn, so it can be disabled a moment after we focus it; a disabled
      // control cannot hold focus and it falls to <body> (rounds 1 and 3). The
      // bar is the one target here that is always focusable and never raises
      // the on-screen keyboard, which is why the textarea is not used below
      // COMPOSER_FOCUS_MIN_WIDTH.
      if (window.innerWidth >= COMPOSER_FOCUS_MIN_WIDTH) inputEl.value?.focus({ preventScroll: true })
      else composerBarEl.value?.focus()
    }
  }
}
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
const chat = computed(() => store.activeChat!)

/**
 * The command the assistant suggested for this chat, if it holds one.
 *
 * The offer is transient and engine-owned: the browser keeps it in memory and
 * reads it from the store's map, never from the chat row, because a row's
 * copy is the payload the list was read at and the map is the live one. An
 * archived chat holds no offer — the command died with the chat — so the
 * card is not shown for one.
 */
const terminalOffer = computed(() => {
  const current = chat.value
  if (!current || current.archived) return null
  const offer = store.terminalOffers[current.chat_id]
  return offer ? offer : null
})

// When the card that holds the focus goes away — dismissed by the user, or
// dropped by the engine over the socket — it unmounts and the focus would
// fall to <body>. Hand it to the composer instead, the way the queue hands
// it over when a chip disappears. The watch is on the card's own render
// condition, so the handoff also happens when the chat is archived or gone.
// A replacement patches the card in place, so the focus belongs to the card
// that is still there.
watch(
  () => terminalOffer.value,
  (offer, previous) => {
    // Only a removal unmounts the card.
    if (offer !== null || previous === null) return
    const held = (document.activeElement as HTMLElement | null)?.closest<HTMLElement>('.terminal-offer')
    if (!held) return
    void nextTick(() => {
      // The textarea would raise the soft keyboard on a phone and scroll the
      // transcript out from under the reader, so the bar takes the focus
      // there — the same rule the move-to-background chip follows.
      if (window.innerWidth >= COMPOSER_FOCUS_MIN_WIDTH) inputEl.value?.focus({ preventScroll: true })
      else composerBarEl.value?.focus()
    })
  },
  { flush: 'pre' },
)

const archivePostprocess = computed(() => store.chatPostprocess(chat.value.chat_id))
// The memory pass this chat spawned, recorded on its own postprocess record.
// Present from the
// moment the pass is enqueued, so the link works while it is still running and
// after the pass is archived.
const archiveMemoryPassChatId = computed(() => memoryPassChatId(archivePostprocess.value))
function openMemoryPass(): void {
  if (archiveMemoryPassChatId.value) void store.switchChat(archiveMemoryPassChatId.value)
}

/**
 * The conversation this chat is a memory pass for, or null when the chat is an
 * ordinary one. The pass is the app's own chat in a project the sidebar hides
 * and the tiers do not list, so nothing else on the surface says what it is or
 * which conversation it belongs to. Null rather than an empty title, because a
 * pass whose source was deleted still needs a row on Home and a composer here,
 * and a bare "memory insight" line with nothing to open is worse than none.
 */
const memoryPassOrigin = computed<{ chatId: string; title: string } | null>(() => {
  const source = memoryPassSource(chat.value)
  if (!source) return null
  const title = chat.value.memory_source?.title || source.title
  if (!title) return null
  return { chatId: source.chatId, title }
})

/**
 * A delegated chat's first message, read back as the task that was handed
 * over, or null. Only for a chat the engine stamped as a task delegation, only
 * its first user message, and only when that message carries the fenced
 * description `build_prompt` writes; anything else renders as typed.
 */
const taskHandover = computed<{ msg: ChatMessage; handover: TaskHandover } | null>(() => {
  if (chat.value.helper?.kind !== 'task_delegation') return null
  const msg = store.activeMessages.find(m => m.role === 'user')
  if (!msg) return null
  const handover = parseTaskHandover(msg.content)
  return handover ? { msg, handover } : null
})

/** Engine-written user turns (wakes, automation runs, resumes, webhooks, update
 *  tasks) and a retried request, keyed by message identity like taskHandover. */
const injectedPrompts = computed(() => {
  const map = new Map<ChatMessage, InjectedPrompt>()
  for (const m of store.activeMessages) {
    // A user turn the engine wrote, or the system row the backend draws for a
    // background shell the provider ran itself.
    if (m.role !== 'user' && !(m.role === 'system' && m.origin === 'background_run')) continue
    const parsed = parseInjectedPrompt(m.content, m.origin)
    if (parsed) map.set(m, parsed)
  }
  return map
})

// The source is archived, so the transcript is the only thing it can open. The
// browser's chat list holds active chats only, so the path comes from the pass's
// own `memory_source`, which the engine sends on the row.
function openMemoryPassSource(): void {
  const archivePath = chat.value.memory_source?.archive_path
  if (archivePath) void fileViewer.open(archivePath)
}
watch(() => chat.value.provider, () => {
  void loadSlashCommands()
})

// Inline editing state for queued messages. Keyed by queue entry id.
const editingQueueId = ref<string | null>(null)
const editingQueueText = ref('')
// The edit UI only edits text, so hold the entry's images and re-send them on
// save — otherwise the backend clears attachments it resolves from an empty list.
const editingQueueImages = ref<string[] | undefined>(undefined)

// Entries that "Steer all" combines: queue-mode ones not already handed over,
// in list order.
const steerAllIds = computed(() =>
  store.currentQueued.filter(q => q.delivery === 'queue' && !q.steering).map(q => q.id),
)

const steeringAnnouncement = computed(() => {
  const n = store.currentQueued.filter(q => q.steering).length
  return n ? `Steering ${n} ${n === 1 ? 'message' : 'messages'}` : ''
})

// When the chip that holds focus leaves the queue (it was steered, sent or
// removed), focus would fall to <body>. Hand it to the composer instead.
watch(
  () => store.currentQueued.map(q => q.id),
  (ids) => {
    const held = (document.activeElement as HTMLElement | null)?.closest<HTMLElement>('.queued-chip')
    const id = held?.dataset.queueId
    if (!id || ids.includes(id)) return
    void nextTick(() => {
      if (window.innerWidth >= COMPOSER_FOCUS_MIN_WIDTH) inputEl.value?.focus({ preventScroll: true })
      else composerBarEl.value?.focus()
    })
  },
  { flush: 'pre' },
)

function queuedExcerpt(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > 40 ? `${flat.slice(0, 40)}…` : flat
}

// The clicked button is replaced once the chip changes state, which would drop
// keyboard focus to the page. Hand it to the chip (or the composer when the
// chip is gone) so the next Tab starts from a sensible place.
function steerQueued(event: Event, ids: string[], delivery: 'queue' | 'steer') {
  const chip = (event.currentTarget as HTMLElement | null)?.closest<HTMLElement>('.queued-chip') ?? null
  store.setQueuedDelivery(chat.value.chat_id, ids, delivery)
  void nextTick(() => {
    if (chip?.isConnected) chip.focus()
    else inputEl.value?.focus()
  })
}

function queuedChipTitle(q: { delivery: string; steering?: boolean }): string {
  if (q.steering) return 'Handed to the running reply; waiting for it to take the message'
  if (!store.isStreaming) return 'Held until you send it, edit it or remove it'
  return q.delivery === 'steer'
    ? 'Will be sent into the running reply'
    : 'Will be sent when current response finishes'
}

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

const injectedSchedule = computed<{ id: string; title: string } | null>(() => {
  const linked = chatSchedules.value[0]
  const id = linked?.schedule_id || chat.value.schedule_id || ''
  return id ? { id, title: linked?.title || chat.value.schedule_title || 'Automation' } : null
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
  return items
})

onMounted(() => {
  taskStore.fetchSchedules().catch(() => {})
})

const project = computed(() => store.activeProject)
const codeModeOn = computed(() => isChatCodeModeOn(chat.value, project.value?.repo))
// The chat payload's turn count only refreshes on refetch, so a user row in the
// loaded transcript locks the switch as soon as the first message is sent.
const codeModeLocked = computed(() =>
  isCodeModeLocked(chat.value)
  || (codeModeOn.value && store.activeMessages.some(m => m.role === 'user')))

// A turn's duration lives on its closing bubble's footer meta. The Activity
// row sits before that bubble, so look ahead within the same turn.
function turnDurationAfter(index: number): number | undefined {
  for (let j = index + 1; j < renderItems.value.length; j += 1) {
    const next = renderItems.value[j]
    if (next.kind === 'user' || next.kind === 'trace') return undefined
    if (next.kind === 'assistant' && next.meta?.duration_ms) return next.meta.duration_ms
  }
  return undefined
}

// Time · duration · model for a turn's footer, joined without a stray
// leading separator when the first field is missing.
function turnMetaText(meta: { timestamp?: string; duration_ms?: number; effective_model?: string }): string {
  return [
    meta.timestamp ? formatTime(meta.timestamp) : '',
    meta.duration_ms ? formatDuration(meta.duration_ms) : '',
    meta.effective_model || '',
  ].filter(Boolean).join(' · ')
}

// The reply whose action row stays visible: the newest assistant message.

/** Chat details opened as a drawer, on a pane too narrow to show it in place. */
const inspectorOpen = ref(false)
const inspectorTrigger = ref<HTMLButtonElement | null>(null)
// One row per file name: the same note shows up under several spellings
// (relative, workspace-prefixed, before and after a move), and the reader
// thinks of it as one file.
// Temp-dir files are the agent's scratch work; the inline cards still show
// them, but the chat's file list is what it made for the user.
// Newest first: the merge keeps each file where it is first met, so walking
// the turns backwards puts a file at its latest touch.
const inspectorOutputs = computed<TraceOutput[]>(() => collapseOutputsByName(mergeTraceOutputs(
  renderItems.value.map(item => (item.kind === 'trace' || item.kind === 'assistant' ? item.outputs : undefined))
    .reverse()
    .map(outputs => outputs && [...outputs].reverse()),
).filter(output => !isScratchPath(output.file_path))))
// Every activity line this chat has produced: its turns, the subagents they
// ran, and the turn in flight. The rail reads skills and MCP tools from it.
const chatActivityLines = computed<string[]>(() => {
  const lines: string[] = []
  const take = (messages: ChatMessage[] | undefined) => {
    for (const m of messages ?? []) {
      if (m.tool_name === '_activity' && m.content) lines.push(...activityLines(m.content))
    }
  }
  for (const item of renderItems.value) {
    if (item.kind !== 'trace') continue
    take(item.steps)
    for (const sub of item.subs ?? []) take(sub.messages)
  }
  for (const entry of store.currentTimeline) {
    if (entry.kind === 'tool') lines.push(...activityLines(entry.content))
  }
  return lines
})
const toolUsage = computed(() => collectToolUsage(chatActivityLines.value))

// Paths the replies name that no turn recorded as a file card - typically a
// file a delegate or a shell command wrote. Listed as "mentioned", never as
// produced, because the chat has no record of the write itself.
const mentionedFiles = computed<string[]>(() => {
  const produced = new Set(inspectorOutputs.value.map(output => fileCardBasename(output.file_path)))
  const found: string[] = []
  for (const item of renderItems.value) {
    if (item.kind !== 'assistant' || !item.msg.content) continue
    for (const path of mentionedFilePaths(item.msg.content)) {
      if (isScratchPath(path)) continue
      const name = fileCardBasename(path)
      if (produced.has(name) || found.some(p => fileCardBasename(p) === name)) continue
      found.push(path)
    }
  }
  return found
})

// The rail's lists show the five most recent entries; the rest are one click
// away. Turns are kept in order, so newest-first is the order reversed.
const RAIL_LIST_MAX = 5
const railShowAll = ref({ files: false, mentioned: false, tools: false })
watch(() => chat.value?.chat_id, () => { railShowAll.value = { files: false, mentioned: false, tools: false } })
const railMentioned = computed(() => [...mentionedFiles.value].reverse())
const railTools = computed(() => [
  ...toolUsage.value.skills.map(skill => ({ ...skill, kind: 'skill' as const, tools: undefined as string[] | undefined })),
  ...toolUsage.value.mcp.map(server => ({ ...server, kind: 'mcp' as const })),
])
const shownOutputs = computed(() => railShowAll.value.files ? inspectorOutputs.value : inspectorOutputs.value.slice(0, RAIL_LIST_MAX))
const shownMentioned = computed(() => railShowAll.value.mentioned ? railMentioned.value : railMentioned.value.slice(0, RAIL_LIST_MAX))
const shownTools = computed(() => railShowAll.value.tools ? railTools.value : railTools.value.slice(0, RAIL_LIST_MAX))

// How full the model's context window was at the end of the latest turn, from
// the usage the provider reported (the last model call of that turn). This is
// the transcript's only statement of occupancy: the Chat details rail shows it
// as a meter, because a percentage means nothing without the window's size
// beside it. Providers report it as text ("13.2%"), so parseFloat, not
// Number. Null when it reported none.
const contextPct = computed<number | null>(() => {
  const items = renderItems.value
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i]
    if (item.kind !== 'assistant' || !item.meta?.usage) continue
    const usage = item.meta.usage as Record<string, unknown>
    const pct = Number.parseFloat(String(usage.context_pct ?? usage.contextPct ?? ''))
    return Number.isFinite(pct) && pct > 0 ? pct : null
  }
  return null
})
const compacting = computed(() => Boolean(chat.value && store.compactRequests[chat.value.chat_id]))
const compactDisabled = computed(() => store.isStreaming || compacting.value || Boolean(chat.value?.archived))
function compactContext() {
  if (!chat.value || compactDisabled.value) return
  void store.compactChat(chat.value.chat_id)
}

// The poll's rows include background shells (`subagent_type: "shell"`); the
// composer lists those as processes, everything else counts as an agent.
const runningRows = computed(() => (chat.value ? store.runningSubagentsFor(chat.value.chat_id) : []))
const runningSubagents = computed(() => runningRows.value.filter(sub => !isShellRow(sub)))
const chatRuns = computed(() => (chat.value?.archived ? [] : store.activeBackgroundRuns))
// Same rule as the sidebar row (ChatSignals): the watcher's pushed count and
// the running-subagent poll tick on different clocks, so take the larger.
const composerAgentCount = computed(() => Math.max(store.activeBackgroundAgents, runningSubagents.value.length))


// On a pane wide enough for the rail beside a full reading column, Chat
// details is shown in place; narrower, the same panel opens over the chat as
// a drawer. Measured on the panel rather than the window, because the sidebar
// and a pinned file both take width from this pane.
const panelEl = ref<HTMLElement | null>(null)
const railEl = ref<HTMLElement | null>(null)
const railContextEl = ref<HTMLElement | null>(null)
const railRepoEl = ref<HTMLElement | null>(null)
const isWidePane = ref(false)
// Chat details steps aside while a file is pinned: the tile is the window on
// the right then, and two docked windows would leave the chat a sliver. It
// comes back when the file is unpinned, because ChatLayout mounts a separate
// ChatPanel for each layout, so the prop never changes on a live instance.
// The info tab still opens it on demand.
const railOpen = ref(!props.filePinned)
const railShown = computed(() => isWidePane.value && railOpen.value)
const drawerShown = computed(() => !isWidePane.value && inspectorOpen.value)
let panelObserver: ResizeObserver | null = null
onMounted(() => {
  if (typeof ResizeObserver === 'undefined' || !panelEl.value) return
  panelObserver = new ResizeObserver(entries => {
    const width = entries[0]?.contentRect.width ?? 0
    isWidePane.value = width > 940
    if (isWidePane.value) inspectorOpen.value = false
  })
  panelObserver.observe(panelEl.value)
})
onBeforeUnmount(() => { panelObserver?.disconnect() })

const railHideButton = ref<HTMLButtonElement | null>(null)
const railRunningEl = ref<HTMLElement | null>(null)

/**
 * Show Chat details: in place on a wide pane, as the drawer otherwise. Focus
 * goes to the section the caller names, or to the close button, so it never
 * drops on a trigger that just unmounted. The drawer's focus trap places it,
 * so the named section has to be its initial focus rather than a later move.
 */
type DetailsTarget = 'close' | 'running' | 'context' | 'repo'
const detailsTarget = ref<DetailsTarget>('close')
const detailsFocusEl = computed<HTMLElement | null>(() => {
  if (detailsTarget.value === 'running') return railRunningEl.value ?? railHideButton.value
  if (detailsTarget.value === 'context') return railContextEl.value ?? railHideButton.value
  if (detailsTarget.value === 'repo') return railRepoEl.value ?? railHideButton.value
  return railHideButton.value
})
// Bumped when a caller points at the agent context, so its Always loaded
// disclosure opens instead of focusing a closed summary.
const contextRevealKey = ref(0)
function showDetails(target: DetailsTarget = 'close') {
  detailsTarget.value = target
  // The rail may not be mounted yet (narrow drawer, or a wide rail that
  // starts closed), and its watch is lazy: bump the key once it exists.
  const reveal = () => { if (target === 'context') contextRevealKey.value += 1 }
  if (!isWidePane.value) {
    // The trap hands focus back to whatever held it on open: make that the
    // trigger, so Escape returns there and not to wherever the click came from.
    inspectorTrigger.value?.focus()
    inspectorOpen.value = true
    void nextTick(reveal)
    return
  }
  railOpen.value = true
  void nextTick(() => {
    reveal()
    const el = detailsFocusEl.value
    el?.focus()
    if (el && target !== 'close') el.scrollIntoView({ block: 'nearest' })
  })
}
function closeDetails() {
  if (isWidePane.value) railOpen.value = false
  else inspectorOpen.value = false
  void nextTick(() => inspectorTrigger.value?.focus())
}
function toggleWorkDetails() {
  showDetails()
}
// The composer bar's activity cluster opens Chat details at Running; a finished
// run's notice can outlive the section it points at.
function openRunDetails() {
  showDetails('running')
}
function openProjectKnowledge() {
  showDetails('context')
}
useModalFocus(railEl, drawerShown, {
  initialFocus: detailsFocusEl,
  onEscape: () => { inspectorOpen.value = false },
})

// The browser terminal takes the screen on a narrow pane, and the chat
// details drawer is a modal surface there: while it is up, everything outside
// it is inert, and it claims Escape in the capture phase. The terminal is
// opened from this drawer — its Terminal section carries the button — so
// the drawer steps aside when the terminal opens, rather than leaving a
// dead, inert pane on top of the screen.
const terminal = useTerminalStore()
watch(() => terminal.visible, (open) => {
  if (open && drawerShown.value) inspectorOpen.value = false
})

async function openInspectorFile(path: string) {
  inspectorOpen.value = false
  await nextTick()
  openFileCard(path)
}

watch(() => chat.value?.chat_id, () => {
  inspectorOpen.value = false
})

const models = ref<string[]>(['haiku', 'sonnet', 'opus', 'fable'])
const providerModels = ref<Record<string, string[]>>({})
const providerDefaults = ref<Record<string, string>>({})
const modelsResponse = ref<ModelsResponse | null>(null)

const thinkingLevels = ref<Record<string, string[]>>({})

const openTraces = ref<Record<number, boolean>>({})
// Outputs disclosure per render item. Collapsed by default: the file list is
// a reference, not part of the reply, and a turn that touched many files used
// to push the answer off screen behind a wall of pills.
const openOutputs = ref<Record<number, boolean>>({})
// The live trace starts compact; explicit expansion is kept through completion.
// `Reading` means they scrolled away from the tail (transcript or trace body),
// so the finished trace must not move under them.
const liveTraceOpen = ref(false)
const liveTraceTouched = ref(false)
const liveTraceReading = ref(false)
const liveBodyAtBottom = ref(true)
const liveTraceBodyEl = ref<HTMLElement>()
// Render index of a finished trace held open for a reader; it collapses when
// they return to the bottom.
const pendingCollapseIdx = ref<number | null>(null)
// Armed once the reader has actually moved away from the bottom while the
// finished trace is held open; only then does returning to the bottom collapse it.
const pendingCollapseArmed = ref(false)
let lastScrollTop = 0
let lastBodyScrollTop = 0
const copiedMessageKey = ref<string | null>(null)
const forkLoadingKey = ref<string | null>(null)
const commentComposeDraftRef = ref<InstanceType<typeof CommentComposePopover> | null>(null)
const commentComposeEditRef = ref<InstanceType<typeof CommentComposePopover> | null>(null)
const isNearBottom = ref(true)
let messagesResizeObserver: ResizeObserver | null = null
const showScrollBtn = computed(() => Boolean(messagesEl.value && store.activeMessages.length > 0 && !isNearBottom.value))
const showModelPicker = ref(false)
const modelPickerRef = ref<HTMLElement>()

// Which picker is open. 'normal' is the composer's model chip: a pick there
// only changes the model. 'recovery' is opened from a failed turn - the
// retry card's "Continue with..." or the error bubble's "Switch model" - and
// a pick there retries that turn on the model the user chose.
type PickerMode = 'normal' | 'recovery'

interface RecoveryContext {
  chatId: string
  // The error bubble to resend. Null for a deferred retry, which the engine
  // resumes on its own schedule.
  errorIdx: number | null
}

const pickerMode = ref<PickerMode>('normal')
const recoveryContext = ref<RecoveryContext | null>(null)
// A recovery stages its thinking level instead of saving it: the level only
// counts if the model the user picks supports it, and the picker stays open
// until a model is picked.
const stagedThinkingLevel = ref<string | null>(null)
// One pick at a time. A second click while the first is still applying must
// not start a second turn.
const modelSelectBusy = ref(false)

const isRecoveryPicker = computed(() => pickerMode.value === 'recovery')

// The chip that reads as chosen: the staged level in a recovery (it belongs
// to the retry, not to the chat yet), the chat's own level otherwise.
const thinkingChipLevel = computed(() => {
  if (pickerMode.value === 'recovery' && stagedThinkingLevel.value !== null) {
    return stagedThinkingLevel.value
  }
  return chat.value?.thinking_level || ''
})

// A recovery belongs to the chat it was opened in: closing the picker,
// switching chat or unmounting drops it, so a later pick cannot retry a turn
// in another conversation.
function resetPickerState() {
  pickerMode.value = 'normal'
  recoveryContext.value = null
  stagedThinkingLevel.value = null
}

watch(() => chat.value?.chat_id, () => {
  resetPickerState()
})

onBeforeUnmount(() => {
  resetPickerState()
})

interface ContextProjectFile {
  path: string
  vault_path: string
  kind: 'markdown' | 'image' | 'text' | 'binary'
  size: number
  mtime: string
}
const projectFiles = ref<ContextProjectFile[]>([])
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
    return
  }
  try {
    const resp = await fetch(`/api/projects/${project.value.project_id}/files`, {
      credentials: 'same-origin',
    })
    projectFiles.value = resp.ok ? await resp.json() : []
  } catch {
    projectFiles.value = []
  }
}

// Feeds the @-mention picker's file list.
watch(
  () => [project.value?.project_id, project.value?.vault_folder] as const,
  () => { if (project.value?.vault_folder) loadProjectFiles() },
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

function toggleOutputs(i: number) {
  openOutputs.value = { ...openOutputs.value, [i]: !openOutputs.value[i] }
}

function toggleLiveTrace() {
  liveTraceTouched.value = true
  liveTraceOpen.value = !liveTraceOpen.value
  if (liveTraceOpen.value) {
    // The body remounts, so it follows its tail again from the top.
    liveBodyAtBottom.value = true
    lastBodyScrollTop = 0
  }
}

function resetLiveTraceState() {
  liveTraceOpen.value = false
  liveTraceTouched.value = false
  liveTraceReading.value = false
  liveBodyAtBottom.value = true
  pendingCollapseIdx.value = null
  pendingCollapseArmed.value = false
  lastBodyScrollTop = 0
}

function onLiveBodyScroll() {
  const el = liveTraceBodyEl.value
  if (!el) return
  // Only upward movement means the reader left the tail; content growth also
  // fires scroll events and must not latch.
  if (el.scrollHeight - el.scrollTop - el.clientHeight <= 4) {
    liveBodyAtBottom.value = true
  } else if (el.scrollTop < lastBodyScrollTop - 1) {
    liveBodyAtBottom.value = false
    liveTraceReading.value = true
  }
  lastBodyScrollTop = el.scrollTop
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
  if (target.closest('.file-card, .answer-outputs')) return true
  if (target.closest('.subagent-rows')) return true
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
  liveTraceTouched.value = true
  liveTraceOpen.value = false
}

function checkScroll() {
  const el = messagesEl.value
  if (!el) return
  const threshold = 4
  isNearBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight <= threshold
  const movedUp = el.scrollTop < lastScrollTop - 1
  lastScrollTop = el.scrollTop
  if (store.isStreaming && movedUp && !isNearBottom.value) liveTraceReading.value = true
  const collapseIdx = pendingCollapseIdx.value
  if (collapseIdx !== null && !store.isStreaming) {
    if (!isNearBottom.value) {
      pendingCollapseArmed.value = true
    } else if (pendingCollapseArmed.value) {
      openTraces.value = { ...openTraces.value, [collapseIdx]: false }
      pendingCollapseIdx.value = null
      pendingCollapseArmed.value = false
      nextTick(landOnReply)
    }
  }
  onChatScrollReanchor()
  // Paginated history: near the top of a windowed timeline, pull the older
  // page in. The store's loadingOlder flag debounces repeat fires.
  if (el.scrollTop <= 80) {
    const chatId = store.activeChatId
    if (chatId && store.canLoadOlderByChat[chatId] && !store.isLoadingOlderByChat[chatId]) {
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

// Land the viewport on the start of the reply when it is taller than the
// viewport, otherwise at the bottom. Direct scrollTop assignments only.
function landOnReply() {
  const el = messagesEl.value
  if (!el) return
  // Only a reply that follows the last user message belongs to this turn.
  const users = el.querySelectorAll<HTMLElement>('[data-msg-role="user"]')
  const lastUserEl = users.length ? users[users.length - 1] : null
  const replies = Array.from(el.querySelectorAll<HTMLElement>('[data-msg-role="assistant"]'))
    .filter(r => !lastUserEl || Boolean(lastUserEl.compareDocumentPosition(r) & Node.DOCUMENT_POSITION_FOLLOWING))
  const reply = replies.length ? replies[replies.length - 1] : null
  // A deliberate landing wins over the chat-open bottom pin.
  releasePin()
  if (!reply) {
    scrollToBottom()
  } else if (reply.offsetHeight > el.clientHeight - 32) {
    const delta = reply.getBoundingClientRect().top - el.getBoundingClientRect().top
    el.scrollTop = el.scrollTop + delta - 16
  } else {
    scrollToBottom()
  }
  checkScroll()
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
const permissionSubmission = computed(() => {
  const id = store.activeChatId
  return id ? store.permissionSubmissions[id] : undefined
})
function permissionSubmittingFor(requestId: string, sessionId = '') {
  const submission = permissionSubmission.value
  return submission?.requestId === requestId
    && (!sessionId || !submission.sessionId || submission.sessionId === sessionId)
    && submission.pending === true
}
function permissionErrorFor(requestId: string, sessionId = '') {
  const submission = permissionSubmission.value
  return submission?.requestId === requestId
    && (!sessionId || !submission.sessionId || submission.sessionId === sessionId)
    ? submission.error
    : ''
}

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

const questionSubmission = computed(() => {
  const id = store.activeChatId
  const submission = id ? store.questionSubmissions[id] : undefined
  const requestId = activeQuestions.value[0]?.requestId
  return submission && requestId === submission.requestId ? submission : undefined
})
const questionSubmitting = computed(() => questionSubmission.value?.pending === true)
const questionError = computed(() => questionSubmission.value?.error || '')

function questionAnswerMap(limit = activeQuestions.value.length): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (let i = 0; i < Math.min(limit, activeQuestions.value.length); i++) {
    const q = activeQuestions.value[i]
    // Conditions are ordered: only answers from fields that are still active
    // may control later fields. Keeping an old answer from a now-hidden field
    // would leave chained dependent questions visible and submit stale data.
    if (!questionIsActive(q, out, activeQuestions.value)) continue
    const answer = questionAnswers.value[i]
    const values = answer ? [...answer.selected] : []
    if (answer?.other?.trim()) values.push(answer.other.trim())
    if (values.length) out[q.id] = values
  }
  return out
}

function isQuestionActive(index: number, question: ActiveQuestion): boolean {
  return questionIsActive(question, questionAnswerMap(index), activeQuestions.value)
}

// The card is only on screen when it wins the dock, or when the dock strip is
// expanded behind a permission. Named because the keyboard shortcuts below key
// off the same condition as the template: a hidden card must not eat digits.
const questionCardVisible = computed(() =>
  activeQuestions.value.length > 0 && (dockPrimary.value === 'question' || dockExpanded.value),
)

type QuestionAnswer = { selected: Set<string>; other: string }
const questionAnswers = ref<Record<number, QuestionAnswer>>({})
const questionTouched = ref<Record<number, boolean>>({})

// Reset per-question selections whenever the active chat changes or the
// model fires a fresh AskUserQuestion. Watching the array reference catches
// both "new chat" and "new questions in same chat" without us touching the
// answers or validation state by hand.
watch(activeQuestions, () => {
  questionAnswers.value = {}
  questionTouched.value = {}
})

function markQuestionTouched(i: number) {
  if (!questionTouched.value[i]) questionTouched.value = { ...questionTouched.value, [i]: true }
}

function questionValidationMessage(question: ActiveQuestion, index: number): string | null {
  return questionTouched.value[index]
    ? questionAnswerError(question, questionAnswers.value[index])
    : null
}

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
    // A scalar V2 field cannot submit both a predefined option and Other.
    a.other = ''
  }
  // Force reactivity since Set mutations aren't tracked.
  questionAnswers.value = { ...questionAnswers.value, [i]: { ...a } }
}

function setQuestionOther(i: number, value: string) {
  const a = ensureAnswer(i)
  a.other = value
  const question = activeQuestions.value[i]
  if (question && !question.multiSelect) a.selected.clear()
  questionAnswers.value = { ...questionAnswers.value, [i]: { ...a } }
}

function questionInputType(q: ActiveQuestion): string {
  if (q.isSecret) return 'password'
  if (q.type === 'number' || q.type === 'integer') return 'number'
  if (q.format === 'email') return 'email'
  if (q.format === 'uri') return 'url'
  if (q.format === 'date') return 'date'
  if (q.format === 'date-time') return 'datetime-local'
  return 'text'
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
    store.respondPermission(
      chat.value.chat_id,
      first.request_id,
      false,
      'User denied',
      first.session_id || '',
    )
    return true
  }
  if (e.key === '2') {
    store.respondPermission(
      chat.value.chat_id,
      first.request_id,
      true,
      '',
      first.session_id || '',
    )
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
  toggleQuestionOption(0, opt.value ?? opt.label, q.multiSelect)
  return true
}

// Block Send answer until every active field is present and satisfies the
// provider's V2 constraints. Optional fields may remain empty; non-empty
// values still need to be valid before the request leaves the browser.
const allQuestionsAnswered = computed(() => {
  const qs = activeQuestions.value
  if (!qs.length) return false
  const answers = questionAnswerMap()
  return qs.every((q, index) => (
    !questionIsActive(q, answers, qs)
      || questionAnswerIsValid(q, questionAnswers.value[index])
  ))
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
  if (questionSubmitting.value || !allQuestionsAnswered.value) return
  if (!chat.value || chat.value.archived) return
  const qs = activeQuestions.value
  if (!qs.length) return
  const lines: string[] = []
  const nativeAnswers: Record<string, string[]> = {}
  for (let i = 0; i < qs.length; i++) {
    const q = qs[i]
    if (!isQuestionActive(i, q)) continue
    const a = questionAnswers.value[i]
    const picked = a ? Array.from(a.selected) : []
    const other = (a?.other || '').trim()
    const parts: string[] = []
    if (picked.length) parts.push(...picked)
    if (other) parts.push(other)
    if (parts.length) nativeAnswers[q.id] = parts
    const answer = parts.length ? parts.join(', ') : '(no answer)'
    lines.push(`**${questionPromptLabel(q, i)}**: ${answer}`)
  }
  const requestId = qs[0]?.requestId || ''
  const sessionId = qs[0]?.sessionId || ''
  if (requestId) {
    if (sessionId) {
      store.respondQuestion(
        chat.value.chat_id,
        requestId,
        nativeAnswers,
        'reply',
        sessionId,
      )
    } else {
      store.respondQuestion(chat.value.chat_id, requestId, nativeAnswers, 'reply')
    }
    return
  }
  const text = lines.join('\n')
  // sendMessage clears activeQuestions for this chat automatically.
  store.sendMessage(chat.value.chat_id, text)
}

function dismissQuestions() {
  if (questionSubmitting.value) return
  const id = store.activeChatId
  if (!id) return
  const requestId = activeQuestions.value[0]?.requestId || ''
  const sessionId = activeQuestions.value[0]?.sessionId || ''
  if (requestId) {
    if (sessionId) store.respondQuestion(id, requestId, {}, 'cancel', sessionId)
    else store.respondQuestion(id, requestId, {}, 'cancel')
  } else {
    // Claude picker has no round-trip; remember it as resolved so a stale
    // server snapshot can't rebuild it after dismissal.
    store.markResolvedQuestion(id)
    delete store.activeQuestions[id]
    questionAnswers.value = {}
  }
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
// catalog reports levels. The chips are the current model's set, in a
// recovery too: the pick applies the model and retries in one go, so the
// chips cannot follow a model the user has not chosen yet. A recovery's
// staged level is validated against the picked model in selectModel, which
// drops it when that model does not offer it.
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
  const comments = store.pendingChatComments.length + store.pendingComments.length
  if (comments) return `Reply — ${comments} comment${comments === 1 ? '' : 's'} will be sent with it`
  if (store.isStreaming) return 'Reply — it is queued until Ciao finishes'
  return 'Reply to Ciao'
})
const keyboard = useKeyboardSettings()
const touchKeyboard = () => typeof window !== 'undefined'
  && typeof window.matchMedia === 'function'
  && window.matchMedia('(pointer: coarse)').matches
const enterSends = () => keyboard.settings.value.keyboard_send_mode === 'enter' && !touchKeyboard()
const sendChordLabel = computed(() => enterSends() ? '↩' : (isApplePlatform() ? '⌘↩' : 'Ctrl+↩'))


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

// Selecting transcript text and typing (or pasting) opens the
// composer directly, so the "Comment" pill is a hint rather than a required
// click.
useTypeToComment({
  isActive: () => !!selectionAnchor.value && !commentDraft.value,
  open: (initialText: string) => openCommentForSelection(initialText),
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

let modelsSeq = 0
async function loadModels(): Promise<void> {
  const seq = ++modelsSeq
  try {
    const r = await api.get<ModelsResponse>('/api/models')
    // Quick reopens can resolve out of order; only the newest request lands.
    if (seq !== modelsSeq) return
    modelsResponse.value = r
    models.value = r.models
    providerModels.value = r.provider_models || {}
    providerDefaults.value = r.provider_defaults || {}
    thinkingLevels.value = r.thinking_levels || {}
  } catch { /* use defaults */ }
}

onMounted(async () => {
  window.addEventListener('ciao:native-file-drag-enter', handleNativeFileDragEnter)
  window.addEventListener('ciao:native-file-drag-leave', handleNativeFileDragLeave)
  window.addEventListener('ciao:native-file-drop', handleNativeFileDrop)
  await loadModels()
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
  composer.persistDraft()
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

// The conversations the @-mention picker offers are the conversations this
// reader can open. Use that same set for message references so a reference
// can never link a chat the picker omits (see lib/chatLinks.ts).
function reachableChatLinks(): [string, string][] {
  const pairs: [string, string][] = []
  for (const chat of mentionChats.value) {
    if (!isChatId(chat.chat_id)) continue
    pairs.push([chat.chat_id, chat.title || ''])
  }
  return pairs
}

// A reference links to the conversation's title, so a renamed conversation
// changes the rendering too. The key is the id/title pairs themselves, sorted
// by id: the number of chats says nothing about whether a title moved.
const knownChatLinksKey = computed(() => JSON.stringify(
  reachableChatLinks().sort((a, b) => a[0].localeCompare(b[0])),
))

// The map the renderers take. It is derived from the key, so it is rebuilt
// only when an id or a title actually changes: a chat-list poll that returns
// the same conversations leaves the transcript alone.
const knownChatLinks = computed(() => {
  const known = new Map<string, string>()
  const pairs = JSON.parse(knownChatLinksKey.value) as [string, string][]
  for (const [id, title] of pairs) known.set(id, title)
  return known
})

const mdCache = new Map<string, string>()
// User bubbles render through a different pipeline (raw HTML is escaped, see
// lib/safeMarkdown.ts), so the two renderings cannot share one text-keyed cache.
const userMdCache = new Map<string, string>()
const MAX_MD_CACHE_SIZE = 500
// Rendered output depends on the known-file-path set (for linkification), so
// drop the cache whenever that set actually changes — keying on text alone is
// then safe, and avoids a stale render when paths change without the text.
watch(() => knownFilePaths.value.join('|'), () => {
  mdCache.clear()
  userMdCache.clear()
})
watch(knownChatLinksKey, () => {
  mdCache.clear()
  userMdCache.clear()
})

function renderMarkdown(text: string): string {
  if (!text) return ''
  const cached = mdCache.get(text)
  if (cached !== undefined) return cached

  const rendered = renderSafeMarkdown(text, knownFilePaths.value, knownChatLinks.value, { openLocal: true })
  if (mdCache.size >= MAX_MD_CACHE_SIZE) {
    // Plain FIFO eviction — fine for chat scroll-back.
    const firstKey = mdCache.keys().next().value
    if (firstKey !== undefined) mdCache.delete(firstKey)
  }
  mdCache.set(text, rendered)
  return rendered
}

function renderUserMessage(text: string): string {
  if (!text) return ''
  const cached = userMdCache.get(text)
  if (cached !== undefined) return cached

  const rendered = renderSafeUserMarkdown(text, knownFilePaths.value, knownChatLinks.value, { openLocal: true })
  if (userMdCache.size >= MAX_MD_CACHE_SIZE) {
    const firstKey = userMdCache.keys().next().value
    if (firstKey !== undefined) userMdCache.delete(firstKey)
  }
  userMdCache.set(text, rendered)
  return rendered
}

function renderActivityLine(line: string): string {
  return linkifyText(line, knownFilePaths.value)
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

// Subagent activity lines are tagged with the leading turnstile arrow by the
// store's tool_use handler when an event arrives with parent_tool_use_id set.
// Used to indent and de-emphasize them so the trace reads "parent → subagent
// → parent" without the user mistaking subagent work for the parent's own.
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
  if (isImageFilePath(path)) {
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
  if (handleAppLinkClick(e)) return
  if (handleOpenLocalFileClick(e, (m) => store.pushErrorToast('Could not open file', m))) return
  handleFileLinkClick(e)
}

// The live row stays one short line: the step (in the label) and how long
// the turn has been running. Counts and tokens belong to the finished turn's
// summary and footer, where they no longer change every second.
const liveTraceMetaParts = computed(() => {
  const parts: { key: string; text: string; shortText?: string; isImportant?: boolean }[] = []
  const startedAt = store.currentStreamStartedAt
  if (startedAt) {
    const elapsed = nowTs.value - startedAt
    if (elapsed >= 0) parts.push({ key: 'duration', text: formatDuration(elapsed), isImportant: true })
  }
  return parts
})

// Live trace label: "Working..." when real tool work or visible text is in
// progress, otherwise "Thinking..." while the model reasons.
// What the agent is doing right now, for the live Activity line. Read from
// the last timeline entry; empty when the timeline gives nothing concrete.
// Permission lines still waiting for an answer. An answered one stays in the
// trace as history but is no longer the current step (#1618).
const openPermissionLines = computed(() => new Set(
  pendingApprovals.value.flatMap(p => activityLines(permissionActivityLine(p.tool_name, p.message))),
))
const liveStepLabel = computed(() => {
  if (store.currentStreamingThinking) return ''
  const last = store.currentTimeline[store.currentTimeline.length - 1]
  if (!last) return ''
  if (last.kind === 'filecard' && last.file_path) return `Editing ${fileCardBasename(last.file_path)}`
  if (last.kind !== 'tool') return ''
  return liveToolStep(activityLines(last.content), last.tool_use_ids, openPermissionLines.value, new Set(store.currentDeniedToolUseIds))
})
const liveStepPreview = computed(() => {
  if (store.currentStreamingThinking) return 'Thinking…'
  if (store.currentStreamingText) return 'Writing response…'
  const last = store.currentTimeline[store.currentTimeline.length - 1]
  if (last?.kind === 'tool' || last?.kind === 'filecard') return liveStepLabel.value || 'Working…'
  if (last?.kind === 'status') return last.content
  if (last?.kind === 'text') return 'Sharing an update…'
  return 'Thinking…'
})
// Key the selected activity, not its rendered description: IDs distinguish
// repeated same-label tools, and the line index covers providers with no IDs.
// Live phases get a stable key, so token additions and clock ticks never slide.
const liveStepIdentity = computed(() => {
  if (store.currentStreamingThinking) return 'phase:thinking'
  if (store.currentStreamingText) return 'phase:response'
  const last = store.currentTimeline[store.currentTimeline.length - 1]
  if (!last) return 'phase:thinking'
  const entryIndex = store.currentTimeline.length - 1
  if (last.kind === 'tool') {
    const lines = activityLines(last.content)
    let lineIndex = lines.length - 1
    while (lineIndex >= 0 && isPermissionActivityLine(lines[lineIndex]) && !openPermissionLines.value.has(lines[lineIndex].trim())) lineIndex--
    if (lineIndex < 0) return `activity:${entryIndex}:tool:idle`
    const toolId = last.tool_use_ids?.[lineIndex]
    const denied = lineIndex < lines.length - 1 && !!toolId && new Set(store.currentDeniedToolUseIds).has(toolId)
    const permissionState = isPermissionActivityLine(lines[lineIndex]) ? 'pending' : denied ? 'denied' : 'active'
    return `activity:${entryIndex}:tool:${lineIndex}:${toolId || 'line'}:${permissionState}`
  }
  if (last.kind === 'filecard') return `activity:${entryIndex}:file:${last.tool_use_id || last.file_path}`
  return `activity:${entryIndex}:${last.kind}`
})

// Image extensions get routed through openImage so the binary streams
// directly instead of round-tripping through the text endpoint. Everything
// else (markdown, code, config, plain text) goes through `open`. Binary
// formats the viewer doesn't render (PDF, docx, xlsx, pptx, zip) fall
// through to `open`, which will 415 and show a clear error.
function openFileCard(filePath: string): void {
  if (!filePath) return
  const cid = chat.value?.chat_id || ''
  if (isImageFilePath(filePath)) {
    fileViewer.openImage(filePath, cid)
  } else {
    fileViewer.open(filePath, null, cid)
  }
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

    // If narration is followed by `_thinking` blocks, the turn was interrupted
    // mid-thought. Fold everything into a trace so the user doesn't see a
    // standalone bubble that's actually mid-reasoning. A substantive reply
    // followed by a reasoning-only step is still the answer.
    //
    // BUT: trailing-only `_activity` (tool calls) does NOT mean the turn was
    // interrupted. A model commonly emits its final answer text and then runs
    // bookkeeping tools (TodoWrite, etc.) that produce no further user-facing
    // text. In that case the answer text is the real reply and must render as
    // a normal assistant bubble; the trailing tools just join the trace.

    if (finalMsg && isInterruptedTail(finalMsg, trailing)) {
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
    // Non-final text lives in the one trace; only the final reply is a bubble.
    const turnItems: RenderItem[] = buildTurnParts(buffer, finalIdx).map((part) =>
      withKey({ kind: 'trace', steps: part.steps, turnIndex: currentTurnIndex ?? undefined }),
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
  // Only the latest messages are loaded, so subagents of older turns find no
  // rendered turn to attach to. A finished one stays out of view until its turn
  // loads; it must not pile into the live trace or the last turn's trace.
  let firstRenderedTurn: number | null = null
  for (const it of items) {
    if (it.kind === 'user' && typeof it.turnIndex === 'number') { firstRenderedTurn = it.turnIndex; break }
  }
  const belongsToUnloadedTurn = (sub: SubagentTranscript): boolean =>
    sub.status !== 'running'
    && typeof sub.turn_index === 'number'
    && firstRenderedTurn !== null
    && sub.turn_index < firstRenderedTurn
  // While streaming, the in-flight turn's subagents nest in the live trace,
  // along with any still running from an earlier turn. Finished agents of
  // earlier rendered turns were already placed by flushTurn.
  if (store.isStreaming) {
    const live = [...subsByTurn.values()].flat().concat(unanchoredSubs)
      .filter(sub => !belongsToUnloadedTurn(sub))
    return { items, liveSubs: live, liveStandaloneSubs: [] }
  }
  // Anything still unplaced (turn not in history yet, or no turn info):
  // attach anchored subagents to their actual user-turn's trace block so a
  // late-finishing agent doesn't drift to the most recent trace. Unanchored
  // leftovers still fall back to the last trace block, or a fresh block if
  // there is none.
  const leftovers = [...subsByTurn.values()].flat().concat(unanchoredSubs)
    .filter(sub => !belongsToUnloadedTurn(sub))
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
const transcriptEmpty = computed(() => renderItems.value.length === 0)
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
  resetLiveTraceState()
  // The dock disclosure is per-chat state; carrying it across a switch meant a
  // chat you never expanded opened with its dock already open.
  dockExpanded.value = false
  nextTick(() => {
    if (messagesEl.value) messagesEl.value.scrollTop = messagesEl.value.scrollHeight
    lastScrollTop = messagesEl.value?.scrollTop ?? 0
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

watch(() => store.isStreaming, (streaming) => {
  if (streaming) resetLiveTraceState()
})

// Hold the finished turn's trace open until the reader returns to the bottom.
function holdTraceOpen() {
  let lastUser = -1
  let traceIdx = -1
  renderItems.value.forEach((item, idx) => {
    if (item.kind === 'user') lastUser = idx
  })
  renderItems.value.forEach((item, idx) => {
    if (item.kind === 'trace' && idx > lastUser) traceIdx = idx
  })
  if (traceIdx < 0) return
  checkScroll()
  openTraces.value = { ...openTraces.value, [traceIdx]: true }
  pendingCollapseIdx.value = traceIdx
  if (!isNearBottom.value) pendingCollapseArmed.value = true
}

// Keep the live trace body on its newest step unless the user scrolled up in it.
watch(
  () => [store.currentTimeline.length, store.currentStreamingText, store.currentStreamingThinking],
  () => {
    nextTick(() => {
      const body = liveTraceBodyEl.value
      if (body && liveBodyAtBottom.value) body.scrollTop = body.scrollHeight
    })
  },
)

// Auto-scroll only when the user is already near the bottom.
// Use direct scrollTop assignment instead of scrollIntoView — the latter
// can stop short of the absolute bottom with smooth scrolling, especially
// inside flex containers where the anchor is a zero-height child.
let prevStreaming = store.isStreaming
let prevStreamingChatId = store.activeChatId
watch(
  () => [store.activeMessages.length, store.currentStreamingText, store.currentActivity.length, store.isStreaming],
  () => {
    const streaming = store.isStreaming
    // A chat switch also flips isStreaming; that is not a turn end.
    const justFinished = prevStreaming && !streaming && prevStreamingChatId === store.activeChatId
    prevStreaming = streaming
    prevStreamingChatId = store.activeChatId
    if (justFinished) {
      if (liveTraceReading.value || (liveTraceTouched.value && liveTraceOpen.value)) {
        // The reader keeps their place: hold the finished trace open until
        // they come back to the bottom.
        nextTick(holdTraceOpen)
        return
      }
      if (isNearBottom.value) {
        nextTick(landOnReply)
        return
      }
    }
    nextTick(() => {
      stickToBottomIfNeeded()
      checkScroll()
    })
  },
  { deep: true }
)

function handleInput(): void {
  autoResize()
  refreshComposerPickers()
}

function handleKeydown(e: KeyboardEvent) {
  // IME uses Enter to accept a candidate. Let the browser and composition
  // system finish that interaction before any picker or send logic runs.
  if (e.isComposing || e.keyCode === 229) return
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

  if (e.key === 'Enter' && enterSends() && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
    e.preventDefault()
    send()
    return
  }

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
    composer.clearDraft(chat.value.chat_id)
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
  // The turn is tagged as a retry: it is a new turn the user asked to run
  // again, and the engine records the tag per turn. The tag is the only
  // thing the client chooses; the text never decides it.
  return store.sendMessage(
    chat.value.chat_id,
    prior.text,
    {
      composed: prior.text,
      imageRefs: prior.images.length ? prior.images : undefined,
      fileComments: [],
      chatComments: [],
      origin: 'retry',
    },
  )
}

// A known error gets its own fix instead of a diagnosis chat: a usage limit
// retries hourly until it resets (nothing while a retry is armed, the retry
// card owns it), a missing or unsuitable model opens the model picker, and a
// signed-out provider gets the sign-in steps under the bubble. When that fix
// cannot run here (archived chat, no prior turn to resend), the bubble falls
// back to the "Fix this error" chat rather than offering nothing.
function errorBubbleAction(errorIdx: number, content: string): 'retry-hourly' | 'switch-model' | 'fix' | null {
  const action = errorQuickAction(content)
  if (action === 'fix') return 'fix'
  if (chat.value.archived) return 'fix'
  if (action === 'switch-model') return 'switch-model'
  if (!lastUserBefore(errorIdx)) return 'fix'
  if (action === 'sign-in') return null
  return chat.value.retry?.status === 'pending' ? null : 'retry-hourly'
}

// Arm the chat's hourly deferred retry with the turn that hit the limit.
async function retryHourlyFromError(errorIdx: number) {
  if (chat.value.archived) return
  const prior = lastUserBefore(errorIdx)
  if (!prior) return
  try {
    await store.setChatRetry(chat.value.chat_id, prior.text, prior.images)
  } catch (e) {
    store.pushErrorToast('Could not schedule the retry', `${(e as Error)?.message || e}`)
  }
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

// Quota retries back off from an hour (1h, 2h, 4h, then 8h), weekly/monthly
// limits wait a day; connection, startup and auth retries 30s.
const retryCardTitle = computed(() => {
  const retry = chat.value?.retry
  if (retry && isProviderAuthError(retry.last_error)) return 'Signed out — retrying this turn'
  const seconds = retry?.interval_seconds || 0
  if (seconds && seconds < 3600) return 'Retrying this turn shortly'
  if (seconds >= 86400) return 'Retrying this turn once a day'
  if (seconds > 3600) return 'Retrying this turn every few hours'
  return 'Retrying this turn every hour'
})

async function tryRetryNow() {
  if (!chat.value || store.isStreaming) return
  await store.tryChatRetryNow(chat.value.chat_id)
}

async function stopRetry() {
  if (!chat.value) return
  await store.stopChatRetry(chat.value.chat_id)
}

// Open the picker for a recovery: the retry card's "Continue with..." or the
// error bubble's "Switch model". The chat and the error bubble are captured
// now, so the retry lands on the turn that failed even if the screen moves
// while the picker is open.
function openRecoveryPicker(errorIdx: number | null) {
  if (!chat.value || chat.value.archived) return
  if (store.isStreaming) return
  recoveryContext.value = { chatId: chat.value.chat_id, errorIdx }
  pickerMode.value = 'recovery'
  stagedThinkingLevel.value = null
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
  if (showModelPicker.value) {
    showModelPicker.value = false
    return
  }
  // The composer's chip is selection-only: a pick there changes the model
  // and never retries.
  pickerMode.value = 'normal'
  recoveryContext.value = null
  stagedThinkingLevel.value = null
  showModelPicker.value = true
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


// Put the chosen model on the chat. A provider change is a handover, and the
// handover keeps the chat's own thinking level: the staged one belongs to
// the retry, so it goes on top. Returns false when the change did not
// happen, so nothing is retried after a failure.
async function applyModelChoice(
  chatId: string,
  updates: { provider: ProviderKey; model: string; thinking_level?: string },
  handover: boolean,
  staged: string | null,
): Promise<boolean> {
  try {
    if (handover) {
      // The handover route reads only the provider and model; the thinking
      // level is applied by the PATCH below, so it is not sent here.
      await store.handoverChat(chatId, { provider: updates.provider, model: updates.model })
      if (staged !== null) {
        // The handover keeps the chat's own thinking level; the staged one
        // belongs to the retry, so it goes on top. Best effort: the handover
        // itself already happened.
        try {
          await store.updateChat(chatId, { thinking_level: updates.thinking_level ?? '' })
        } catch (e) {
          store.pushErrorToast('Could not apply the thinking level', errorMessage(e))
        }
      }
    } else {
      await store.updateChat(chatId, updates)
    }
    return true
  } catch (e) {
    store.pushErrorToast('Could not switch model', errorMessage(e))
    return false
  }
}

// The second half of a recovery: the failed turn runs again on the model the
// user just picked. A deferred retry resumes the engine's own schedule; an
// error bubble resends the turn it failed on, with that turn's images.
async function runRecovery(recovery: RecoveryContext) {
  // The user may have moved to another chat while the picker was open: the
  // retry belongs to the chat it was captured in.
  if (chat.value?.chat_id !== recovery.chatId) return
  if (recovery.errorIdx !== null) {
    retryFromError(recovery.errorIdx)
    return
  }
  await store.tryChatRetryNow(recovery.chatId)
}

async function selectModel(value: string | string[], sectionKey = '') {
  // One pick at a time: a second click while the first is still applying
  // must not start a second turn.
  if (modelSelectBusy.value) return
  const model = Array.isArray(value) ? value[0] : value
  if (!model || !chat.value) {
    showModelPicker.value = false
    return
  }
  // Capture the intent before the first await: the picker closes itself on a
  // pick (which drops the recovery) and a confirmation dialog can sit over a
  // chat that changes.
  const recovery = pickerMode.value === 'recovery' ? recoveryContext.value : null
  const chatId = chat.value.chat_id
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
  if (sameModelAndRoute && !recovery) {
    // The composer's picker is selection-only: the model already in use is a
    // no-op. In a recovery the same model still retries.
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
  // A recovery's staged level belongs to the model the user is about to pick:
  // when the catalog says that model cannot do it, the retry falls back to
  // the provider default.
  const staged = recovery ? stagedThinkingLevel.value : null
  if (staged !== null) {
    const providerLevels = modelsResponse.value?.thinking_levels?.[updates.provider]
    const allowed = targetLevels?.length ? targetLevels : providerLevels
    updates.thinking_level = (allowed?.length && staged && !allowed.includes(staged)) ? '' : staged
  } else if (
    chat.value.thinking_level
    && targetLevels
    && !targetLevels.includes(chat.value.thinking_level)
  ) {
    updates.thinking_level = ''
  }
  modelSelectBusy.value = true
  try {
    // A recovery that keeps the model already in use and staged no thinking
    // level has nothing to apply: only the retry is left to do.
    if (!sameModelAndRoute || staged !== null) {
      if (bucketLocked.value && targetRoute !== currentRoute) {
        const ok = await askConfirm(
          `Hand over this chat to ${bucketLabel(targetBucket)} / ${model}? The same visible chat will continue with a fresh provider session.`,
          {
            title: 'Hand over chat',
            confirmLabel: 'Hand over',
          },
        )
        // A cancelled handover changes no model and retries nothing.
        if (!ok) return
        // The user answered a dialog: the chat may have moved under it.
        if (chat.value?.chat_id !== chatId) return
        const applied = await applyModelChoice(chatId, updates, true, staged)
        if (!applied) return
      } else {
        const applied = await applyModelChoice(chatId, updates, false, staged)
        if (!applied) return
      }
    }
    // The model is in place. Only a recovery retries: the composer's picker
    // never does, not even with a deferred retry waiting.
    if (recovery) {
      try {
        await runRecovery(recovery)
      } catch (e) {
        store.pushErrorToast('Could not retry the failed turn', errorMessage(e))
      }
    }
  } finally {
    modelSelectBusy.value = false
    showModelPicker.value = false
  }
}

async function selectThinking(level: string) {
  if (pickerMode.value === 'recovery') {
    // A recovery stages the level for the retry: the model that is about to
    // be picked may not support it, and the picker stays open until then.
    stagedThinkingLevel.value = level
    return
  }
  // '' = provider default. Safe mid-chat: it never invalidates the
  // provider session, so no handover is involved.
  try {
    await store.updateChat(chat.value.chat_id, { thinking_level: level })
    showModelPicker.value = false
  } catch (e) {
    store.pushErrorToast('Could not change the thinking level', errorMessage(e))
  }
}

/* Close picker on click outside or Escape */
watch(showModelPicker, (open) => {
  if (!open) {
    // A capability-card "Open picker" filtered the sections; drop the filter
    // so the next normal open shows the full list again.
    capabilityPickerSection.value = ''
    // The picker closed: the recovery is over and its staged thinking level
    // goes with it.
    resetPickerState()
    return
  }
  // Refetched on every open, not only on mount: a provider whose catalog was
  // still loading when the chat opened would otherwise stay missing from the
  // picker until the chat is reopened. The server caches the list.
  void loadModels()
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

/** The task note's Mark done already asked, and said the chat would be archived. */
async function archiveAfterTaskDone() {
  if (!chat.value || chat.value.archived) return
  try {
    await store.archiveChat(chat.value.chat_id)
  } catch {
    return
  }
  emit('close')
}

const { archiveChatWithTaskCheck } = useArchiveChat()
async function doArchive() {
  try {
    if (!await archiveChatWithTaskCheck(chat.value)) return
  } catch {
    // archiveChat already reconnected the sockets and raised an error toast.
    // Keep the pane open so the still-live chat stays reachable.
    return
  }
  // Notify ChatLayout so it closes the chat pane.
  emit('close')
}

const taskSignals = useTaskSignalsStore()

/**
 * The task this archived chat's attempt still holds, settled because the chat
 * was archived. Continuing it as a plain chat would lose the task: the new chat
 * carries no delegation, so its report is refused and the card stays stuck. The
 * board's own way on is a retry, which starts a new attempt in a new chat that
 * is handed what this one did.
 */
const heldSettledTask = computed(() => {
  const current = chat.value
  if (!current || current.helper?.kind !== 'task_delegation') return null
  const task = taskSignals.taskForChat(current)
  if (!task || task.status === 'done' || !task.attempt_id) return null
  if (!taskSignals.chatHoldsTask(current, task)) return null
  return isLiveAttemptState(task.attempt_state) ? null : task
})

async function continueChat() {
  if (!chat.value) return
  isContinuing.value = true
  try {
    const task = heldSettledTask.value
    if (task) {
      const answer = await api.post<TaskAttemptActionResponse>(
        `/api/tasks/${encodeURIComponent(task.id)}/attempt/${encodeURIComponent(task.attempt_id)}/retry`,
        { workspace: taskSignals.loadedWorkspace },
      )
      void taskSignals.reload(taskSignals.loadedWorkspace)
      const next = answer?.attempt?.chat_id || answer?.chat_id
      if (next) await store.switchChat(next)
      return
    }
    await store.continueArchivedChat(chat.value.chat_id)
  } catch (e) {
    console.error('Failed to continue archived chat:', e)
    store.pushErrorToast('Could not continue chat', `${errorMessage(e)}`)
  } finally {
    isContinuing.value = false
  }
}

// Cmd+Backspace mirrors the header archive button (including its confirm
// dialog). Fires even while a text field is focused: that is the point of the
// binding, and the confirm dialog is what makes it safe.
function archiveActiveChat() {
  if (!chat.value || chat.value.archived) return
  void doArchive()
}

// Expose app-level shortcuts to the layout, which owns the global keydown.
defineExpose({ toggleModelPicker, archiveActiveChat, handleQuestionShortcut, handlePermissionShortcut, handleSendShortcut })
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

/* ── Page grid for the chat body ─────────────────────────────────────────
   The body is the shared page frame (App.vue --page-*): capped at
   --page-max, centred, with the gutter inside. The left column holds the
   whole conversation (context bar, transcript, dock, composer), so the
   transcript, the composer and the header title share one left edge; the
   rail takes --page-rail on the right when Chat details is shown in place. */
/* The body spans the whole pane. The conversation keeps a reading measure
   (--chat-measure, ~75 characters of body text) centred in the space beside
   the rail, while the scroll box, the rail and the header actions reach the
   pane's edges: a wide window is spent on room around the chat, never on
   lines too long to read. */
.chat-panel {
  --chat-measure: 768px;
  --chat-rail-w: 320px;
  --chat-rail-gap: var(--space-6);
  /* Wide blocks (tables, code) may grow past the measure up to this. */
  --chat-wide: 1120px;
}
.chat-body {
  flex: 1;
  display: flex;
  gap: var(--chat-rail-gap);
  box-sizing: border-box;
  width: 100%;
  min-height: 0;
  padding-inline: calc(var(--page-gutter) + var(--safe-left)) calc(var(--space-4) + var(--safe-right));
}
.chat-panel:not(.chat-panel--rail) .chat-body {
  padding-right: calc(var(--page-gutter) + var(--safe-right));
}
/* Every band of the conversation sits on the same centred measure. */
.chat-column > :not(.chat-with-sidebar):not(.sr-only),
.messages-content {
  box-sizing: border-box;
  width: 100%;
  max-width: var(--chat-measure);
  margin-left: auto;
  margin-right: auto;
}
.messages { container-type: inline-size; }
/* The header title starts where the reading column starts, and its actions
   end over the rail at the pane's right edge. */
.chat-panel :deep(.pane-header) {
  padding-left: max(var(--page-gutter), calc((100% - var(--chat-measure)) / 2));
  padding-right: calc(var(--page-gutter) + var(--safe-right));
}
.chat-panel--rail :deep(.pane-header) {
  padding-left: max(
    var(--page-gutter),
    calc((100% - var(--page-gutter) - var(--space-4) - var(--chat-rail-w) - var(--chat-rail-gap) - var(--chat-measure)) / 2 + var(--page-gutter))
  );
  padding-right: calc(var(--space-4) + var(--safe-right));
}

/* Top right of the chat body, where the rail's heading would be. */
.chat-body { position: relative; }
.work-inspector-trigger {
  position: absolute;
  top: 12px;
  right: calc(var(--page-gutter) + var(--safe-right));
  z-index: 2;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--fg2);
  min-width: 34px;
  min-height: 34px;
  padding: 7px;
}
@media (pointer: coarse) {
  .work-inspector-trigger { min-width: var(--touch); min-height: var(--touch); }
}
.work-inspector-trigger:hover { color: var(--fg); border-color: var(--border-strong); }
/* The header's own button rules set `position: relative`, which would let the
   floating trigger's `top` push this one below the header's centre line. */
.work-inspector-trigger--header {
  position: static;
  top: auto;
  right: auto;
  flex: none;
  /* Borderless like Archive beside it: no tile, the same hover fill. */
  border-color: transparent;
  background: transparent;
  color: var(--fg2);
}
.work-inspector-trigger--header:hover {
  color: var(--fg);
  border-color: transparent;
  background: var(--bg3);
}

.chat-rail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  /* The pinned-file tile's title bar height (PinnedFilePanel), so both docked
     windows carry the same bar. */
  min-height: 44px;
  margin: -16px -16px 16px;
  padding: 0 12px 0 16px;
  border-bottom: 1px solid var(--border);
}
.chat-rail-head .rail-title { margin-bottom: 0; }
.chat-rail-hide {
  min-width: 34px;
  min-height: 34px;
  padding: 7px;
  color: var(--fg2);
}
@media (pointer: coarse) {
  .chat-rail-hide { min-width: var(--touch); min-height: var(--touch); }
}

.chat-column {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.chat-rail {
  flex: 0 0 var(--chat-rail-w);
  width: var(--chat-rail-w);
  min-width: 0;
  overflow-y: auto;
  /* As tall as its content, not the pane: a short rail is a window resting
     at the top, not an empty column. It scrolls once it would pass the
     bottom. The 16px insets match the composer's gap to the pane edge
     (.input-bar's bottom padding), so rail and composer sit on one line. */
  align-self: flex-start;
  max-height: calc(100% - 32px);
  box-sizing: border-box;
  margin-block: 16px;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg2);
  /* The docked pinned-file tile's shadow (ChatLayout .chat-split-side): the
     two windows are one layer and lift the same way. */
  box-shadow: 0 12px 32px -14px rgb(0 0 0 / 45%);
  font-size: var(--text-sm);
}
:global(:root.theme-light) .chat-rail:not(.chat-rail--drawer) {
  box-shadow: 0 10px 28px -14px rgb(26 26 46 / 28%);
}

.chat-rail-origin {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin: 0 0 18px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg2);
  line-height: 1.45;
}
.chat-rail-origin-icon { flex: none; margin-top: 2px; color: var(--fg3); }
.chat-rail-origin a {
  color: var(--fg);
  font-weight: 650;
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 3px;
}
.chat-rail-origin a:hover { text-decoration-color: currentColor; }

/* The same origin sentence for a chat whose Chat details rail is hidden, above
   the transcript. Flat, and sharing the rail's link treatment: one sentence in
   two places, never two different sentences. */
.chat-origin-note {
  display: flex;
  gap: 10px;
  align-items: center;
  /* Clear of the header rule and of the Chat details tab over its corner. */
  margin: 12px 0;
  padding: 9px calc(34px + 12px) 9px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.45;
}
.chat-origin-note-icon { flex: none; color: var(--fg3); }
@media (pointer: coarse) {
  .chat-origin-note { padding-right: calc(var(--touch) + 12px); }
}
.chat-origin-link {
  min-height: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-weight: 650;
  cursor: pointer;
  text-align: left;
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 3px;
}
.chat-origin-link:hover { text-decoration-color: currentColor; }
.chat-origin-link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}

.chat-rail-tool {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fg);
}
.chat-rail-tool small {
  margin-right: 4px;
  color: var(--fg3);
  font-size: var(--text-xs);
}
.chat-rail-sublabel { margin-top: var(--space-3); }
/* The shared rail rhythm is `.rail-section + .rail-section`; this wrapper
   (the focus target for "See what Ciao knows") sits between two of them. */
.chat-rail-agent-context + .rail-section { margin-top: var(--space-5); }
/* The terminal row closes the repository block, a little tighter than the
   gap between sections; with no repository it starts the rail. */
.chat-rail-repo + .chat-rail-terminal { margin-top: var(--space-3); }
.chat-rail-terminal + .chat-rail-agent-context { margin-top: var(--space-5); }
.chat-rail-more {
  margin-top: var(--space-1);
  padding: 4px 0;
  border: 0;
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-xs);
  text-decoration: underline;
  text-decoration-color: var(--border-strong);
  text-underline-offset: 3px;
  cursor: pointer;
}
.chat-rail-more:hover { color: var(--fg); text-decoration-color: currentColor; }
.chat-rail-more:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
@media (pointer: coarse) {
  .chat-rail-more { min-height: var(--touch); }
}
.chat-rail-repo:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 4px;
  border-radius: var(--radius-xs);
}
.chat-rail-agent-context:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 4px;
  border-radius: var(--radius-xs);
}
.chat-rail-subagent {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chat-rail-subhead {
  margin: var(--space-3) 0 var(--space-1);
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.chat-rail-running .rail-label + .chat-rail-subhead { margin-top: var(--space-1); }

.chat-rail-file { min-width: 0; max-width: 100%; }
.chat-rail-file > span,
.chat-rail-file > small { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* .rail-item stacks its children; an agent row reads as one line. */
.chat-rail-agent {
  flex-direction: row;
  align-items: center;
  justify-content: flex-start;
  gap: var(--space-2);
}
.chat-rail-agent small { flex-shrink: 0; font-family: var(--font-mono); }
.chat-rail-agent-arrow { flex-shrink: 0; margin-left: auto; color: var(--fg3); }


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
  width: var(--touch);
  height: var(--touch);
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
  /* One child now: the breadcrumb, which is a one- or two-line block of text.
     Centring it keeps the title row on the optical middle of whichever the
     block turns out to be. */
  align-items: center;
  min-width: 0;
  text-align: left;
}

/* Sizing, hover fill and touch padding all come from PaneHeader's shared
   .btn-icon rules, so this only sets the resting colour: the same quiet --fg3
   as the trailing actions, since closing the chat is not the errand you came
   here for. `flex: none` keeps the box its own size inside the title row. */
.close-btn { color: var(--fg3); flex: none; }
.close-btn:hover { color: var(--fg); }

/* The title header names the conversation only; project context lives in
   Chat details and the sidebar rather than competing with the title. */
.header-breadcrumb {
  display: flex;
  align-items: center;
  column-gap: 8px;
  min-width: 0;
  flex: 1;
  position: relative;
}

.header-breadcrumb .chat-title {
  flex: 0 1 auto;
}
.header-breadcrumb .chat-title-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 0 1 auto;
  min-width: 0;
  max-width: 100%;
}
/* A long title wraps to at most two lines instead of pushing the code badge
   out of the header. PaneHeader's shared .pane-title rules are `nowrap` with
   an ellipsis, so they are overridden here rather than globally: the chat
   title is the one title that may take a second line, and the full title
   stays in the title attribute. `overflow-wrap: anywhere` breaks a title
   that has no spaces in it (a URL, a path). */
.chat-title-row .chat-title {
  flex: 0 1 auto;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  overflow-wrap: anywhere;
  white-space: normal;
  line-height: 1.25;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
}
.chat-code-badge { display: inline-grid; place-items: center; flex: none; min-width: 24px; min-height: 24px; padding: 0 2px; border: 0; background: none; cursor: pointer; border-radius: var(--radius-xs); }
.chat-code-badge-tag { display: inline-grid; place-items: center; height: 20px; padding: 0 3px; color: var(--accent); font: 700 calc(14px * var(--font-scale))/1 var(--font-mono); letter-spacing: -0.04em; }
.chat-code-badge:hover { background: var(--bg3); }
.chat-code-badge:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (pointer: coarse), (max-width: 768px) { .chat-code-badge { min-width: var(--touch); min-height: var(--touch); } }

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
  /* The transcript never scrolls sideways: wide content (code, tables)
     scrolls inside its own box, and long tokens wrap. A stray horizontal
     bar used to appear while a turn streamed a long unbroken token. */
  overflow-x: hidden;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
  /* The column already carries the page gutter; the transcript only keeps
     breathing room above and below. The scroll box reaches 22px past the
     column on each side and pads it back, so the text stays aligned while
     focus rings are not clipped by the horizontal overflow guard below. */
  margin-inline: -22px;
  padding: 28px 26px 24px;
  min-height: 0;
  position: relative;
}
.messages-content {
  display: flex;
  flex-direction: column;
  /* One rhythm between turns (~14px), below each message's action row. */
  gap: 14px;
  /* Pin the transcript to the bottom when it's shorter than the viewport, but
     collapse to 0 and scroll normally when it overflows. `margin-top: auto`
     is resolution-independent — unlike `min-height: 100%`, which resolved
     against `.messages` (no explicit height) to 0 in some engines and left
     short/streaming chats stuck at the top with dead space below. */
  margin-top: auto;
}

.messages--empty .messages-content {
  flex: 1;
  justify-content: center;
  margin-top: 0;
}

.chat-empty-state {
  width: 100%;
  max-width: 640px;
  margin: 0;
  padding: var(--space-5) 0;
  text-align: left;
}

.chat-empty-state h2 {
  margin: 0 0 4px;
  color: var(--fg);
  font-size: calc(20px * var(--font-scale));
  font-weight: 650;
  line-height: 1.2;
  letter-spacing: -0.02em;
}

.chat-empty-context {
  margin: 0;
  color: var(--fg3);
  font-size: var(--text-sm);
  line-height: 1.5;
}

.chat-empty-knowledge {
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font: inherit;
  cursor: pointer;
}

@media (pointer: coarse) {
  .chat-empty-knowledge {
    display: inline-flex;
    align-items: center;
    min-height: var(--touch);
  }
}

.chat-empty-knowledge:hover {
  text-decoration: underline;
  text-underline-offset: 3px;
}

/* Suggestions as hairline rows, not a cloud of pills. */
.chat-empty-starters {
  display: flex;
  flex-direction: column;
  margin-top: var(--space-4);
  border-top: 1px solid var(--border);
}

.chat-empty-starter {
  display: block;
  width: 100%;
  min-height: var(--touch);
  padding: 12px 2px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: none;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-base);
  text-align: left;
  cursor: pointer;
}

.chat-empty-starter:hover {
  color: var(--accent);
}

.chat-empty-starter:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-xs);
}

/* Geometry copied from .message.user / .message.assistant deliberately: the
   whole point is that the placeholder occupies the same box as the row that
   replaces it. */
.history-skeleton-stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
  width: 100%;
  min-width: 0;
}

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
  box-sizing: border-box;
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

.message-wrap {
  position: relative;
}

.message-wrap.user {
  align-self: flex-end;
  align-items: flex-end;
}

.message-wrap.assistant {
  align-self: flex-start;
}

/* The handed-over task spans the column: it is the brief the turns below
   answer, not a bubble the user typed. */
.message-wrap.task-handover-wrap {
  align-self: stretch;
}
.message-wrap.injected-prompt-wrap {
  align-self: stretch;
}

.message-row {
  display: flex;
  align-items: flex-start;
  min-width: 0;
  width: 100%;
  position: relative;
}

/* A reply's row stacks its prose over the action footer, so it is a column.
   A request's row is a column too, right-aligned, so its Copy chip lines up
   under the bubble instead of beside it. */
.message-wrap.user .message-row,
.message-wrap.assistant .message-row {
  flex-direction: column;
}

.message-wrap.user .message-row {
  align-items: flex-end;
}

.message {
  max-width: 100%;
  font-size: var(--text-base);
  line-height: 1.5;
  word-break: break-word;
  min-width: 0;
  transition: background 0.15s ease, border-color 0.15s ease;
}

/* The request: an accent-tinted bubble on the right, capped so a long prompt
   still reads as the user's turn rather than a second column of prose. */
.message.user {
  max-width: min(560px, 85%);
  padding: 10px 14px;
  background: color-mix(in srgb, var(--accent) 9%, var(--bg));
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
  border-radius: 12px;
  color: var(--fg);
}

/* The answer: plain prose on the page. A bubble around every reply made the
   transcript a stack of cards; the activity line above and the action footer
   below already mark where a turn starts and ends. */
.message.assistant {
  padding: 0;
  background: transparent;
  border: 0;
  line-height: 1.65;
}

/* A message's actions and the turn's facts. The row is always laid out, so it
   holds its height whether or not it shows: revealing it on hover never moves
   the transcript. Its buttons and facts fade in while the pointer is over the
   message or focus is inside it (Tab reaches the buttons, which is what shows
   them), and on a device with no hover they simply stay visible. A reply's
   Outputs disclosure leads the row and never fades: it is content, not an
   action. */
.message-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 10px;
  margin-top: 6px;
}
.message-wrap .message-actions > :is(.message-action-btn, .message-meta) {
  opacity: 0;
  transition: opacity 0.15s;
}
.message-wrap:hover .message-actions > :is(.message-action-btn, .message-meta),
.message-wrap:focus-within .message-actions > :is(.message-action-btn, .message-meta) {
  opacity: 1;
}
.message-wrap:hover .message-actions > .message-action-btn:disabled,
.message-wrap:focus-within .message-actions > .message-action-btn:disabled {
  opacity: 0.6;
}
@media (hover: none) {
  .message-wrap .message-actions > :is(.message-action-btn, .message-meta) { opacity: 1; }
  .message-wrap .message-actions > .message-action-btn:disabled { opacity: 0.6; }
}

/* A reply's buttons share the prose's left edge: the quiet button's own
   horizontal padding would otherwise indent the icon. The row stretches to
   the turn's width: as a column-flex item it would otherwise be max-content
   wide, so the facts strip could not shrink to the turn and got clipped at
   the transcript edge instead of scrolling inside it. */
.message-wrap.assistant .message-actions {
  align-self: stretch;
  margin-left: -8px;
}

.message-action-btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--fg2);
  cursor: pointer;
  font: inherit;
  font-size: var(--text-sm);
  white-space: nowrap;
  transition: color 0.12s, background 0.12s;
}

.message-action-btn svg {
  flex: none;
}

.message-action-btn:hover,
.message-action-btn:focus-visible {
  background: var(--bg3);
  color: var(--fg);
}

.message-action-btn:active {
  transform: scale(0.97);
}

.message-action-btn:disabled {
  cursor: default;
}

/* Touch: the row keeps its 28px visual but each button grows a 44px hit
   area around it, so taps land without spreading the transcript out. */
@media (pointer: coarse) {
  .message-action-btn::after {
    content: '';
    position: absolute;
    inset: -8px 0;
  }
}

/* The turn's facts close the footer: when it finished, how long it took and
   what ran it, then what it cost. Context-window occupancy is deliberately
   absent — the rail states it as a meter against the window's size.
   The facts stay on one line beside the actions at every width: the zero
   flex-basis keeps them from breaking onto a line of their own, and when the
   pane cannot hold them the strip scrolls sideways. The first fact carries
   the auto margin, so the strip is right-aligned while there is room and both
   ends stay reachable once it overflows. A user message's send time is a
   single text-only flex container and keeps `flex-end`. */
.message-actions .message-meta {
  flex: 1 1 0;
  display: flex;
  flex-wrap: nowrap;
  justify-content: flex-start;
  gap: 2px 12px;
  min-width: 0;
  white-space: nowrap;
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-width: none;
  overscroll-behavior-inline: contain;
}
/* This strip is the one opt-out from the app's visible thin scrollbars: it
   still scrolls by wheel, trackpad or touch, but a bar under a reply reads as
   a rule across the transcript rather than as an affordance. */
.message-actions .message-meta::-webkit-scrollbar {
  display: none;
}
.message-actions .message-meta > :first-child { margin-left: auto; }
.message-actions > span.message-meta { justify-content: flex-end; }

.message-action-btn--busy {
  animation: action-busy-pulse 1s ease-in-out infinite;
}

@keyframes action-busy-pulse {
  50% { opacity: 0.35; }
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

.retry-card {
  align-self: center;
  width: min(680px, 90%);
  display: flex;
  flex-wrap: wrap;
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
.retry-card-hint { flex-basis: 100%; margin-top: 0; }

@media (max-width: 640px) {
  .retry-card { align-items: stretch; flex-direction: column; }
  .retry-card-actions { justify-content: flex-end; }
  .retry-card-hint { flex-basis: auto; }
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

/* The live turn's spinner: a small ring, the one moving thing on the line. */
.activity-spinner {
  width: 12px;
  height: 12px;
  flex-shrink: 0;
  box-sizing: border-box;
  border: 2px solid color-mix(in srgb, var(--accent) 30%, transparent);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: activity-spin 0.8s linear infinite;
}

@keyframes activity-spin {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .activity-spinner { animation-duration: 2.4s; }
}

@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

.activity-lines {
  padding: 4px 10px 6px;
  border-top: 1px solid var(--border);
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
/* Code: a raised card, not text on the page background. Inline code is a
   quiet chip; fenced blocks (lib/safeMarkdown.ts emits the wrapper, a header
   strip with the language and the copy button, then the pre) scroll inside
   the card instead of wrapping, so logs and JSON keep their shape. */
.message-content :deep(pre) {
  margin: 8px 0;
  padding: 12px 14px;
  overflow-x: auto;
  max-width: 100%;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: color-mix(in srgb, var(--bg2) 70%, var(--bg3));
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  line-height: 1.6;
  white-space: pre;
  tab-size: 2;
}

.message-content :deep(code) {
  padding: 1px 5px;
  border: 1px solid color-mix(in srgb, var(--fg) 10%, transparent);
  border-radius: 5px;
  background: color-mix(in srgb, var(--fg) 7%, transparent);
  color: var(--fg);
  font-family: var(--font-mono);
  font-size: 0.88em;
}

.message-content :deep(pre code) {
  padding: 0;
  border: 0;
  background: transparent;
  font-size: inherit;
}

/* Anchored on .chat-panel so it also covers traces and the streaming text. */
.chat-panel :deep(.code-block) {
  min-width: 0;
  margin: 10px 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: color-mix(in srgb, var(--bg2) 70%, var(--bg3));
}

.chat-panel :deep(.code-block-head) {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 34px;
  padding: 0 6px 0 14px;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg3) 55%, transparent);
}

.chat-panel :deep(.code-block-lang) {
  flex: 1;
  min-width: 0;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  letter-spacing: 0.02em;
}

.chat-panel :deep(.code-block pre) {
  width: 100%;
  box-sizing: border-box;
  margin: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
}

.chat-panel :deep(.code-copy-btn) {
  position: relative;
  display: inline-flex;
  align-items: center;
  min-height: 26px;
  padding: 0 10px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--bg-elev);
  color: var(--fg);
  font-family: var(--font);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.2;
  cursor: pointer;
  user-select: none;
  transition: background 120ms var(--ease), color 120ms var(--ease), border-color 120ms var(--ease);
}

/* Touch: a full touch target without moving the header. */
@media (hover: none) {
  .chat-panel :deep(.code-copy-btn::after) {
    content: '';
    position: absolute;
    inset: -9px -6px;
  }
}

.chat-panel :deep(.code-copy-btn:hover),
.chat-panel :deep(.code-copy-btn:focus-visible) {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, var(--bg-elev));
}

.chat-panel :deep(.code-copy-btn:active) {
  transform: scale(0.96);
}

.chat-panel :deep(.code-copy-btn[data-copy-state="copied"]) {
  color: var(--success);
  border-color: color-mix(in srgb, var(--success) 45%, var(--border));
}

.chat-panel :deep(.code-copy-btn[data-copy-state="failed"]) {
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
/* A quote: a calm accent rule and a faint surface, upright text at full
   legibility. Italic grey on the page colour was close to invisible. */
.message-content :deep(blockquote) {
  margin: 10px 0;
  padding: 8px 14px;
  border-left: 2px solid color-mix(in srgb, var(--accent) 70%, transparent);
  border-radius: 0 8px 8px 0;
  background: color-mix(in srgb, var(--accent) 6%, var(--bg2));
  color: var(--fg);
}
.message-content :deep(blockquote > :first-child) { margin-top: 0; }
.message-content :deep(blockquote > :last-child) { margin-bottom: 0; }

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
/* A tool call whose result came back is_error (#1416). */
.activity-line.failed { color: var(--error); }
.message-content :deep(a.file-link:hover),
.activity-line :deep(a.file-link:hover),
:deep(a.file-link:hover) {
  color: var(--accent);
  text-decoration: underline solid;
}
/* A reference to another conversation (#1768). An ordinary accent link, so
   it reads as a link into the app and not as the dotted file-path chip. */
.message-content :deep(a.chat-link),
:deep(a.chat-link) {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}
.message-content :deep(a.chat-link:hover),
:deep(a.chat-link:hover) {
  color: var(--accent-strong);
}
/* A wide table may run past the reading measure into the empty space on its
   right (up to --chat-wide), so its columns stay readable instead of
   scrolling inside a narrow box; it still scrolls once that runs out. */
.message-content :deep(.markdown-table-scroll) {
  width: fit-content;
  max-width: min(
    var(--chat-wide),
    calc(100% + max(0px, (100cqw - var(--chat-measure)) / 2) - var(--space-2))
  );
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
  font-size: var(--text-xs);
  color: var(--fg3);
  margin-top: 6px;
  line-height: 1.3;
}

/* Proportional, not mono: mono set the facts ~40% wider, which pushed them
   off the buttons' line onto one of their own on an ordinary pane. Tabular
   figures keep the numbers steady. */
.message-actions .message-meta {
  margin-top: 0;
  font-variant-numeric: tabular-nums;
}

.message.user .message-meta {
  text-align: right;
}

/* Marks a request sent into the running reply. Same treatment as the
   automation mark: a property of the message, not part of the timestamp. */
.steered-mark {
  color: var(--accent);
  font-weight: 600;
  letter-spacing: 0.02em;
  margin-right: 6px;
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
  gap: 6px;
  padding: 4px 4px 2px;
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
  top: -12px;
  right: -12px;
  box-sizing: content-box;
  width: 18px;
  height: 18px;
  padding: 13px;
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
  min-height: var(--touch);
  padding: 2px 6px;
  font-size: 11px;
  font-family: var(--font-mono);
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
  align-items: center;
  gap: 6px;
  flex: 0 1 auto;
  min-width: 0;
  max-width: 100%;
  min-height: 30px;
  padding: 0 4px 0 8px;
  border: 1px solid color-mix(in srgb, var(--accent2) 45%, var(--border));
  border-radius: 8px;
  background: color-mix(in srgb, var(--accent2) 12%, transparent);
  color: var(--fg);
  font-size: var(--text-xs);
  line-height: 1.35;
  box-sizing: border-box;
}
/* The chip whose edit popover is open. */
.comment-chip.is-editing {
  border-color: var(--accent2);
  background: color-mix(in srgb, var(--accent2) 20%, transparent);
}
.comment-chip-icon { flex: none; line-height: 1; color: var(--accent2); }
.comment-chip-body {
  display: flex;
  flex-direction: row;
  align-items: baseline;
  gap: 6px;
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
.comment-chip-body > * { min-width: 0; }
.comment-chip-file {
  flex: 0 1 auto;
  max-width: 180px;
  color: var(--fg2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.comment-chip-line { color: var(--fg2); font-weight: 400; }
.comment-chip-quote {
  flex: 0 1 auto;
  max-width: 180px;
  color: var(--fg2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.comment-chip-note {
  flex: 0 1 auto;
  max-width: 160px;
  color: var(--fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.comment-chip-remove {
  position: relative;
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  padding: 0;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--fg3);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}
.comment-chip-remove:hover { background: var(--bg3); color: var(--fg); }
@media (pointer: coarse) {
  .comment-chip-remove::after { content: ''; position: absolute; inset: -10px; }
}


/* Dock strip: the single counted line for everything the dock defers. */
.dock-strip-wrap {
  flex-shrink: 0;
}

.dock-strip {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-height: var(--touch);
  padding: var(--space-2) 0;
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

/* Focus lands here from the composer bar's activity cluster; the outline says where. */
.chat-rail-running:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 4px;
  border-radius: var(--radius-xs);
}

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


/* Slash-command picker */
.commands-picker {
  max-height: 240px;
  overflow-y: auto;
  margin: 0;
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
  font-family: var(--font-mono);
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
  font-family: var(--font-mono);
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
  font-family: var(--font-mono);
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
  flex-shrink: 0;
  box-sizing: border-box;
  /* The composer surface carries its own border; the bar only leaves room
     under it. No bottom safe-area inset: in standalone PWA mode that would
     reserve ~34px for the home indicator, which iOS already reserves. */
  padding: 4px 0 16px;
}



/* Buttons sit in a row at the bottom by default; once the textarea grows
   tall enough they stack vertically. */
.input-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}





.chat-input {
  display: block;
  width: 100%;
  box-sizing: border-box;
  resize: none;
  height: auto;
  min-height: 56px;
  max-height: 200px;
  padding: 10px 12px 6px;
  border: 0;
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: calc(15px * var(--font-scale));
  line-height: 1.45;
}

/* The command surface (same shape as Home's HomeIntake): attachments,
   the prompt, then one bar of controls under a hairline. */
.composer-surface {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  padding: 7px;
  border: 1px solid var(--border-strong);
  border-radius: 15px;
  background: var(--bg2);
  box-shadow: 0 12px 40px rgb(0 0 0 / 12%);
  transition: border-color 160ms var(--ease), box-shadow 160ms var(--ease);
}

.composer-surface:focus-within {
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border-strong));
  box-shadow: 0 12px 40px rgb(0 0 0 / 12%), 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
}

.composer-surface .chat-input:focus {
  outline: none;
  box-shadow: none;
}

.composer-surface .chat-input::placeholder {
  color: var(--fg3);
}

.composer-bar {
  min-width: 0;
  padding: 6px 2px 0 2px;
  border-top: 1px solid var(--border);
}

.composer-bar:focus { outline: none; }
.composer-bar:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.composer-kbd {
  margin-right: 6px;
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--fg3);
  font-size: var(--text-sm);
}
/* A real keycap: readable glyphs at body size, bordered, so the chord is
   legible next to the send button rather than a faint mono smudge. It follows
   the body font (--font), so Serif or Mono in Settings > Appearance changes it. */
.composer-kbd kbd {
  display: inline-grid;
  place-items: center;
  min-width: 22px;
  height: 22px;
  padding: 0 6px;
  box-sizing: border-box;
  border: 1px solid var(--border-strong);
  border-bottom-width: 2px;
  border-radius: 5px;
  background: var(--bg-elev);
  color: var(--fg2);
  font-family: var(--font);
  font-size: var(--text-sm);
  font-weight: 600;
  line-height: 1;
}

.composer-model {
  min-width: 0;
  height: auto;
}

.composer-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  max-width: 100%;
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elev);
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  transition: border-color 120ms var(--ease), color 120ms var(--ease);
}

.composer-chip:hover,
.composer-chip[aria-expanded="true"] {
  border-color: var(--border-strong);
  color: var(--fg);
}

.composer-chip svg { flex: none; }

.composer-chip-label {
  min-width: 0;
  max-width: 30ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-move-bg { flex: none; margin-right: 6px; }
.composer-move-bg:disabled { opacity: 0.6; cursor: progress; }
.composer-move-bg:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.composer-move-bg-short { display: none; }

.composer-model-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--accent2);
}

/* Touch layouts keep full 44px targets for every composer control. */
@media (pointer: coarse), (max-width: 700px) {
  .composer-chip { min-height: var(--touch); }
  .composer-move-bg { min-width: var(--touch); }
  .image-btn,
  .send-btn { width: var(--touch); height: var(--touch); }
}

@media (max-width: 700px) {
  .chat-input { font-size: max(16px, calc(16px * var(--font-scale))); }
  .composer-kbd { display: none; }
  .composer-chip-label { max-width: 16ch; }
  .composer-move-bg-long { display: none; }
  .composer-move-bg-short { display: inline; }
}

@media (prefers-reduced-motion: reduce) {
  .composer-surface { transition: none; }
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

/* The link back to the memory pass this chat spawned. 44px like every control
   in this footer. */
.archive-memory-pass-btn {
  min-height: 44px;
  min-width: 44px;
}

.image-btn {
  flex-shrink: 0;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  color: var(--fg2);
  transition: background 120ms var(--ease), color 120ms var(--ease);
}
.image-btn:hover { background: var(--bg3); color: var(--fg); }
.image-btn:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }

.send-btn {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  padding: 0;
  border: none;
  border-radius: 9px;
  cursor: pointer;
  transition: background 120ms var(--ease), transform 120ms var(--ease);
  background: var(--accent);
  color: var(--on-accent);
}
.send-btn:hover { background: var(--accent-strong); }
.send-btn:active { transform: scale(0.96); }
.send-btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }
.send-btn.is-stop {
  background: var(--bg3);
  color: var(--fg);
}
.send-btn.is-stop:hover { background: color-mix(in srgb, var(--error) 22%, var(--bg3)); }
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

/* Queued message chips. Each chip is the message text, then one control row:
   the delivery toggle on the left, the edit actions on the right. In a wide
   pane the two share a line; in a narrow one the controls wrap under the text
   instead of the text being squeezed between them. */
.queued-messages {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 6px 0;
  flex-shrink: 0;
}
.queued-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 0 2px 0 12px;
}
.queued-count {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--fg3);
}
.queued-chip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  column-gap: 12px;
  row-gap: 2px;
  padding: 4px 4px 4px 12px;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: 13px;
  color: var(--fg2);
}
.queued-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.queued-chip.is-steering { opacity: 0.85; padding: 8px 12px; }
.queued-steering {
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  flex-shrink: 0;
}
.queued-steering:empty { display: none; }
/* The body's grow factor dwarfs the controls': on a shared line the text takes
   the free space and the controls sit at their own width on the right; once
   wrapped, the controls are alone on their line and stretch to push the
   actions to the far edge. */
.queued-body {
  flex: 1000 1 240px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 4px 0;
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
  color: var(--fg);
}
.queued-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1 0 auto;
}
.queued-label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--accent);
  flex-shrink: 0;
}
.queued-mode {
  display: inline-flex;
  flex-shrink: 0;
  padding: 2px;
  gap: 2px;
  background: var(--bg);
  border-radius: var(--radius-sm);
}
.queued-mode-option {
  background: none;
  border: none;
  border-radius: calc(var(--radius-sm) - 2px);
  color: var(--fg2);
  font-size: 12px;
  font-weight: 600;
  padding: 3px 10px;
  cursor: pointer;
}
.queued-mode-option[aria-pressed="true"] { background: var(--accent); color: var(--bg); }
.queued-mode-option:not([aria-pressed="true"]):hover { background: var(--bg2); color: var(--fg); }
/* Inset and in --fg: the pressed option is filled with --accent, so an accent
   ring would vanish on it. */
.queued-mode-option:focus-visible {
  outline: 2px solid var(--fg);
  outline-offset: -2px;
}
.queued-actions {
  display: flex;
  align-items: center;
  gap: 0;
  flex-shrink: 0;
  margin-left: auto;
}
.queued-steer-all,
.queued-send-now {
  background: none;
  color: var(--accent);
  border: none;
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 600;
  padding: 4px 10px;
  cursor: pointer;
  flex-shrink: 0;
}
.queued-steer-all:hover,
.queued-send-now:hover { background: var(--bg2); }
.queued-steer-all:disabled,
.queued-send-now:disabled { opacity: 0.4; cursor: not-allowed; }
.queued-action,
.queued-remove {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  width: 28px;
  height: 28px;
  padding: 0;
  border-radius: var(--radius-sm);
  flex-shrink: 0;
}
.queued-action:hover:not(:disabled),
.queued-remove:hover { color: var(--fg); background: var(--bg2); }
.queued-action:disabled,
.queued-remove:disabled { opacity: 0.3; cursor: not-allowed; }
.queued-steer-all:focus-visible,
.queued-send-now:focus-visible,
.queued-action:focus-visible,
.queued-remove:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
@media (pointer: coarse) {
  .queued-steer-all,
  .queued-send-now,
  .queued-mode-option {
    min-height: 44px;
  }
  .queued-action,
  .queued-remove {
    width: 44px;
    height: 44px;
  }
  .queued-mode { padding: 0; gap: 0; }
  .queued-mode-option { min-width: 64px; }
}
.steer-notice {
  padding: 0 0 6px;
  margin: 0;
  font-size: 12px;
  color: var(--fg2);
}
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
  margin: 0 0 8px;
  padding: 12px 14px;
  background: var(--bg2);
  border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
  border-radius: 12px;
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
.question-card-note { margin: 0; font-size: 12px; line-height: 1.4; color: var(--fg2); }
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
  font-family: var(--font-mono);
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
  font-family: var(--font-mono);
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
.question-card-error {
  color: var(--error);
  font-size: 12px;
  line-height: 1.4;
}
.question-validation {
  margin: 0;
  color: var(--error);
  font-size: 12px;
  line-height: 1.35;
}
.question-option:disabled,
.question-other:disabled,
.question-card-dismiss:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

/* Native question controls are frequently completed one-handed on a phone.
   Keep their full hit areas at the shared touch minimum while leaving the
   denser desktop controls unchanged. The narrow-width companion covers a
   resized desktop window, where a touch screen may still report a fine
   pointer. */
@media (pointer: coarse), (max-width: 768px) {
  .question-card-dismiss {
    display: grid;
    place-items: center;
    min-width: var(--touch);
    min-height: var(--touch);
    margin: -9px -10px -9px 0;
    padding: 0;
  }
  .question-option,
  .question-other,
  .question-card-actions .btn-sm {
    min-height: var(--touch);
  }
  .question-option { justify-content: center; }
  .question-other {
    padding: 10px 12px;
    font-size: 16px;
  }
}

/* Pending Auto-mode permission prompts. Sticks above the input until the
   user answers. Chrome uses --warning (this is a "waiting on you" state,
   not an action) so --accent reads as a single, unambiguous signal on the
   Approve button rather than being smeared across the whole card. */
.permission-requests {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 0 0 8px;
  flex-shrink: 0;
}

.permission-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  background: color-mix(in srgb, var(--warning) 7%, var(--bg2));
  border: 1px solid color-mix(in srgb, var(--warning) 45%, var(--border));
  border-radius: 12px;
  font-size: var(--text-sm);
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
  gap: 8px;
  color: var(--fg);
  line-height: 1.4;
}

.permission-icon {
  width: 16px;
  height: 16px;
  color: var(--warning);
  flex-shrink: 0;
}

.permission-tool {
  font-size: var(--text-sm);
  font-weight: 650;
  color: var(--fg);
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
  padding: 8px 10px;
  background: var(--bg);
  border: 0;
  border-radius: 8px;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--fg);
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
  padding: 8px 10px;
  background: var(--bg);
  border: 0;
  border-radius: 8px;
  font-size: var(--text-xs);
  max-height: 220px;
  overflow: auto;
}

.permission-args dt {
  font-family: var(--font-mono);
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
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.5;
  text-align: center;
  color: var(--fg2);
  background: color-mix(in srgb, var(--fg) 8%, transparent);
  border: 1px solid currentColor;
  border-radius: 3px;
  opacity: 0.75;
}
.btn-approve .permission-key { color: inherit; }
.btn-approve {
  background: var(--accent);
  color: var(--on-accent);
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
}

/* Recovery picker: here a pick is a retry of the failed turn, not a model
   change, and the picker says so before the user chooses. */
.model-picker-recovery {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.model-picker-recovery__title {
  margin: 0;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg);
}

.model-picker-recovery__note {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--fg2);
  line-height: 1.4;
}

.thinking-levels {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: 2px 4px;
}

.thinking-levels__label {
  font-size: var(--text-sm);
  color: var(--fg2);
}

/* Segmented control, the same shape as Settings → Providers' thinking picker. */
.thinking-levels__chips {
  display: inline-flex;
  flex-wrap: wrap;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
}

.thinking-chip {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  padding: 0 10px;
  border: 0;
  border-radius: 6px;
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  transition: background 120ms var(--ease), color 120ms var(--ease);
}

.thinking-chip:hover {
  color: var(--fg);
}

.thinking-chip--active {
  background: var(--bg3);
  color: var(--fg);
  font-weight: 600;
}

@media (pointer: coarse) {
  .thinking-chip { min-height: var(--touch); }
}

/* PaneHeader squares every header .btn-icon to 30px, which crushed this
   labelled button: the icon shrank to nothing and the word spilled out. The
   doubled class outranks that rule so the button sizes to icon + label. */
.chat-archive-btn.btn-icon {
  box-sizing: border-box;
  gap: var(--space-2);
  width: auto;
  height: 34px;
  min-width: 0;
  margin: 0;
  padding: 0 14px;
  white-space: nowrap;
}
.chat-archive-btn.btn-icon::before { inset: 0; }
.chat-archive-btn svg { flex-shrink: 0; }
/* Beside the header's Chat details opener Archive is icon-only and squares to
   the opener's 34px; coarse pointers get the touch minimum below. */
.chat-archive-btn--icon .chat-archive-label { display: none; }
.chat-archive-btn.btn-icon.chat-archive-btn--icon {
  padding: 0;
  min-width: 34px;
}
@media (pointer: coarse) {
  .chat-archive-btn { min-height: var(--touch); }
  .chat-archive-btn.btn-icon.chat-archive-btn--icon { min-width: var(--touch); }
}

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
  color: var(--on-accent);
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

/* The split — a pinned file beside the chat, or the terminal's tile: when
   the chat column is narrow, reclaim horizontal space from gutter padding so
   bubbles stay readable without extra scroll. */
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

/* Phones (the pane header bell's breakpoint): the title needs the room. Back and
   the menu already leave the chat, and Chat details' Repository section carries
   the code mode switch, so neither control is lost. Hidden, not removed, so
   desktop keeps them. */
@media (max-width: 767px) {
  .close-btn,
  .chat-code-badge { display: none; }
}

@media (max-width: 768px) {
  /* Keep the title header compact while preserving safe tap targets. */
  :deep(.pane-header) {
    padding-left: calc(12px + var(--safe-left));
    padding-right: calc(12px + var(--safe-right));
  }
  :deep(.header-title) { text-align: left; min-width: 0; }
  .header-left { min-width: 0; }
  .chat-archive-btn {
    color: var(--fg2);
  }
  /* The label is hidden here, so the button is icon-only and must carry the
     touch minimum itself. The base `.chat-archive-btn.btn-icon { height: 34px }`
     outranks App.vue's `.btn-icon { min-height: var(--touch) }`, and the
     `pointer: coarse` rule below never fires on a fine-pointer device at a
     narrow viewport, so without this the button measures 46x34 and
     narrow-viewport.spec.ts fails. min-height beats the fixed height. */
  .chat-archive-btn.btn-icon,
  .chat-archive-btn.btn-icon.chat-archive-btn--icon {
    min-width: var(--touch);
    min-height: var(--touch);
  }
  .chat-archive-btn:hover { color: var(--fg); }
  .chat-archive-label { display: none; }
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
     Preserve the 61px footer lock (44 + 8 + 8 + 1) used on desktop. */
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
}

/* Chat comment selection trigger + composer */
/* Comment trigger pill. Shape and behaviour match the danger-red variant
 * used in FileViewerModal and PinnedFilePanel so the "Comment" affordance
 * looks the same regardless of where the user is in the app. */
/* Selection → "Comment": an inverted pill that reads as a tool, not an
   alert (it used to be the error red). */
.chat-comment-trigger {
  position: fixed;
  z-index: 5;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 30px;
  padding: 0 10px;
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--bg);
  background: var(--fg);
  border: none;
  border-radius: 8px;
  cursor: pointer;
  box-shadow: 0 8px 20px rgb(0 0 0 / 25%);
  user-select: none;
}
.chat-comment-trigger:hover { background: color-mix(in srgb, var(--fg) 88%, var(--bg)); }
.chat-comment-trigger-icon { font-size: var(--text-sm); line-height: 1; }
@media (pointer: coarse) {
  .chat-comment-trigger { min-height: var(--touch); }
}

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
  color: var(--on-accent);
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
  /* Its clip box reaches 22px past the text column (padded back), so the
     transcript's widened scroll box fits inside it instead of being cut at the column edge. */
  margin-inline: -22px;
  padding-inline: 22px;
}
.chat-with-sidebar > .messages {
  flex: 1;
  min-width: 0;
}

/* Chat details as a drawer: the rail's own markup, over the chat on a pane
   too narrow to hold it beside a reading column. Full height, one scroll, the
   close button in its head; a bottom sheet on a phone. */
.chat-rail.chat-rail--drawer {
  position: absolute;
  z-index: 55;
  top: 0;
  right: 0;
  bottom: 0;
  width: min(390px, 100%);
  /* The rail's flex-start would shrink an absolutely positioned box to its
     content; the drawer spans the pane. */
  align-self: stretch;
  max-height: none;
  margin: 0;
  border-width: 0 0 0 1px;
  border-color: var(--border-strong);
  border-radius: 0;
  box-shadow: -1rem 0 3rem rgb(0 0 0 / 28%);
  overscroll-behavior: contain;
  animation: chat-drawer-in 190ms var(--ease);
}
.chat-rail--drawer .chat-rail-head {
  position: sticky;
  top: -16px;
  z-index: 1;
  background: var(--bg2);
}
@keyframes chat-drawer-in {
  from { opacity: 0; transform: translateX(18px); }
  to { opacity: 1; transform: none; }
}
@media (max-width: 600px) {
  .chat-rail.chat-rail--drawer {
    top: auto;
    left: 0;
    width: 100%;
    max-height: min(76dvh, 680px);
    padding-bottom: calc(16px + var(--safe-bottom));
    border-width: 1px 0 0;
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    box-shadow: 0 -1rem 3rem rgb(0 0 0 / 34%);
    animation-name: chat-drawer-sheet-in;
  }
  @keyframes chat-drawer-sheet-in {
    from { opacity: 0; transform: translateY(18px); }
    to { opacity: 1; transform: none; }
  }
}
@media (prefers-reduced-motion: reduce) {
  .chat-rail--drawer { animation: none; }
}
/* Inline text highlights inside message bubbles. Use :deep() because
 * highlight spans are inserted via DOM manipulation in applyHighlights()
 * and don't carry Vue's scoped attribute. */
:deep(.comment-highlight) {
  background: color-mix(in srgb, var(--accent2) 26%, transparent);
  border-bottom: 1px solid color-mix(in srgb, var(--accent2) 70%, transparent);
  cursor: pointer;
  transition: background 0.15s;
  border-radius: 2px;
}
:deep(.comment-highlight:hover) {
  background: color-mix(in srgb, var(--accent2) 40%, transparent);
}
/* In-progress draft selection: the accent, so it reads as "being written". */
:deep(.comment-highlight[data-comment-id="__draft__"]) {
  background: color-mix(in srgb, var(--accent) 28%, transparent);
  border-bottom-color: color-mix(in srgb, var(--accent) 70%, transparent);
}
/* Brief flash when navigated to from a pending-comment chip. */
:deep(.comment-highlight--pulse) {
  animation: comment-pulse 1.1s var(--ease) 1;
}
@keyframes comment-pulse {
  0%   { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent2) 0%, transparent); }
  25%  { box-shadow: 0 0 0 5px color-mix(in srgb, var(--accent2) 30%, transparent); }
  100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent2) 0%, transparent); }
}
@media (prefers-reduced-motion: reduce) {
  :deep(.comment-highlight--pulse) { animation: none; }
}

</style>

<style scoped src="./chatTrace.css"></style>

<!-- The openable dead link: a `file:` path rendered as a button the engine
     can act on. Shared with the file viewer and the pinned-file panel. -->
<style scoped src="./markdownDeadLink.css"></style>

<!-- The shared naming placeholder: the same bar the sidebar shows while the
     engine names a chat. -->
<style scoped src="./titleShimmer.css"></style>
