import { describe, expect, it } from 'vitest'

import { buildKnownPathRules, linkifyHtml, linkifyText } from './filePaths'

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
