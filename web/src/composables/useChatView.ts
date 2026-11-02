import { computed, reactive } from 'vue'
import { useProjectStore } from '../stores/projects'

/** One chat's view of the project store: the per-chat twin of the store's active-chat getters,
 *  under the same names, so a pane can show a chat that is not the active one. */
export function useChatView(chatId: () => string) {
  const store = useProjectStore()
  const timeline = computed(() => store.timelineFor(chatId()))
  return reactive({
    chat: computed(() => store.chatById(chatId())),
    project: computed(() => store.projectFor(chatId())),
    activeMessages: computed(() => store.messagesFor(chatId())),
    messageHistoryLoading: computed(() => store.isMessageHistoryLoading(chatId())),
    activeSubagents: computed(() => store.subagentsFor(chatId())),
    isStreaming: computed(() => store.isChatStreaming(chatId())),
    isWakeStreaming: computed(() => store.isChatWakeStreaming(chatId())),
    currentStreamingText: computed(() => store.streamingTextFor(chatId())),
    currentStreamingThinking: computed(() => store.streamingThinkingFor(chatId())),
    currentQueued: computed(() => store.queuedFor(chatId())),
    activeBackgroundAgents: computed(() => store.backgroundAgentsFor(chatId())),
    activeBackgroundableCalls: computed(() => store.backgroundableCallsFor(chatId())),
    currentTimeline: timeline,
    currentDeniedToolUseIds: computed(() => store.deniedToolUseIdsFor(chatId())),
    currentStreamStartedAt: computed(() => store.streamStartedAtFor(chatId())),
    currentActivity: computed(() =>
      timeline.value.flatMap(e => (e.kind === 'tool' ? e.content.split('\n') : [])),
    ),
    pendingImages: computed(() => store.pendingImagesFor(chatId())),
    pendingComments: computed(() => store.pendingCommentsFor(chatId())),
    pendingChatComments: computed(() => store.pendingChatCommentsFor(chatId())),
  })
}
