import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import { apiErrorMessage } from '../lib/errorMessage'

/**
 * `entity` is a record OF the thing (a person, a project, a place) and `note`
 * is a document ABOUT something. The server owns the distinction and refuses
 * anything else, so it is a union here rather than a `string`: a typo in one of
 * the two literals would otherwise compile clean and quietly disable the row.
 */
export type EntityTypeKind = 'entity' | 'note'

/** One effective category, as `GET`/`PATCH /api/memory/entity-types` serves it. */
export interface EntityTypeRow {
  id: string
  label: string
  kind: EntityTypeKind
  folder: string
  description: string
  aliases: string[]
  stale_after_days: number
  enabled: boolean
  /** True for a category the install ships. Read-only, and set by the server. */
  builtin: boolean
  /** Notes carrying this `type:`. The server's own count; not part of a PATCH. */
  note_count: number
}

export interface EntityTypesResponse {
  workspace: string
  vault: string
  types: EntityTypeRow[]
}

/**
 * One row as a `PATCH` body carries it: the category's own fields, without the
 * count the server computed. Named rather than left as a record so the shape a
 * test asserts on is a shape the compiler checks.
 */
export type EntityTypePatchRow = Omit<EntityTypeRow, 'note_count'>

export interface EntityTypesPatchBody {
  types: EntityTypePatchRow[]
}

/**
 * What an id may be: the same `^[a-z][a-z0-9-]{0,63}$` the server checks.
 *
 * The rule is repeated rather than imported so the form can refuse a bad id
 * before a round trip. The server still has the final word — this is the fast
 * half of the check, not a replacement for it.
 */
export const ENTITY_ID_PATTERN = /^[a-z][a-z0-9-]{0,63}$/

/** Whether *value* is an id the server would accept. */
export function isValidEntityTypeId(value: string): boolean {
  return ENTITY_ID_PATTERN.test(value)
}

/**
 * The body a `PATCH` carries, from the rows the form is holding.
 *
 * The API takes the whole desired list rather than a diff, so this maps one list
 * to another and drops the one field the server computed: `note_count` is its
 * count of the vault's notes, not part of the document being described. Sending
 * it back would be read as an unknown key and ignored, so it is simply not sent.
 */
export function toPatchBody(rows: EntityTypeRow[]): EntityTypesPatchBody {
  return {
    types: rows.map((row) => ({
      id: row.id,
      label: row.label,
      kind: row.kind,
      folder: row.folder,
      description: row.description,
      aliases: [...row.aliases],
      stale_after_days: row.stale_after_days,
      enabled: row.enabled,
      builtin: row.builtin,
    })),
  }
}

/**
 * The rows a response describes, with every field defaulted.
 *
 * A GET is the one place the field set can drift, so each is read defensively:
 * a category with no aliases, a count that did not come, a `kind` this build
 * does not know, and a missing list all render as this panel's empty values
 * rather than as `undefined` in a table cell.
 */
export function fromResponse(json: Partial<EntityTypesResponse> | null | undefined): EntityTypeRow[] {
  const rows = Array.isArray(json?.types) ? json.types : []
  return rows.map((row) => {
    const raw = (row ?? {}) as Partial<EntityTypeRow>
    return {
      id: typeof raw.id === 'string' ? raw.id : '',
      label: typeof raw.label === 'string' ? raw.label : '',
      kind: raw.kind === 'entity' ? 'entity' : 'note',
      folder: typeof raw.folder === 'string' ? raw.folder : '',
      description: typeof raw.description === 'string' ? raw.description : '',
      aliases: Array.isArray(raw.aliases) ? raw.aliases.filter((a): a is string => typeof a === 'string') : [],
      stale_after_days: typeof raw.stale_after_days === 'number' ? raw.stale_after_days : 0,
      enabled: raw.enabled !== false,
      builtin: raw.builtin === true,
      note_count: typeof raw.note_count === 'number' ? raw.note_count : 0,
    }
  })
}

function entityTypesUrl(workspace: string): string {
  return `/api/memory/entity-types?workspace=${encodeURIComponent(workspace)}`
}

/**
 * The vault's category list, scoped to one workspace.
 *
 * Shaped like `vaultReview` and `proposals`: a per-workspace `loadedWorkspace`,
 * a load state, and a separate `loadError` so a failed refresh cannot clear an
 * unread save's message. The registry is per vault FILE, so two workspaces on
 * one vault root share this list — which is the server's rule, not one imposed
 * here, and the reason `save` adopts the response rather than assuming the
 * submission is what was stored.
 */
export const useEntityTypesStore = defineStore('entityTypes', () => {
  const types = ref<EntityTypeRow[]>([])
  const loadedWorkspace = ref('')
  const loading = ref(false)
  const loadError = ref('')
  const error = ref('')
  const saving = ref(false)
  /** Ticket for the newest in-flight list request; older responses are dropped. */
  let requestSeq = 0

  /**
   * Read the list for `workspace`, always.
   *
   * A refresh that fails keeps the rows it already had: the panel is still
   * showing the configuration the user was reading, and swapping it for an
   * error card would throw away a list they could still act on.
   */
  async function reload(workspace: string): Promise<void> {
    if (!workspace) return
    const seq = ++requestSeq
    loading.value = true
    loadError.value = ''
    try {
      const data = await api.get<EntityTypesResponse>(entityTypesUrl(workspace))
      if (seq !== requestSeq) return
      types.value = fromResponse(data)
      loadedWorkspace.value = workspace
    } catch (e) {
      if (seq !== requestSeq) return
      loadError.value = apiErrorMessage(e, 'Could not load categories')
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  /**
   * Read the list for `workspace` unless it is already held.
   *
   * A workspace switch is the only thing that should re-read it: coming back to
   * a section you have already seen costs no round trip, and the explicit
   * `reload` (the panel's Retry) always does.
   */
  async function ensureLoaded(workspace: string): Promise<void> {
    if (!workspace) return
    if (loadedWorkspace.value === workspace) return
    await reload(workspace)
  }

  /**
   * Write the whole list, and adopt the one the server produced.
   *
   * A `PATCH` is the desired list, not a diff, so there is no per-row save to
   * fail independently — and its answer is a fresh effective list rather than an
   * echo, which is what `types` becomes. A failure (a duplicate folder, an alias
   * that is another entry's id, a delete whose notes still use it) leaves the
   * rows exactly as they were and the message in `error` for the form to show,
   * because the edit is still the user's to fix.
   */
  async function save(workspace: string, rows: EntityTypeRow[]): Promise<boolean> {
    if (!workspace) return false
    saving.value = true
    error.value = ''
    try {
      const data = await api.patch<EntityTypesResponse>(entityTypesUrl(workspace), toPatchBody(rows))
      types.value = fromResponse(data)
      loadedWorkspace.value = workspace
      return true
    } catch (e) {
      error.value = apiErrorMessage(e, 'Could not save categories')
      return false
    } finally {
      saving.value = false
    }
  }

  /** Drop a message a dismissed form is done with. */
  function clearError(): void {
    error.value = ''
  }

  return {
    types, loadedWorkspace, loading, loadError, error, saving,
    ensureLoaded, reload, save, clearError,
  }
})
