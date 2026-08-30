import DOMPurify from 'dompurify'
import { Marked, Renderer, type Tokens } from 'marked'

import { CODE_BLOCK_CLASS, codeCopyButtonHtml } from './codeCopy'
import { COMMENT_TAGS } from './commentContext'
import { isPlausibleFilePath, linkifyHtml } from './filePaths'
import { buildMarkdownIndex, resolveVaultLinkTarget, vaultNoteRefFromHref } from './vaultLinks'

type FileMarkdownOptions = {
  resolveImageSrc: (href: string) => string
  filePath?: string
  markdownPaths?: string[]
}

const MARKDOWN_OPTIONS = { breaks: true }

const chatMarkdownRenderer = new Renderer()
chatMarkdownRenderer.table = function table(token: Tokens.Table): string {
  return [
    '<div class="markdown-table-scroll" role="region" aria-label="Scrollable table" tabindex="0">',
    Renderer.prototype.table.call(this, token),
    '</div>',
  ].join('')
}

// Fenced blocks get a wrapper plus a copy button in the markup itself: chat
// markdown is injected with v-html, so the affordance has to come from the
// renderer and be driven by delegation (see lib/codeCopy.ts).
chatMarkdownRenderer.code = function code(token: Tokens.Code): string {
  return [
    `<div class="${CODE_BLOCK_CLASS}">`,
    codeCopyButtonHtml(),
    Renderer.prototype.code.call(this, token),
    '</div>',
  ].join('')
}

/**
 * The workspace file a chat link points at, or '' when it is an ordinary link.
 *
 * An agent that writes `[`notes.md`](personal/memory-vault/notes.md)` means the
 * file, not a web page. Left to the default renderer that became a plain
 * relative anchor which `withExternalLinkAttrs` then opened in a new tab, so
 * clicking it resolved the path against the current route and loaded
 * `http://<host>/chat/personal/memory-vault/notes.md` in a browser instead of
 * opening the note in the preview panel.
 *
 * Both halves have to look like a file: the href so `../notes.md` and
 * `Workspace/notes.md` qualify, and its basename so a relative *web* link
 * ("example.com/page") is left alone.
 */
function chatFileHref(href: string): string {
  const raw = (href || '').trim()
  // In-page anchors, absolute URLs and mailto: are ordinary links. A leading
  // `/` is left alone too: the viewer resolves against the workspace root, so
  // an absolute filesystem path is not something it could open.
  if (!raw || raw.startsWith('#') || raw.startsWith('/')) return ''
  if (/^[a-z][a-z0-9+.-]*:/i.test(raw)) return ''
  const path = raw.split('#')[0].split('?')[0]
  if (!path) return ''
  let decoded = path
  try {
    decoded = decodeURIComponent(path)
  } catch {
    decoded = path
  }
  const base = decoded.replace(/[/\\]+$/, '').split(/[/\\]/).pop() || ''
  if (!isPlausibleFilePath(decoded) || !isPlausibleFilePath(base)) return ''
  return decoded
}

// Emit the same `a.file-link` shape the path linkifier and the note viewer
// already produce, so the delegated click handler opens it in the panel.
chatMarkdownRenderer.link = function link(this: Renderer, token: Tokens.Link): string {
  const target = chatFileHref(token.href)
  if (!target) return Renderer.prototype.link.call(this, token)
  const label = this.parser.parseInline(token.tokens)
  return `<a class="file-link" href="#" data-file-path="${escapeAttr(target)}">${label}</a>`
}

const chatMarkdownParser = new Marked({
  ...MARKDOWN_OPTIONS,
  renderer: chatMarkdownRenderer,
})

function sanitizeHtml(html: string): string {
  return DOMPurify.sanitize(html, {
    // Allow the inert custom elements used to wrap quoted "comment" context
    // (see lib/commentContext.ts) so they survive into the chat bubble and can
    // be styled as quote cards, instead of being stripped to bare text.
    ADD_TAGS: [...COMMENT_TAGS],
    ADD_ATTR: ['target', 'rel', 'loading', 'data-file-path', 'data-line'],
    FORBID_ATTR: ['style'],
  })
}

function withExternalLinkAttrs(html: string): string {
  return html.replace(/<a\s+([^>]*?)>/gi, (match, attrs) => {
    if (/\btarget\s*=/i.test(attrs)) return match
    // A file link is handled in-app by the delegated click handler; a
    // `target="_blank"` on it would be a second, wrong answer to the click.
    if (/\bclass\s*=\s*"[^"]*\bfile-link\b/i.test(attrs)) return match
    return `<a ${attrs} target="_blank" rel="noopener noreferrer">`
  })
}

function escapeAttr(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

export function renderMarkdown(text: string, knownPaths: string[] = []): string {
  try {
    const html = chatMarkdownParser.parse(text) as string
    return linkifyHtml(sanitizeHtml(withExternalLinkAttrs(html)), knownPaths)
  } catch {
    return sanitizeHtml(text)
  }
}

export function renderFileMarkdown(text: string, options: FileMarkdownOptions): string {
  // Cross-note links are relative markdown links (`[Mo](./People/Mo.md)`).
  // Left to the default renderer they would emit `<a href="./People/Mo.md">`,
  // and the viewer only intercepts `a.file-link` (`onMdClick`), so clicking one
  // would do nothing useful. The `link()` override below is what makes an
  // in-vault link navigate; it is not cosmetic.
  const filePath = options.filePath || ''
  const vaultPaths = filePath && options.markdownPaths?.length ? options.markdownPaths : null
  const linkIndex = vaultPaths ? buildMarkdownIndex(vaultPaths) : null
  const linkPathSet = vaultPaths ? new Set(vaultPaths) : null

  const renderer = {
    image({ href, title, text: alt }: { href: string; title: string | null; text: string }): string {
      const src = href ? options.resolveImageSrc(href) : ''
      const titleAttr = title ? ` title="${escapeAttr(title)}"` : ''
      return `<img src="${escapeAttr(src)}" alt="${escapeAttr(alt ?? '')}"${titleAttr} loading="lazy" />`
    },
    link(this: Renderer, token: Tokens.Link): string {
      if (!linkIndex || !linkPathSet) return Renderer.prototype.link.call(this, token)
      const ref = vaultNoteRefFromHref(token.href)
      // External URLs, absolute paths, in-page anchors and non-note targets
      // stay ordinary anchors.
      if (ref === null) return Renderer.prototype.link.call(this, token)
      const label = this.parser.parseInline(token.tokens)
      const target = resolveVaultLinkTarget(ref, filePath, linkIndex, linkPathSet)
      // A dangling link stays visibly non-clickable rather than becoming a
      // dead anchor that swallows the tap.
      if (!target) {
        return `<span class="vault-link-unresolved" title="${escapeAttr(token.href)}">${label}</span>`
      }
      return `<a class="file-link" href="#" data-file-path="${escapeAttr(target)}">${label}</a>`
    },
  }
  const parser = new Marked({ ...MARKDOWN_OPTIONS, renderer })
  try {
    return sanitizeHtml(parser.parse(text) as string)
  } catch {
    return sanitizeHtml(text)
  }
}
