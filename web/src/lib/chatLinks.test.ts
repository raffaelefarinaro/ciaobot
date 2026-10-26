// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'

import { isChatId, linkifyChatIds } from './chatLinks'

// The two conversations this reader knows, by id and human title.
const KNOWN = new Map([
  ['chat-0123abcd', 'Budget review'],
  ['chat-89abcdef', 'Release notes'],
])

function host(html: string): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = html
  return el
}

describe('isChatId', () => {
  it('accepts the shape the engine mints and nothing else', () => {
    expect(isChatId('chat-0123abcd')).toBe(true)
    expect(isChatId('chat-00000000')).toBe(true)
    for (const value of [
      'chat-0123abcde',
      'chat-0123abc',
      'chat-0123ABCD',
      'chat-1',
      'chat-',
      'chat-0123abcd-extra',
      'xchat-0123abcd',
      'notes/chat-0123abcd',
      '',
    ]) {
      expect(isChatId(value), value).toBe(false)
    }
  })
})

describe('linkifyChatIds', () => {
  it('links a known id to its human title and keeps the raw id as metadata', () => {
    const el = host(linkifyChatIds('<p>see chat-0123abcd for the numbers</p>', KNOWN))
    const link = el.querySelector('a.chat-link')
    expect(link).not.toBeNull()
    expect(link!.getAttribute('href')).toBe('/chat/chat-0123abcd')
    expect(link!.getAttribute('title')).toBe('chat-0123abcd')
    expect(link!.getAttribute('data-chat-id')).toBe('chat-0123abcd')
    expect(link!.textContent).toBe('Budget review')
    // The prose around the reference is untouched.
    expect(el.textContent).toBe('see Budget review for the numbers')
  })

  it('links every reference in a message', () => {
    const el = host(linkifyChatIds('<p>chat-0123abcd then chat-89abcdef</p>', KNOWN))
    const links = el.querySelectorAll('a.chat-link')
    expect(links.length).toBe(2)
    expect(links[0]!.textContent).toBe('Budget review')
    expect(links[1]!.textContent).toBe('Release notes')
  })

  it("links the app's own @chat/ mention and leaves the prefix as text", () => {
    const el = host(linkifyChatIds('<p>see @chat/chat-0123abcd now</p>', KNOWN))
    const link = el.querySelector('a.chat-link')
    expect(link).not.toBeNull()
    expect(link!.getAttribute('href')).toBe('/chat/chat-0123abcd')
    expect(link!.textContent).toBe('Budget review')
    // The mention prefix stays exactly as it was typed.
    expect(el.textContent).toBe('see @chat/Budget review now')
  })

  it('does not mistake another path or a URL for the mention', () => {
    for (const html of [
      '<p>notes/chat-0123abcd</p>',
      '<p>project/chat-0123abcd</p>',
      '<p>x@chat/chat-0123abcd</p>',
      '<p>https://example.test/chat-0123abcd</p>',
      '<p>@chat/notes/chat-0123abcd</p>',
    ]) {
      const el = host(linkifyChatIds(html, KNOWN))
      expect(el.querySelector('a.chat-link'), html).toBeNull()
    }
  })

  it('does not mistake a scheme-less query for the mention', () => {
    for (const text of [
      'example.com/?to=@chat/chat-0123abcd',
      '?to=@chat/chat-0123abcd',
      'x=@chat/chat-0123abcd',
    ]) {
      const el = host(linkifyChatIds(`<p>${text}</p>`, KNOWN))
      expect(el.querySelector('a.chat-link'), text).toBeNull()
      expect(el.textContent, text).toBe(text)
    }
  })

  it('links a mention whose @ opens the token', () => {
    for (const text of [
      '@chat/chat-0123abcd',
      'see (@chat/chat-0123abcd now',
      'first line\n@chat/chat-0123abcd',
    ]) {
      const el = host(linkifyChatIds(`<p>${text}</p>`, KNOWN))
      expect(
        el.querySelector('a.chat-link')?.getAttribute('data-chat-id'),
        text,
      ).toBe('chat-0123abcd')
    }
  })

  it('leaves an unknown id plain', () => {
    // Well formed, so only the known-list lookup can keep it plain.
    const html = '<p>chat-ffffffff is not a chat I know</p>'
    const out = linkifyChatIds(html, KNOWN)
    expect(out).not.toContain('chat-link')
    expect(out).toContain('chat-ffffffff')
  })

  it('changes nothing when no chat is known', () => {
    const html = '<p>see chat-0123abcd for the numbers</p>'
    expect(linkifyChatIds(html, new Map())).toBe(html)
  })

  it('changes nothing when the message names no chat at all', () => {
    const html = '<p>nothing to see here</p>'
    expect(linkifyChatIds(html, KNOWN)).toBe(html)
  })

  it('links a reference inside inline code but never inside a code block', () => {
    const inline = host(linkifyChatIds('<p>the id is <code>chat-0123abcd</code></p>', KNOWN))
    expect(inline.querySelector('code a.chat-link')?.getAttribute('data-chat-id')).toBe('chat-0123abcd')

    const block = host(
      linkifyChatIds('<pre><code>chat-0123abcd\nchat-89abcdef</code></pre>', KNOWN),
    )
    expect(block.querySelector('a.chat-link')).toBeNull()
    expect(block.textContent).toBe('chat-0123abcd\nchat-89abcdef')
  })

  it('never rewrites an existing anchor or its attributes', () => {
    const html = '<p><a class="file-link" href="#" data-file-path="notes.md">chat-0123abcd</a></p>'
    const out = linkifyChatIds(html, KNOWN)
    expect(out).toContain('class="file-link"')
    expect(out).toContain('data-file-path="notes.md"')
    expect(out).not.toContain('chat-link')
    // The anchor's own text is not rewritten either.
    expect(out).toContain('>chat-0123abcd</a>')
  })

  it('never rewrites an ordinary link that happens to name a chat', () => {
    const html = '<p><a href="https://example.com/chat-0123abcd">chat-0123abcd</a></p>'
    const out = linkifyChatIds(html, KNOWN)
    expect(out).toContain('href="https://example.com/chat-0123abcd"')
    expect(out).not.toContain('chat-link')
  })

  it('leaves a hostile title as text, never as markup', () => {
    const hostile = new Map([['chat-0123abcd', '<img src=x onerror=alert(1)>']])
    const el = host(linkifyChatIds('<p>see chat-0123abcd</p>', hostile))
    expect(el.querySelector('img')).toBeNull()
    const link = el.querySelector('a.chat-link')!
    expect(link.textContent).toBe('<img src=x onerror=alert(1)>')
    expect(link.getAttribute('onerror')).toBeNull()
  })

  it('falls back to the raw id when the title is empty', () => {
    const untitled = new Map([['chat-0123abcd', '   ']])
    const el = host(linkifyChatIds('<p>see chat-0123abcd</p>', untitled))
    expect(el.querySelector('a.chat-link')!.textContent).toBe('chat-0123abcd')
  })

  it('matches only a standalone id, never a longer or embedded token', () => {
    for (const text of [
      'chat-0123abcde',
      'chat-0123abcdef',
      'chat-0123abcd-extra',
      'xchat-0123abcd',
      'chat-0123ABCD',
      'CHAT-0123abcd',
      'notes/chat-0123abcd',
      'https://example.com/chat-0123abcd',
      'https://example.com/?to=chat-0123abcd',
    ]) {
      const el = host(linkifyChatIds(`<p>${text}</p>`, KNOWN))
      expect(el.querySelector('a.chat-link'), text).toBeNull()
      expect(el.textContent, text).toBe(text)
    }
  })

  it('links a reference that stands between punctuation and spaces', () => {
    const el = host(linkifyChatIds('<p>(chat-0123abcd), then done.</p>', KNOWN))
    expect(el.querySelector('a.chat-link')?.getAttribute('data-chat-id')).toBe('chat-0123abcd')
  })

  it('never touches the content of a script, style or button', () => {
    for (const tag of ['script', 'style', 'textarea', 'button']) {
      const el = host(linkifyChatIds(`<${tag}>chat-0123abcd</${tag}>`, KNOWN))
      expect(el.querySelector('a.chat-link'), tag).toBeNull()
    }
  })
})
