export const loadChatPanel = () => import('../components/ChatPanel.vue')
export const loadPinnedFilePanel = () => import('../components/PinnedFilePanel.vue')
export const loadFileViewerModal = () => import('../components/FileViewerModal.vue')

/** Start the chat chunk beside bootstrap requests for a concrete chat URL. */
export function prefetchChatPanelForRoute(
  path: string,
  chatId: string | undefined,
  loader: () => Promise<unknown> = loadChatPanel,
): void {
  if (!path.startsWith('/chat/') || !chatId) return
  void loader().catch(() => {})
}
