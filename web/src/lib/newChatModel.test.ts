import { describe, expect, it } from 'vitest'
import { chosenRuntimeFor, FOLLOW_DEFAULT_KEY, FOLLOW_DEFAULT_SECTION, newChatModelView } from './newChatModel'
import type { ModelsResponse, WorkspaceInfo } from './types'

const RESPONSE = {
  models: [], default: 'default',
  providers: [{ id: 'claude', label: 'Anthropic (via Claude Code)', short_label: 'Claude Code', capabilities: {} }],
  provider_models: { claude: ['default', 'opus', 'sonnet'] },
  provider_defaults: { claude: 'default' },
  provider_catalog_evidence: { claude: { status: 'observed', source: null, reason: null, models: [
    { id: 'default', label: 'Default (recommended)', description: 'Opus 4.5' },
    { id: 'opus', label: 'Opus 4.5' },
    { id: 'sonnet', label: 'Sonnet 4.5' },
  ] } },
} as unknown as ModelsResponse
const WORKSPACE = { name: 'personal', vault_root: '', default_provider: 'claude' } as WorkspaceInfo

describe('newChatModelView', () => {
  it('names and highlights the default as its concrete row, with no default row', () => {
    const view = newChatModelView(RESPONSE, WORKSPACE, null)
    expect(view.providerLabel).toBe('Claude Code')
    expect(view.modelLabel).toBe('Opus 4.5')
    expect(view.activeModel).toBe('opus')
    expect(view.sections.map(section => section.models)).toEqual([['opus', 'sonnet']])
  })

  it("keeps following the default when its concrete row is picked, and pins anything else", () => {
    expect(chosenRuntimeFor('opus', 'anthropic', null, RESPONSE, WORKSPACE)).toBeNull()
    expect(chosenRuntimeFor('sonnet', 'anthropic', null, RESPONSE, WORKSPACE)).toEqual({ model: 'sonnet', provider: 'claude' })
    // Once pinned, the same row is an explicit pick.
    const pinned = { model: 'sonnet', provider: 'claude' }
    expect(chosenRuntimeFor('opus', 'anthropic', pinned, RESPONSE, WORKSPACE)).toEqual({ model: 'opus', provider: 'claude' })
    expect(chosenRuntimeFor(FOLLOW_DEFAULT_KEY, FOLLOW_DEFAULT_SECTION, pinned, RESPONSE, WORKSPACE)).toBeNull()
    expect(newChatModelView(RESPONSE, WORKSPACE, pinned).sections.at(-1)?.modelLabels?.[FOLLOW_DEFAULT_KEY])
      .toBe('Use workspace default (Claude Code · Opus 4.5)')
  })
})
