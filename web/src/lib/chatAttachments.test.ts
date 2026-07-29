import { describe, expect, it } from 'vitest'

import { formatAttachedFilePath, nativeAbsoluteFilePath } from './chatAttachments'

describe('nativeAbsoluteFilePath', () => {
  it('uses an absolute path exposed by a desktop webview', () => {
    const file = {
      name: 'notes.md',
      path: '/Users/ada/Project notes/notes.md',
    } as unknown as File
    expect(nativeAbsoluteFilePath(file)).toBe('/Users/ada/Project notes/notes.md')
  })

  it('rejects browser-relative paths and plain filenames', () => {
    expect(nativeAbsoluteFilePath(
      { name: 'notes.md', path: 'folder/notes.md' } as unknown as File,
    )).toBeNull()
    expect(nativeAbsoluteFilePath({ name: 'notes.md' } as unknown as File)).toBeNull()
  })
})

describe('formatAttachedFilePath', () => {
  it('keeps a path with spaces together as one prompt token', () => {
    expect(formatAttachedFilePath('/Users/ada/Project notes/notes.md'))
      .toBe('`/Users/ada/Project notes/notes.md`')
  })

  it('does not alter a filename containing a backtick', () => {
    expect(formatAttachedFilePath('/Users/ada/a`b.md'))
      .toBe('"/Users/ada/a`b.md"')
  })
})
