// Node-only helper for the PWA repository's inventory tests: it reads every
// workshop story file with Storybook's own CSF parser, so the story coverage
// guard derives its story IDs from the story files instead of hand-written
// lists. Nothing in the app imports this module; only the vitest inventory
// tests do.
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadCsf } from 'storybook/internal/csf-tools'

export interface StoryModule {
  /** Repo-relative posix path, e.g. `web/src/stories/X.stories.ts`. */
  file: string
  /** The CSF meta `id`, e.g. `settings-switch`. */
  id: string
  /** Every CSF story ID declared in the file, in declaration order. */
  storyIds: string[]
  /** Import specifiers parsed from the file by the CSF tool. */
  imports: string[]
}

const dir = fileURLToPath(new URL('.', import.meta.url))

/** Every workshop story file, as a repo-relative posix path. */
export function storyFiles(): string[] {
  return readdirSync(dir)
    .filter((name) => name.endsWith('.stories.ts'))
    .sort()
    .map((name) => `web/src/stories/${name}`)
}

/** Parse one workshop story file's CSF: its meta id, story IDs and imports. */
export function parseStoryFile(file: string): StoryModule {
  const code = readFileSync(join(dir, file.slice('web/src/stories/'.length)), 'utf8')
  const parsed = loadCsf(code, {
    fileName: file,
    makeTitle: (userTitle) => userTitle,
  }).parse()
  const storyIds = parsed.stories.map((story) => story.id)
  if (!storyIds.length) throw new Error(`Story file without stories: ${file}`)
  return { file, id: parsed.meta?.id ?? '', storyIds, imports: parsed.imports }
}

/** Parse every workshop story file. The single source of story IDs for the tests. */
export function readStoryIndex(): StoryModule[] {
  return storyFiles().map(parseStoryFile)
}
