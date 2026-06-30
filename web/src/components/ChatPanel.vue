<template>
  <div class="chat-panel" @dragover.prevent="dragOver = true" @dragleave="dragOver = false" @drop.prevent="handleDrop" @click="handleFileLinkClick">
    <div v-if="dragOver" class="drop-overlay">Drop images here</div>

    <!-- Header -->
    <PaneHeader @open-sidebar="$emit('open-sidebar')">
      <template #title>
        <div class="header-left">
          <button class="close-btn desktop-only" @click="$emit('close')" title="Close chat">&times;</button>
          <div class="header-breadcrumb" ref="breadcrumbRef">
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
            <template v-else>
              <span
                v-if="project && project.name !== 'General'"
                class="breadcrumb-project"
                @click.stop="toggleContext"
                :class="{ active: showContext }"
              >{{ project.name }}</span>
              <span v-if="project && project.name !== 'General'" class="breadcrumb-separator">/</span>
              <span class="chat-title" @dblclick.stop="startEditTitle" @click.stop>{{ chat.title }}</span>
            </template>
            <!-- Project context popup -->
            <div
              v-if="showContext"
              class="context-popup"
              @click.stop
            >
              <div class="context-popup-body">
                <div v-if="project?.vault_doc_path" class="context-popup-section">
                  <span class="label-eyebrow">Project</span>
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
                      <span class="context-file-icon">{{ f.kind === 'image' ? '🖼' : f.kind === 'markdown' ? '📄' : '📎' }}</span>
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
        <div class="model-picker-wrap" ref="modelPickerRef">
          <button
            class="model-picker-btn btn-icon"
            :title="chat.model + (chat.thinking_level ? ' · ' + chat.thinking_level : '')"
            @click.stop="toggleModelPicker"
            aria-label="Model"
          >
            <span aria-hidden="true">🧠</span>
          </button>
          <div
            v-if="showModelPicker"
            class="model-picker-dropdown"
            @click.stop
          >
            <div class="picker-section">
              <div class="picker-label">Provider</div>
              <div class="picker-pills">
                <button
                  v-for="b in bucketOptions"
                  :key="b.key"
                  class="picker-pill"
                  :class="{ active: activeBucket === b.key, handover: bucketLocked && activeBucket !== b.key }"
                  :title="bucketLocked && activeBucket !== b.key ? 'Start a provider handover in this chat.' : ''"
                  @click="selectBucket(b.key)"
                >{{ b.label }}</button>
              </div>
              <p v-if="bucketLocked" class="picker-hint">Choosing another provider starts a handover in this chat.</p>
            </div>
            <div class="picker-section">
              <div class="picker-label">Model</div>
              <div class="picker-list">
                <button
                  v-for="m in filteredModels"
                  :key="m"
                  class="picker-item"
                  :class="{ active: chat.model === m }"
                  @click="selectModel(m)"
                >{{ m }}</button>
              </div>
            </div>
            <div v-if="filteredThinkingLevels.length" class="picker-section">
              <div class="picker-label">Thinking</div>
              <div class="picker-pills">
                <button
                  class="picker-pill"
                  :class="{ active: !(chat.thinking_level || '') }"
                  @click="selectThinking('')"
                >default</button>
                <button
                  v-for="lvl in filteredThinkingLevels"
                  :key="lvl"
                  class="picker-pill"
                  :class="{ active: chat.thinking_level === lvl }"
                  @click="selectThinking(lvl)"
                >{{ lvl }}</button>
              </div>
            </div>
          </div>
        </div>
        <button class="btn-icon" @click="doArchive" title="Archive chat" aria-label="Archive chat">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/></svg>
        </button>
      </template>
    </PaneHeader>

    <!-- Messages + comment sidebar -->
    <div class="chat-with-sidebar">
    <div class="messages" ref="messagesEl" @click="handleHighlightClick">
      <template v-for="(item, i) in renderItems" :key="i">
        <!-- Reasoning trace: intermediate assistant text + tool calls grouped -->
        <div v-if="item.kind === 'trace'" class="trace-block" :class="{ open: openTraces[i] }">
          <div class="trace-summary" @click="toggleTrace(i)">
            <span class="trace-chevron">{{ openTraces[i] ? '\u25BE' : '\u25B8' }}</span>
            <span class="trace-icon">&#129504;</span>
            <span class="trace-label">Reasoning</span>
            <span class="trace-meta">{{ traceSummaryMeta(item.steps) }}</span>
          </div>
          <div v-if="openTraces[i]" class="trace-body">
            <template v-for="(step, j) in item.steps" :key="j">
              <div v-if="step.tool_name === '_activity'" class="trace-tools">
                <div
                  v-for="(line, k) in step.content.split('\n')"
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
                <span class="file-card-icon" aria-hidden="true">{{ fileCardIcon(step.file_path || step.content) }}</span>
                <span class="file-card-main">
                  <span class="file-card-name">{{ fileCardBasename(step.file_path || step.content) }}</span>
                  <span class="file-card-meta">
                    <span class="file-card-action">{{ step.action || 'touched' }}</span>
                    <span v-if="fileCardDirname(step.file_path || step.content)" class="file-card-dir"> · {{ fileCardDirname(step.file_path || step.content) }}</span>
                  </span>
                </span>
                <span class="file-card-chevron" aria-hidden="true">&#8599;</span>
              </button>
              <div
                v-else-if="step.tool_name === '_thinking'"
                class="trace-text trace-thinking"
                v-html="renderMarkdown(step.content)"
              ></div>
              <div v-else class="trace-text" v-html="renderMarkdown(step.content)"></div>
            </template>
          </div>
        </div>
        <!-- User message -->
        <div v-else-if="item.kind === 'user'" class="message user">
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
          <div v-if="item.msg.timestamp" class="message-meta">
            {{ formatTime(item.msg.timestamp) }}
          </div>
        </div>
        <!-- Final assistant message -->
        <div v-else-if="item.kind === 'assistant'" class="message assistant" :class="{ error: item.msg.is_error }">
          <div class="message-content" v-html="renderMarkdown(item.msg.content)"></div>
          <div v-if="item.msg.timestamp || item.msg.effective_model" class="message-meta">
            <span v-if="item.msg.timestamp">{{ formatTime(item.msg.timestamp) }}</span>
            <span v-if="item.msg.duration_ms"> &middot; {{ formatDuration(item.msg.duration_ms) }}</span>
            <span v-if="item.msg.effective_model"> &middot; {{ item.msg.effective_model }}</span>
            <span v-if="item.msg.usage?.input_tokens"> | in:{{ item.msg.usage.input_tokens }} out:{{ item.msg.usage.output_tokens }}</span>
          </div>
        </div>
        <!-- System message (errors, etc) -->
        <div v-else-if="item.kind === 'system'" class="message system">
          <div class="message-content" v-html="renderMarkdown(item.msg.content)"></div>
          <button
            v-if="isErrorMsg(item.msg.content) && lastUserBefore(i)"
            class="retry-btn"
            @click="retryFromError(i)"
          >Retry</button>
        </div>
      </template>

      <div v-if="chat.retry?.status === 'pending'" class="retry-card">
        <div class="retry-card-main">
          <span class="retry-card-icon">⏱</span>
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
      <div v-if="store.isStreaming" class="trace-block open live">
        <div class="trace-summary">
          <span class="activity-spinner"></span>
          <span class="trace-icon">&#129504;</span>
          <span class="trace-label">{{ (store.currentTimeline.length || store.currentStreamingText) ? 'Working...' : 'Thinking...' }}</span>
        </div>
        <div
          v-if="store.currentTimeline.length || store.currentStreamingText || store.currentStreamingThinking"
          class="trace-body"
        >
          <template v-for="(entry, j) in store.currentTimeline" :key="j">
            <div v-if="entry.kind === 'tool'" class="trace-tools">
              <div
                v-for="(line, k) in entry.content.split('\n')"
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
              <span class="file-card-icon" aria-hidden="true">{{ fileCardIcon(entry.file_path) }}</span>
              <span class="file-card-main">
                <span class="file-card-name">{{ fileCardBasename(entry.file_path) }}</span>
                <span class="file-card-meta">
                  <span class="file-card-action">{{ entry.action }}</span>
                  <span v-if="fileCardDirname(entry.file_path)" class="file-card-dir"> · {{ fileCardDirname(entry.file_path) }}</span>
                </span>
              </span>
              <span class="file-card-chevron" aria-hidden="true">&#8599;</span>
            </button>
            <div
              v-else-if="entry.kind === 'thinking'"
              class="trace-text trace-thinking"
              v-html="renderMarkdown(entry.content)"
            ></div>
            <div v-else class="trace-text" v-html="renderMarkdown(entry.content)"></div>
          </template>
          <div
            v-if="store.currentStreamingThinking"
            class="trace-text trace-thinking trace-streaming"
            v-html="renderMarkdown(store.currentStreamingThinking)"
          ></div>
          <div v-if="store.currentStreamingText" class="trace-text trace-streaming" v-html="renderMarkdown(store.currentStreamingText)"></div>
        </div>
        </div>

      <div ref="scrollAnchor"></div>

      <!-- Floating "Comment" pill is teleported to body so it isn't clipped by
           .messages (position: relative + overflow-y: auto). The composer
           itself lives in the right-side comment sidebar, not as a popover. -->
      <Teleport to="body">
        <button
          v-if="selectionAnchor"
          class="chat-comment-trigger"
          :style="{ top: selectionAnchor.top + 'px', left: selectionAnchor.left + 'px' }"
          @mousedown.prevent
          @click="openCommentForSelection"
          type="button"
          title="Comment on this selection"
        >
          <span class="chat-comment-trigger-icon">💬</span>
          Comment
        </button>
      </Teleport>
    </div>

    <!-- Right-side comment sidebar: shows pending chat comments (and the
         in-flight draft) while you're composing the next message. Cleared
         on send, same lifecycle as the existing pending-chip strip. -->
    <aside v-if="commentSidebarVisible" class="chat-comment-sidebar">
      <div class="chat-sidebar-header">
        <span class="chat-sidebar-title">Comments</span>
        <span class="chat-sidebar-count">{{ store.pendingChatComments.length + (commentDraft ? 1 : 0) }}</span>
      </div>

      <div v-if="commentDraft" class="chat-sidebar-draft" @mousedown.stop>
        <div class="chat-sidebar-draft-header">
          <span class="chat-sidebar-draft-label">New comment</span>
          <button class="chat-sidebar-card-remove" @click="cancelChatComment" title="Cancel">&times;</button>
        </div>
        <div class="chat-sidebar-card-quote">"{{ truncate(commentDraft.selection, 120) }}"</div>
        <textarea
          ref="chatCommentInputEl"
          v-model="commentDraft.text"
          class="chat-sidebar-draft-input"
          placeholder="Add a comment…"
          rows="3"
          @keydown="onChatCommentKeydown"
        ></textarea>
        <div v-if="commentDraftImages.length" class="chat-sidebar-draft-images">
          <span v-for="(img, i) in commentDraftImages" :key="img" class="draft-image-preview">
            <img :src="`/api/images/${img}`" :alt="img" class="draft-image-thumb" />
            <button class="draft-image-remove" @click="removeDraftImage(i)" title="Remove">&times;</button>
          </span>
        </div>
        <div class="chat-sidebar-draft-actions">
          <label class="image-btn-sm" title="Upload images">
            <input type="file" accept="image/*" multiple hidden @change="handleDraftImageUpload" />
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
          </label>
          <button class="btn-sm" @click="cancelChatComment" type="button">Cancel</button>
          <button
            class="btn-sm primary"
            :disabled="!commentDraft.text.trim()"
            @click="saveChatComment"
            type="button"
          >Add comment</button>
        </div>
      </div>

      <div class="chat-sidebar-list" ref="sidebarListEl">
        <div
          v-for="c in store.pendingChatComments"
          :key="c.id"
          class="chat-sidebar-card"
          :class="{ 'is-editing': editingChatCommentId === c.id }"
          :data-card-id="c.id"
          @click="editingChatCommentId !== c.id && jumpToCommentHighlight(c.id)"
        >
          <div class="chat-sidebar-card-header">
            <span class="chat-sidebar-card-file">Selection</span>
            <div class="chat-sidebar-card-actions">
              <button
                v-if="editingChatCommentId !== c.id"
                class="chat-sidebar-card-edit"
                @click.stop="startEditChatComment(c)"
                title="Edit"
              >&#9998;</button>
              <button
                class="chat-sidebar-card-remove"
                @click.stop="deleteChatComment(c.id)"
                title="Delete"
              >&times;</button>
            </div>
          </div>
          <div class="chat-sidebar-card-quote">"{{ truncate(c.selection, 120) }}"</div>
          <div v-if="editingChatCommentId !== c.id && c.images?.length" class="chat-sidebar-card-images">
            <img
              v-for="img in c.images"
              :key="img"
              :src="`/api/images/${img}`"
              :alt="img"
              class="card-image-thumb"
              @click.stop
            />
          </div>
          <div v-if="editingChatCommentId !== c.id" class="chat-sidebar-card-note">{{ c.comment }}</div>
          <div v-if="editingChatCommentId === c.id" class="chat-sidebar-edit-body" @mousedown.stop @click.stop>
            <textarea
              ref="sidebarEditInputEl"
              v-model="editingChatCommentText"
              class="chat-sidebar-edit-input"
              rows="3"
              @keydown="onEditChatCommentKeydown($event, c.id)"
            ></textarea>
            <div v-if="editingChatCommentImages.length" class="chat-sidebar-edit-images">
              <span v-for="(img, i) in editingChatCommentImages" :key="img" class="draft-image-preview">
                <img :src="`/api/images/${img}`" :alt="img" class="draft-image-thumb" />
                <button class="draft-image-remove" @click.stop="removeEditImage(i)" title="Remove">&times;</button>
              </span>
            </div>
            <div class="chat-sidebar-edit-actions">
              <label class="image-btn-sm" title="Upload images">
                <input type="file" accept="image/*" multiple hidden @change="handleEditImageUpload($event, c.id)" />
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
              </label>
              <button class="btn-sm" @click="cancelEditChatComment" type="button">Cancel</button>
              <button
                class="btn-sm primary"
                :disabled="!editingChatCommentText.trim()"
                @click="saveEditChatComment(c.id)"
                type="button"
              >Save</button>
            </div>
          </div>
        </div>
      </div>
    </aside>
    </div>

    <!-- Scroll-to-bottom float button -->
    <button
      v-if="showScrollBtn"
      class="scroll-to-bottom-btn"
      @click="scrollToBottom"
      title="Scroll to bottom"
      aria-label="Scroll to bottom"
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
    </button>

    <!-- AskUserQuestion picker. The model paused mid-turn to ask the user
         a structured question; we render an interactive option list so the
         answer flows back as the next user message. The SDK's built-in CLI
         picker can't run headless, so this is the only path. -->
    <div v-if="activeQuestions.length" class="question-card">
      <div class="question-card-header">
        <span class="question-card-icon">&#10067;</span>
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
        <div class="question-block-prompt">{{ q.question }}</div>
        <div class="question-options">
          <button
            v-for="opt in q.options"
            :key="opt.label"
            type="button"
            class="question-option"
            :class="{ selected: isQuestionOptionSelected(qi, opt.label) }"
            @click="toggleQuestionOption(qi, opt.label, q.multiSelect)"
          >
            <span class="question-option-label">{{ opt.label }}</span>
            <span v-if="opt.description" class="question-option-desc">{{ opt.description }}</span>
          </button>
        </div>
        <input
          type="text"
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

    <!-- Pending Auto-mode permission prompts. Shown above queued/input so
         the user can't miss them. Each prompt sticks until it's approved or
         denied; the server resolves still-open prompts on turn teardown. -->
    <div v-if="pendingApprovals.length" class="permission-requests">
      <div
        v-for="p in pendingApprovals"
        :key="p.request_id"
        class="permission-card"
      >
        <div class="permission-header">
          <span class="permission-icon">&#128679;</span>
          <span class="permission-tool">{{ p.tool_name }}</span>
          <span class="permission-message">{{ p.message }}</span>
        </div>
        <pre v-if="p.tool_input" class="permission-input">{{ p.tool_input }}</pre>
        <div class="permission-actions">
          <button
            class="btn-deny"
            @click="store.respondPermission(chat.chat_id, p.request_id, false, 'User denied')"
          >Deny</button>
          <button
            class="btn-approve"
            @click="store.respondPermission(chat.chat_id, p.request_id, true)"
          >Approve</button>
        </div>
      </div>
    </div>

    <!-- Queued messages (sent while a response was already streaming). -->
    <div v-if="store.currentQueued.length" class="queued-messages">
      <div v-for="(q, i) in store.currentQueued" :key="i" class="queued-chip" title="Will be sent when current response finishes">
        <span class="queued-label">Queued</span>
        <div class="queued-body">
          <div v-if="q.images?.length" class="queued-images">
            <img v-for="img in q.images" :key="img" :src="`/api/images/${img}`" :alt="img" class="queued-image-thumb" />
          </div>
          <span class="queued-text">{{ q.text }}</span>
        </div>
        <button class="queued-remove" @click="store.removeQueued(chat.chat_id, i)" title="Remove">&times;</button>
      </div>
    </div>

    <!-- Pending image attachments. File and chat comments stay visible in
         their own sidebars/viewers to avoid duplicate composer chips. -->
    <div
      v-if="store.pendingImages.length"
      class="pending-attachments"
    >
      <span v-for="(ref, i) in store.pendingImages" :key="`img-${ref}`" class="image-preview">
        <img :src="`/api/images/${ref}`" :alt="ref" class="image-preview-thumb" />
        <button class="image-ref-chip" @click="insertImageRef(i + 1)" title="Insert reference at cursor">[Image {{ i + 1 }}]</button>
        <button class="image-preview-remove" @click="removePendingImage(i)" title="Remove">&times;</button>
      </span>
    </div>

    <!-- Input -->
    <!-- Streaming controls bar (separate row above input) -->
    <div v-if="store.isStreaming && !chat.archived" class="streaming-bar">
      <span class="streaming-spinner" aria-hidden="true">{{ spinnerFrame }}</span>
      <span class="streaming-label">ciaobot is thinking</span>
      <button class="stop-btn" @click="store.stopChat(chat.chat_id)">
        <span class="stop-icon">&#9632;</span>
        <span class="stop-text">stop</span>
      </button>
    </div>

    <!-- Slash-command picker (shown when the input starts with "/") -->
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
        <span class="commands-picker-name">/{{ cmd.name }}</span>
        <span v-if="cmd.argument_hint" class="commands-picker-hint">{{ cmd.argument_hint }}</span>
        <span v-if="cmd.description" class="commands-picker-desc">{{ cmd.description }}</span>
        <span class="commands-picker-source">{{ cmd.source }}</span>
      </div>
    </div>

    <div class="input-bar" :class="{ disabled: chat.archived, 'has-streaming-bar': store.isStreaming && !chat.archived }">
      <template v-if="chat.archived">
        <div class="archived-notice">
          <span>This chat is archived.</span>
          <button class="btn-sm primary continue-chat-btn" @click="continueChat" :disabled="isContinuing">
            {{ isContinuing ? 'Continuing...' : 'Continue in new chat' }}
          </button>
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
          @input="autoResize"
          @paste="handlePaste"
          @focus="handleInputFocus"
        ></textarea>
        <div class="input-actions">
          <!-- Voice recording is allowed during streaming too: the user's
               transcript becomes a queued follow-up, same as typed text. -->
          <VoiceRecorder v-if="!transcribing" @recorded="handleVoice" />
          <span v-else class="voice-transcribing" title="Transcribing...">
            <span class="transcribe-spinner"></span>
          </span>
          <label class="image-btn" title="Upload images" aria-label="Upload images">
            <input type="file" accept="image/*" multiple hidden @change="handleFileSelect" />
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
          </label>
          <button
            class="send-btn"
            :disabled="!inputText.trim() && !store.pendingImages.length && !store.pendingComments.length && !store.pendingChatComments.length"
            :title="store.isStreaming ? 'Queue message (sends when current turn finishes)' : 'Send'"
            :aria-label="store.isStreaming ? 'Queue message' : 'Send message'"
            @click="send"
          ><span class="send-glyph">{{ store.isStreaming ? '»' : '↵' }}</span></button>
        </div>
      </template>
    </div>

  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useFileViewerStore } from '../stores/fileViewer'
