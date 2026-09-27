// @vitest-environment jsdom

/**
 * The category list behind `/memory/categories`.
 *
 * Two things here are load-bearing and easy to get wrong silently: the PATCH
 * body must not carry the server's own `note_count` (it is the vault's count,
 * not part of the document being described), and a refused save must leave the
 * rows the user was editing exactly as they were, with the server's sentence in
 * `error` so the form can keep the edit open and show it.
 */
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
import { api } from '../../lib/api'
import {
  fromResponse,
  isValidEntityTypeId,
  toPatchBody,
  useEntityTypesStore,
  type EntityTypeRow,
  type EntityTypesResponse,
} from '../entityTypes'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.mocked(api.get).mockReset()
  vi.mocked(api.patch).mockReset()
})

function row(overrides: Partial<EntityTypeRow> = {}): EntityTypeRow {
  return {
    id: 'person',
    label: 'Person',
    kind: 'entity',
    folder: 'People',
    description: 'A human the user knows or works with.',
    aliases: ['human'],
    stale_after_days: 90,
    enabled: true,
    builtin: true,
    note_count: 4,
    ...overrides,
  }
}

/** The body both methods answer. Typed, so a fixture row that is not a
 * `EntityTypeRow` fails here rather than at the assertion that reads it. */
function body(types: EntityTypeRow[]): EntityTypesResponse {
  return { workspace: 'personal', vault: '/tmp/vault', types }
}

describe('toPatchBody', () => {
  test('drops the note count and keeps the schema fields', () => {
    const payload = toPatchBody([row()])
    expect(payload.types).toHaveLength(1)
    expect(payload.types[0]).toEqual({
      id: 'person',
      label: 'Person',
      kind: 'entity',
      folder: 'People',
      description: 'A human the user knows or works with.',
      aliases: ['human'],
      stale_after_days: 90,
      enabled: true,
      builtin: true,
    })
    // The count is the server's read of the vault, not something a form knows.
    expect('note_count' in payload.types[0]!).toBe(false)
  })

  test('sends the whole list, in order, alias list copied per row', () => {
    const rows = [row({ id: 'person' }), row({ id: 'customer', label: 'Customer', builtin: false })]
    const payload = toPatchBody(rows)
    expect(payload.types.map((entry) => entry.id)).toEqual(['person', 'customer'])
    payload.types[0]!.aliases.push('mutated')
    expect(rows[0]!.aliases).toEqual(['human'])
  })
})

describe('fromResponse', () => {
  test('reads every field off the wire shape', () => {
    const parsed = fromResponse(body([row()]))
    expect(parsed).toEqual([row()])
  })

  test('defaults the fields a sparse response leaves out', () => {
    const parsed = fromResponse({ types: [{ id: 'customer', label: 'Customer' }] } as Partial<EntityTypesResponse>)
    expect(parsed[0]).toEqual({
      id: 'customer',
      label: 'Customer',
      kind: 'note',
      folder: '',
      description: '',
      aliases: [],
      stale_after_days: 0,
      enabled: true,
      builtin: false,
      note_count: 0,
    })
  })

  test('treats a missing or unusable list as no categories', () => {
    expect(fromResponse({} as Partial<EntityTypesResponse>)).toEqual([])
    expect(fromResponse(null)).toEqual([])
    expect(fromResponse({ types: 'nope' } as unknown as Partial<EntityTypesResponse>)).toEqual([])
  })

  test('a category the build does not know the kind of is a note, not a crash', () => {
    const parsed = fromResponse({ types: [{ id: 'x', kind: 'widget' }] } as never)
    expect(parsed[0]!.kind).toBe('note')
  })
})

describe('isValidEntityTypeId', () => {
  test('holds the same shape the server checks', () => {
    expect(isValidEntityTypeId('customer')).toBe(true)
    expect(isValidEntityTypeId('skill-proposal')).toBe(true)
    expect(isValidEntityTypeId('a')).toBe(true)
    expect(isValidEntityTypeId('Customer')).toBe(false)
    expect(isValidEntityTypeId('1customer')).toBe(false)
    expect(isValidEntityTypeId('customer_1')).toBe(false)
    expect(isValidEntityTypeId('')).toBe(false)
    expect(isValidEntityTypeId('a'.repeat(65))).toBe(false)
  })
})

