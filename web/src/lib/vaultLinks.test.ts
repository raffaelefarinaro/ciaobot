import { describe, expect, it } from 'vitest'

import {
  buildMarkdownIndex,
  joinRelative,
  resolveVaultLinkTarget,
  vaultNoteRefFromHref,
} from './vaultLinks'

const PATHS = [
  'memory-vault/work/projects/active/rossmann/README.md',
  'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
  'memory-vault/People/Mo.md',
  'memory-vault/Projects/Foo.md',
]

const CURRENT = 'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md'

describe('vault links', () => {
  it('joinRelative collapses dot segments', () => {
    expect(joinRelative('memory-vault/work/a/', '../b/note.md')).toBe('memory-vault/work/b/note.md')
  })

  it('resolves same-folder README links', () => {
    const index = buildMarkdownIndex(PATHS)
    const pathSet = new Set(PATHS)
    const target = resolveVaultLinkTarget('README', CURRENT, index, pathSet)
    expect(target).toBe('memory-vault/work/projects/active/rossmann/README.md')
  })

  it('resolves vault-wide path links', () => {
    const index = buildMarkdownIndex(PATHS)
    const pathSet = new Set(PATHS)
    expect(resolveVaultLinkTarget('People/Mo', CURRENT, index, pathSet)).toBe('memory-vault/People/Mo.md')
  })

  it('resolves unique bare stem links', () => {
    const index = buildMarkdownIndex(PATHS)
    const pathSet = new Set(PATHS)
    expect(resolveVaultLinkTarget('Mo', CURRENT, index, pathSet)).toBe('memory-vault/People/Mo.md')
  })

  it('leaves ambiguous bare stems unresolved', () => {
    const paths = [
      'memory-vault/a/README.md',
      'memory-vault/b/README.md',
    ]
    const index = buildMarkdownIndex(paths)
    const pathSet = new Set(paths)
    expect(resolveVaultLinkTarget('README', 'memory-vault/other/note.md', index, pathSet)).toBeNull()
  })

  // The migrated link form is `[Mo](./People/Mo.md)` — a relative destination
  // that has to survive the trip through the ref normaliser, or every migrated
  // link resolves to nothing.
  it('resolves a relative markdown destination the way the migration writes it', () => {
    const index = buildMarkdownIndex(PATHS)
    const pathSet = new Set(PATHS)
    expect(resolveVaultLinkTarget('./README.md', CURRENT, index, pathSet))
      .toBe('memory-vault/work/projects/active/rossmann/README.md')
    expect(resolveVaultLinkTarget('../../../../People/Mo.md', CURRENT, index, pathSet))
      .toBe('memory-vault/People/Mo.md')
  })

  it('treats relative markdown destinations as vault note refs', () => {
    expect(vaultNoteRefFromHref('./People/Mo.md')).toBe('./People/Mo.md')
    expect(vaultNoteRefFromHref('../Projects/Foo.markdown')).toBe('../Projects/Foo.markdown')
    expect(vaultNoteRefFromHref('People/Mo.md')).toBe('People/Mo.md')
  })

  it('percent-decodes destinations so note names with spaces resolve', () => {
    expect(vaultNoteRefFromHref('./Shelf%20Recognition%20Spec.md')).toBe('./Shelf Recognition Spec.md')
  })

  it('drops the anchor and query so a heading link still opens the note', () => {
    expect(vaultNoteRefFromHref('./People/Mo.md#History')).toBe('./People/Mo.md')
    expect(vaultNoteRefFromHref('./People/Mo.md?v=2')).toBe('./People/Mo.md')
  })

  // Anything rejected here renders as a plain anchor, so a false positive would
  // turn a working external link into an unclickable span.
  it('rejects destinations that are not in-vault note links', () => {
    expect(vaultNoteRefFromHref('https://example.com/x.md')).toBeNull()
    expect(vaultNoteRefFromHref('mailto:a@b.com')).toBeNull()
    expect(vaultNoteRefFromHref('javascript:alert(1)')).toBeNull()
    expect(vaultNoteRefFromHref('//example.com/x.md')).toBeNull()
    expect(vaultNoteRefFromHref('/People/Mo.md')).toBeNull()
    expect(vaultNoteRefFromHref('#Decisions')).toBeNull()
    expect(vaultNoteRefFromHref('./assets/diagram.png')).toBeNull()
    expect(vaultNoteRefFromHref('')).toBeNull()
  })
})