import VoiceRecorder from './VoiceRecorder.vue'
// SubagentPanel removed: subagent data is per-chat, not per-turn, so it
// doesn't map well to the turn-based rendering. Re-add when we have
// per-turn subagent association.
import { api } from '../lib/api'
import type { ModelsResponse, ChatMessage } from '../lib/types'
import PaneHeader from './PaneHeader.vue'
import { linkifyText } from '../lib/filePaths'
import { renderMarkdown as renderSafeMarkdown } from '../lib/safeMarkdown'
import { formatTime, formatDuration } from '../lib/time'

type RenderItem =
  | { kind: 'user'; msg: ChatMessage }
  | { kind: 'assistant'; msg: ChatMessage }
  | { kind: 'system'; msg: ChatMessage }
  | { kind: 'trace'; steps: ChatMessage[] }

type ChatComment = {
  id: string
  selection: string
  comment: string
  images?: string[]
}

const emit = defineEmits<{ close: [], 'open-sidebar': [] }>()

const store = useProjectStore()
const fileViewer = useFileViewerStore()
const inputText = ref('')
const inputEl = ref<HTMLTextAreaElement>()
const isContinuing = ref(false)

// Braille spinner for the "ciaobot is thinking" indicator. We tick a
// ref instead of using a CSS @keyframes since content can't be animated
// reliably across browsers.
const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
const spinnerIdx = ref(0)
const spinnerFrame = computed(() => SPINNER_FRAMES[spinnerIdx.value])
let spinnerTimer: ReturnType<typeof setInterval> | null = null
watch(() => store.isStreaming, (streaming) => {
  if (streaming && !spinnerTimer) {
    spinnerTimer = setInterval(() => {
      spinnerIdx.value = (spinnerIdx.value + 1) % SPINNER_FRAMES.length
    }, 90)
  } else if (!streaming && spinnerTimer) {
    clearInterval(spinnerTimer)
    spinnerTimer = null
  }
}, { immediate: true })