describe('ensureLoaded', () => {
  test('reads the workspace it is asked for, and skips a second call', async () => {
    vi.mocked(api.get).mockResolvedValue(body([row()]) as never)
    const et = useEntityTypesStore()

    await et.ensureLoaded('personal')
    expect(vi.mocked(api.get)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.get).mock.calls[0]![0]).toBe('/api/memory/entity-types?workspace=personal')
    expect(et.loadedWorkspace).toBe('personal')
    expect(et.types.map((entry) => entry.id)).toEqual(['person'])

    // Same scope: nothing to re-read.
    await et.ensureLoaded('personal')
    expect(vi.mocked(api.get)).toHaveBeenCalledTimes(1)
  })

  test('a workspace switch re-reads, scoped by the query', async () => {
    vi.mocked(api.get).mockResolvedValue(body([row()]) as never)
    const et = useEntityTypesStore()
    await et.ensureLoaded('personal')
    await et.ensureLoaded('work & side')
    expect(vi.mocked(api.get).mock.calls[1]![0]).toBe('/api/memory/entity-types?workspace=work%20%26%20side')
    expect(et.loadedWorkspace).toBe('work & side')
  })

  test('reload always re-reads, and a failure keeps the rows on screen', async () => {
    vi.mocked(api.get).mockResolvedValue(body([row()]) as never)
    const et = useEntityTypesStore()
    await et.ensureLoaded('personal')
    vi.mocked(api.get).mockRejectedValue(new Error('vault unavailable'))
    await et.reload('personal')

    expect(et.loadError).toBe('vault unavailable')
    expect(et.types.map((entry) => entry.id)).toEqual(['person'])
    expect(et.loading).toBe(false)
  })

  test('an empty workspace is not a request', async () => {
    const et = useEntityTypesStore()
    await et.ensureLoaded('')
    expect(vi.mocked(api.get)).not.toHaveBeenCalled()
  })
})

describe('save', () => {
  test('PATCHes the whole list and adopts the response', async () => {
    vi.mocked(api.get).mockResolvedValue(body([row()]) as never)
    const et = useEntityTypesStore()
    await et.ensureLoaded('personal')

    const desired = [row({ enabled: false }), row({ id: 'customer', label: 'Customer', builtin: false, note_count: 0 })]
    vi.mocked(api.patch).mockResolvedValue(body([row({ enabled: false }), row({ id: 'customer', label: 'Customer', builtin: false })]) as never)

    await expect(et.save('personal', desired)).resolves.toBe(true)
    const [path, sent] = vi.mocked(api.patch).mock.calls[0]!
    expect(path).toBe('/api/memory/entity-types?workspace=personal')
    expect(sent).toEqual(toPatchBody(desired))
    // The answer is the file the next GET will serve, not the submission echoed.
    expect(et.types[0]!.enabled).toBe(false)
    expect(et.types.map((entry) => entry.id)).toEqual(['person', 'customer'])
    expect(et.error).toBe('')
    expect(et.saving).toBe(false)
  })

  test('a 400 keeps the previous rows and puts the server sentence in error', async () => {
    vi.mocked(api.get).mockResolvedValue(body([row()]) as never)
    const et = useEntityTypesStore()
    await et.ensureLoaded('personal')

    vi.mocked(api.patch).mockRejectedValue(
      Object.assign(new Error('HTTP 400'), { payload: { error: "two enabled categories share the folder 'People'" } }),
    )

    await expect(et.save('personal', [row({ folder: 'People' })])).resolves.toBe(false)
    expect(et.types).toEqual([row()])
    expect(et.error).toBe("two enabled categories share the folder 'People'")
    expect(et.saving).toBe(false)
  })

  test('a delete the server refuses keeps the category and says why', async () => {
    vi.mocked(api.get).mockResolvedValue(body([row()]) as never)
    const et = useEntityTypesStore()
    await et.ensureLoaded('personal')

    vi.mocked(api.patch).mockRejectedValue(
      Object.assign(new Error('HTTP 400'), {
        payload: { error: 'refusing to delete a category its notes still use: customer (2 notes) — retype those notes first, or keep the category' },
      }),
    )

    await expect(et.save('personal', [])).resolves.toBe(false)
    expect(et.types.map((entry) => entry.id)).toEqual(['person'])
    expect(et.error).toContain('customer (2 notes)')
  })

  test('clearError drops a message a dismissed form is done with', async () => {
    vi.mocked(api.patch).mockRejectedValue(new Error('nope'))
    const et = useEntityTypesStore()
    await et.save('personal', [row()])
    expect(et.error).toBe('nope')
    et.clearError()
    expect(et.error).toBe('')
  })
})
