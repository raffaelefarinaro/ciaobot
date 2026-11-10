import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  FOLDER_BROWSER_FEATURES,
  createWorkspaceDirDriver,
  fetchWorkspaceDir,
  folderErrorMessage,
  fromVfPath,
  parentFolderOf,
  seedFolderBrowserConfig,
  toFsData,
  toVfPath,
  type WorkspaceDirListing,
  type WorkspaceDirRoot,
} from './folderBrowserDriver'

afterEach(() => vi.unstubAllGlobals())

const roots: WorkspaceDirRoot[] = [
  { name: 'workspace', path: '/ws' },
  { name: 'repo', path: '/r' },
]

function listingAt(path: string, rootList: WorkspaceDirRoot[] = roots, entries: WorkspaceDirListing['entries'] = []): WorkspaceDirListing {
  const root = rootList.find(r => path === r.path || path.startsWith(r.path + '/')) ?? rootList[0]
  return { path, root, roots: rootList, parent: null, entries, truncated: false }
}

describe('folder browser driver', () => {
  it('toVfPath and fromVfPath round-trip under each root', () => {
    expect(toVfPath('/ws', roots)).toBe('workspace://')
    expect(fromVfPath('workspace://', roots)).toBe('/ws')
    expect(toVfPath('/ws/a/b', roots)).toBe('workspace://a/b')
    expect(fromVfPath('workspace://a/b', roots)).toBe('/ws/a/b')
    expect(toVfPath('/r/x', roots)).toBe('repo://x')
    expect(fromVfPath('repo://x', roots)).toBe('/r/x')
    expect(toVfPath('/elsewhere', roots)).toBe('')
    expect(fromVfPath('/abs', roots)).toBe('/abs')
    expect(fromVfPath('nope://x', roots)).toBe('')
  })

  it('toVfPath and fromVfPath handle a Windows-shaped root', () => {
    const win: WorkspaceDirRoot[] = [{ name: 'workspace', path: 'C:/ws' }]
    expect(toVfPath('C:/ws/a', win)).toBe('workspace://a')
    expect(fromVfPath('workspace://a', win)).toBe('C:/ws/a')
  })

  it('toFsData maps a listing onto VueFinder entries', () => {
    const listing = listingAt('/ws/a', roots, [
      { name: 'b.md', path: '/ws/a/b.md', type: 'file', size: 12, mtime: 1700000000, ext: 'md' },
      { name: 'sub', path: '/ws/a/sub', type: 'dir', size: null, mtime: 1700000001, ext: '' },
    ])
    const data = toFsData(listing)

    expect(data.dirname).toBe('workspace://a')
    expect(data.storages).toEqual(['workspace', 'repo'])
    expect(data.read_only).toBe(true)
    const [file, dir] = data.files
    expect(file.dir).toBe(data.dirname)
    expect(file.storage).toBe('workspace')
    expect(file.read_only).toBe(true)
    expect(file.basename).toBe('b.md')
    expect(file.extension).toBe('md')
    expect(file.path).toBe('workspace://a/b.md')
    expect(file.file_size).toBe(12)
    expect(file.last_modified).toBe(1700000000)
    expect(dir.file_size).toBeNull()
  })

  it('the driver lists through the injected fetch and refreshes roots', async () => {
    const listing = listingAt('/ws/a')
    const fetchDir = vi.fn(async () => ({ ok: true as const, listing }))
    const onListing = vi.fn()
    const driver = createWorkspaceDirDriver({ chatId: 'chat-1', roots, fetchDir, onListing })

    const data = await driver.list({ path: 'workspace://a' })

    expect(fetchDir).toHaveBeenCalledWith('/ws/a', 'chat-1', undefined)
    expect(data.dirname).toBe('workspace://a')
    expect(driver.roots).toEqual(roots)
    expect(onListing).toHaveBeenCalledTimes(1)
    expect(onListing).toHaveBeenCalledWith(listing)
  })

  it('the first listing answers VueFinder first list without a request', async () => {
    const initial = listingAt('/ws/exports')
    const fetchDir = vi.fn(async () => ({ ok: true as const, listing: initial }))
    const driver = createWorkspaceDirDriver({ roots, initial, fetchDir })

    const first = await driver.list({ path: 'workspace://exports' })
    expect(fetchDir).not.toHaveBeenCalled()
    expect(first.dirname).toBe('workspace://exports')

    await driver.list({ path: 'workspace://exports' })
    expect(fetchDir).toHaveBeenCalledTimes(1)
  })

  it('a refused listing rejects with the server reason', async () => {
    const fetchDir = vi.fn(async () => ({ ok: false as const, status: 403, error: 'unreadable' }))
    const driver = createWorkspaceDirDriver({ roots, fetchDir })

    await expect(driver.list({ path: 'workspace://a' })).rejects.toThrow('Ciaobot cannot read this folder.')
  })

  it('folderErrorMessage maps reasons before statuses', () => {
    expect(folderErrorMessage(403, 'outside_roots')).toBe('This folder is outside the folders Ciaobot can browse.')
    expect(folderErrorMessage(403, 'unreadable')).toBe('Ciaobot cannot read this folder.')
    expect(folderErrorMessage(404, 'chat not found')).toBe('This chat no longer exists.')
    expect(folderErrorMessage(404, 'not found')).toBe('Folder not found.')
    expect(folderErrorMessage(400, 'not_a_directory')).toBe('This path is not a folder.')
    expect(folderErrorMessage(400, 'bad path')).toBe('This is not a valid folder path.')
    expect(folderErrorMessage(403, '')).toBe('This folder is outside the folders Ciaobot can browse.')
    expect(folderErrorMessage(0, 'Failed to fetch')).toBe('Could not reach Ciaobot.')
    expect(folderErrorMessage(500, 'boom')).toBe('Could not load this folder.')
  })

  it('every write operation rejects and URLs are empty', async () => {
    const driver = createWorkspaceDirDriver({ roots })
    const writes = [
      driver.delete({ path: 'x', items: [] }),
      driver.rename({ path: 'x', item: 'a', name: 'b' }),
      driver.copy({ sources: [], destination: 'x' }),
      driver.move({ sources: [], destination: 'x' }),
      driver.archive({ items: [], path: 'x', name: 'a.zip' }),
      driver.unarchive({ item: 'a.zip', path: 'x' }),
      driver.createFile({ path: 'x', name: 'a' }),
      driver.createFolder({ path: 'x', name: 'a' }),
      driver.save({ path: 'x', content: '' }),
      driver.getContent({ path: 'x' }),
    ]
    for (const write of writes) {
      await expect(write).rejects.toThrow('The folder browser is read-only.')
    }
    expect(driver.getPreviewUrl({ path: 'x' })).toBe('')
    expect(driver.getDownloadUrl({ path: 'x' })).toBe('')
  })

  it('search filters the current folder by name and returns entries', async () => {
    const listing = listingAt('/ws/a', roots, [
      { name: 'readme.md', path: '/ws/a/readme.md', type: 'file', size: 1, mtime: 1, ext: 'md' },
      { name: 'a.txt', path: '/ws/a/a.txt', type: 'file', size: 1, mtime: 1, ext: 'txt' },
    ])
    const driver = createWorkspaceDirDriver({ roots, fetchDir: vi.fn(async () => ({ ok: true as const, listing })) })

    const found = await driver.search({ path: 'workspace://a', filter: 'READ' })

    expect(Array.isArray(found)).toBe(true)
    expect(found.map(f => f.basename)).toEqual(['readme.md'])
  })

  it('seedFolderBrowserConfig writes defaults and keeps user choices', () => {
    const store = new Map<string, string>()
    const storage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
    }
    const key = 'vuefinder_config_ciao-folder-browser'
    const read = () => JSON.parse(store.get(key) ?? 'null')

    seedFolderBrowserConfig(storage)
    expect(read().view).toBe('list')
    expect(read().showTreeView).toBe(true)
    expect(read().persist).toBe(false)

    store.set(key, JSON.stringify({ view: 'grid', persist: true, path: 'workspace://x' }))
    seedFolderBrowserConfig(storage)
    expect(read().view).toBe('grid')
    expect(read().persist).toBe(false)
    expect(read().path).toBe('')

    store.set(key, 'not json')
    seedFolderBrowserConfig(storage)
    expect(read().view).toBe('list')
  })

  it('seedFolderBrowserConfig hides the tree on a narrow screen even when a desktop session stored it shown', () => {
    const store = new Map<string, string>()
    const storage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
    }
    const key = 'vuefinder_config_ciao-folder-browser'
    store.set(key, JSON.stringify({ showTreeView: true }))
    seedFolderBrowserConfig(storage, undefined, true)
    expect(JSON.parse(store.get(key) ?? 'null').showTreeView).toBe(false)
  })

  it('seedFolderBrowserConfig keeps a stored tree choice on a wide screen', () => {
    const store = new Map<string, string>()
    const storage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
    }
    const key = 'vuefinder_config_ciao-folder-browser'
    store.set(key, JSON.stringify({ showTreeView: false }))
    seedFolderBrowserConfig(storage, undefined, false)
    expect(JSON.parse(store.get(key) ?? 'null').showTreeView).toBe(false)
    store.delete(key)
    seedFolderBrowserConfig(storage, undefined, false)
    expect(JSON.parse(store.get(key) ?? 'null').showTreeView).toBe(true)
  })

  it('parentFolderOf returns the folder that holds a file', () => {
    expect(parentFolderOf('/ws/notes/a.md')).toBe('/ws/notes')
    expect(parentFolderOf('/ws/notes/a.md:12')).toBe('/ws/notes')
    expect(parentFolderOf('/a.md')).toBe('/')
    expect(parentFolderOf('C:/x/a.md')).toBe('C:/x')
    expect(parentFolderOf('C:/a.md')).toBe('C:/')
    expect(parentFolderOf('notes/a.md')).toBe('notes')
    expect(parentFolderOf('a.md')).toBe('')
    expect(parentFolderOf('/ws/dir/')).toBe('/ws')
  })

  it('fetchWorkspaceDir classifies the answer', async () => {
    const listing = listingAt('/ws/exports')
    const fetchMock = vi.fn(async (_url: string) => new Response(JSON.stringify(listing), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const ok = await fetchWorkspaceDir('/ws/exports', 'chat-1')
    expect(ok).toEqual({ ok: true, listing })
    const url = new URL(String(fetchMock.mock.calls[0][0]), 'http://x')
    expect(url.pathname).toBe('/api/workspace-dir')
    expect(url.searchParams.get('path')).toBe('/ws/exports')
    expect(url.searchParams.get('chat_id')).toBe('chat-1')

    await fetchWorkspaceDir('')
    const bare = new URL(String(fetchMock.mock.calls[1][0]), 'http://x')
    expect(bare.searchParams.has('path')).toBe(false)
    expect(bare.searchParams.has('chat_id')).toBe(false)

    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ error: 'not_a_directory', kind: 'file', path: '/ws/f' }),
      { status: 400 },
    )))
    expect(await fetchWorkspaceDir('/ws/f')).toEqual({
      ok: false, status: 400, error: 'not_a_directory', kind: 'file', path: '/ws/f',
    })

    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    const network = await fetchWorkspaceDir('/ws')
    expect(network).toMatchObject({ ok: false, status: 0 })
  })

  it('features keep the browser read-only', () => {
    for (const name of ['search', 'upload', 'delete', 'rename', 'move', 'copy', 'newfolder', 'newfile', 'edit', 'download'] as const) {
      expect(FOLDER_BROWSER_FEATURES[name]).toBe(false)
    }
    expect(FOLDER_BROWSER_FEATURES.history).toBe(true)
  })
})