// Slash-command picker: populated once on mount from /api/commands.
type SlashCommand = {
  name: string
  description: string
  argument_hint: string
  source: 'project' | 'user'
  path: string
}
const slashCommands = ref<SlashCommand[]>([])
const commandHighlightIdx = ref(0)

const filteredCommands = computed<SlashCommand[]>(() => {
  const text = inputText.value
  if (!text.startsWith('/')) return []
  // Only show while the user is typing the command name itself — once they
  // add a space or newline, they're typing arguments and the picker steps aside.
  const firstToken = text.slice(1).split(/\s/, 1)[0] ?? ''
  if (text.slice(1).includes(' ') || text.slice(1).includes('\n')) return []
  const needle = firstToken.toLowerCase()
  return slashCommands.value.filter(c => c.name.toLowerCase().startsWith(needle))
})

const showCommandsPicker = computed(() => filteredCommands.value.length > 0)

watch(filteredCommands, (list) => {
  if (commandHighlightIdx.value >= list.length) commandHighlightIdx.value = 0
})

function applyCommand(cmd: SlashCommand) {
  // Trailing space only when the command expects arguments, so a naked
  // `/brief` is ready to send without requiring an extra keystroke.
  inputText.value = cmd.argument_hint ? `/${cmd.name} ` : `/${cmd.name}`
  commandHighlightIdx.value = 0
  nextTick(() => {
    inputEl.value?.focus()
    autoResize()
  })
}
const messagesEl = ref<HTMLElement>()
const scrollAnchor = ref<HTMLElement>()
const editingTitle = ref(false)
const titleValue = ref('')
const dragOver = ref(false)
const chat = computed(() => store.activeChat!)
const project = computed(() => store.activeProject)
const models = ref<string[]>(['sonnet', 'opus', 'haiku'])
const providerModels = ref<Record<string, string[]>>({})
const providerDefaults = ref<Record<string, string>>({})
const ollamaModels = ref<string[]>([])
const thinkingLevels = ref<Record<string, string[]>>({})
const openTraces = ref<Record<number, boolean>>({})
const transcribing = ref(false)
const isNearBottom = ref(true)
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
  ([open]) => { if (open) loadProjectFiles() },
  { immediate: true }
)

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

type ProviderKey = 'claude' | 'pi'
type BucketKey = 'claude_work' | 'claude_personal' | 'pi_personal'

const BUCKET_DEFS: { key: BucketKey; label: string; provider: ProviderKey }[] = [
  { key: 'claude_work', label: 'Claude (Work)', provider: 'claude' },
  { key: 'claude_personal', label: 'Claude (Personal)', provider: 'claude' },
  { key: 'pi_personal', label: 'Pi (Personal)', provider: 'pi' },
]

const bucketOptions = computed(() => BUCKET_DEFS.filter(b => (providerModels.value[b.key] || []).length > 0))

function toggleTrace(i: number) {
  openTraces.value = { ...openTraces.value, [i]: !openTraces.value[i] }
}

