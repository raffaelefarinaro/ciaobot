import { ref } from 'vue'

/**
 * Promise-backed trigger for the Cmd+T "new chat" picker.
 *
 * Mirrors askConfirm/askPrompt: the PWA cannot rely on a native dialog inside
 * the macOS webview, and an in-page overlay needs a way to hand the result back
 * to whoever opened it. The resolved value is the chosen project id, or null
 * when the user cancelled.
 */
export interface NewChatPickerState {
  resolve: (projectId: string | null) => void
}

export const pendingNewChat = ref<NewChatPickerState | null>(null)

export function openNewChatPicker(): Promise<string | null> {
  // A second request would orphan the first one's promise, leaving its caller
  // awaiting forever. Resolve the outstanding one as cancelled first.
  pendingNewChat.value?.resolve(null)
  return new Promise<string | null>(resolve => {
    let settled = false
    pendingNewChat.value = {
      resolve: (projectId: string | null) => {
        if (settled) return
        settled = true
        pendingNewChat.value = null
        resolve(projectId)
      },
    }
  })
}
