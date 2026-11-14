import { describe, expect, it } from 'vitest'
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { Dirent } from 'node:fs'
import { filterInventory, inventory } from '../stories/inventory'
import type { InventoryEntry, InventoryFilters } from '../stories/inventory'

const src = fileURLToPath(new URL('../', import.meta.url))
const root = resolve(src, '../..')
const posix = (path: string) => path.split(sep).join('/')
const excluded = new Set(['node_modules', '__tests__', 'stories', '.storybook', 'styles', 'dist', 'e2e', 'fixtures', '__fixtures__'])

type SourceDirent = Pick<Dirent, 'name' | 'isSymbolicLink' | 'isDirectory' | 'isFile'>
function walk(base: string, read: (dir: string) => SourceDirent[] = (dir) => readdirSync(dir, { withFileTypes: true }), suffix = '.vue'): string[] {
  const paths: string[] = []
  function visit(dir: string) {
    for (const entry of read(dir)) {
      if (entry.isSymbolicLink()) continue
      const path = join(dir, entry.name)
      if (entry.isDirectory()) { if (!excluded.has(entry.name)) visit(path) }
      else if (entry.isFile() && entry.name.endsWith(suffix) && !/\.(test|spec|story|stories)\.(vue|ts)$/.test(entry.name)) paths.push(posix(relative(base, path)))
    }
  }
  visit(base)
  return paths.sort()
}

function assertOwners(expected: string[], owners: string[]) {
  const seen = new Set<string>()
  for (const path of owners) {
    if (seen.has(path)) throw new Error(`Duplicate owner: ${path}`)
    seen.add(path)
    if (!expected.includes(path)) throw new Error(`Deleted or unexpected owner: ${path}`)
  }
  for (const path of expected) if (!seen.has(path)) throw new Error(`Missing owner: ${path}`)
}

type PathOps = { lstat: (path: string) => { isSymbolicLink(): boolean; isFile(): boolean }; realpath: (path: string) => string }
const fsOps: PathOps = { lstat: lstatSync, realpath: realpathSync }
function assertSourcePath(base: string, path: string, ops: PathOps = fsOps) {
  if (!path.startsWith('web/src/') || isAbsolute(path) || path.includes('\\') || path.split('/').some((part) => !part || part === '.' || part === '..') || /(^|\/)(__tests__|stories|fixtures|__fixtures__|node_modules)(\/|$)|\.(test|spec|story|stories)\./.test(path)) throw new Error(`Invalid source path: ${path}`)
  const parts = path.slice('web/src/'.length).split('/')
  let current = base
  for (const part of parts) {
    current = join(current, part)
    if (ops.lstat(current).isSymbolicLink()) throw new Error(`Symlink source path: ${path}`)
  }
  if (!ops.lstat(current).isFile()) throw new Error(`Not a source file: ${path}`)
  const rel = relative(ops.realpath(base), ops.realpath(current))
  if (!rel || rel === '..' || rel.startsWith(`..${sep}`) || isAbsolute(rel)) throw new Error(`Source path escapes root: ${path}`)
}

const covered: Record<string, string> = { default: 'foundation-tab-bar--default', counts: 'foundation-tab-bar--counts', 'long-content': 'foundation-tab-bar--long-labels', narrow: 'foundation-tab-bar--narrow-pane' }
const switchCovered: Record<string, string[]> = {
  default: ['settings-switch--on', 'settings-switch--off'],
  disabled: ['settings-switch--disabled-on', 'settings-switch--disabled-off'],
  busy: ['settings-switch--busy'],
  narrow: ['settings-switch--narrow'],
}
const coverageOwners = [
  { id: 'components-tab-bar', path: 'web/src/components/TabBar.vue', states: Object.fromEntries(Object.entries(covered).map(([state, id]) => [state, [id]])) },
  { id: 'components-settings-settings-switch', path: 'web/src/components/settings/SettingsSwitch.vue', states: switchCovered },
]
function assertCoverage(entries: InventoryEntry[]) {
  const ids = new Set<string>()
  for (const entry of entries) {
    if (ids.has(entry.id)) throw new Error(`Duplicate ID: ${entry.id}`)
    expect(entry.id).toMatch(/^[a-z0-9]+(?:-[a-z0-9]+)*$/)
    ids.add(entry.id)
    expect(entry.name.trim()).not.toBe(''); expect(entry.family.trim()).not.toBe(''); expect(entry.owner.anchor.trim()).not.toBe('')
    expect(entry.variantNotes.trim()).not.toBe(''); expect(entry.loading.detail.trim()).not.toBe(''); expect(entry.migration.note.trim()).not.toBe('')
    if (!entry.variants.length) expect(entry.variantNotes).toMatch(/unassessed|not yet been audited|no configurable/i)
    expect(['known', 'none', 'unknown']).toContain(entry.loading.status)
    expect(['unassessed', 'retained', 'planned', 'migrated']).toContain(entry.migration.status)
    if (entry.kind === 'composition') expect(entry.rationale?.trim()).toBeTruthy()
    expect(entry.states.length).toBeGreaterThan(0)
    expect(new Set(entry.states.map((state) => state.name)).size).toBe(entry.states.length)
    for (const state of entry.states) {
      expect(state.name.trim()).not.toBe(''); expect(state.note.trim()).not.toBe('')
      if (state.coverage === 'covered') {
        const owner = coverageOwners.find((owner) => owner.id === entry.id && owner.path === entry.owner.path)
        if (entry.kind !== 'component' || !owner?.states[state.name] || JSON.stringify(state.storyIds) !== JSON.stringify(owner.states[state.name])) throw new Error(`Invalid covered state: ${entry.id}/${state.name}`)
      } else if (state.storyIds.length) throw new Error(`Noncovered state has story IDs: ${entry.id}/${state.name}`)
    }
  }
}