function checkScroll() {
  const el = messagesEl.value
  if (!el) return
  const threshold = 80
  isNearBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < threshold
}

function scrollToBottom() {
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
  isNearBottom.value = true
}

const pendingApprovals = computed(() => {
  const id = store.activeChatId
  if (!id) return []
  return store.pendingPermissions[id] || []
})

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

function submitQuestionAnswers() {
  if (!allQuestionsAnswered.value) return
  if (!chat.value || chat.value.archived) return
  const qs = activeQuestions.value
  if (!qs.length) return
  const lines: string[] = []
  for (let i = 0; i < qs.length; i++) {
    const q = qs[i]
    const a = questionAnswers.value[i]
    const picked = a ? Array.from(a.selected) : []
    const other = (a?.other || '').trim()
    const parts: string[] = []
    if (picked.length) parts.push(...picked)
    if (other) parts.push(other)
    const answer = parts.length ? parts.join(', ') : '(no answer)'
    lines.push(`**${q.header || q.question}**: ${answer}`)
  }
  const text = lines.join('\n')
  // sendMessage clears activeQuestions for this chat automatically.
  store.sendMessage(chat.value.chat_id, text, 'queue')
}

function dismissQuestions() {
  const id = store.activeChatId
  if (!id) return
  delete store.activeQuestions[id]
  questionAnswers.value = {}
}

const activeProvider = computed<ProviderKey>(() => {
  return (chat.value?.provider as ProviderKey) || 'claude'
})

const activeBucket = computed<BucketKey>(() => {
  const c = chat.value
  if (!c) return 'claude_work'
  if (c.provider === 'pi') return 'pi_personal'
  // The server records the explicit bucket choice. Legacy values are kept
  // for existing chats; new workspace config may use clearer bucket names.
  if (c.model_bucket === 'work' || c.model_bucket === 'anthropic') return 'claude_work'
  if (c.model_bucket === 'personal' || c.model_bucket === 'ollama') return 'claude_personal'
  return ollamaModels.value.includes(c.model) ? 'claude_personal' : 'claude_work'
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

const filteredModels = computed(() => {
  return providerModels.value[activeBucket.value] || models.value
})

// Thinking levels are provider-native (claude → SDK effort, pi → --thinking),
// so they key off the provider, not the bucket.
const filteredThinkingLevels = computed(() => {
  return thinkingLevels.value[activeProvider.value] || []
})

// Most recent CLI-reported context-window occupancy (e.g. "73.2%").
// Populated from the ResultEvent's usage dict; undefined until the first
// turn completes, and omitted if the CLI call fails.
const latestContextPct = computed(() => {
  const msgs = store.activeMessages
  for (let i = msgs.length - 1; i >= 0; i--) {
    const pct = msgs[i]?.usage?.context_pct
    if (pct) return pct
  }
  return ''
})

const inputPlaceholder = computed(() => {
  if (store.isStreaming) return 'Follow-up...'
  // Model name is already shown in the header selector, so it's redundant
  // here. Context % is the genuinely useful info that isn't on screen
  // elsewhere, so surface it alone when available.
  const ctx = latestContextPct.value
  return ctx ? `Message (${ctx})` : 'Message'
})

// ── Chat comment selection UX ─────────────────────────────────────────
type ChatCommentDraft = { selection: string; text: string }
const selectionAnchor = ref<{ top: number; left: number } | null>(null)
const commentDraft = ref<ChatCommentDraft | null>(null)
const chatCommentInputEl = ref<HTMLTextAreaElement>()
const sidebarEditInputEl = ref<HTMLTextAreaElement>()
const sidebarListEl = ref<HTMLElement>()
const editingChatCommentId = ref<string | null>(null)
const editingChatCommentText = ref('')
const commentDraftImages = ref<string[]>([])
const editingChatCommentImages = ref<string[]>([])
let lastChatSelectionText = ''
// Bubble element the current selection originated in. Captured at selection
// time so applyHighlights() can re-wrap only the right bubble (otherwise a
// short selection like "OK" could wrongly highlight in any other message).
let lastChatSelectionBubble: HTMLElement | null = null
let draftBubbleEl: HTMLElement | null = null
const commentBubbleById = new Map<string, HTMLElement>()

const commentSidebarVisible = computed(
  () => store.pendingChatComments.length > 0 || commentDraft.value !== null
)

function truncate(s: string, n: number): string {
  if (!s) return ''
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

function onChatSelectionChange(): void {
  if (commentDraft.value) return
  const sel = window.getSelection()
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
    selectionAnchor.value = null
    return
  }
  const range = sel.getRangeAt(0)
  // Only react to selections inside message bubbles
  const msgs = messagesEl.value
  if (!msgs || !msgs.contains(range.startContainer) || !msgs.contains(range.endContainer)) {
    selectionAnchor.value = null
    return
  }
  // Skip if the selection is inside an input/textarea
  const startEl = range.startContainer instanceof Element ? range.startContainer : range.startContainer.parentElement
  const endEl = range.endContainer instanceof Element ? range.endContainer : range.endContainer.parentElement
  if (startEl?.closest('textarea, input') || endEl?.closest('textarea, input')) {
    selectionAnchor.value = null
    return
  }
  // Find the bubble this selection lives in. Required so applyHighlights()
  // only wraps the matching text in the originating bubble, not every bubble
  // that happens to contain the same string.
  const bubble = startEl?.closest('.message') as HTMLElement | null
  if (!bubble) { selectionAnchor.value = null; return }
  const text = sel.toString().trim()
  if (!text) { selectionAnchor.value = null; return }
  lastChatSelectionText = text
  lastChatSelectionBubble = bubble

  // Anchor at the END of the selection (last client rect), not the bounding
  // box of the whole range — multi-line selections otherwise push the pill
  // far below the visible end of the highlight.
  const rects = range.getClientRects()
  const endRect = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect()
  // Viewport-relative coordinates because the trigger/popover are teleported
  // to body with position: fixed.
  const popoverW = Math.min(420, window.innerWidth * 0.9)
  const top = endRect.bottom + 2
  const left = Math.min(
    Math.max(8, endRect.right + 6),
    Math.max(8, window.innerWidth - popoverW - 8)
  )
  selectionAnchor.value = { top, left }
}

function openCommentForSelection(): void {
  if (!selectionAnchor.value || !lastChatSelectionText) return
  draftBubbleEl = lastChatSelectionBubble
  commentDraft.value = {
    selection: lastChatSelectionText,
    text: '',
  }
  selectionAnchor.value = null
  window.getSelection()?.removeAllRanges()
  nextTick(() => chatCommentInputEl.value?.focus())
}

function cancelChatComment(): void {
  commentDraft.value = null
  commentDraftImages.value = []
  draftBubbleEl = null
  lastChatSelectionText = ''
  lastChatSelectionBubble = null
}

function saveChatComment(): void {
  const draft = commentDraft.value
  if (!draft) return
  const note = draft.text.trim()
  if (!note) return
  const id = store.addPendingChatComment({ selection: draft.selection, comment: note, images: commentDraftImages.value.length ? commentDraftImages.value : undefined })
  if (draftBubbleEl) commentBubbleById.set(id, draftBubbleEl)
  draftBubbleEl = null
  commentDraft.value = null
  commentDraftImages.value = []
  lastChatSelectionText = ''
  lastChatSelectionBubble = null
  nextTick(() => applyHighlights())
}

async function handleDraftImageUpload(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  if (!input.files?.length) return
  const chatId = store.activeChatId
  if (!chatId) return
  try {
    const refs = await store.uploadImageRefs(chatId, Array.from(input.files))
    commentDraftImages.value.push(...refs)
  } catch (err) {
    console.error('Comment image upload failed:', err)
  }
  input.value = ''
}

function removeDraftImage(index: number): void {
  commentDraftImages.value.splice(index, 1)
}

function onChatCommentKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    e.preventDefault()
    cancelChatComment()
    return
  }
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault()
    saveChatComment()
  }
}

