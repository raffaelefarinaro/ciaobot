// Node-only helper for the PWA repository's inventory tests: it reads every
// workshop story file with Storybook's own CSF parser, so the story coverage
// guard derives its story IDs from the story files instead of hand-written
// lists, and it implements that guard itself as a pure function on a story
// list and a claim map. Nothing in the app imports this module; only the
// vitest inventory tests do.
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { loadCsf } from 'storybook/internal/csf-tools'

export interface StoryModule {
  /** Repo-relative posix path, e.g. `web/src/stories/X.stories.ts`. */
  file: string
  /** The CSF meta `id`, e.g. `confirm-dialog`. */
  id: string
  /** The CSF meta `title`, e.g. `Dialogs/Confirm dialog`. */
  title: string
  /** Every CSF story ID declared in the file, in declaration order. */
  storyIds: string[]
  /** Repo-relative paths the story file's own import list resolves to outside `web/src/stories/`. */
  renders: string[]
  /** Repo-relative paths of the stories-local host files the story file itself imports (never `.stories.ts`). */
  hosts: string[]
}

export interface StoryIndex {
  /** Every story file, parsed; the single source of story IDs for the tests. */
  modules: StoryModule[]
  /** Host file repo path → the production paths the host's own import list resolves to. */
  hosts: Map<string, string[]>
}

export interface StoryClaimState {
  name: string
  coverage: string
  storyIds: string[]
}

export interface StoryClaim {
  owner: string
  states: StoryClaimState[]
}

export interface StoryCoverageOptions {
  /** Host file repo path → production paths the host renders, from `readStoryIndex`. */
  hosts?: ReadonlyMap<string, readonly string[]>
  /** Story IDs of the workshop's own pages, which no inventory row claims. */
  exemptStoryIds?: readonly string[]
}

const workshopDir = fileURLToPath(new URL('.', import.meta.url))
const repoRoot = resolve(workshopDir, '..', '..', '..')
const webSrc = join(repoRoot, 'web', 'src')
const storiesPrefix = 'web/src/stories/'
const posixPath = (value: string): string => value.split(sep).join('/')
const toRepoPath = (absolute: string): string => posixPath(relative(repoRoot, absolute))

/** The `<script>` block contents of a Vue single-file component, concatenated. */
function scriptContentOfVue(code: string): string {
  return [...code.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map((match) => match[1]).join('\n')
}

/** The import specifiers of a TypeScript source, from the TypeScript compiler
 * API: comments are not import statements and string literals that merely
 * mention imports are ordinary string literals, while `import type` and
 * `export type` declarations import no runtime value — so none of them can
 * satisfy the story-owner guard (c). */
export function importSpecifiers(code: string): string[] {
  const source = ts.createSourceFile('imports.ts', code, ts.ScriptTarget.Latest, false, ts.ScriptKind.TS)
  const specifiers: string[] = []
  const visit = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      const clause = node.importClause
      const named = clause?.namedBindings && ts.isNamedImports(clause.namedBindings) ? clause.namedBindings.elements : []
      const importsValues =
        !clause?.isTypeOnly && (Boolean(clause?.name) || named.length === 0 || named.some((element) => !element.isTypeOnly))
      if (importsValues) specifiers.push(node.moduleSpecifier.text)
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier) && !node.isTypeOnly) {
      specifiers.push(node.moduleSpecifier.text)
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  return specifiers
}

/**
 * Paths of the story files under `baseDir`, relative to it, posix and sorted.
 * Recursive, exactly matching the `main.ts` storybook glob at any depth:
 * `../src/stories` with any number of directories, then a `.stories.ts` file.
 */
export function storyFiles(baseDir: string = workshopDir): string[] {
  const files: string[] = []
  const visit = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) continue
      const path = join(dir, entry.name)
      if (entry.isDirectory()) visit(path)
      else if (entry.name.endsWith('.stories.ts')) files.push(posixPath(relative(baseDir, path)))
    }
  }
  visit(baseDir)
  return files.sort()
}

/** Resolve one relative or `@/` import specifier to a repo-relative posix path,
 * or null for bare specifiers and unresolvable targets. */
