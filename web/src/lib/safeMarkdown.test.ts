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
