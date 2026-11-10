// Pure driver for the read-only folder browser (VueFinder over GET /api/workspace-dir).
// Imports vuefinder TYPES ONLY: the runtime package is loaded by FolderBrowser.vue's
// lazy chunk, so this module and the store/node tests never pull it in.
import type { DirEntry, Driver, FeaturesConfig, FsData, ListParams, SearchParams } from 'vuefinder'

export interface WorkspaceDirRoot { name: string; path: string }
export interface WorkspaceDirEntry { name: string; path: string; type: 'dir' | 'file'; size: number | null; mtime: number; ext: string }
export interface WorkspaceDirListing { path: string; root: WorkspaceDirRoot; roots: WorkspaceDirRoot[]; parent: string | null; entries: WorkspaceDirEntry[]; truncated: boolean }
export type WorkspaceDirResult =
  | { ok: true; listing: WorkspaceDirListing }
  | { ok: false; status: number; error: string; kind?: 'file'; path?: string }

export const FOLDER_BROWSER_ID = 'ciao-folder-browser'

// search is off: VueFinder teleports its search modal to <body>, outside the Reka focus trap.
export const FOLDER_BROWSER_FEATURES = {
  search: false, history: true, preview: false, edit: false, newfile: false, newfolder: false,
  upload: false, delete: false, rename: false, move: false, copy: false, archive: false,
  unarchive: false, download: false, fullscreen: false, theme: false, language: false, pinned: false,
} satisfies FeaturesConfig

export async function fetchWorkspaceDir(path: string, chatId = '', signal?: AbortSignal): Promise<WorkspaceDirResult> {
  const query = new URLSearchParams()
  if (path) query.set('path', path)
  if (chatId) query.set('chat_id', chatId)
  try {
    const resp = await fetch(`/api/workspace-dir?${query}`, { credentials: 'same-origin', signal })
    const body = await resp.json().catch(() => ({})) as Record<string, unknown>
    if (resp.ok) return { ok: true, listing: body as unknown as WorkspaceDirListing }
    const result: WorkspaceDirResult = {
      ok: false,
      status: resp.status,
      error: typeof body.error === 'string' ? body.error : `HTTP ${resp.status}`,
    }
    if (body.kind === 'file') result.kind = 'file'
    if (typeof body.path === 'string') result.path = body.path
    return result
  } catch (err) {
    return { ok: false, status: 0, error: err instanceof Error ? err.message : String(err) }
  }
}

// Maps, not objects: the error string comes from the server, so 'constructor' must not hit a prototype key.
const OUTSIDE = 'This folder is outside the folders Ciaobot can browse.'
const REASON_MESSAGES = new Map<string, string>([
  ['outside_roots', OUTSIDE],
  ['unreadable', 'Ciaobot cannot read this folder.'],
  ['chat not found', 'This chat no longer exists.'],
  ['not_a_directory', 'This path is not a folder.'],
  ['bad path', 'This is not a valid folder path.'],
  ['not found', 'Folder not found.'],
])

const STATUS_MESSAGES = new Map<number, string>([
  [403, OUTSIDE],
  [404, 'Folder not found.'],
  [400, 'This path is not a folder.'],
  [0, 'Could not reach Ciaobot.'],
])

export function folderErrorMessage(status: number, error = ''): string {
  return REASON_MESSAGES.get(error) ?? STATUS_MESSAGES.get(status) ?? 'Could not load this folder.'
}

// VueFinder paths are `storage://relative`; storages are the server's root names.
export function toVfPath(absPath: string, roots: WorkspaceDirRoot[]): string {
  for (const root of roots) {
    const base = root.path.replace(/\/+$/, '')
    if (absPath === root.path || absPath === base) return `${root.name}://`
    if (absPath.startsWith(base + '/')) return `${root.name}://${absPath.slice(base.length + 1)}`
  }
  return ''
}

export function fromVfPath(vfPath: string, roots: WorkspaceDirRoot[]): string {
  const sep = vfPath.indexOf('://')
  if (sep < 0) return vfPath
  const root = roots.find(r => r.name === vfPath.slice(0, sep))
  if (!root) return ''
  const rel = vfPath.slice(sep + 3).replace(/^\/+|\/+$/g, '')
  if (!rel) return root.path
  return `${root.path.replace(/\/+$/, '')}/${rel}`
}

