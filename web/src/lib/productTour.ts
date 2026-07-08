export const TOUR_STORAGE_KEY = 'ciao-product-tour-completed'

export type TourPlacement = 'top' | 'bottom' | 'left' | 'right' | 'center'

export type TourBeforeEnter = 'openSidebar' | 'welcomeChat' | 'chatRoute'

export interface ProductTourStep {
  id: string
  title: string
  body: string
  /** Shown when the UI for this step is not available yet (no chat open, empty list, etc.). */
  missingHint?: string
  /** Matches a `data-tour` attribute on a visible element. Omit for centered cards. */
  target?: string
  placement?: TourPlacement
  beforeEnter?: TourBeforeEnter[]
  /** Illustrative screenshot shown in the card for features that may not be visible live. */
  image?: string
  imageAlt?: string
}

export type TourAvailabilityContext = {
  hasActiveChat: boolean
  projectCount: number
}

export function shouldShowMissingHint(
  step: ProductTourStep,
  ctx: TourAvailabilityContext,
  targetFound: boolean,
): boolean {
  if (!step.missingHint) return false
  if (step.target) {
    if (!targetFound) return true
    if (step.id === 'projects' && ctx.projectCount === 0) return true
    return false
  }
  if (step.beforeEnter?.includes('welcomeChat') && !ctx.hasActiveChat) return true
  return false
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
    missingHint:
      'You will see workspace tabs in the sidebar once the main view is open. A default personal/work split is created on first launch.',
  },
  {
    id: 'projects',
    title: 'Projects and chats',
    body:
      'Projects group related chats and inject durable context (notes, files, decisions) into every conversation inside them.',
    target: 'sidebar-projects',
    placement: 'right',
    beforeEnter: ['openSidebar', 'chatRoute'],
    missingHint:
      'You will see projects listed here after setup — a General project is created automatically for each workspace.',
  },
  {
    id: 'schedules',
    title: 'Schedules',
    body:
      'Set recurring or one-off routines that dispatch fresh prompts into a project or chat — daily reviews, weekly digests, and more.',
    target: 'nav-schedules',
    placement: 'bottom',
    beforeEnter: ['openSidebar', 'chatRoute'],
    missingHint: 'You will find the Schedules shortcut in the sidebar header once the sidebar is open.',
  },
  {
    id: 'model',
    title: 'Model',
    body:
      'Pick the model for this chat from the header. Each workspace has a default, and you can switch it per chat whenever you need more or less power.',
    target: 'model-picker',
    placement: 'bottom',
    beforeEnter: ['welcomeChat', 'chatRoute'],
    missingHint: 'Open a chat from the sidebar — the model picker appears in the chat header.',
  },
  {
    id: 'chat-comments',
    title: 'Comment, copy, or listen',
    body:
      'Select any passage in a message, then tap the comment button — your note stays in the sidebar and rides along with your next message. Hover any message to copy it, and hover a reply to have it read aloud.',
    target: 'chat-messages',
    placement: 'top',
    beforeEnter: ['welcomeChat', 'chatRoute'],
    missingHint: 'Open a chat to see messages here, then select text to comment or hover a message to copy or listen.',
    image: '/tour/chat-comment.png',
    imageAlt: 'Selecting text in a message reveals a Comment action',
  },
  {
    id: 'chat-input',
    title: 'Type, dictate, or attach',
    body:
      'Type here, or tap the mic to dictate — your transcript is sent like typed text. Attach images with the paperclip, and queue a follow-up while the agent is still working. Pending chat comments ride along on the next send.',
    target: 'chat-input',
    placement: 'top',
    beforeEnter: ['welcomeChat', 'chatRoute'],
    missingHint: 'Open a chat from the sidebar — the message box, mic, and paperclip sit at the bottom of the chat view.',
  },
  {
    id: 'file-cards',
    title: 'Files in the chat',
    body:
      'When the agent reads or edits a file, an inline card appears in the thread. Click it to open a preview with history, diff, and restore.',
    placement: 'center',
    beforeEnter: ['welcomeChat', 'chatRoute'],
    missingHint:
      'You will see inline file cards in the chat thread once the agent reads or edits a file.',
  },
  {
    id: 'pin-preview',
    title: 'Pin and annotate documents',
    body:
      'From the file viewer, pin a document to keep it open beside the chat. Select text in the preview to add line-level comments — they are attached to your next message, same as chat comments.',
    placement: 'center',
    beforeEnter: ['welcomeChat', 'chatRoute'],
    missingHint:
      'Open a file from a chat card, then use Pin in the viewer to keep it open beside the conversation.',
    image: '/tour/pin-annotate.png',
    imageAlt: 'A document pinned in a split view beside the chat',
  },
  {
    id: 'rich-preview',
    title: 'Images, PDFs, and slides',
    body:
      'See images inline in chat, and open images, PDFs, and PowerPoint (.pptx) files directly in the viewer without leaving Ciaobot — you can also pin them on the side.',
    placement: 'center',
    beforeEnter: ['welcomeChat', 'chatRoute'],
    missingHint:
      'You will see image and PDF previews once files show up in a chat or the file viewer.',
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
