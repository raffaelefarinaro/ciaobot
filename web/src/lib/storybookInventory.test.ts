import { describe, expect, it } from 'vitest'
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { Dirent } from 'node:fs'
import { parse as parseSfc } from '@vue/compiler-sfc'
import ts from 'typescript'
import { filterInventory, inventory } from '../stories/inventory'
import type { InventoryEntry, InventoryFilters } from '../stories/inventory'
import { checkStorybookIndex } from '../../scripts/check-storybook-index'
import { assertStoryCoverage, importSpecifiers, parseStoryFile, readStoryIndex, storyFiles } from '../stories/storyIndex'
import type { StoryClaim, StoryClaimState, StoryCoverageOptions, StoryModule } from '../stories/storyIndex'

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

/** Direct consumers of every production SFC: the production `.vue` and `.ts`
 * files that import it, statically (type imports included) or as a literal
 * dynamic `import('...')`, relative or `@/`, as repo paths. */
function staticSpecifiers(code: string): string[] {
  const specifiers: string[] = []
  const visit = (node: ts.Node): void => {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) specifiers.push(node.moduleSpecifier.text)
    ts.forEachChild(node, visit)
  }
  visit(ts.createSourceFile('consumer.ts', code, ts.ScriptTarget.Latest, false, ts.ScriptKind.TS))
  return specifiers
}
function resolveSfcImport(from: string, specifier: string, base: string, prefix: string): string | null {
  if (!specifier.startsWith('.') && !specifier.startsWith('@/')) return null
  const target = specifier.startsWith('@/') ? join(base, specifier.slice(2)) : resolve(dirname(from), specifier)
  return target.endsWith('.vue') && existsSync(target) ? `${prefix}${posix(relative(base, target))}` : null
}
function sfcConsumers(base: string, prefix = 'web/src/'): Map<string, string[]> {
  const consumers = new Map<string, string[]>()
  for (const path of [...walk(base), ...walk(base, undefined, '.ts')]) {
    const absolute = join(base, path)
    const code = readFileSync(absolute, 'utf8')
    const script = path.endsWith('.vue')
      ? [parseSfc(code).descriptor.script?.content, parseSfc(code).descriptor.scriptSetup?.content].filter(Boolean).join('\n')
      : code
    const dynamic = [...script.matchAll(/\bimport\(\s*['"]([^'"]+)['"]\s*\)/g)].map((match) => match[1])
    for (const specifier of [...staticSpecifiers(script), ...dynamic]) {
      const target = resolveSfcImport(absolute, specifier, base, prefix)
      if (target === null || target === `${prefix}${path}`) continue
      consumers.set(target, [...new Set([...(consumers.get(target) ?? []), `${prefix}${path}`])].sort())
    }
  }
  return consumers
}
function assertConsumers(entries: InventoryEntry[], derived: Map<string, string[]>) {
  const drift: string[] = []
  for (const entry of entries) {
    if (entry.kind !== 'component' && entry.kind !== 'composition') continue
    const actual = derived.get(entry.owner.path) ?? []
    if (JSON.stringify([...entry.consumers].sort()) !== JSON.stringify(actual)) drift.push(`${entry.id}: listed ${JSON.stringify(entry.consumers)}, imported by ${JSON.stringify(actual)}`)
  }
  if (drift.length) throw new Error(`Consumer drift:\n${drift.join('\n')}`)
}

// The CSF story index of the workshop (#3263): the story files are the single
// source of story IDs. The guard derives them with Storybook's own CSF parser
// and the pure coverage guard (see web/src/stories/storyIndex.ts) instead of
// hand-written per-owner maps.
const storyIndex = readStoryIndex()

/** Fresh claim rows for the pure guard: entry ID → the row's owner and states. */
function claimRows(): Map<string, StoryClaim> {
  return new Map(inventory.map((entry) => [entry.id, { owner: entry.owner.path, states: entry.states.map((state) => ({ ...state, storyIds: [...state.storyIds] })) }]))
}

