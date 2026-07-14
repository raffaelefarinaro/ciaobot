import { describe, expect, it } from 'vitest'

import {
  buildMarkdownIndex,
  extractWikilinks,
  joinRelative,
  linkifyWikilinksInMarkdown,
  resolveWikilinkTarget,
} from './wikilinks'

const PATHS = [
  'memory-vault/work/projects/active/rossmann/README.md',
  'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
  'memory-vault/People/Mo.md',
  'memory-vault/Projects/Foo.md',
]

describe('wikilinks', () => {
  it('extracts alias and anchor forms', () => {
    expect(extractWikilinks('See [[People/Mo|Mo]] and [[Projects/Foo#Decisions]].')).toEqual([
      { ref: 'People/Mo', display: 'Mo', anchor: null },
      { ref: 'Projects/Foo', display: 'Foo', anchor: 'Decisions' },
    ])
  })

  it('joinRelative collapses dot segments', () => {
    expect(joinRelative('memory-vault/work/a/', '../b/note.md')).toBe('memory-vault/work/b/note.md')
  })

  it('resolves same-folder README links', () => {
    const index = buildMarkdownIndex(PATHS)
    const pathSet = new Set(PATHS)
    const current = 'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md'
    const target = resolveWikilinkTarget('README', current, index, pathSet)
    expect(target).toBe('memory-vault/work/projects/active/rossmann/README.md')
  })

  it('resolves vault-wide path links', () => {
    const index = buildMarkdownIndex(PATHS)
    const pathSet = new Set(PATHS)
    const current = 'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md'
    expect(resolveWikilinkTarget('People/Mo', current, index, pathSet)).toBe('memory-vault/People/Mo.md')
  })

  it('resolves unique bare stem links', () => {
    const index = buildMarkdownIndex(PATHS)
    const pathSet = new Set(PATHS)
    const current = 'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md'
    expect(resolveWikilinkTarget('Mo', current, index, pathSet)).toBe('memory-vault/People/Mo.md')
  })

  it('leaves ambiguous bare stems unresolved', () => {
    const paths = [
      'memory-vault/a/README.md',
      'memory-vault/b/README.md',
    ]
    const index = buildMarkdownIndex(paths)
    const pathSet = new Set(paths)
    expect(resolveWikilinkTarget('README', 'memory-vault/other/note.md', index, pathSet)).toBeNull()
  })

  it('linkifies wikilinks but skips inline and fenced code', () => {
    const html = linkifyWikilinksInMarkdown(
      [
        'See [[README|Rossmann MVP]].',
        '',
        '```md',
        '[[Should/NotCount]]',
        '```',
        '',
        'Inline `[[Also/Skip]]` but [[People/Mo|Mo]] works.',
      ].join('\n'),
      'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
      PATHS,
    )

    expect(html).toContain('class="file-link wikilink"')
    expect(html).toContain('data-file-path="memory-vault/work/projects/active/rossmann/README.md"')
    expect(html).toContain('>Rossmann MVP</a>')
    expect(html).toContain('[[Should/NotCount]]')
    expect(html).toContain('`[[Also/Skip]]`')
    expect(html).toContain('data-file-path="memory-vault/People/Mo.md"')
  })

  it('marks unresolved targets', () => {
    const html = linkifyWikilinksInMarkdown(
      'Missing [[Nowhere/Note]].',
      'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
      PATHS,
    )
    expect(html).toContain('class="wikilink-unresolved"')
    expect(html).toContain('title="Nowhere/Note"')
  })
})
