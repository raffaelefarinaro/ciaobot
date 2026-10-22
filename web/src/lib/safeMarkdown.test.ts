// @vitest-environment jsdom

import { describe, expect, it, vi } from 'vitest'

import * as loopback from './loopback'
import { renderFileMarkdown, renderMarkdown, renderUserMarkdown } from './safeMarkdown'

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

  it('keeps a task link in the app instead of opening a new tab', () => {
    const id = '0123456789abcdef0123456789abcdef'
    const html = renderMarkdown(`[Reply to Ivo](/tasks/${id}) and [short](/tasks/9920898d)`)
    expect(html).toContain(`<a href="/tasks/${id}">Reply to Ivo</a>`)
    // Not an address the app knows, so it is an ordinary link.
    expect(html).toContain('href="/tasks/9920898d" target="_blank"')
  })

  it('keeps supported page and anchor links in the app instead of opening a new tab', () => {
    const html = renderMarkdown(
      '[Tasks](/tasks) [Automations](/schedules) [Memory](/memory/map) ' +
      '[Settings](/settings) [Workspaces](/settings/workspaces) ' +
      '[New workspace](/settings/workspaces#new-workspace) [Home](/)',
    )

    expect(html).toContain('<a href="/tasks">Tasks</a>')
    expect(html).toContain('<a href="/schedules">Automations</a>')
    expect(html).toContain('<a href="/memory/map">Memory</a>')
    expect(html).toContain('<a href="/settings">Settings</a>')
    expect(html).toContain('<a href="/settings/workspaces">Workspaces</a>')
    expect(html).toContain('<a href="/settings/workspaces#new-workspace">New workspace</a>')
    expect(html).toContain('<a href="/">Home</a>')
    expect(html).not.toContain('target="_blank"')
  })

  it('leaves unknown app paths, queries and external sites as ordinary links', () => {
    const html = renderMarkdown(
      '[unknown](/settings/unknown) [query](/settings?tab=models) ' +
      '[api](/api/settings) [external](https://example.com/settings/workspaces)',
    )

    for (const href of [
      '/settings/unknown',
      '/settings?tab=models',
      '/api/settings',
      'https://example.com/settings/workspaces',
    ]) {
      expect(html, href).toContain(`href="${href}" target="_blank" rel="noopener noreferrer"`)
    }
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

  it('keeps allowed comment markup and safe links while stripping nested unsafe HTML', () => {
    const html = renderMarkdown(
      '<user-comment-reference>' +
      '<reference-source>notes.md (line 3)</reference-source>' +
      '<quoted-text><img src=x onerror=alert(1)> ' +
      '<a href="javascript:alert(2)">bad</a> ' +
      '<a href="https://example.com/guide">safe</a></quoted-text>' +
      '<user-comment>please check this</user-comment>' +
      '</user-comment-reference>',
    )

    // Unsafe event handlers and javascript: URLs nested inside the allowed
    // quote card are still removed.
    expect(html).not.toContain('onerror')
    expect(html).not.toContain('javascript:')
    expect(html).not.toContain('alert(1)')
    expect(html).not.toContain('alert(2)')
    // The app-owned quote-card tags survive so they can be styled.
    expect(html).toContain('<user-comment-reference>')
    expect(html).toContain('<reference-source>')
    expect(html).toContain('<quoted-text>')
    expect(html).toContain('<user-comment>')
    expect(html).toContain('please check this')
    // A safe https link keeps its href and gains the external-link attrs.
    expect(html).toContain('href="https://example.com/guide"')
    expect(html).toContain('rel="noopener noreferrer"')
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

describe('chat markdown file links', () => {
  it('opens a workspace-relative markdown link in the panel instead of the browser', () => {
    const html = renderMarkdown(
      'Created and opened [`wedding-speech.md`](personal/memory-vault/Workspace/wedding-speech.md) in the preview panel.',
    )

    expect(html).toContain('class="file-link"')
    expect(html).toContain('data-file-path="personal/memory-vault/Workspace/wedding-speech.md"')
    // The old markup was a relative anchor opened in a new tab, which the
    // browser resolved against the current /chat/... route.
    expect(html).not.toContain('href="personal/memory-vault/Workspace/wedding-speech.md"')
    expect(html).not.toContain('target="_blank"')
  })

  it('decodes a percent-encoded path into the path the viewer opens', () => {
    const html = renderMarkdown('[note](Workspace/my%20note.md)')

    expect(html).toContain('data-file-path="Workspace/my note.md"')
  })

  it('leaves external, absolute and anchor links as ordinary links', () => {
    const external = renderMarkdown('[docs](https://example.com/guide.md)')
    expect(external).toContain('href="https://example.com/guide.md"')
    expect(external).not.toContain('file-link')

    const absolute = renderMarkdown('[root](/etc/hosts)')
    expect(absolute).toContain('href="/etc/hosts"')
    expect(absolute).not.toContain('file-link')

    const anchor = renderMarkdown('[section](#results)')
    expect(anchor).toContain('href="#results"')
    expect(anchor).not.toContain('file-link')
  })

  it('leaves a relative web link alone when its last segment is not a filename', () => {
    const html = renderMarkdown('[page](example.com/pricing)')

    expect(html).not.toContain('file-link')
  })

  it('leaves a schemeless bare domain alone', () => {
    // One segment whose dotted suffix passes every path-shape test, so it used
    // to render as a file-link to a workspace path that does not exist — and
    // the click went nowhere, since file links deliberately skip target/rel.
    for (const href of ['anthropic.com', 'example.co.uk', 'docs.example.io']) {
      const html = renderMarkdown(`[docs](${href})`)
      expect(html).not.toContain('file-link')
      expect(html).toContain(`href="${href}"`)
    }
  })

  it('still treats a bare filename with a known extension as a file', () => {
    expect(renderMarkdown('[notes](notes.md)')).toContain('data-file-path="notes.md"')
    expect(renderMarkdown('[build](Makefile)')).toContain('data-file-path="Makefile"')
    expect(renderMarkdown('[ignore](.gitignore)')).toContain('data-file-path=".gitignore"')
  })
})

// Issue #1768: a message that names another conversation links to it, under
// its human title. The link is the app's own: it opens in this tab, and the
// raw id stays in the tooltip and in `data-chat-id`.
describe('chat markdown chat references', () => {
  const KNOWN = new Map([['chat-0123abcd', 'Budget review']])

  function render(input: string): HTMLElement {
    const el = document.createElement('div')
    el.innerHTML = renderMarkdown(input, [], KNOWN)
    return el
  }

  it('links a known reference to its title and keeps the raw id', () => {
    const link = render('see chat-0123abcd for the numbers').querySelector('a.chat-link')!
    expect(link.getAttribute('href')).toBe('/chat/chat-0123abcd')
    expect(link.getAttribute('title')).toBe('chat-0123abcd')
    expect(link.getAttribute('data-chat-id')).toBe('chat-0123abcd')
    expect(link.textContent).toBe('Budget review')
  })

  it('keeps the reference in the app instead of opening a new tab', () => {
    const html = renderMarkdown('see chat-0123abcd', [], KNOWN)
    expect(html).not.toContain('target="_blank"')
  })

  it('leaves an unknown id plain', () => {
    const html = renderMarkdown('see chat-ffffffff', [], KNOWN)
    expect(html).not.toContain('chat-link')
    expect(html).toContain('chat-ffffffff')
  })

  it('links a reference in a user bubble too', () => {
    const el = document.createElement('div')
    el.innerHTML = renderUserMarkdown('see chat-0123abcd', [], KNOWN)
    expect(el.querySelector('a.chat-link')?.textContent).toBe('Budget review')
  })

  it('leaves the file-link pipeline alone', () => {
    const html = renderMarkdown('see [notes](notes.md) and chat-0123abcd', [], KNOWN)
    expect(html).toContain('data-file-path="notes.md"')
    expect(html).toContain('data-chat-id="chat-0123abcd"')
  })

  it('never rewrites an existing anchor', () => {
    const id = '0123456789abcdef0123456789abcdef'
    const html = renderMarkdown(`[chat-0123abcd](/tasks/${id})`, [], KNOWN)
    expect(html).toContain(`href="/tasks/${id}"`)
    expect(html).not.toContain('chat-link')
  })
})

describe('chat code blocks and long links', () => {
  it('gives a fenced block a header with its language and the copy button', () => {
    const html = renderMarkdown('```json\n{"a": 1}\n```')
    const host = document.createElement('div')
    host.innerHTML = html
    expect(host.querySelector('.code-block .code-block-head .code-block-lang')?.textContent).toBe('json')
    expect(host.querySelector('.code-block .code-block-head .code-copy-btn')).not.toBeNull()
    expect(host.querySelector('.code-block pre code')?.textContent).toContain('"a": 1')
  })

  it('never lets the fence info string inject markup', () => {
    const host = document.createElement('div')
    host.innerHTML = renderMarkdown('```<img/onerror=alert(1)>\nx\n```')
    expect(host.querySelector('img')).toBeNull()
    expect(host.querySelector('.code-block-lang')?.textContent).toBe('imgonerroralert1')
  })

  it('shortens the label of a long bare URL and keeps the full href', () => {
    const url = 'https://github.com/raffaelefarinaro/ciaobot/issues/new?title=Backfill%20fails&body=' + 'a%20'.repeat(40)
    const host = document.createElement('div')
    host.innerHTML = renderMarkdown(url)
    const link = host.querySelector('a')!
    expect(link.getAttribute('href')).toBe(url)
    expect(link.getAttribute('title')).toBe(url)
    expect(link.textContent!.length).toBeLessThan(60)
    expect(link.textContent).toMatch(/^github\.com\/raffaelefarinaro\/ciaobot/)
  })
})

// Issue #867: a user bubble is rendered back from the string the person typed,
// through the same markdown pipeline as the assistant. marked passes raw HTML
// through, so `<p id=out>` used to disappear from the bubble and split the line.
// User text must read as text; the only markup allowed in it is what the app
// itself writes (the comment-context tags).
describe('renderUserMarkdown', () => {
  function render(input: string): HTMLElement {
    const el = document.createElement('div')
    el.innerHTML = renderUserMarkdown(input)
    return el
  }

  it('shows a typed tag as text instead of an element', () => {
    const el = render('<p id=out>')
    expect(el.textContent).toContain('<p id=out>')
    expect(el.querySelector('#out')).toBeNull()
  })

  it('shows a typed tag inline between text as typed', () => {
    const el = render('before <p id=out> after')
    expect(el.textContent?.trim()).toBe('before <p id=out> after')
    expect(el.querySelector('#out')).toBeNull()
  })

  it('shows an image tag with an event handler as text', () => {
    const el = render('<img src=x onerror=alert(1)>')
    expect(el.querySelector('img')).toBeNull()
    expect(el.textContent).toContain('<img src=x onerror=alert(1)>')
  })

  it('shows a script tag as text', () => {
    const el = render('<script>alert(1)</script>')
    expect(el.querySelector('script')).toBeNull()
    expect(el.textContent).toContain('<script>alert(1)</script>')
  })

  it('leaves a tag inside a code span as text', () => {
    const el = render('use `<div>` here')
    expect(el.querySelector('code')?.textContent).toBe('<div>')
  })

  it('still renders the markdown a user types', () => {
    const el = render([
      '**bold**',
      '',
      '```js',
      'const a = 1',
      '```',
      '',
      '| a | b |',
      '| --- | --- |',
      '| 1 | 2 |',
      '',
      '[ok](https://example.com)',
    ].join('\n'))

    expect(el.querySelector('strong')?.textContent).toBe('bold')
    expect(el.querySelector('.code-block pre code')?.textContent).toContain('const a = 1')
    expect(el.querySelector('.markdown-table-scroll table')).not.toBeNull()
    expect(el.querySelector('a[href="https://example.com"]')?.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('keeps the comment-context tags the app writes itself', () => {
    const withSource =
      '<user-comment-reference><reference-source>a.md (line 3)</reference-source>' +
      '<quoted-text>\nhello\n</quoted-text><user-comment>\nhi\n</user-comment></user-comment-reference>'
    const acrossBlankLines =
      '<user-comment-reference><quoted-text>\npara one\n\npara two\n</quoted-text>' +
      '<user-comment>\nnote\n</user-comment></user-comment-reference>'

    const html = renderUserMarkdown(withSource)
    expect(html).toContain('<user-comment-reference>')
    expect(html).toContain('<reference-source>')
    expect(html).toContain('<quoted-text>')
    expect(html).toContain('<user-comment>')

    const spanned = renderUserMarkdown(acrossBlankLines)
    expect(spanned).toContain('<user-comment-reference>')
    expect(spanned).toContain('para one')
    expect(spanned).toContain('para two')
    expect(spanned).toContain('note')

    // Byte-identical to the assistant renderer: the quote card is markup the
    // app owns, so the user pipeline must leave it exactly as it found it.
    expect(html).toBe(renderMarkdown(withSource))
    expect(spanned).toBe(renderMarkdown(acrossBlankLines))
  })

  it('escapes raw HTML inside a comment card', () => {
    const el = render('<quoted-text><img src=x onerror=alert(1)></quoted-text>')
    expect(el.querySelector('img')).toBeNull()
    expect(el.innerHTML).toContain('<quoted-text>')
    expect(el.textContent).toContain('<img src=x onerror=alert(1)>')
  })

  it('leaves the assistant renderer alone', () => {
    const el = document.createElement('div')
    el.innerHTML = renderMarkdown('<p id=out>')
    expect(el.querySelector('#out')).not.toBeNull()
  })
})

// A link whose target the sanitizer refuses must not survive as a link.
// DOMPurify drops a `file:`, `javascript:` or `data:` href, but the anchor
// used to stay behind: still painted with the accent colour and an
// underline, still swallowing the click, and pointing nowhere.
//
// A refused link is inert text — except a local file path on the engine's
// own machine, and only in a surface that opted in (`{ openLocal: true }`).
// There the engine itself can act on it, so it is rendered as a clickable
// button that opens the path with the OS default application. A surface
// without a click handler for that button must not opt in: a control that
// looks clickable and does nothing is the defect this whole change is about.
describe('links the sanitizer refuses', () => {
  // The folder link from the report that started this.
  const FILE_LINK =
    '[zendesk-code39-93-itf-images](file:///Users/raffaelefarinaro/Documents/zendesk-code39-93-itf-images)'
  const FILE_PATH = '/Users/raffaelefarinaro/Documents/zendesk-code39-93-itf-images'

  function host(html: string): HTMLElement {
    const el = document.createElement('div')
    el.innerHTML = html
    return el
  }

  it('shows a local file link as a button the engine can open, with the path still readable', () => {
    // The opt-in a surface with a delegated handler passes. The path list
    // and the chat map are filled in deliberately: the option is the fourth
    // positional argument.
    const html = renderMarkdown(FILE_LINK, [], new Map(), { openLocal: true })
    const el = host(html)

    // Still no anchor: the path must never be navigable from the browser.
    expect(el.querySelector('a')).toBeNull()
    const button = el.querySelector('button.markdown-dead-link--openable')
    expect(button).not.toBeNull()
    expect(button?.getAttribute('type')).toBe('button')
    expect(button?.getAttribute('data-local-path')).toBe(FILE_PATH)
    expect(button?.getAttribute('title')).toBe('Open on this computer')
    // The label stays readable, and the path stays visible and copyable.
    expect(el.textContent).toContain('zendesk-code39-93-itf-images')
    expect(el.textContent).toContain(FILE_PATH)
    // Nothing in the markup can navigate to the folder.
    expect(html).not.toContain('href=')
    expect(html).not.toContain('file://')
  })

  it('shows the same in a user bubble', () => {
    const el = host(renderUserMarkdown(FILE_LINK, [], new Map(), { openLocal: true }))

    expect(el.querySelector('a')).toBeNull()
    expect(el.querySelector('button.markdown-dead-link--openable')).not.toBeNull()
    expect(el.textContent).toContain(FILE_PATH)
  })

  it('shows the same in a note', () => {
    const el = host(renderFileMarkdown(FILE_LINK, {
      resolveImageSrc: (href) => href,
      openLocal: true,
    }))

    expect(el.querySelector('a')).toBeNull()
    expect(el.querySelector('button.markdown-dead-link--openable')).not.toBeNull()
    expect(el.textContent).toContain(FILE_PATH)
  })

  it('keeps the authority of a network path', () => {
    const el = host(renderMarkdown('[share](file://server/share/notes.md)', [], new Map(), { openLocal: true }))

    expect(el.querySelector('a')).toBeNull()
    expect(el.textContent).toContain('//server/share/notes.md')
    // The host is part of the path: `/share/notes.md` is a different place.
    expect(el.textContent).not.toContain('(/share/notes.md)')
    // The engine is asked for the whole authority-qualified path.
    expect(el.querySelector('button.markdown-dead-link--openable')?.getAttribute('data-local-path'))
      .toBe('//server/share/notes.md')
  })

  it('keeps the inline formatting of the label', () => {
    const el = host(renderMarkdown('[**bold** label](file:///tmp/notes.md)', [], new Map(), { openLocal: true }))

    expect(el.querySelector('.markdown-dead-link strong')?.textContent).toBe('bold')
  })

  it('leaves a local link without a path segment inert', () => {
    // `file://myhost` is a machine name, not a file to open.
    const html = renderMarkdown('[server](file://myhost)', [], new Map(), { openLocal: true })
    const el = host(html)

    expect(el.querySelector('a')).toBeNull()
    expect(el.querySelector('button')).toBeNull()
    expect(html).not.toContain('data-local-path')
    expect(el.textContent).toContain('server')
  })

  it('keeps a local link inert off the engine\'s own machine', () => {
    // A phone or another computer on the LAN: the engine would open the file
    // on a screen nobody is looking at, so the affordance is not rendered.
    const spy = vi.spyOn(loopback, 'isLoopbackPage').mockReturnValue(false)
    try {
      const html = renderMarkdown(FILE_LINK, [], new Map(), { openLocal: true })
      const el = host(html)

      expect(el.querySelector('a')).toBeNull()
      expect(el.querySelector('button')).toBeNull()
      expect(el.querySelector('.markdown-dead-link')?.textContent).toContain(FILE_PATH)
      expect(html).not.toContain('data-local-path')
    } finally {
      spy.mockRestore()
    }
  })

  it('keeps a local link inert in a surface that did not opt in', () => {
    // No opt-in: this surface has no click handler for the button, so the
    // link stays copyable text rather than a control that goes nowhere.
    const html = renderMarkdown(FILE_LINK, [], new Map())
    const el = host(html)

    expect(el.querySelector('a')).toBeNull()
    expect(el.querySelector('button')).toBeNull()
    expect(html).not.toContain('data-local-path')
    expect(el.querySelector('.markdown-dead-link')?.textContent).toContain(FILE_PATH)
  })

  it('turns a raw anchor without a target into inert text', () => {
    const el = host(renderMarkdown('<a>plain</a>'))

    expect(el.querySelector('a')).toBeNull()
    expect(el.querySelector('.markdown-dead-link')?.textContent).toBe('plain')
  })

  it('turns a raw anchor with a refused target into inert text', () => {
    const el = host(renderMarkdown('<a href="file:///tmp/x">raw</a>'))

    expect(el.querySelector('a')).toBeNull()
    expect(el.querySelector('.markdown-dead-link')?.textContent).toBe('raw')
  })

  it('refuses javascript and data links, and keeps no trace of the target', () => {
    const html = renderMarkdown(
      '[bad](javascript:alert(1)) [also](data:text/html;base64,PHNjcmlwdD4=)',
    )
    const el = host(html)

    expect(el.querySelector('a')).toBeNull()
    expect(el.textContent).toContain('bad')
    expect(el.textContent).toContain('also')
    expect(html).not.toContain('javascript:')
    expect(html).not.toContain('data:')
  })

  it('leaves the links a reader can follow alone', () => {
    const html = renderMarkdown([
      '[web](https://example.com/guide)',
      '[mail](mailto:me@example.com)',
      '[app](/tasks)',
      '[note](notes.md)',
    ].join('\n'))

    expect(html).toContain('href="https://example.com/guide"')
    expect(html).toContain('href="mailto:me@example.com"')
    expect(html).toContain('href="/tasks"')
    expect(html).toContain('data-file-path="notes.md"')
    expect(html).not.toContain('markdown-dead-link')
  })
})
