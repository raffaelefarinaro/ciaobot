// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'

import {
  buildKnownPathRules,
  findFileMentionSpans,
  isFolderLinkPath,
  linkifyHtml,
  linkifyText,
  looksLikeFolderPath,
} from './filePaths'

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

describe('checked file mentions', () => {
  it('checked mentions require exact confirmed targets', () => {
    const empty = linkifyHtml('<p>Edited docs/a.md, see notes.md and ./x.md</p>', [], new Map())
    expect(empty).not.toContain('class="file-link"')

    const mapped = linkifyHtml('<p>Edited docs/a.md:17 now</p>', [], new Map([['docs/a.md:17', 'workspace/docs/a.md']]))
    expect(mapped).toContain('data-file-path="workspace/docs/a.md"')
    expect(mapped).toContain('data-line="17"')
    expect(mapped).toContain('>docs/a.md:17</a>')

    // A touched file is a known path, so it links even with nothing confirmed.
    const known = linkifyHtml('<p>Read src/a.ts now</p>', ['src/a.ts'], new Map())
    expect(known).toContain('data-file-path="src/a.ts"')
  })

  it('checked mentions never manufacture basename aliases', () => {
    const map = new Map([['docs/a.md', '/w/docs/a.md']])
    const html = linkifyHtml('<p>See a.md and docs/a.md</p>', [], map)
    expect(html).toContain('>docs/a.md</a>')
    expect(html).not.toContain('>a.md</a>')

    // A unique touched basename keeps its existing rule.
    const known = linkifyHtml('<p>See a.md</p>', ['memory/docs/a.md'], map)
    expect(known).toContain('data-file-path="memory/docs/a.md"')

    // A known path wins an overlap with a confirmed target.
    const overlap = linkifyHtml('<p>docs/a.md</p>', ['docs/a.md'], map)
    expect(overlap).toContain('data-file-path="docs/a.md"')
    expect(overlap).not.toContain('/w/docs/a.md')
  })

  it('whole path boundaries reject foreign partial suffixes', () => {
    for (const path of ['~/src/a.md', 'C:\\repo\\src\\a.md', 'src\\a.md', 'C:/repo/src/a.md', './src/a.md', '../src/a.md']) {
      expect(findFileMentionSpans(`see ${path} now`).map(s => s.input), path).toEqual([path])
    }
    for (const token of [
      'https://example.com/src/a.md', 'user@host:src/a.md', '\\\\server\\share\\src\\a.md',
      '//server/share/a.md', '\\\\?\\C:\\src\\a.md', 'C:src/a.md', '~other/src/a.md',
      'src/a.md:0', 'src/a.md:2147483648', 'src/a.md:017', 'src/a.md:1x', 'src/*.md', '$HOME/src/a.md',
    ]) {
      expect(findFileMentionSpans(`see ${token} now`), token).toEqual([])
      const html = linkifyHtml(`<p>${token}</p>`, [], new Map([['src/a.md', '/w/src/a.md']]))
      expect(html, token).not.toContain('class="file-link"')
    }
    expect(findFileMentionSpans('see src/a.md:17 now')).toMatchObject([{ index: 4, length: 11, input: 'src/a.md:17', line: '17' }])
  })

  it('checked nodes escape attributes and preserve decoded text', () => {
    const map = new Map([['docs/Tom & "Jerry" café.md', '/w/docs/Tom & "Jerry" café.md']])
    const html = linkifyHtml(
      '<p><code>docs/Tom &amp; "Jerry" café.md</code> <em>see</em> <a href="#">x</a> <button>docs/b.md</button></p>',
      [],
      map,
    )
    expect(html).toContain('data-file-path="/w/docs/Tom &amp; &quot;Jerry&quot; café.md"')
    expect(html).toContain('>docs/Tom &amp; "Jerry" café.md</a>')
    expect(html).toContain('<em>see</em>')
    expect(html).toContain('<a href="#">x</a>')
    expect(html).toContain('<button>docs/b.md</button>')
    expect(html.split('<a ').length - 1).toBe(2)
  })

  it('fenced content keeps known links but never discovers new paths', () => {
    const html = linkifyHtml(
      '<pre><code>open src/a.ts:17 and docs/b.md</code></pre>',
      ['src/a.ts'],
      new Map([['docs/b.md', '/w/docs/b.md']]),
    )
    expect(html).toContain('data-file-path="src/a.ts"')
    expect(html).not.toContain('/w/docs/b.md')
    expect(html.split('class="file-link"')).toHaveLength(2)
  })

  it('checked mode never discovers folders', () => {
    const map = new Map([
      ['exports/sloth-v5', '/w/exports/sloth-v5'],
      ['exports/', '/w/exports/'],
    ])
    const html = linkifyHtml('<p>Open exports/sloth-v5 and exports/ now</p>', [], map)
    expect(html).not.toContain('class="file-link"')
    expect(findFileMentionSpans('exports/sloth-v5 exports/')).toEqual([])
  })
})

describe('known paths keep an immediate line suffix', () => {
  const cases = [
    { name: 'relative', known: 'src/a.ts', text: 'Open src/a.ts:17 now', path: 'src/a.ts', display: 'src/a.ts:17' },
    {
      name: 'absolute',
      known: '/Users/x/repo/src/a.ts',
      text: 'Open /Users/x/repo/src/a.ts:17 now',
      path: '/Users/x/repo/src/a.ts',
      display: '/Users/x/repo/src/a.ts:17',
    },
    { name: 'basename', known: 'memory/src/a.ts', text: 'Open a.ts:17 now', path: 'memory/src/a.ts', display: 'a.ts:17' },
  ]
  const renders: Record<string, (text: string, known: string[]) => string> = {
    linkifyText: (text, known) => linkifyText(text, known),
    legacy: (text, known) => linkifyHtml(`<p>${text}</p>`, known),
    checked: (text, known) => linkifyHtml(`<p>${text}</p>`, known, new Map()),
  }
  for (const c of cases) {
    for (const [mode, render] of Object.entries(renders)) {
      it(`${mode} keeps the :17 of a ${c.name} known path`, () => {
        const html = render(c.text, [c.known])
        expect(html.split('class="file-link"')).toHaveLength(2)
        expect(html).toContain(`data-file-path="${c.path}"`)
        expect(html).not.toContain(`data-file-path="${c.path}:17"`)
        expect(html).toContain('data-line="17"')
        expect(html).toContain(`>${c.display}</a>`)
      })
    }
  }
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
