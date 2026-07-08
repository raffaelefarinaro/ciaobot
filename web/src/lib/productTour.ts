export const TOUR_STORAGE_KEY = 'ciao-product-tour-completed'

export type TourPlacement = 'top' | 'bottom' | 'left' | 'right' | 'center'

export type TourBeforeEnter = 'openSidebar' | 'welcomeChat' | 'chatRoute'

export interface ProductTourStep {
  id: string
  title: string
  body: string
  /** Matches a `data-tour` attribute on a visible element. Omit for centered cards. */
  target?: string
  placement?: TourPlacement
  beforeEnter?: TourBeforeEnter[]
}

export const PRODUCT_TOUR_STEPS: ProductTourStep[] = [
  {
    id: 'welcome',
    title: 'Welcome to Ciaobot',
    body:
      'This quick tour shows how to navigate workspaces, annotate chats, preview files inline, and pin documents beside your conversation. You can replay it anytime from Settings → Home.',
    placement: 'center',
  },
  {
    id: 'workspaces',
    title: 'Workspaces',
    body:
      'Switch life areas here — personal, work, a client. Each workspace has its own vault, default model, and projects.',
    target: 'sidebar-workspaces',
    placement: 'right',
    beforeEnter: ['openSidebar', 'chatRoute'],
  },
  {
    id: 'projects',
    title: 'Projects and chats',
    body:
      'Projects group related chats and inject durable context (notes, files, decisions) into every conversation inside them.',
    target: 'sidebar-projects',
    placement: 'right',
    beforeEnter: ['openSidebar', 'chatRoute'],
  },
  {
    id: 'schedules',
    title: 'Schedules',
    body:
      'Set recurring or one-off routines that dispatch fresh prompts into a project or chat — daily reviews, weekly digests, and more.',
    target: 'nav-schedules',
    placement: 'bottom',
    beforeEnter: ['openSidebar', 'chatRoute'],
  },
  {
    id: 'model',
    title: 'Model and voice',
    body:
      'Pick the model for this chat. Use the mic to dictate — your transcript is sent like typed text. Attach images with the paperclip.',
    target: 'model-picker',
    placement: 'bottom',
    beforeEnter: ['welcomeChat', 'chatRoute'],
  },
  {
    id: 'chat-comments',
    title: 'Comment on chat text',
    body:
      'Select any passage in a message, then tap the comment button. Your note stays in the sidebar and is sent with your next message so the agent knows exactly what you mean.',
    target: 'chat-messages',
    placement: 'top',
    beforeEnter: ['welcomeChat', 'chatRoute'],
  },
  {
    id: 'chat-input',
    title: 'Send with context',
    body:
      'Type here, paste images, or queue a follow-up while the agent is still working. Pending chat comments ride along on the next send.',
    target: 'chat-input',
    placement: 'top',
    beforeEnter: ['welcomeChat', 'chatRoute'],
  },
  {
    id: 'file-cards',
    title: 'Files in the chat',
    body:
      'When the agent reads or edits a file, an inline card appears in the thread. Click it to open a preview with history, diff, and restore.',
    placement: 'center',
    beforeEnter: ['welcomeChat', 'chatRoute'],
  },
  {
    id: 'pin-preview',
    title: 'Pin and annotate documents',
    body:
      'From the file viewer, pin a document to keep it open beside the chat. Select text in the preview to add line-level comments — they are attached to your next message, same as chat comments.',
    placement: 'center',
    beforeEnter: ['welcomeChat', 'chatRoute'],
  },
  {
    id: 'rich-preview',
    title: 'Images, PDFs, and slides',
    body:
      'Images render inline in chat and in the viewer. PDFs open in a built-in preview. PowerPoint (.pptx) files are converted to PDF for viewing (requires LibreOffice on the server).',
    placement: 'center',
    beforeEnter: ['welcomeChat', 'chatRoute'],
  },
  {
    id: 'done',
    title: 'You are set',
    body:
      'Ask "what can Ciaobot do?" in any chat for a deeper walkthrough of memory, skills, and integrations. Replay this UI tour from Settings → Home whenever you like.',
    placement: 'center',
  },
]

export function isTourCompleted(): boolean {
  try {
    return localStorage.getItem(TOUR_STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

export function markTourCompleted(): void {
  try {
    localStorage.setItem(TOUR_STORAGE_KEY, '1')
  } catch {
    // ignore quota / private browsing
  }
}

export function clearTourCompleted(): void {
  try {
    localStorage.removeItem(TOUR_STORAGE_KEY)
  } catch {
    // ignore
  }
}

export function tourTargetSelector(target: string): string {
  return `[data-tour="${target}"]`
}