// last_modified stays in seconds: VueFinder renders new Date(x * 1e3).
export function toFsData(listing: WorkspaceDirListing): FsData {
  const dirname = toVfPath(listing.path, listing.roots)
  return {
    storages: listing.roots.map(r => r.name),
    dirname,
    files: listing.entries.map((e): DirEntry => ({
      dir: dirname,
      basename: e.name,
      extension: e.ext,
      path: toVfPath(e.path, listing.roots),
      storage: listing.root.name,
      type: e.type,
      file_size: e.type === 'file' ? e.size : null,
      last_modified: e.mtime,
      mime_type: null,
      read_only: true,
      visibility: 'public',
    })),
    read_only: true,
  }
}

export interface WorkspaceDirDriver extends Driver { roots: WorkspaceDirRoot[] }

export function createWorkspaceDirDriver(opts: {
  chatId?: string
  roots: WorkspaceDirRoot[]
  initial?: WorkspaceDirListing
  onListing?: (listing: WorkspaceDirListing) => void
  fetchDir?: typeof fetchWorkspaceDir
}): WorkspaceDirDriver {
  const { chatId = '', initial, onListing, fetchDir = fetchWorkspaceDir } = opts
  let seeded: WorkspaceDirListing | null = initial ?? null
  const readOnly = (): Promise<never> => Promise.reject(new Error('The folder browser is read-only.'))
  // Methods refer to `driver`, never `this`: VueFinder may call them detached.
  const driver: WorkspaceDirDriver = {
    roots: [...opts.roots],
    async list(params?: ListParams): Promise<FsData> {
      const abs = fromVfPath(params?.path ?? '', driver.roots)
      let listing: WorkspaceDirListing
      if (seeded && (abs === '' || abs === seeded.path)) {
        // VueFinder's first list() asks for the folder the component already fetched.
        listing = seeded
        seeded = null
      } else {
        seeded = null
        const result = await fetchDir(abs, chatId, params?.signal)
        if (!result.ok) throw new Error(folderErrorMessage(result.status, result.error))
        listing = result.listing
      }
      driver.roots = listing.roots
      onListing?.(listing)
      return toFsData(listing)
    },
    async search(params: SearchParams): Promise<DirEntry[]> {
      const data = await driver.list({ path: params.path, signal: params.signal })
      const needle = params.filter.trim().toLowerCase()
      return needle ? data.files.filter(f => f.basename.toLowerCase().includes(needle)) : data.files
    },
    delete: readOnly, rename: readOnly, copy: readOnly, move: readOnly,
    archive: readOnly, unarchive: readOnly, createFile: readOnly, createFolder: readOnly,
    save: readOnly, getContent: readOnly,
    getPreviewUrl: () => '',
    getDownloadUrl: () => '',
  }
  return driver
}

// VueFinder 4.8 merges config as {...defaults, ...passed, ...stored}, so a passed
// `view` or `persist` loses to what the user last stored. Seed localStorage instead.
export function seedFolderBrowserConfig(storage: Pick<Storage, 'getItem' | 'setItem'>, id = FOLDER_BROWSER_ID, narrow = false): void {
  const key = `vuefinder_config_${id}`
  let stored: Record<string, unknown> = {}
  try {
    const parsed: unknown = JSON.parse(storage.getItem(key) ?? '{}')
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) stored = parsed as Record<string, unknown>
  } catch { /* corrupt entry: keep defaults */ }
  storage.setItem(key, JSON.stringify({
    view: 'list', showThumbnails: false, expandTreeByDefault: false, showHiddenFiles: false,
    ...stored,
    // Decided by the screen, not the stored value: a phone session must not hide the tree on desktop, nor the reverse.
    showTreeView: !narrow,
    persist: false, fullScreen: false, path: '',
  }))
}

export function parentFolderOf(filePath: string): string {
  const clean = filePath.replace(/:\d+$/, '').replace(/[\\/]+$/, '')
  const idx = Math.max(clean.lastIndexOf('/'), clean.lastIndexOf('\\'))
  if (idx < 0) return ''
  if (idx === 0) return '/'
  const parent = clean.slice(0, idx)
  return /^[A-Za-z]:$/.test(parent) ? `${parent}/` : parent
}
