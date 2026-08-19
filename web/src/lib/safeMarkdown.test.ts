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

  it('wraps tables in a keyboard-scrollable region', () => {
    const html = renderMarkdown([
      '| Question | Resolution |',
      '| --- | --- |',
      '| PORT | Use the configured port |',
    ].join('\n'))

    expect(html).toContain(
      '<div class="markdown-table-scroll" role="region" aria-label="Scrollable table" tabindex="0">',
    )
    expect(html).toContain('<table>')
    expect(html.indexOf('markdown-table-scroll')).toBeLessThan(html.indexOf('<table>'))
    expect(html.indexOf('</table>')).toBeLessThan(html.indexOf('</div>'))
  })

  // The viewer only intercepts `a.file-link` (onMdClick), so a relative
  // markdown link left to the default renderer is a link that does nothing
  // when clicked. This is the regression test for that.
  it('renders a relative markdown link as a clickable vault file-link', () => {
    const html = renderFileMarkdown('See [Rossmann MVP](./README.md) for context.', {
      resolveImageSrc: (href) => href,
      filePath: 'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
      markdownPaths: [
        'memory-vault/work/projects/active/rossmann/README.md',
        'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
      ],
    })

    expect(html).toContain('class="file-link"')
    expect(html).toContain('href="#"')
    expect(html).toContain('data-file-path="memory-vault/work/projects/active/rossmann/README.md"')
    expect(html).toContain('>Rossmann MVP</a>')
    expect(html).not.toContain('href="./README.md"')
  })

  it('resolves an up-directory vault link to its real path', () => {
    const html = renderFileMarkdown('Owner: [Mo](../../../../People/Mo.md)', {
      resolveImageSrc: (href) => href,
      filePath: 'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
      markdownPaths: [
        'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
        'memory-vault/People/Mo.md',
      ],
    })

    expect(html).toContain('data-file-path="memory-vault/People/Mo.md"')
  })

  it('leaves external links alone instead of hijacking them as vault links', () => {
    const html = renderFileMarkdown('Spec at [OKF](https://example.com/okf/SPEC.md).', {
      resolveImageSrc: (href) => href,
      filePath: 'memory-vault/People/Mo.md',
      markdownPaths: ['memory-vault/People/Mo.md'],
    })

    expect(html).toContain('href="https://example.com/okf/SPEC.md"')
    expect(html).not.toContain('file-link')
    expect(html).not.toContain('data-file-path')
  })

  it('marks a dangling vault link non-clickable rather than emitting a dead anchor', () => {
    const html = renderFileMarkdown('Missing [Nowhere](./Nowhere/Note.md).', {
      resolveImageSrc: (href) => href,
      filePath: 'memory-vault/People/Mo.md',
      markdownPaths: ['memory-vault/People/Mo.md'],
    })

    expect(html).toContain('class="vault-link-unresolved"')
    expect(html).toContain('title="./Nowhere/Note.md"')
    expect(html).toContain('>Nowhere</span>')
    expect(html).not.toContain('<a')
  })

  it('keeps vault links out of code spans and fences', () => {
    const html = renderFileMarkdown([
      'Inline `[Skip](./README.md)` stays literal.',
      '',
      '```md',
      '[Also skip](./README.md)',
      '```',
    ].join('\n'), {
      resolveImageSrc: (href) => href,
      filePath: 'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
      markdownPaths: [
        'memory-vault/work/projects/active/rossmann/README.md',
        'memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md',
      ],
    })

    expect(html).not.toContain('data-file-path')
    expect(html).toContain('[Skip](./README.md)')
    expect(html).toContain('[Also skip](./README.md)')
  })

  it('does not linkify note links when no vault path index is supplied', () => {
    const html = renderFileMarkdown('See [README](./README.md).', {
      resolveImageSrc: (href) => href,
    })

    expect(html).toContain('href="./README.md"')
    expect(html).not.toContain('data-file-path')
  })
})
