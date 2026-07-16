// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'

import { renderFileMarkdown, renderMarkdown } from './safeMarkdown'

describe('safe markdown rendering', () => {
  it('removes raw HTML event handlers before v-html rendering', () => {
    const html = renderMarkdown('<img src=x onerror=alert(1)>')

    expect(html).not.toContain('onerror')
    expect(html).not.toContain('alert(1)')
  })

  it('removes javascript links while preserving normal links', () => {
    const html = renderMarkdown('[bad](javascript:alert(1)) [ok](https://example.com)')

    expect(html).not.toContain('javascript:')
    expect(html).toContain('href="https://example.com"')
    expect(html).toContain('rel="noopener noreferrer"')
  })

  it('resolves relative markdown images through the workspace image endpoint', () => {
    const html = renderFileMarkdown('![Logo](assets/logo.png)', {
      resolveImageSrc: (href) => `/api/workspace-image?path=${encodeURIComponent(`/workspace/${href}`)}`,
    })

    expect(html).toContain('/api/workspace-image?path=')
    expect(html).toContain('loading="lazy"')
  })

  it('removes unsafe image URLs from file markdown', () => {
    const html = renderFileMarkdown('![bad](javascript:alert(1))', {
      resolveImageSrc: (href) => href,
    })

    expect(html).not.toContain('javascript:')
  })

  it('preserves comment-context tags so they can be styled in the bubble', () => {
    const html = renderMarkdown(
      '<user-comment-reference><reference-source>a.md (line 3)</reference-source>' +
      '<quoted-text>\nhello\n</quoted-text><user-comment>\nhi\n</user-comment></user-comment-reference>',
    )

    expect(html).toContain('<user-comment-reference>')
    expect(html).toContain('<reference-source>')
    expect(html).toContain('<quoted-text>')
    expect(html).toContain('<user-comment>')
    expect(html).toContain('hello')
    expect(html).toContain('hi')
  })

  it('keeps the reference intact when quoted text spans blank lines', () => {
    const html = renderMarkdown(
      '<user-comment-reference><quoted-text>\npara one\n\npara two\n</quoted-text>' +
      '<user-comment>\nnote\n</user-comment></user-comment-reference>',
    )
    expect(html).toContain('<user-comment-reference>')
    expect(html).toContain('</user-comment-reference>')
    expect(html).toContain('para one')
    expect(html).toContain('para two')
    expect(html).toContain('note')
  })

  it('still strips dangerous tags even with comment tags allowed', () => {
    const html = renderMarkdown('<quoted-text><img src=x onerror=alert(1)></quoted-text>')
    expect(html).not.toContain('onerror')
    expect(html).toContain('<quoted-text>')
  })

  it('resolves Obsidian wikilinks into file-link anchors', () => {
    const html = renderFileMarkdown('See [[README|Rossmann MVP]] for context.', {
      resolveImageSrc: (href) => href,
      filePath: 'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
      markdownPaths: [
        'memory-vault/work/projects/active/rossmann/README.md',
        'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
      ],
    })

    expect(html).toContain('class="file-link wikilink"')
    expect(html).toContain('data-file-path="memory-vault/work/projects/active/rossmann/README.md"')
    expect(html).toContain('>Rossmann MVP</a>')
  })
})
