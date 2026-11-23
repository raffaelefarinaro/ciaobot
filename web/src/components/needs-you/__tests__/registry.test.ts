// @vitest-environment jsdom

/**
 * `components/needs-you/registry.ts` — the per-kind body and icon maps must
 * agree with the copy `actionCopy` returns, so a kind added to one table and
 * not the others fails here, naming the kind.
 */
import { describe, expect, it } from 'vitest'
import { CHAT_ACTION_KINDS, actionCopy } from '../../../lib/chatActions'
import type { ChatAction, ChatActionKind } from '../../../lib/types'
import { NEEDS_YOU_BODIES, NEEDS_YOU_ICONS } from '../registry'

/** Deliberate: no card at all. `limit` is reserved for the engine's usage-limit card. */
const NO_CARD_KINDS: readonly ChatActionKind[] = ['limit']
/** Deliberate: has a body but shows the default 'activity' icon. */
const DEFAULT_ICON_KINDS: readonly ChatActionKind[] = ['command']

function sample(kind: ChatActionKind): ChatAction {
  return { id: 'a1', kind, origin: 'engine', state: 'open', params: {}, error: '', created_at: 1 }
}

describe('needs-you registry', () => {
  it('gives every kind with a body non-null copy, and every kind with copy a body', () => {
    for (const kind of CHAT_ACTION_KINDS) {
      expect({
        kind,
        hasBody: kind in NEEDS_YOU_BODIES,
        hasCopy: actionCopy(sample(kind)) !== null,
      }).toEqual({
        kind,
        hasBody: !NO_CARD_KINDS.includes(kind),
        hasCopy: !NO_CARD_KINDS.includes(kind),
      })
    }
  })

  it('gives every kind with a body an icon, except the default-icon kinds', () => {
    for (const kind of CHAT_ACTION_KINDS) {
      expect({ kind, hasIcon: kind in NEEDS_YOU_ICONS }).toEqual({
        kind,
        hasIcon: kind in NEEDS_YOU_BODIES && !DEFAULT_ICON_KINDS.includes(kind),
      })
    }
  })
})