/** The workshop's own page story, which no row claims: the guard's only exemption. */
const exemptStoryIds = ['inventory--all']

function guarded(claims: ReadonlyMap<string, StoryClaim>, options: StoryCoverageOptions = {}): void {
  assertStoryCoverage(storyIndex.modules, claims, { hosts: storyIndex.hosts, exemptStoryIds, ...options })
}

function statesOf(claims: ReadonlyMap<string, StoryClaim>, entryId: string): StoryClaimState[] {
  const states = claims.get(entryId)?.states
  if (!states) throw new Error(`No claim row: ${entryId}`)
  return states
}

/** Row kinds whose covered states may claim stories. A composition row is a view
 * composed of several components; its stories render the real composition. */
const storyClaimingKinds: InventoryEntry['kind'][] = ['component', 'composition', 'inline']

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
    if (new Set(entry.states.map((state) => state.name)).size !== entry.states.length) throw new Error(`Duplicate state: ${entry.id}`)
    for (const state of entry.states) {
      expect(state.name.trim()).not.toBe(''); expect(state.note.trim()).not.toBe('')
      if (state.coverage !== 'covered' && state.storyIds.length) throw new Error(`Noncovered state has story IDs: ${entry.id}/${state.name}`)
      if (state.coverage === 'covered' && !storyClaimingKinds.includes(entry.kind)) throw new Error(`Invalid covered state (kind ${entry.kind}): ${entry.id}/${state.name} may not claim story coverage`)
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

  it('lists exactly the SFCs that import each component or composition owner', () => {
    assertConsumers(inventory, sfcConsumers(src))
  })

  it('derives consumers from fixture imports and fails on a missing or extra consumer', () => fixture((base) => {
    const write = (name: string, body: string) => { file(base, name); writeFileSync(join(base, name), body) }
    write('components/Owner.vue', '<script setup lang="ts"></script>')
    write('components/Host.vue', "<script setup lang=\"ts\">\nimport Owner from './Owner.vue'\n</script>")
    const entry = (consumers: string[]) => [{ ...structuredClone(inventory[0]), kind: 'component', owner: { path: 'web/src/components/Owner.vue', anchor: 'x' }, consumers }] as InventoryEntry[]
    const derived = () => sfcConsumers(base)
    expect(() => assertConsumers(entry(['web/src/components/Host.vue']), derived())).not.toThrow()
    expect(() => assertConsumers(entry([]), derived())).toThrow()
    write('components/Other.vue', "<script setup lang=\"ts\">\nimport Owner from '@/components/Owner.vue'\n</script>")
    expect(() => assertConsumers(entry(['web/src/components/Host.vue']), derived())).toThrow()
    write('lazy.ts', "export const load = () => import('./components/Owner.vue')\nimport type Typed from './components/Owner.vue'\n")
    expect(derived().get('web/src/components/Owner.vue')).toEqual(['web/src/components/Host.vue', 'web/src/components/Other.vue', 'web/src/lazy.ts'])
  }))

  it('keeps inventory shape, with story IDs on covered states only', () => {
    assertCoverage(inventory)
  })

  it('claims story coverage only from component, inline and composition rows', () => {
    const row = inventory.find((entry) => entry.id === 'components-confirm-dialog')!
    const css = { ...structuredClone(row), id: 'fake-css', kind: 'css' as const, rationale: 'Not a component owner' }
    expect(() => assertCoverage([css]), 'Invalid covered state (kind css)').toThrow('Invalid covered state (kind css): fake-css/default')
    expect(() => assertCoverage([structuredClone(row)])).not.toThrow()
    // A composition row may claim its own rendered stories, like a component row.
    const composition = { ...structuredClone(row), id: 'fake-composition', kind: 'composition' as const, rationale: 'A view composed of several components' }
    expect(() => assertCoverage([composition])).not.toThrow()
    // The real composition row that claims its own rendered stories (MemoryMapView).
    const memoryMap = inventory.find((entry) => entry.id === 'components-memory-map-view')!
    expect(memoryMap.kind).toBe('composition')
    expect(memoryMap.states.some((state) => state.coverage === 'covered' && state.storyIds.length > 0)).toBe(true)
    // An inline family renders through its owner SFC (task sheets, the B12
    // slice): its row is the one that can claim those states.
    const inlineRow = inventory.find((entry) => entry.id === 'task-completion')!
    expect(() => assertCoverage([structuredClone(inlineRow)])).not.toThrow()
  })

  describe('the CSF-derived story coverage guard', () => {
    it('runs the real story index against the real registry, so it cannot pass vacuously', () => {
      expect(storyIndex.modules.length).toBeGreaterThan(0)
      expect(claimRows().size).toBeGreaterThan(0)
      for (const id of exemptStoryIds) {
        expect(storyIndex.modules.flatMap((module) => module.storyIds), id).toContain(id)
      }
      expect(() => guarded(claimRows())).not.toThrow()
    })

    it('fails when a covered state names a story ID no story file declares', () => {
      const claims = claimRows()
      statesOf(claims, 'components-settings-settings-switch')[0].storyIds.push('settings-switch--ghost')
      expect(() => guarded(claims)).toThrow('settings-switch--ghost is not a story in the story files')
    })

    it('fails when a story is claimed twice, across rows or inside one state', () => {
      const claims = claimRows()
      statesOf(claims, 'components-prompt-dialog').find((state) => state.name === 'empty')!.storyIds.push('settings-switch--on')
      expect(() => guarded(claims)).toThrow('Story settings-switch--on is claimed by both components-settings-settings-switch/default and components-prompt-dialog/empty')

      const repeated = claimRows()
      const narrow = statesOf(repeated, 'components-settings-settings-switch').find((state) => state.name === 'narrow')!
      narrow.storyIds.push(narrow.storyIds[0])
      expect(() => guarded(repeated)).toThrow(/Story settings-switch--narrow is claimed by both .* and components-settings-settings-switch\/narrow/)
    })

    it('fails when a story file is left unclaimed by every covered state', () => {
      const claims = claimRows()
      const states = statesOf(claims, 'components-settings-settings-switch')
      states[0].storyIds = states[0].storyIds.filter((id) => id !== 'settings-switch--on')
      expect(() => guarded(claims)).toThrow('Orphan story: settings-switch--on')
    })

    it('fails a confirm-dialog state that claims a prompt-dialog story, and the other way round', () => {
      const claims = claimRows()
      const confirm = statesOf(claims, 'components-confirm-dialog')
      const prompt = statesOf(claims, 'components-prompt-dialog')
      confirm.find((state) => state.name === 'default')!.storyIds = ['prompt-dialog--empty']
      prompt.find((state) => state.name === 'empty')!.storyIds = []
      expect(() => guarded(claims)).toThrow(/Story web\/src\/stories\/PromptDialog\.stories\.ts \(prompt-dialog\) does not render web\/src\/components\/ConfirmDialog\.vue/)

      const reversed = claimRows()
      const confirmReversed = statesOf(reversed, 'components-confirm-dialog')
      const promptReversed = statesOf(reversed, 'components-prompt-dialog')
      promptReversed.find((state) => state.name === 'empty')!.storyIds = ['confirm-dialog--normal']
      confirmReversed.find((state) => state.name === 'default')!.storyIds = []
      expect(() => guarded(reversed)).toThrow(/Story web\/src\/stories\/ConfirmDialog\.stories\.ts \(confirm-dialog\) does not render web\/src\/components\/PromptDialog\.vue/)
    })

    it('admits a host-based story only when the host renders the owner and the story self-identifies with it', () => {
      const story: StoryModule = {
        file: 'web/src/stories/Fake.stories.ts', id: 'fake-owner', title: 'Fakes/Fake owner', storyIds: ['fake-owner--default'],
        renders: [], hosts: ['web/src/stories/FakeHost.vue'],
      }
      const claims = new Map<string, StoryClaim>([['fake', { owner: 'web/src/components/FakeOwner.vue', states: [{ name: 'default', coverage: 'covered', storyIds: ['fake-owner--default'] }] }]])
      const hosts = new Map([['web/src/stories/FakeHost.vue', ['web/src/components/FakeOwner.vue']]])
      expect(() => assertStoryCoverage([story], claims, { hosts })).not.toThrow()
      const alien = { ...story, id: 'fake-alien' }
      expect(() => assertStoryCoverage([alien], claims, { hosts })).toThrow(/Story web\/src\/stories\/Fake\.stories\.ts \(fake-alien\) does not render web\/src\/components\/FakeOwner\.vue/)
      const wronglyTitled = { ...story, title: 'Fakes/Unrelated' }
      expect(() => assertStoryCoverage([wronglyTitled], claims, { hosts })).toThrow('does not render')
      const hostWithoutOwner = new Map([['web/src/stories/FakeHost.vue', ['web/src/components/Other.vue']]])
      expect(() => assertStoryCoverage([story], claims, { hosts: hostWithoutOwner })).toThrow('does not render')
      expect(() => assertStoryCoverage([story], claims)).toThrow('does not render')
    })

    it('fails when a noncovered state claims stories', () => {
      const claims = claimRows()
      statesOf(claims, 'components-settings-settings-switch')[0].coverage = 'unknown'
      expect(() => guarded(claims)).toThrow('Noncovered state has story IDs: components-settings-settings-switch/default')
    })
  })

  describe('story file discovery and import parsing', () => {
    const writeHollowStory = (path: string) => writeFileSync(path, [
      "import type { Meta, StoryObj } from '@storybook/vue3-vite'",
      "const inString = \"import Ghost from '@/components/DecisionReason.vue'\"",
      "const inTemplate = `import Ghost from '@/components/DecisionReason.vue'`",
      "// import Ghost from '@/components/DecisionReason.vue'",
      "/* import Ghost from '@/components/DecisionReason.vue' */",
      "import type Ghost from '@/components/DecisionReason.vue'",
      "const meta = { id: 'fake-hollow', title: 'Fakes/Hollow' } satisfies Meta<{}>",
      'export default meta',
      'type Story = StoryObj<typeof meta>',
      'export const Default: Story = {}',
    ].join('\n'))
    const writeDirectStory = (path: string) => { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, [
      "import type { Meta, StoryObj } from '@storybook/vue3-vite'",
      "import DecisionReason from '@/components/DecisionReason.vue'",
      "const meta = { id: 'fake-direct', title: 'Fakes/Direct', component: DecisionReason } satisfies Meta<typeof DecisionReason>",
      'export default meta',
      'type Story = StoryObj<typeof meta>',
      'export const Default: Story = {}',
    ].join('\n')) }
    const directOwnerClaim: ReadonlyMap<string, StoryClaim> = new Map([['fake', {
      owner: 'web/src/components/DecisionReason.vue',
      states: [{ name: 'default', coverage: 'covered', storyIds: ['fake-direct--default'] }],
    }]])

    it('walks the storybook glob recursively and detects a nested story file', () => fixture((base) => {
      writeHollowStory(join(base, 'Hollow.stories.ts'))
      writeDirectStory(join(base, 'nested', 'Direct.stories.ts'))
      expect(storyFiles(base)).toEqual(['Hollow.stories.ts', 'nested/Direct.stories.ts'])
      expect(readStoryIndex(base).modules.flatMap((module) => module.storyIds)).toEqual(['fake-hollow--default', 'fake-direct--default'])
      expect(readFileSync(join(src, '../.storybook/main.ts'), 'utf8')).toContain("stories: ['../src/stories/**/*.stories.ts']")
    }))

    it('parses import specifiers from the compiler AST: comments, string literals and type-only imports are not imports', () => {
      const path = '@/components/DecisionReason.vue'
      expect(importSpecifiers([
        `// import LineComment from '${path}'`,
        `/* import BlockComment from '${path}' */`,
        `const inString = "import InString from '${path}'"`,
        "const inTemplate = `import InTemplate from '" + path + "'`",
        `import type TypeOnlyValue from '${path}'`,
        `import type { TypeOnlyNamed } from '${path}'`,
        `import { value, type MaybeType } from '${path}'`,
        `import Default from '${path}'`,
        `import { first, second } from '${path}'`,
      ].join('\n'))).toEqual([path, path, path])
      expect(importSpecifiers("export { Value } from '" + path + "'")).toEqual([path])
      expect(importSpecifiers(`SideEffect('${path}')\nimport SideEffect from '${path}'`)).toEqual([path])
    })

    it('keeps commented, string-literal and type-only imports from claiming an owner', () => fixture((base) => {
      const path = join(base, 'Hollow.stories.ts')
      writeHollowStory(path)
      const module = parseStoryFile(path)
      expect(module.renders).toEqual([])
      expect(module.hosts).toEqual([])
      const claims = new Map<string, StoryClaim>([['fake', { owner: 'web/src/components/DecisionReason.vue', states: [{ name: 'default', coverage: 'covered', storyIds: ['fake-hollow--default'] }] }]])
      expect(() => assertStoryCoverage([module], claims)).toThrow('(fake-hollow) does not render web/src/components/DecisionReason.vue')
    }))

    it('renders a nested story file through its valued direct import', () => fixture((base) => {
      writeDirectStory(join(base, 'nested', 'Direct.stories.ts'))
      const module = parseStoryFile(join(base, 'nested', 'Direct.stories.ts'))
      expect(module.renders).toEqual(['web/src/components/DecisionReason.vue'])
      expect(module.hosts).toEqual([])
      expect(() => assertStoryCoverage([module], directOwnerClaim)).not.toThrow()
    }))
  })

  it('checks built story existence and type independently of owner policy', () => {
    const entry = structuredClone(inventory.find((entry) => entry.id === 'components-sidebar-row')!)
    entry.states = [entry.states[0]]
    const id = entry.states[0].storyIds[0]
    expect(() => checkStorybookIndex([entry], { [id]: { type: 'story' } })).not.toThrow()
    for (const entries of [{}, { [id]: { type: 'docs' } }, { [id]: {} }]) {
      expect(() => checkStorybookIndex([entry], entries)).toThrow(`SidebarRow.vue/default: invalid or missing story ${id}`)
    }
    for (const bad of ['', 'foundation-sidebar-row--docs', 'inventory--all']) {
      entry.states[0].storyIds = [bad]
      expect(() => checkStorybookIndex([entry], { [bad]: { type: 'story' } })).toThrow('invalid or missing story')
    }
    entry.states[0].storyIds = []
    expect(() => checkStorybookIndex([entry], {})).toThrow('covered state has no story IDs')
    entry.states[0].coverage = 'uncovered'
    entry.states[0].storyIds = [id]
    expect(() => checkStorybookIndex([entry], { [id]: { type: 'story' } })).toThrow('noncovered state has story IDs')
    const stories = Object.fromEntries(inventory.flatMap((entry) => entry.states.flatMap((state) => state.storyIds.map((id) => [id, { type: 'story' }]))))
    expect(() => checkStorybookIndex(inventory, stories)).not.toThrow()
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
      // The B12 slice renders one story per documented state and claims it:
      // the coverage pins track the row now instead of the pre-slice registry.
      expect(state?.coverage, name).toBe('covered')
      expect(state?.storyIds, name).toEqual([`task-completion--${name}`])
      for (const symbol of symbols) {
        expect(state?.note, `${name}: ${symbol}`).toContain(symbol)
        expect(source, symbol).toContain(symbol)
      }
    }
  })

  it('keeps mandatory inline ownership and important loading branches explicit', () => {
    const owners: Record<string, string> = {
      'global-buttons': 'styles/app.css', 'global-fields-forms': 'styles/app.css', 'global-badges': 'styles/app.css', 'global-surfaces': 'styles/app.css', 'global-page-layout': 'styles/app.css', 'global-typography': 'styles/app.css', 'global-focus': 'styles/app.css', 'global-modals': 'styles/app.css', 'global-terminal-branding': 'styles/app.css',
      'chat-panel-messages': 'components/ChatPanel.vue', 'chat-panel-composer': 'components/ChatPanel.vue', 'chat-panel-question-capability': 'components/ChatPanel.vue', 'home-intake-composer': 'components/HomeIntake.vue',
      'task-board-cards': 'components/TaskWorkspaceSurface.vue', 'task-editor': 'components/TaskWorkspaceSurface.vue', 'task-completion': 'components/TaskWorkspaceSurface.vue', 'task-delegation': 'components/TaskWorkspaceSurface.vue', 'task-ordering': 'components/TaskWorkspaceSurface.vue', 'file-modal-preview-editor': 'components/FileViewerModal.vue', 'pinned-file-preview-editor': 'components/PinnedFilePanel.vue', 'memory-graph': 'components/MemoryMapView.vue', 'memory-list-selection': 'components/MemoryMapView.vue',
    }
    for (const [id, path] of Object.entries(owners)) expect(inventory.find((entry) => entry.id === id)?.owner.path).toBe(`web/src/${path}`)
    for (const id of ['components-skeleton-loader', 'components-sidebar-list-skeleton', 'components-chat-pane-skeleton', 'components-chat-panel', 'components-task-workspace-surface']) expect(inventory.find((entry) => entry.id === id)?.loading.status).toBe('known')
    const task = inventory.find((entry) => entry.id === 'components-task-workspace-surface')!
    // The board states the TaskWorkspaceSurface stories claim (#3296): each one
    // is covered by exactly its own story, and the other surface states stay uncovered.
    const claimed: Record<string, string> = { firstLoad: 'first-load', loadFailed: 'load-failed', loadStale: 'load-stale', filteredEmpty: 'filtered-empty', boardEmpty: 'board-empty', ordering: 'ordering' }
    for (const [name, story] of Object.entries(claimed)) expect(task.states.find((state) => state.name === name)).toMatchObject({ coverage: 'covered', storyIds: [`task-workspace-surface--${story}`] })
    expect(task.states.find((state) => state.name === 'missingTaskId')?.coverage).toBe('uncovered')
  })
})

describe('filtering and isolation', () => {
  it('searches all fields with trimmed case-insensitive AND terms in stable order', () => {
    for (const query of ['  CHAT   COMPOSER ', 'TaskWorkspaceSurface', 'settings/SettingsSwitch', 'onboarding/OnboardingButton', 'btn-primary']) expect(filterInventory(inventory, { ...defaults, query }).length, query).toBeGreaterThan(0)
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
  it('keeps story files and workshop config out of the application vitest suite', () => {
    const config = readFileSync(resolve(root, 'web/vite.config.ts'), 'utf8')
    const exclude = config.match(/test:\s*\{[\s\S]*?exclude:\s*\[([^\]]*)\]/)
    expect(exclude, 'application vitest test.exclude list').not.toBeNull()
    const globs = (exclude![1].match(/'[^']+'/g) ?? []).map((glob) => glob.slice(1, -1))
    expect(globs).toContain('**/*.stories.*')
    expect(globs).toContain('.storybook/**')
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