function resolveImport(fromFile: string, specifier: string): string | null {
  if (!specifier.startsWith('.') && !specifier.startsWith('@/')) return null
  const base = specifier.startsWith('@/') ? join(webSrc, specifier.slice(2)) : resolve(dirname(fromFile), specifier)
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`, `${base}.vue`, join(base, 'index.ts')]) {
    if (existsSync(candidate)) return toRepoPath(candidate)
  }
  return null
}

function directImports(absolutePath: string, source: string): string[] {
  return [...new Set(importSpecifiers(source).map((specifier) => resolveImport(absolutePath, specifier)).filter((target) => target !== null))]
}

/** The direct production imports of a stories-local host file (`.vue` script
 * blocks or `.ts` source). One level deep: imports inside `web/src/stories/`
 * are dropped, so a host never inherits reach from other stories-local files. */
function hostRenders(absolutePath: string): string[] {
  const code = readFileSync(absolutePath, 'utf8')
  const source = absolutePath.endsWith('.vue') ? scriptContentOfVue(code) : code
  return directImports(absolutePath, source).filter((target) => !target.startsWith(storiesPrefix))
}

/** Parse one story file's CSF with Storybook's own parser: its meta id and
 * title, story IDs, and the import specifiers of the file itself parsed with
 * the TypeScript compiler, resolved into production `renders` and
 * stories-local `hosts`. */
export function parseStoryFile(absolutePath: string): StoryModule {
  const code = readFileSync(absolutePath, 'utf8')
  const parsed = loadCsf(code, {
    fileName: absolutePath,
    makeTitle: (userTitle) => userTitle,
  }).parse()
  const storyIds = parsed.stories.map((story) => story.id)
  if (!storyIds.length) throw new Error(`Story file without stories: ${absolutePath}`)
  const direct = directImports(absolutePath, code)
  return {
    file: toRepoPath(absolutePath),
    id: parsed.meta?.id ?? '',
    title: parsed.meta?.title ?? '',
    storyIds,
    renders: direct.filter((target) => !target.startsWith(storiesPrefix) && !target.endsWith('.stories.ts')),
    hosts: direct.filter((target) => target.startsWith(storiesPrefix) && !target.endsWith('.stories.ts')),
  }
}

/** Parse every story file under `baseDir` (the workshop's real stories
 * directory by default, a fixture directory in tests) and resolve the hosts
 * those story files import. The single source of story IDs for the tests. */
export function readStoryIndex(baseDir: string = workshopDir): StoryIndex {
  const modules = storyFiles(baseDir).map((file) => parseStoryFile(join(baseDir, file)))
  const hosts = new Map<string, string[]>()
  for (const module of modules) {
    for (const file of module.hosts) {
      if (!hosts.has(file)) hosts.set(file, hostRenders(resolve(repoRoot, file)))
    }
  }
  return { modules, hosts }
}

const kebabCase = (value: string): string =>
  value
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/([A-Z])([A-Z][a-z])/g, '$1-$2')
    .replace(/[^A-Za-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .toLowerCase()

/** Host-based stories prove their owner through their own CSF identity: both
 * the meta id and the last title segment must be the owner component's file
 * name in kebab case, so `confirm-dialog`/`Dialogs/Confirm dialog` matches
 * `ConfirmDialog.vue` while a prompt-dialog story can never claim it. */
function storyIdentityMatchesOwner(story: StoryModule, owner: string): boolean {
  const component = kebabCase(owner.split('/').pop()?.replace(/\.[a-z]+$/, '') ?? owner)
  return kebabCase(story.id) === component && kebabCase(story.title.split('/').pop() ?? '') === component
}

/** Guard (c). A story may claim an owner by importing it itself, or — the
 * documented exception for host-based stories — by rendering a stories-local
 * host whose own import list contains the owner, when the story's CSF
 * identity names that owner. The exception exists because
 * `DialogStoryHost.vue` imports both `ConfirmDialog.vue` and
 * `PromptDialog.vue` behind its `kind` arg, so a shared host alone cannot
 * prove which dialog a story shows: the identity check is what keeps a
 * confirm-dialog state from claiming a prompt-dialog story. */
function assertStoryRendersOwner(story: StoryModule, owner: string, hosts: ReadonlyMap<string, readonly string[]>): void {
  if (story.renders.includes(owner)) return
  for (const file of story.hosts) {
    if (hosts.get(file)?.includes(owner) && storyIdentityMatchesOwner(story, owner)) return
  }
  throw new Error(`Story ${story.file} (${story.id}) does not render ${owner}`)
}

/**
 * The CSF-derived story coverage guard (#3263): a pure, throwing function on
 * a story list and a claim map.
 * (a) every covered state's story IDs exist in the story list;
 * (b) every story ID is claimed by exactly one row state, and every story
 *     except `exemptStoryIds` (the workshop's own `inventory--all` page) is
 *     claimed by some covered state, so the guard cannot pass vacuously;
 * (c) every claimed story renders its claimed owner (see the host-based
 *     exception documented on `assertStoryRendersOwner`);
 * (d) noncovered states claim no stories.
 */
export function assertStoryCoverage(
  stories: readonly StoryModule[],
  claims: ReadonlyMap<string, StoryClaim>,
  options: StoryCoverageOptions = {},
): void {
  const storyOf = new Map<string, StoryModule>()
  const fileOf = new Map<string, string>()
  for (const story of stories) {
    for (const id of story.storyIds) {
      if (fileOf.has(id)) throw new Error(`Story ${id} is declared by both ${fileOf.get(id)} and ${story.file}`)
      storyOf.set(id, story)
      fileOf.set(id, story.file)
    }
  }
  const claimed = new Map<string, string>()
  for (const [entryId, claim] of claims) {
    for (const state of claim.states) {
      if (state.coverage !== 'covered') {
        if (state.storyIds.length) throw new Error(`Noncovered state has story IDs: ${entryId}/${state.name}`)
        continue
      }
      for (const id of state.storyIds) {
        const story = storyOf.get(id)
        if (!story) throw new Error(`${entryId}/${state.name}: ${id} is not a story in the story files`)
        if (claimed.has(id)) throw new Error(`Story ${id} is claimed by both ${claimed.get(id)} and ${entryId}/${state.name}`)
        claimed.set(id, `${entryId}/${state.name}`)
        assertStoryRendersOwner(story, claim.owner, options.hosts ?? new Map())
      }
    }
  }
  const orphans = [...storyOf.keys()].filter(
    (id) => !claimed.has(id) && !(options.exemptStoryIds ?? []).includes(id),
  )
  if (orphans.length) throw new Error(`Orphan story: ${orphans.join(', ')} is not claimed by any covered state`)
}