// ── Edit / remove pending chat comments from the sidebar ─────────────
function startEditChatComment(c: { id: string; comment: string; images?: string[] }): void {
  editingChatCommentId.value = c.id
  editingChatCommentText.value = c.comment
  editingChatCommentImages.value = c.images ? [...c.images] : []
  nextTick(() => sidebarEditInputEl.value?.focus())
}
function cancelEditChatComment(): void {
  editingChatCommentId.value = null
  editingChatCommentText.value = ''
  editingChatCommentImages.value = []
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

function onEditChatCommentKeydown(e: KeyboardEvent, id: string): void {
  if (e.key === 'Escape') {
    e.preventDefault()
    cancelEditChatComment()
    return
  }
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault()
    saveEditChatComment(id)
  }
}
function deleteChatComment(id: string): void {
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

function highlightInElement(root: HTMLElement, selection: string, commentId: string): boolean {
  const text = selection.trim()
  if (!text) return false
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes: Text[] = []
  let node: Node | null
  while ((node = walker.nextNode())) nodes.push(node as Text)
  if (!nodes.length) return false

  let fullText = ''
  const offsets: { node: Text; start: number; end: number }[] = []
  for (const n of nodes) {
    const start = fullText.length
    fullText += n.textContent || ''
    offsets.push({ node: n, start, end: fullText.length })
  }

  let matchStart = -1
  let matchEnd = -1

  // Exact match first
  const exactIdx = fullText.indexOf(text)
  if (exactIdx !== -1) {
    matchStart = exactIdx
    matchEnd = exactIdx + text.length
  } else {
    // Fallback: whitespace-normalized match for selections that span
    // <br>, block boundaries, or have extra whitespace from rendering.
    const normFull = fullText.replace(/\s+/g, '')
    const normText = text.replace(/\s+/g, '')
    const normIdx = normFull.indexOf(normText)
    if (normIdx !== -1) {
      // Map normalized start index back to original text position
      let charCount = 0
      for (let i = 0; i < fullText.length; i++) {
        if (!/\s/.test(fullText[i])) {
          if (charCount === normIdx) {
            matchStart = i
            break
          }
          charCount++
        }
      }
      // Map normalized end index back to original text position
      if (matchStart !== -1) {
        charCount = 0
        for (let i = 0; i < fullText.length; i++) {
          if (!/\s/.test(fullText[i])) {
            charCount++
            if (charCount === normIdx + normText.length) {
              matchEnd = i + 1
              break
            }
          }
        }
        if (matchEnd === -1) matchEnd = fullText.length
      }
    }
  }

  if (matchStart === -1 || matchEnd === -1) {
    console.warn('Comment highlight not found:', commentId, text.slice(0, 80))
    return false
  }

  let success = false
  for (let i = offsets.length - 1; i >= 0; i--) {
    const o = offsets[i]
    if (o.end <= matchStart || o.start >= matchEnd) continue
    const localStart = Math.max(0, matchStart - o.start)
    const localEnd = Math.min(o.end - o.start, matchEnd - o.start)
    if (localStart >= localEnd) continue

    const textNode = o.node
    const slice = textNode.textContent?.slice(localStart, localEnd) || ''
    if (!slice.trim()) continue

    try {
      // Split off the tail after the match so the slice we want to wrap
      // becomes its own text node. Order matters: split the end first so
      // `localStart` still references valid offsets in the original node.
      textNode.splitText(localEnd)
      const mid = textNode.splitText(localStart)
      const span = document.createElement('span')
      span.className = 'comment-highlight'
      span.dataset.commentId = commentId
      mid.parentNode?.replaceChild(span, mid)
      span.appendChild(mid)
      success = true
    } catch {
      // Skip this node; other nodes may still wrap successfully.
    }
  }
  return success
}

function findBubbleForComment(root: HTMLElement, c: ChatComment): HTMLElement | null {
  const stored = commentBubbleById.get(c.id)
  if (stored && root.contains(stored)) {
    const content = stored.querySelector('.message-content')
    const text = content?.textContent || ''
    const normText = text.replace(/\s+/g, '')
    const normSelection = c.selection.replace(/\s+/g, '')
    if (text.includes(c.selection) || normText.includes(normSelection)) {
      return stored
    }
  }
  const bubbles = root.querySelectorAll('.message')
  for (const bubble of Array.from(bubbles)) {
    const content = bubble.querySelector('.message-content')
    if (!content) continue
    const text = content.textContent || ''
    const normText = text.replace(/\s+/g, '')
    const normSelection = c.selection.replace(/\s+/g, '')
    if (text.includes(c.selection) || normText.includes(normSelection)) {
      return bubble as HTMLElement
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
      highlightInElement(bubble, c.selection, c.id)
    } else {
      console.warn('Bubble not found for comment', c.id, c.selection.slice(0, 80))
    }
  }
}

// ── Click sync between highlights and sidebar cards ──────────────────
function handleHighlightClick(e: MouseEvent): void {
  const target = e.target as HTMLElement | null
  if (!target) return
  const highlight = target.closest('.comment-highlight') as HTMLElement | null
  if (!highlight) return
  const id = highlight.dataset.commentId
  if (!id) return
  e.stopPropagation()
  scrollSidebarToCard(id)
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

function scrollSidebarToCard(id: string): void {
  const root = sidebarListEl.value
  if (!root) return
  const card = root.querySelector(`.chat-sidebar-card[data-card-id="${id}"]`) as HTMLElement | null
  if (!card) return
  const top = offsetTopWithin(card, root) - (root.clientHeight - card.offsetHeight) / 2
  root.scrollTo({ top: Math.max(0, top), behavior: 'smooth' })
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
function jumpToCommentHighlight(id: string): void {
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
}

if (typeof document !== 'undefined') {
  document.addEventListener('selectionchange', onChatSelectionChange)
}

onMounted(async () => {
  try {
    const r = await api.get<ModelsResponse>('/api/models')
    models.value = r.models
    providerModels.value = r.provider_models || {}
    providerDefaults.value = r.provider_defaults || {}
    ollamaModels.value = r.ollama_models || []
    thinkingLevels.value = r.thinking_levels || {}
  } catch { /* use defaults */ }
  try {
    const r = await api.get<{ commands: SlashCommand[] }>('/api/commands')
    slashCommands.value = r.commands ?? []
  } catch { /* leave empty; picker just won't show */ }
  notifyChatFocused(chat.value?.chat_id)
  messagesEl.value?.addEventListener('scroll', checkScroll, { passive: true })
  nextTick(() => {
    if (messagesEl.value) {
      messagesEl.value.scrollTop = messagesEl.value.scrollHeight
      checkScroll()
    }
  })
})

onBeforeUnmount(() => {
  if (typeof document !== 'undefined') {
    document.removeEventListener('selectionchange', onChatSelectionChange)
  }
  messagesEl.value?.removeEventListener('scroll', checkScroll)
  if (spinnerTimer) { clearInterval(spinnerTimer); spinnerTimer = null }
})

// Tell the service worker which chat is in focus so it can clear the badge
function notifyChatFocused(chatId: string | undefined) {
  if (!chatId) return
  if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage({ type: 'chat-focused', chat_id: chatId })
  }
}

watch(() => chat.value?.chat_id, (id) => notifyChatFocused(id))

function renderMarkdown(text: string): string {
  return renderSafeMarkdown(text)
}

function renderActivityLine(line: string): string {
  return linkifyText(line)
}

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
  fileViewer.open(path, Number.isFinite(line as number) ? line : null)
}

function traceSummaryMeta(steps: ChatMessage[]): string {
  let toolCount = 0
  let textCount = 0
  let thinkingCount = 0
  let fileCount = 0
  for (const s of steps) {
    if (s.tool_name === '_activity') {
      toolCount += s.content.split('\n').filter(Boolean).length
    } else if (s.tool_name === '_thinking') {
      thinkingCount += 1
    } else if (s.tool_name === '_filecard') {
      fileCount += 1
    } else if (s.role === 'assistant') {
      textCount += 1
    }
  }
  const parts: string[] = []
  if (thinkingCount) parts.push(`${thinkingCount} thought${thinkingCount === 1 ? '' : 's'}`)
  if (textCount) parts.push(`${textCount} note${textCount === 1 ? '' : 's'}`)
  if (toolCount) parts.push(`${toolCount} tool call${toolCount === 1 ? '' : 's'}`)
  if (fileCount) parts.push(`${fileCount} file${fileCount === 1 ? '' : 's'}`)
  return parts.join(' · ') || 'steps'
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

function fileCardIcon(filePath: string): string {
  if (_IMAGE_EXT_RE.test(filePath)) return '\u{1F5BC}'  // 🖼
  if (/\.(md|markdown|txt)$/i.test(filePath)) return '\u{1F4DD}'  // 📝
  if (/\.(json|ya?ml|toml|ini|cfg)$/i.test(filePath)) return '\u{2699}️'  // ⚙
  if (/\.(pdf|docx?|xlsx?|pptx?)$/i.test(filePath)) return '\u{1F4C4}'  // 📄
  if (/\.(ipynb)$/i.test(filePath)) return '\u{1F4D3}'  // 📓
  return '\u{1F4C4}'  // 📄
}

const renderItems = computed<RenderItem[]>(() => {
  const items: RenderItem[] = []
  let buffer: ChatMessage[] = []

  const flushTurn = (isFinal = false) => {
    if (!buffer.length) return
    // Find index of the LAST assistant text message (the final answer).
    // _activity (tool calls) and _thinking (model reasoning) are part of
    // the trace, never the final user-facing reply.
    let finalIdx = -1
    for (let k = buffer.length - 1; k >= 0; k--) {
      const m = buffer[k]
      if (m.role === 'assistant' && m.tool_name !== '_activity' && m.tool_name !== '_thinking') {
        finalIdx = k
        break
      }
    }
    const intermediate = finalIdx >= 0 ? buffer.slice(0, finalIdx) : buffer.slice()
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
      items.push({ kind: 'trace', steps: buffer.slice() })
      buffer = []
      return
    }

    // While streaming, the final buffer in activeMessages is an incomplete turn
    // that was already persisted to the server session file. The active turn's
    // activity and text are already fully rendered in the live "Working..." block
    // below. Clear the buffer so we don't duplicate it as a collapsed reasoning trace.
    if (isFinal && store.isStreaming) {
      buffer = []
      return
    }

    if (intermediate.length) {
      items.push({ kind: 'trace', steps: intermediate })
    }
    if (finalMsg) {
      items.push({ kind: 'assistant', msg: finalMsg })
    }
    if (trailing.length) {
      items.push({ kind: 'trace', steps: trailing })
    }
    buffer = []
  }

  for (const msg of store.activeMessages) {
    if (msg.role === 'user') {
      flushTurn()
      items.push({ kind: 'user', msg })
    } else if (
      msg.role === 'system'
      && msg.tool_name !== '_activity'
      && msg.tool_name !== '_thinking'
      && msg.tool_name !== '_filecard'
    ) {
      flushTurn()
      items.push({ kind: 'system', msg })
    } else {
      // assistant text, _activity tool block, _thinking note, or _filecard:
      // part of the current turn's trace.
      buffer.push(msg)
    }
  }
  flushTurn(true)
  return items
})

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
  nextTick(() => {
    if (messagesEl.value) {
      messagesEl.value.scrollTop = messagesEl.value.scrollHeight
      checkScroll()
    }
  })
})

