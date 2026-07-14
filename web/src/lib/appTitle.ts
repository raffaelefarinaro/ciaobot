export const APP_NAME = 'ciaobot'

export function formatDocumentTitle(pageTitle?: string | null, unread = 0): string {
  const prefix = unread > 0 ? `(${unread}) ` : ''
  const page = pageTitle?.trim().toLowerCase()
  if (!page || page === APP_NAME) return `${prefix}${APP_NAME}`
  return `${prefix}${page} - ${APP_NAME}`
}

export function settingsTabTitle(tab: string | undefined): string {
  switch (tab) {
    case 'providers':
      return 'providers'
    case 'models':
      return 'models'
    case 'context':
      return 'agent context'
    case 'workspaces':
      return 'workspaces'
    case 'skills':
      return 'agent assets'
    case 'home':
    default:
      return 'settings'
  }
}