function fixture(run: (base: string) => void) {
  const base = mkdtempSync(join(tmpdir(), 'ciao-inventory-'))
  try { run(base) } finally { rmSync(base, { recursive: true, force: true }) }
}
function file(base: string, name: string) { const path = join(base, name); mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, '') }
const defaults: InventoryFilters = { query: '', family: 'all', coverage: 'all', migration: 'all' }

describe('production ownership', () => {
  it('matches the exact production SFC set with one owner per file', () => {
    const expected = walk(src).map((path) => `web/src/${path}`)
    const owners = inventory.filter((entry) => entry.kind === 'component' || entry.kind === 'composition').map((entry) => entry.owner.path)
    assertOwners(expected, owners)
    expect(owners).toHaveLength(expected.length)
  })

  it('walks only production sources and reports missing, duplicate and deleted paths', () => fixture((base) => {
    const included = ['App.vue', 'components/X.vue', 'components/settings/Y.vue', 'components/onboarding/Z.vue', 'components/needs-you/B.vue', 'other/New.vue']
    for (const name of [...included, ...[...excluded].map((dir) => `${dir}/Fake.vue`), 'stories/InventoryBrowser.vue', 'node_modules/dep/Fake.vue', 'components/Fake.test.vue', 'components/Fake.spec.vue', 'components/Fake.story.vue', 'components/Fake.stories.vue']) file(base, name)
    try { symlinkSync(join(base, 'components'), join(base, 'linked'), 'dir') } catch (error) {
      if (!['EPERM', 'EACCES', 'ENOTSUP'].includes((error as NodeJS.ErrnoException).code ?? '')) throw error
    }
    expect(walk(base)).toEqual([...included].sort())
    const simulatedLinks: SourceDirent[] = [
      { name: 'injected.vue', isSymbolicLink: () => true, isDirectory: () => false, isFile: () => true },
      { name: 'injected-dir', isSymbolicLink: () => true, isDirectory: () => true, isFile: () => false },
    ]
    expect(walk(base, (dir) => [...readdirSync(dir, { withFileTypes: true }), ...(dir === base ? simulatedLinks : [])])).toEqual([...included].sort())
    expect(() => assertOwners(included, included.slice(1))).toThrow('Missing owner: App.vue')
    expect(() => assertOwners(included, [...included, included[0]])).toThrow('Duplicate owner: App.vue')
    expect(() => assertOwners(included, [...included, 'Deleted.vue'])).toThrow('Deleted or unexpected owner: Deleted.vue')
  }))

  it('validates all owner, support and consumer paths and CSS ownership', () => {
    for (const entry of inventory) {
      for (const path of [entry.owner.path, ...entry.sources, ...entry.consumers]) assertSourcePath(src, path)
      expect(readFileSync(resolve(root, entry.owner.path), 'utf8')).toContain(entry.owner.anchor)
      if (entry.kind === 'inline') expect(entry.consumers).toEqual([])
    }
    const styles = readdirSync(join(src, 'components'), { recursive: true, withFileTypes: true }).filter((d) => d.isFile() && d.name.endsWith('.css')).map((d) => posix(relative(root, join(d.parentPath, d.name))))
    for (const path of [...styles, 'web/src/styles/app.css']) expect(inventory.some((entry) => entry.kind === 'css' && entry.owner.path === path), path).toBe(true)
  })

  it('rejects traversal and external or internal symlinks even without symlink privileges', () => fixture((base) => {
    file(base, 'nested/valid.vue')
    assertSourcePath(base, 'web/src/nested/valid.vue')
    for (const path of ['web/src/../outside', '/web/src/x', 'web/src/nested/./valid.vue', 'web/src/stories/X.vue', 'web/src/nested/x.test.ts', 'web/src/nested\\valid.vue']) expect(() => assertSourcePath(base, path)).toThrow()
    for (const target of [base, tmpdir()]) {
      const link = join(base, target === base ? 'internal' : 'external')
      try {
        symlinkSync(target, link, 'dir')
        expect(() => assertSourcePath(base, `web/src/${target === base ? 'internal' : 'external'}/nested/valid.vue`)).toThrow('Symlink')
      } catch (error) {
        if (existsSync(link) || !['EPERM', 'EACCES', 'ENOTSUP'].includes((error as NodeJS.ErrnoException).code ?? '')) throw error
        const ops: PathOps = { lstat: (path) => ({ isSymbolicLink: () => path === link, isFile: () => true }), realpath: (path) => path }
        expect(() => assertSourcePath(base, `web/src/${target === base ? 'internal' : 'external'}/nested/valid.vue`, ops)).toThrow('Symlink')
      }
    }
    const ops: PathOps = { lstat: () => ({ isSymbolicLink: () => false, isFile: () => true }), realpath: (path) => path === base ? base : resolve(base, '..', 'outside') }
    expect(() => assertSourcePath(base, 'web/src/nested/valid.vue', ops)).toThrow('escapes root')
  }))

  it('rejects symlinked owner files as well as directories', () => fixture((base) => {
    const source = join(base, 'source')
    file(source, 'nested/valid.vue')
    file(base, 'external.vue')
    for (const [name, target] of [['internal.vue', join(source, 'nested/valid.vue')], ['external.vue', join(base, 'external.vue')]]) {
      const link = join(source, name)
      try { symlinkSync(target, link, 'file') } catch (error) {
        if (!['EPERM', 'EACCES', 'ENOTSUP'].includes((error as NodeJS.ErrnoException).code ?? '')) throw error
      }
      const ops: PathOps = existsSync(link) ? fsOps : {
        lstat: (path) => ({ isSymbolicLink: () => path === link, isFile: () => true }),
        realpath: (path) => path === link ? target : path,
      }
      expect(() => assertSourcePath(source, `web/src/${name}`, ops)).toThrow(`Symlink source path: web/src/${name}`)
    }
    expect(() => assertSourcePath(source, 'web/src/nested')).toThrow('Not a source file')
    expect(() => assertSourcePath(source, 'web/src/deleted.vue')).toThrow('deleted.vue')
  }))

  it('preserves TabBar coverage while admitting only the exact switch owner/state IDs', () => {
    assertCoverage(inventory)
    const tab = inventory.find((entry) => entry.id === 'components-tab-bar')!
    expect(Object.fromEntries(tab.states.filter((state) => state.coverage === 'covered').map((state) => [state.name, state.storyIds[0]]))).toEqual(covered)
    for (const kind of ['css', 'inline'] as const) {
      const entry = { ...tab, id: `fake-${kind}`, kind, states: [tab.states[0]] }
      expect(() => assertCoverage([entry])).toThrow('Invalid covered state')
      expect(() => assertCoverage([{ ...entry, id: tab.id }])).toThrow('Invalid covered state')
    }
    expect(() => assertCoverage([tab, tab])).toThrow(`Duplicate ID: ${tab.id}`)
    expect(() => assertCoverage([{ ...tab, states: [{ ...tab.states[0], coverage: 'uncovered' }] }])).toThrow('Noncovered state')
    const entry = inventory.find((row) => row.id === 'components-settings-settings-switch')!
    expect(Object.fromEntries(entry.states.filter((state) => state.coverage === 'covered').map((state) => [state.name, state.storyIds]))).toEqual(switchCovered)
    expect(entry.consumers).toEqual(['web/src/components/settings/SettingsVaultCheck.vue', 'web/src/components/settings/SettingsRestartResume.vue'])
    assertCoverage([tab, entry])
    for (const candidate of [tab, entry]) {
      const state = candidate.states.find((state) => state.coverage === 'covered')!
      for (const storyIds of [[...state.storyIds, 'settings-switch--busy'], ['settings-switch--docs'], ['settings-switch--nonexistent'], ['foundation-tab-bar--counts']]) {
        expect(() => assertCoverage([{ ...candidate, states: [{ ...state, storyIds }] }])).toThrow('Invalid covered state')
      }
      expect(() => assertCoverage([{ ...candidate, owner: { ...candidate.owner, path: 'web/src/components/Other.vue' } }])).toThrow('Invalid covered state')
      expect(() => assertCoverage([{ ...candidate, id: 'unrelated-owner' }])).toThrow('Invalid covered state')
    }
  })

  it('documents the completion sheet rather than completion history', () => {
    const entry = inventory.find((row) => row.id === 'task-completion')!
    expect(entry.owner).toEqual({
      path: 'web/src/components/TaskWorkspaceSurface.vue',
      anchor: '.task-resolution-sheet',
    })
    const source = readFileSync(resolve(root, entry.owner.path), 'utf8')
    const branchNotes: Record<string, string[]> = {
      default: ['resolutionOpen && resolutionTask'],
      complete: ["resolutionMode === 'complete'", 'Mark done'],
      edit: ["resolutionMode.value === 'edit'", 'Save resolution'],
      'review-ready': ['isReviewReady(resolutionTask)', 'DecisionReason', 'resolutionReason', 'Approve done'],
      pending: ['resolutionBusy', 'Saving…', 'disabled'],
      'action-error': ['resolutionError', 'role="alert"', 'refreshResolutionTask'],
      draft: ['resolutionDraft', 'optional'],
      submit: ['submitResolution', 'board.update', 'board.complete'],
      cancel: ['closeResolution', 'resolutionBusy', 'without writing'],
    }
    for (const [name, symbols] of Object.entries(branchNotes)) {
      const state = entry.states.find((state) => state.name === name)
      expect(state, name).toBeDefined()
      expect(state?.coverage, name).toBe('uncovered')
      expect(state?.storyIds, name).toEqual([])
      for (const symbol of symbols) {
        expect(state?.note, `${name}: ${symbol}`).toContain(symbol)
        expect(source, symbol).toContain(symbol)
      }
    }
  })

  it('keeps mandatory inline ownership and important loading branches explicit', () => {
    const owners: Record<string, string> = {
      'global-buttons': 'styles/app.css', 'global-fields-forms': 'styles/app.css', 'global-badges': 'styles/app.css', 'global-surfaces': 'styles/app.css', 'global-page-layout': 'styles/app.css', 'global-typography': 'styles/app.css', 'global-focus': 'styles/app.css', 'global-modals': 'styles/app.css', 'global-terminal-branding': 'styles/app.css',
      'chat-panel-messages': 'components/ChatPanel.vue', 'chat-panel-composer': 'components/ChatPanel.vue', 'chat-panel-question-capability': 'components/ChatPanel.vue', 'home-intake-composer': 'components/HomeIntake.vue', 'subagent-chat-messages': 'components/SubagentChatView.vue',
      'task-board-cards': 'components/TaskWorkspaceSurface.vue', 'task-editor': 'components/TaskWorkspaceSurface.vue', 'task-completion': 'components/TaskWorkspaceSurface.vue', 'task-delegation': 'components/TaskWorkspaceSurface.vue', 'task-ordering': 'components/TaskWorkspaceSurface.vue', 'file-modal-preview-editor': 'components/FileViewerModal.vue', 'pinned-file-preview-editor': 'components/PinnedFilePanel.vue', 'memory-graph': 'components/MemoryMapView.vue', 'memory-list-selection': 'components/MemoryMapView.vue',
    }
    for (const [id, path] of Object.entries(owners)) expect(inventory.find((entry) => entry.id === id)?.owner.path).toBe(`web/src/${path}`)
    for (const id of ['components-skeleton-loader', 'components-sidebar-list-skeleton', 'components-chat-pane-skeleton', 'components-chat-panel', 'components-task-workspace-surface']) expect(inventory.find((entry) => entry.id === id)?.loading.status).toBe('known')
    const task = inventory.find((entry) => entry.id === 'components-task-workspace-surface')!
    for (const name of ['firstLoad', 'loadFailed', 'loadStale', 'filteredEmpty', 'boardEmpty', 'ordering']) expect(task.states.some((state) => state.name === name && state.coverage === 'uncovered')).toBe(true)
  })
})