// Auto-scroll only when the user is already near the bottom.
// Use direct scrollTop assignment instead of scrollIntoView — the latter
// can stop short of the absolute bottom with smooth scrolling, especially
// inside flex containers where the anchor is a zero-height child.
watch(
  () => [store.activeMessages.length, store.currentStreamingText, store.currentActivity.length],
  () => {
    if (!isNearBottom.value) return
    nextTick(() => {
      if (messagesEl.value) {
        messagesEl.value.scrollTop = messagesEl.value.scrollHeight
      }
    })
  },
  { deep: true }
)

function autoResize() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
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

function handleKeydown(e: KeyboardEvent) {
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
      inputText.value = ''
      return
    }
    if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey)) {
      e.preventDefault()
      const cmd = filteredCommands.value[commandHighlightIdx.value]
      if (cmd) applyCommand(cmd)
      return
    }
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

function handleInputFocus() {
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
  store.sendMessage(chat.value.chat_id, sendText, 'queue')
  inputText.value = ''
  nextTick(autoResize)
}

// Compact label for a pending file comment — shows the file basename so a
// long workspace path doesn't blow out the chip width.
function commentBasename(path: string): string {
  const idx = path.lastIndexOf('/')
  return idx === -1 ? path : path.slice(idx + 1)
}

// Pretty line label: empty when no range, "42" for single line, "42-57"
// for ranges. Mirrors the structured `lines="L42-L57"` attribute we send
// to the model, minus the `L` prefix to keep the chip tight.
function commentLineLabel(c: { lineStart?: number | null; lineEnd?: number | null }): string {
  if (!c.lineStart) return ''
  if (!c.lineEnd || c.lineEnd === c.lineStart) return String(c.lineStart)
  return `${c.lineStart}-${c.lineEnd}`
}

