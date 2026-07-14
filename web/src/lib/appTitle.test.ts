import { describe, expect, it } from 'vitest'
import { APP_NAME, formatDocumentTitle, settingsTabTitle } from './appTitle'

describe('formatDocumentTitle', () => {
  it('returns the app name when no page title is given', () => {
    expect(formatDocumentTitle()).toBe(APP_NAME)
    expect(formatDocumentTitle('')).toBe(APP_NAME)
    expect(formatDocumentTitle('ciaobot')).toBe(APP_NAME)
  })

  it('formats page titles in lowercase with the app suffix', () => {
    expect(formatDocumentTitle('Automations')).toBe('automations - ciaobot')
    expect(formatDocumentTitle('Settings')).toBe('settings - ciaobot')
  })

  it('prefixes unread counts', () => {
    expect(formatDocumentTitle('Automations', 2)).toBe('(2) automations - ciaobot')
    expect(formatDocumentTitle(undefined, 1)).toBe('(1) ciaobot')
  })
})

describe('settingsTabTitle', () => {
  it('maps settings tabs to lowercase titles', () => {
    expect(settingsTabTitle(undefined)).toBe('settings')
    expect(settingsTabTitle('providers')).toBe('providers')
    expect(settingsTabTitle('skills')).toBe('agent assets')
  })
})