describe('filtering and isolation', () => {
  it('searches all fields with trimmed case-insensitive AND terms in stable order', () => {
    for (const query of ['  CHAT   COMPOSER ', 'TaskWorkspaceSurface', 'settings/SettingsMcpServers', 'onboarding/OnboardingButton', 'btn-primary']) expect(filterInventory(inventory, { ...defaults, query }).length, query).toBeGreaterThan(0)
    const row = inventory.find((entry) => entry.id === 'chat-panel-composer')!
    for (const query of [row.owner.path, row.owner.anchor, row.sources[0], row.variants[0], row.loading.detail, row.migration.note]) expect(filterInventory([row], { ...defaults, query })).toEqual([row])
    const consumer = inventory.find((entry) => entry.consumers.length)!
    expect(filterInventory([consumer], { ...defaults, query: consumer.consumers[0] })).toEqual([consumer])
    expect(filterInventory(inventory, { ...defaults, query: 'zzzz no such owner' })).toEqual([])
    expect(filterInventory(inventory, defaults)).toEqual(inventory)
  })
  it('intersects exact selectors and preserves mixed coverage without mutating input', () => {
    const base = inventory.find((entry) => entry.id === 'components-tab-bar')!
    const mixed: InventoryEntry = { ...base, states: [...base.states, { name: 'other', coverage: 'uncovered', storyIds: [], note: 'known' }, { name: 'later', coverage: 'unknown', storyIds: [], note: 'unassessed' }] }
    const entries = [mixed, inventory[0]]; const before = JSON.stringify(entries)
    for (const coverage of ['covered', 'uncovered', 'unknown'] as const) expect(filterInventory(entries, { ...defaults, query: 'tab bar', family: mixed.family, migration: 'retained', coverage })).toEqual([mixed])
    expect(filterInventory(entries, { ...defaults, family: 'missing' })).toEqual([])
    expect(filterInventory(entries, { ...defaults, family: mixed.family.toUpperCase() })).toEqual([])
    expect(filterInventory(entries, { ...defaults, migration: 'unassessed', coverage: 'covered' })).toEqual([])
    expect(filterInventory(entries, { ...defaults, query: 'missing', family: mixed.family, migration: 'retained', coverage: 'covered' })).toEqual([])
    expect(filterInventory(entries, { ...defaults, coverage: 'not-applicable' })).toEqual([mixed])
    expect(JSON.stringify(entries)).toBe(before)
  })
  it('searches each documented field independently and keeps input order', () => {
    const row: InventoryEntry = {
      id: 'identity', name: 'displayname', family: 'grouping', kind: 'composition',
      owner: { path: 'ownerpath', anchor: 'selector' }, sources: ['supportpath'], consumers: ['consumerpath'],
      variants: ['option'], variantNotes: 'variantdetail',
      states: [{ name: 'baseline', coverage: 'unknown', storyIds: [], note: 'statedetail' }],
      loading: { status: 'unknown', detail: 'loadingdetail' },
      migration: { status: 'unassessed', note: 'migrationdetail' }, rationale: 'compositionreason',
    }
    for (const query of ['identity', 'displayname', 'grouping', 'composition', 'ownerpath', 'selector', 'supportpath', 'consumerpath', 'option', 'variantdetail', 'baseline', 'statedetail', 'loadingdetail', 'unassessed', 'migrationdetail', 'compositionreason']) {
      expect(filterInventory([row], { ...defaults, query }), query).toEqual([row])
    }
    const entries = [{ ...row, id: 'last' }, { ...row, id: 'first' }]
    expect(filterInventory(entries, { ...defaults, query: '  DISPLAYNAME  SELECTOR ', family: row.family, coverage: 'unknown', migration: 'unassessed' })).toEqual(entries)
    expect(filterInventory(entries, { ...defaults, query: 'displayname missing' })).toEqual([])
    expect(filterInventory(entries, { ...defaults, query: '   ' })).toEqual(entries)
  })
  it('keeps the registry pure and production sources independent of the workshop', () => {
    const source = readFileSync(join(src, 'stories/inventory.ts'), 'utf8')
    expect(source).not.toMatch(/^\s*import\b|\bimport\s*\(|\bimport\.meta/m)
    expect(source).not.toMatch(/import\.meta|readFileSync|window\.|document\./)
    for (const path of [...walk(src), ...walk(src, undefined, '.ts')]) expect(readFileSync(join(src, path), 'utf8'), path).not.toMatch(/stories\/|InventoryBrowser/)
    const story = readFileSync(join(src, 'stories/Inventory.stories.ts'), 'utf8')
    expect(story).toContain("id: 'inventory'")
    expect(story).not.toContain('autodocs')
    const harness = readFileSync(join(src, 'stories/InventoryBrowser.vue'), 'utf8')
    const imports = [...harness.matchAll(/from ['"]([^'"]+)['"]/g)].map((match) => match[1])
    expect(imports.every((path) => ['vue', './inventory'].includes(path))).toBe(true)
    expect(harness).not.toMatch(/fetch\(|new WebSocket|setProperty|localStorage|sessionStorage/)
    expect(harness).toContain('./?path=/story/')
    expect(harness).toContain('target="_top"')
  })
})