// Retry support: error messages are system bubbles whose content starts
// with "Error:" (set in stores/projects.ts error-event handler). If the
// prior user turn is still in the timeline, we can resend its text.
function isErrorMsg(content: string): boolean {
  return typeof content === 'string' && content.startsWith('Error:')
}
function lastUserBefore(errorIdx: number): string | null {
  const items = renderItems.value
  for (let k = errorIdx - 1; k >= 0; k--) {
    const it = items[k]
    if (it.kind === 'user') return it.msg.content
  }
  return null
}
function retryFromError(errorIdx: number) {
  if (chat.value.archived) return
  const text = lastUserBefore(errorIdx)
  if (!text) return
  store.sendMessage(chat.value.chat_id, text, 'queue')
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

async function selectBucket(bucket: BucketKey) {
  if (!chat.value || bucket === activeBucket.value) return
  const models = providerModels.value[bucket] || []
  const defaultModel = providerDefaults.value[bucket] || models[0] || chat.value.model
  const def = BUCKET_DEFS.find(b => b.key === bucket)
  if (!def) return
  // Persist the explicit Claude bucket so the server pins routing:
  // 'work' → Anthropic subscription, 'personal' → Ollama. Non-Claude
  // providers clear it (bucket is implied by the provider).
  const modelBucket: '' | 'work' | 'personal' =
    bucket === 'claude_work' ? 'work' : bucket === 'claude_personal' ? 'personal' : ''
  if (bucketLocked.value) {
    const ok = window.confirm(
      `Hand over this chat to ${def.label} / ${defaultModel}? The same visible chat will continue with a fresh provider session.`,
    )
    if (!ok) return
    await store.handoverChat(chat.value.chat_id, {
      provider: def.provider,
      model: defaultModel,
      model_bucket: modelBucket,
    })
    showModelPicker.value = false
    return
  }
  await store.updateChat(chat.value.chat_id, {
    provider: def.provider,
    model: defaultModel,
    model_bucket: modelBucket,
  })
}

async function selectModel(model: string) {
  await store.updateChat(chat.value.chat_id, { model })
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
  if (!open) return
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
  if (!confirm('Archive this chat?')) return
  await store.archiveChat(chat.value.chat_id)
  // Notify ChatLayout so it closes the chat pane and opens the sidebar
  // on mobile (so the user can pick the next chat).
  emit('close')
}

async function continueChat() {
  if (!chat.value) return
  isContinuing.value = true
  try {
    await store.continueArchivedChat(chat.value.chat_id)
  } catch (e: any) {
    console.error('Failed to continue archived chat:', e)
    alert(`Could not continue chat: ${e?.message || e}`)
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
  } finally {
    transcribing.value = false
  }
}
async function handleFileSelect(e: Event) { const input = e.target as HTMLInputElement; if (!input.files?.length) return; await store.uploadImages(chat.value.chat_id, Array.from(input.files)); input.value = '' }
async function handleDrop(e: DragEvent) { dragOver.value = false; const files = Array.from(e.dataTransfer?.files || []).filter(f => f.type.startsWith('image/')); if (files.length) await store.uploadImages(chat.value.chat_id, files) }
async function handlePaste(e: ClipboardEvent) { const items = Array.from(e.clipboardData?.items || []).filter(i => i.type.startsWith('image/')); if (items.length) { e.preventDefault(); await store.uploadImages(chat.value.chat_id, items.map(i => i.getAsFile()).filter(Boolean) as File[]) } }
function removePendingImage(index: number) { store.removePendingImage(index) }

function insertImageRef(n: number) {
  const el = inputEl.value
  if (!el) return
  const token = `[Image ${n}]`
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

/* Scroll-to-bottom float button */
.scroll-to-bottom-btn {
  position: absolute;
  bottom: 80px;
  right: calc(16px + var(--safe-right));
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
  transform: scale(0.92);
}

/* Header */
.desktop-only { display: inline-flex; }
@media (max-width: 768px) { .desktop-only { display: none; } }

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  text-align: left;
}

.close-btn {
  background: none;
  border: none;
  color: var(--fg2);
  cursor: pointer;
  font-size: 20px;
  padding: 0 4px;
  line-height: 1;
  font-family: var(--font);
  min-width: 30px;
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.close-btn:hover { color: var(--fg); }

.header-breadcrumb {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
  position: relative;
}

.breadcrumb-project {
  font-size: 16px;
  color: var(--fg2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
  transition: color 120ms var(--ease);
  flex-shrink: 0;
}

.breadcrumb-project:hover {
  color: var(--accent);
}

.breadcrumb-separator {
  color: var(--fg3);
  font-size: 16px;
  user-select: none;
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
  font-weight: 600;
  font-size: 16px;
  cursor: pointer;
  /* Block + min-width:0 + max-width:100% are all needed together for
     text-overflow: ellipsis to actually kick in when the title is longer
     than the parent. Inline (the default for <span>) ignores overflow. */
  display: block;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  /* In a flex row with the project name, claim available space so the
     title doesn't get pushed to width:0 when the project name is long. */
  flex: 1;
}

.title-input {
  font-weight: 600;
  font-size: 14px;
  background: var(--bg);
  border: 1px solid var(--accent);
  border-radius: 4px;
  color: var(--fg);
  padding: 2px 6px;
  font-family: var(--font);
  width: 200px;
}

/* Messages */
.messages {
  flex: 1;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
  padding: 12px calc(12px + var(--safe-right)) 20px calc(12px + var(--safe-left));
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
  position: relative;
}
/* Push short chats to the bottom so the input bar doesn't float far
   below a single message. On overflow the auto margin resolves to 0
   and scrolling works normally from the top. */
.messages > :first-child {
  margin-top: auto;
}

.message {
  max-width: 85%;
  padding: 8px 12px;
  border-radius: var(--radius);
  font-size: var(--text-base);
  line-height: 1.5;
  word-break: break-word;
  min-width: 0;
}

.message.user {
  align-self: flex-end;
  background: var(--bg3);
  color: var(--fg);
}

.message.assistant {
  align-self: flex-start;
  background: var(--bg2);
  border: 1px solid var(--border);
  max-width: 90%;
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
  max-width: 90%;
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
  .retry-card { align-items: stretch; flex-direction: column; }
  .retry-card-actions { justify-content: flex-end; }
}

/* Reasoning trace (intermediate assistant text + tool calls grouped) */
.trace-block {
  align-self: flex-start;
  max-width: 90%;
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
  cursor: pointer;
  color: var(--fg2);
  user-select: none;
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: var(--text-sm);
  line-height: 1.4;
}

.trace-summary:hover { color: var(--fg); }

.trace-chevron { font-size: calc(10px * var(--font-scale)); color: var(--fg2); }
.trace-icon { font-size: calc(14px * var(--font-scale)); }
.trace-label { color: var(--fg2); }
.trace-meta { color: var(--fg2); opacity: 0.7; font-weight: 400; margin-left: auto; font-size: var(--text-xs); }

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

.trace-tools {
  background: var(--bg);
  border-radius: 4px;
  padding: 4px 8px;
}

/* Inline file card. Rendered inside the reasoning trace whenever the agent
   calls Write/Edit/MultiEdit/NotebookEdit. Tapping opens the FileViewerModal
   for that path (security-checked server-side by /api/workspace-file). */
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
  max-width: 90%;
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
  width: 10px;
  height: 10px;
  border: 2px solid var(--accent2);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }
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
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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

/* Message content (markdown) */
.message-content {
  min-width: 0;
  overflow-wrap: break-word;
}
.message-content :deep(pre) {
  background: var(--bg);
  padding: 8px;
  border-radius: 4px;
  overflow-x: auto;
  margin: 4px 0;
  white-space: pre-wrap;
  max-width: 100%;
}

.message-content :deep(code) {
  font-family: var(--font);
  font-size: var(--text-base);
}

.message-content :deep(p) { margin: 4px 0; }
.message-content :deep(ul),
.message-content :deep(ol) {
  padding-left: 22px;
  margin: 4px 0;
  list-style-position: outside;
}
/* Collapse the leading/trailing margin of the first/last markdown block so
   the bubble padding isn't compounded by a paragraph margin. */
.message-content :deep(:first-child) { margin-top: 0; }
.message-content :deep(:last-child) { margin-bottom: 0; }
.message-content :deep(li) { padding-left: 2px; }
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
.message-content :deep(table) {
  border-collapse: collapse;
  margin: 6px 0;
  font-size: 13px;
  border: 1px solid var(--fg2);
  max-width: 100%;
  display: block;
  overflow-x: auto;
}
.message-content :deep(th),
.message-content :deep(td) {
  border: 1px solid var(--fg2);
  padding: 5px 9px;
}
.message-content :deep(th) {
  background: var(--bg3);
  font-weight: 600;
  text-align: left;
}

.message-meta {
  font-size: 10px;
  color: var(--fg2);
  margin-top: 4px;
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

/* Pending attachments row: images and file comments live above the input.
   Chat comments live in the right sidebar. */
.pending-attachments {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--bg2);
  border-top: 1px solid var(--border);
  flex-shrink: 0;
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
  max-width: min(360px, 100%);
  padding: 6px 8px 6px 8px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.35;
  color: var(--fg);
}
.comment-chip-icon { line-height: 1; padding-top: 1px; }
.comment-chip-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
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


/* Streaming controls bar (above input) */
.streaming-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  padding-left: calc(12px + var(--safe-left));
  padding-right: calc(12px + var(--safe-right));
  border-top: 1px solid var(--border);
  background: var(--bg);
  flex-shrink: 0;
}

.streaming-spinner {
  color: var(--accent);
  font-size: calc(14px * var(--font-scale));
  line-height: 1;
  width: 1ch;
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

.streaming-label {
  font-size: var(--text-sm);
  color: var(--fg2);
  margin-right: auto;
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
  align-items: center;
  gap: 10px;
  padding: 6px 12px;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
}
.commands-picker-row.active {
  background: var(--hover, rgba(128, 128, 128, 0.12));
}
.commands-picker-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-weight: 600;
  flex-shrink: 0;
}
.commands-picker-hint {
  color: var(--muted, #888);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.85em;
  flex-shrink: 0;
}
.commands-picker-desc {
  color: var(--muted, #888);
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.commands-picker-source {
  font-size: 0.7em;
  color: var(--muted, #888);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  flex-shrink: 0;
  opacity: 0.6;
}

/* Input bar */
.input-bar {
  display: flex;
  align-items: flex-end;
  gap: 8px;
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
}

.input-bar.has-streaming-bar {
  border-top: none;
  padding-top: 4px;
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
  min-height: 44px;
  max-height: 200px;
  padding: 8px 12px;
  line-height: 1.4;
}

.archived-notice {
  flex: 1;
  color: var(--fg2);
  font-size: var(--text-base);
  text-align: center;
  padding: 10px 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
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

.send-btn, .stop-btn {
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
}
.send-btn { background: var(--accent); color: white; }
.send-btn:hover { background: var(--accent-strong); }
.send-btn:active { transform: scale(0.96); }
.send-btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }
.send-glyph {
  font-size: 20px;
  font-weight: 700;
  line-height: 1;
  display: inline-block;
  transform: translateY(-1px);
}
.stop-btn {
  background: var(--error);
  color: white;
  padding: 0;
  width: var(--touch);
  height: var(--touch);
}
.stop-icon {
  font-size: 14px;
  line-height: 1;
  display: inline-block;
}
.stop-text {
  display: none;
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
   user answers. Warmer accent color than the queued chips, because this
   is a blocking action (the turn is waiting on the user). */
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
  border: 1px solid var(--accent);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  font-size: 13px;
  animation: permission-pulse 1.4s ease-out;
}

@keyframes permission-pulse {
  0% { box-shadow: 0 0 0 0 rgba(233, 69, 96, 0.4); }
  100% { box-shadow: 0 0 0 8px rgba(233, 69, 96, 0); }
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
  font-size: 14px;
  flex-shrink: 0;
}

.permission-tool {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
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
  max-height: 120px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.permission-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.btn-approve, .btn-deny {
  min-height: 32px;
  padding: 0 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  font-family: var(--font);
  font-size: 13px;
  font-weight: 600;
  transition: background 120ms var(--ease), transform 120ms var(--ease);
}
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
}

.model-picker-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 30px;
  min-height: 30px;
  border-radius: 6px;
  color: var(--fg2);
  font-size: 18px;
  background: transparent;
  border: none;
  cursor: pointer;
}
.model-picker-btn:hover { background: var(--bg3); }
.model-picker-btn:active { transform: scale(0.96); }

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
  color: var(--fg-muted, var(--fg));
  opacity: 0.7;
}

.picker-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 220px;
  overflow-y: auto;
}

.picker-item {
  text-align: left;
  font-size: 13px;
  padding: 7px 10px;
  border-radius: var(--radius);
  border: none;
  background: transparent;
  color: var(--fg);
  cursor: pointer;
  font-family: var(--font-mono);
  transition: background 120ms var(--ease);
}
.picker-item:hover { background: var(--bg3); }
.picker-item.active {
  background: var(--accent);
  color: white;
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
  .header-breadcrumb {
    flex-wrap: wrap;
    column-gap: 4px;
    row-gap: 1px;
    line-height: 1.15;
  }
  .breadcrumb-project {
    flex: 1 1 100%;
    font-size: 11px;
    line-height: 1.15;
    max-width: 100%;
  }
  .breadcrumb-separator { display: none; }
  :deep(.header-actions) {
    flex-shrink: 0;
    gap: 6px;
  }
  .model-picker-btn {
    min-width: var(--touch);
    min-height: var(--touch);
    border-radius: var(--radius);
  }
  .model-picker-dropdown {
    right: 0;
    min-width: auto;
    width: min(320px, calc(100vw - 24px));
    max-width: none;
  }
  .message { max-width: 92%; padding: 10px 14px; }
  .message.assistant { max-width: 92%; }
  .chat-title {
    flex: 1 1 100%;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    font-size: 12px;
    line-height: 1.2;
    white-space: normal;
  }
  /* Keep input and placeholder at the same size so the text doesn't jump
     when the user starts typing. 16px is the iOS auto-zoom floor: any
     smaller and Safari zooms the page on focus, which is worse than a
     slightly truncated placeholder. */
  .chat-input { font-size: 16px; padding-top: 6px; padding-bottom: 6px; }
  .chat-input::placeholder { font-size: 16px; }
  /* Tighten vertical rhythm in the input bar on mobile. Reduce buttons
     from 44px (--touch) to 36px so the whole bar is ~46px not ~54px.
     36px is still a usable touch target; 44px was designed for standalone
     tap targets, not inline grouped actions. */
  .input-bar { padding-top: 5px; padding-bottom: 5px; }
  .chat-input { min-height: 36px; }
  .stop-btn, .send-btn {
    min-width: 36px;
    min-height: 36px;
    width: 36px;
    height: 36px;
    padding: 0;
  }
  .image-btn { min-height: 36px; min-width: 36px; }
  :deep(.voice-btn) { min-height: 36px; min-width: 36px; }
  /* Compact streaming bar on mobile */
  .streaming-bar { padding-top: 4px; padding-bottom: 4px; }
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
  background: var(--danger, #e06c75);
  border: none;
  border-radius: 999px;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
  user-select: none;
}
.chat-comment-trigger:hover { filter: brightness(1.08); }
.chat-comment-trigger-icon { font-size: var(--text-sm); line-height: 1; }

.btn-sm {
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--fg);
  cursor: pointer;
}
.btn-sm:hover { background: var(--bg2, rgba(255, 255, 255, 0.04)); }
.btn-sm.primary {
  background: var(--accent, #60a5fa);
  border-color: var(--accent, #60a5fa);
  color: var(--bg);
}
.btn-sm.primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Comment sidebar: messages on the left, comments on the right. Sibling of
 * .messages inside .chat-with-sidebar so the input bar (which sits below
 * this wrapper) keeps its full chat-panel width. */
.chat-with-sidebar {
  flex: 1;
  display: flex;
  min-height: 0;
  overflow: hidden;
}
.chat-with-sidebar > .messages {
  min-width: 0;
}
.chat-comment-sidebar {
  width: 280px;
  flex-shrink: 0;
  border-left: 1px solid var(--border);
  background: var(--bg2, rgba(255, 255, 255, 0.04));
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.chat-sidebar-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.chat-sidebar-title {
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--fg);
  flex: 1;
}
.chat-sidebar-count {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--accent, #60a5fa);
  background: var(--bg);
  padding: 1px 6px;
  border-radius: 999px;
}
.chat-sidebar-draft {
  padding: 10px 12px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}
.chat-sidebar-draft-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.chat-sidebar-draft-label {
  font-weight: 600;
  font-size: var(--text-xs);
  color: var(--accent, #60a5fa);
  text-transform: uppercase;
  letter-spacing: 0.02em;
  flex: 1;
}
.chat-sidebar-draft-input {
  width: 100%;
  resize: vertical;
  min-height: 60px;
  font-family: inherit;
  font-size: var(--text-base);
  line-height: 1.45;
  color: var(--fg);
  background: var(--bg2, rgba(255, 255, 255, 0.04));
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  outline: none;
  box-sizing: border-box;
  margin-bottom: 8px;
}
.chat-sidebar-draft-input:focus { border-color: var(--accent, #60a5fa); }
.chat-sidebar-draft-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  align-items: center;
}
.chat-sidebar-draft-images {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.chat-sidebar-card-images {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.chat-sidebar-edit-images {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.draft-image-preview {
  position: relative;
  display: inline-flex;
}
.draft-image-thumb {
  height: 40px;
  width: 40px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg);
}
.card-image-thumb {
  height: 36px;
  width: 36px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg);
}
.draft-image-remove {
  position: absolute;
  top: -4px;
  right: -4px;
  width: 16px;
  height: 16px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: var(--bg3);
  color: var(--fg);
  font-size: 12px;
  line-height: 14px;
  cursor: pointer;
  box-shadow: 0 1px 3px rgba(0,0,0,0.3);
}
.image-btn-sm {
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--fg2);
  transition: background 120ms var(--ease), color 120ms var(--ease), border-color 120ms var(--ease);
}
.image-btn-sm:hover { background: var(--bg3); color: var(--fg); border-color: var(--fg2); }
.chat-sidebar-list {
  flex: 1;
  overflow-y: auto;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.chat-sidebar-card {
  padding: 8px 10px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent2, #a78bfa);
  border-radius: 6px;
  font-size: var(--text-xs);
  line-height: 1.4;
  color: var(--fg);
  cursor: pointer;
  transition: transform 0.1s, box-shadow 0.1s;
}
.chat-sidebar-card:hover {
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}
.chat-sidebar-card.is-editing { cursor: default; }
.chat-sidebar-card-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.chat-sidebar-card-file {
  font-weight: 600;
  font-size: var(--text-xs);
  color: var(--fg2);
  text-transform: uppercase;
  letter-spacing: 0.02em;
  flex: 1;
  min-width: 0;
}
.chat-sidebar-card-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  opacity: 0;
  transition: opacity 0.15s;
}
.chat-sidebar-card:hover .chat-sidebar-card-actions,
.chat-sidebar-card.is-editing .chat-sidebar-card-actions { opacity: 1; }
.chat-sidebar-card-edit,
.chat-sidebar-card-remove {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 16px;
  cursor: pointer;
}
.chat-sidebar-card-remove { font-size: calc(14px * var(--font-scale)); }
.chat-sidebar-card-edit:hover,
.chat-sidebar-card-remove:hover { background: var(--bg2); color: var(--fg); }
.chat-sidebar-card-quote {
  color: var(--fg2);
  font-style: italic;
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.chat-sidebar-card-note {
  color: var(--fg);
  word-break: break-word;
}
.chat-sidebar-edit-body { margin-top: 4px; }
.chat-sidebar-edit-input {
  width: 100%;
  resize: vertical;
  min-height: 44px;
  font-family: inherit;
  font-size: var(--text-base);
  line-height: 1.45;
  color: var(--fg);
  background: var(--bg2, rgba(255, 255, 255, 0.04));
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  outline: none;
  box-sizing: border-box;
  margin-bottom: 6px;
}
.chat-sidebar-edit-input:focus { border-color: var(--accent, #60a5fa); }
.chat-sidebar-edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
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
/* Brief flash when navigated to from a pending-comment chip. */
:deep(.comment-highlight--pulse) {
  animation: comment-pulse 1.1s var(--ease) 1;
}
@keyframes comment-pulse {
  0%   { background: rgba(234, 179, 8, 0.25); box-shadow: 0 0 0 0 rgba(234, 179, 8, 0); }
  25%  { background: rgba(234, 179, 8, 0.7);  box-shadow: 0 0 0 6px rgba(234, 179, 8, 0.18); }
  100% { background: rgba(234, 179, 8, 0.25); box-shadow: 0 0 0 0 rgba(234, 179, 8, 0); }
}

/* On narrow viewports the sidebar would crush the messages. Collapse it to
 * a bottom drawer so both stay usable. Mirrors FileViewerModal's mobile
 * handling at the same breakpoint. */
@media (max-width: 640px) {
  .chat-with-sidebar { flex-direction: column; }
  .chat-comment-sidebar {
    width: auto;
    border-left: none;
    border-top: 1px solid var(--border);
    max-height: 45vh;
  }
}
</style>
