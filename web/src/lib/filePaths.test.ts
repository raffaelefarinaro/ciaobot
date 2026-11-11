import { describe, expect, it } from 'vitest'

import { buildKnownPathRules, isFolderLinkPath, linkifyHtml, linkifyText, looksLikeFolderPath } from './filePaths'

describe('buildKnownPathRules', () => {
  it('adds basename rules only when unique', () => {
    const rules = buildKnownPathRules([
      'memory-vault/work/foo/slide.pptx',
      'other/place/slide.pptx',
    ])
    expect(rules.some(r => r.match === 'slide.pptx')).toBe(false)
  })

  it('adds basename rule for a uniquely touched file', () => {
    const rules = buildKnownPathRules(['memory-vault/work/foo/stradivari-ondevice-slide.pptx'])
    expect(rules.some(r => r.match === 'stradivari-ondevice-slide.pptx')).toBe(true)
  })

  it('ignores bare English words that are not real paths', () => {
    const rules = buildKnownPathRules(['There', 'memory-vault/work/foo/notes.md'])
    expect(rules.some(r => r.match === 'There')).toBe(false)
    expect(linkifyText('There was an error', ['There'])).not.toContain('class="file-link"')
  })
})

describe('linkifyText', () => {
  it('linkifies pptx paths mentioned in assistant text', () => {
    const text = 'Now moved to:\nmemory-vault/work/projects/active/ai-vision-platform/stradivari-ondevice-slide.pptx'
    const html = linkifyText(text)
    expect(html).toContain('class="file-link"')
    expect(html).toContain('data-file-path="memory-vault/work/projects/active/ai-vision-platform/stradivari-ondevice-slide.pptx"')
    expect(html).toContain('stradivari-ondevice-slide.pptx</a>')
  })

  it('linkifies basename-only mentions using known touched paths', () => {
    const html = linkifyText(
      'I saved the deck as stradivari-ondevice-slide.pptx in the project folder.',
      ['memory-vault/work/projects/active/ai-vision-platform/stradivari-ondevice-slide.pptx'],
    )
    expect(html).toContain('data-file-path="memory-vault/work/projects/active/ai-vision-platform/stradivari-ondevice-slide.pptx"')
    expect(html).toContain('>stradivari-ondevice-slide.pptx</a>')
  })

  it('linkifies pdf and image paths', () => {
    const html = linkifyText('See docs/report.pdf and assets/logo.png')
    expect(html).toContain('data-file-path="docs/report.pdf"')
    expect(html).toContain('data-file-path="assets/logo.png"')
  })

  it('linkifies saved-page archive paths', () => {
    const html = linkifyText('See captures/Label-Capture.mhtml')
    expect(html).toContain('data-file-path="captures/Label-Capture.mhtml"')
  })

  it('does not linkify paths inside URLs', () => {
    const html = linkifyText('Visit https://example.com/foo/bar.md for details')
    expect(html).not.toContain('class="file-link"')
  })
})

describe('linkifyHtml', () => {
  it('linkifies paths inside markdown code spans', () => {
    const html = linkifyHtml('<p>Moved to <code>memory-vault/work/foo/slide.pptx</code></p>')
    expect(html).toContain('class="file-link"')
    expect(html).toContain('data-file-path="memory-vault/work/foo/slide.pptx"')
  })
})

describe('isFolderLinkPath', () => {
  it('claims folder-shaped relative paths', () => {
    for (const href of ['exports/sloth-v5', 'exports/', '../exports/x', 'my folder/sub', 'v1.2/notes', 'bin/run']) {
      expect(isFolderLinkPath(href), href).toBe(true)
    }
  })

  it('leaves files, known names, words, web links and absolute paths alone', () => {
    for (const href of [
      'tasks', 'docs/Makefile', 'docs/README', 'exports/v1.2', 'config/.git',
      'my.project/sub', 'example.com/pricing', 'docs.example.io/x/', '/etc/hosts',
      '#results', 'https://a.b/c', 'mailto:a@b.c', 'notes/a.md', 'a//b', '', '~/x',
      './', '../', '.',
    ]) {
      expect(isFolderLinkPath(href), href).toBe(false)
    }
  })
})

describe('looksLikeFolderPath', () => {
  it('asks the engine about folder-shaped paths', () => {
    for (const path of ['exports/sloth-v5', 'exports/', 'bin/run', '/ws/dir']) {
      expect(looksLikeFolderPath(path), path).toBe(true)
    }
  })

  it('never asks about files, known names or dot-only paths', () => {
    for (const path of ['notes/a.md', '.gitignore', 'a.md:12', 'Makefile', 'docs/README', './', '../', '.', '']) {
      expect(looksLikeFolderPath(path), path).toBe(false)
    }
  })
})
