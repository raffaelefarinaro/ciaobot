import DOMPurify from 'dompurify'
import { Marked, Renderer, type Tokens } from 'marked'

import { isAppRouteHref } from './appLinks'
import { CODE_BLOCK_CLASS, codeCopyButtonHtml } from './codeCopy'
import { linkifyChatIds } from './chatLinks'
import { COMMENT_TAGS } from './commentContext'
import { hasKnownFileExtension, isPlausibleFilePath, linkifyHtml } from './filePaths'
import { isLoopbackPage } from './loopback'
import { DEAD_LINK_OPENABLE_CLASS, DEAD_LINK_PATH_ATTR } from './openWorkspaceFile'
import { buildMarkdownIndex, resolveVaultLinkTarget, vaultNoteRefFromHref } from './vaultLinks'

type FileMarkdownOptions = {
  resolveImageSrc: (href: string) => string
  filePath?: string
  markdownPaths?: string[]
  openLocal?: boolean
}

/** Per-render opt-in: may a refused `file:` link become an openable button? */
type RenderOptions = {
  openLocal?: boolean
}

const MARKDOWN_OPTIONS = { breaks: true }

// The class a refused link is rendered as: inert text, not an anchor.
const DEAD_LINK_CLASS = 'markdown-dead-link'

/**
 * Whether the render in progress may turn a refused `file:` link into a
 * button the engine can open. Set for the duration of one render, from the
 * opt-in its caller passes.
 *
 * A module-level flag is safe here because the markdown parsers are
 * synchronous: every renderer callback that reads it fires inside the
 * caller's `parse()` call, and no render awaits anything, so the single
 * thread of the page cannot run another render in between. Each render sets
 * the flag on entry and clears it in a `finally`, so no render can see
 * another one's opt-in.
 */
let openLocalFiles = false

/**
 * Whether a link target can be followed. The sanitizer's own URI policy
 * decides, so the two cannot drift apart: it keeps relative paths,
 * fragments and the ordinary web schemes, and drops `file:`, `data:`,
 * `javascript:` and every other scheme it does not know.
 */
function isUsableHref(href: string | null | undefined): boolean {
  const value = (href || '').trim()
  if (!value) return false
  return DOMPurify.isValidAttribute('a', 'href', value)
}

/**
 * The path a `file:` link points at, or '' when the target is not a file.
 *
 * The scheme is dropped and the path is kept, so the reader can still copy
 * it. An authority is part of the path: `file://server/share` is
 * `//server/share`, not `/share`.
 */
function localPathOf(target: string): string {
  if (!/^file:/i.test(target)) return ''
  let path = target.slice('file:'.length)
  let authority = ''
  if (path.startsWith('//')) {
    const rest = path.slice(2)
    const slash = rest.indexOf('/')
    authority = slash === -1 ? rest : rest.slice(0, slash)
    path = slash === -1 ? '' : rest.slice(slash)
  }
  try {
    path = decodeURIComponent(path)
  } catch {
    // Shown as written when it is not valid percent-encoding.
  }
  if (authority) return `//${authority}${path}`
  // A Windows path keeps its drive letter: `file:///C:/notes` is `C:/notes`.
  return path.replace(/^\/([A-Za-z]:)/, '$1')
}

/**
 * A link the sanitizer will drop: the label stays readable, and a file path
 * stays visible as text the reader can copy. The path goes inside the
 * anchor, whose content `linkifyHtml` skips.
 *
 * A dead link whose target is a local path is not dead after all — but only
 * where the surface opted in (`openLocal`) *and* the app runs on the engine's
 * own machine. There it becomes a clickable button: a readable label and a
 * visible, copyable path, and a click that asks the engine to open the path
 * with the OS default application. The affordance exists only there, because
 * that is the only machine the app can open anything on: elsewhere (a phone,
 * another computer on the LAN) the link stays inert text, because
 * `POST /api/workspace-open` is refused to a remote caller.
 */
function deadLinkHtml(renderer: Renderer, token: Tokens.Link): string {
  const label = renderer.parser.parseInline(token.tokens)
  const path = localPathOf(token.href)
  // A path the engine can open. `file://myhost` is a machine name, not a
  // file: it keeps the plain inert anchor.
  if (path && openLocalFiles && isLoopbackPage() && !/^\/\/[^/]+$/.test(path)) {
    return (
      `<button type="button" class="${DEAD_LINK_CLASS} ${DEAD_LINK_OPENABLE_CLASS}"`
      + ` ${DEAD_LINK_PATH_ATTR}="${escapeAttr(path)}" title="Open on this computer">`
      + `${label} (${escapeAttr(path)})</button>`
    )
  }
  const shown = path ? ` (${escapeAttr(path)})` : ''
  return `<a href="${escapeAttr(token.href)}">${label}${shown}</a>`
}

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
  // A header strip names the language (the fence's first word, when given)
  // and holds the copy button, so the block reads as one card.
  const lang = (token.lang || '').trim().split(/\s+/)[0] || ''
  const label = lang.replace(/[^\w+#.-]/g, '').slice(0, 24)
  return [
    `<div class="${CODE_BLOCK_CLASS}">`,
    '<div class="code-block-head">',
    `<span class="code-block-lang">${label}</span>`,
    codeCopyButtonHtml(),
    '</div>',
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
  // `[docs](anthropic.com)` is a schemeless site link, not a file: one segment
  // whose dotted suffix satisfies every shape test above. Claiming it rendered
  // a file-link to a workspace path that does not exist, and the click then
  // went nowhere because `withExternalLinkAttrs` skips file links. A dotted
  // single segment therefore has to carry an extension the viewer can open;
  // `Makefile` (no dot) and `.gitignore` (dotfile) still qualify.
  const dotted = base.includes('.') && !base.startsWith('.')
  if (dotted && !/[/\\]/.test(decoded) && !hasKnownFileExtension(base)) return ''
  return decoded
}

// Emit the same `a.file-link` shape the path linkifier and the note viewer
// already produce, so the delegated click handler opens it in the panel.
// A bare URL is its own label, and a long one (a prefilled issue link, a
// search URL) used to fill the bubble with percent-encoding. Show the host
// and the start of the path; the full URL stays in href and the tooltip.
const LONG_BARE_LINK = 64

function shortLinkLabel(href: string): string {
  try {
    const url = new URL(href)
    const path = decodeURIComponent(url.pathname).replace(/\/$/, '')
    const tail = path.length > 28 ? `${path.slice(0, 28)}…` : path
    return `${url.host}${tail}${url.search || url.hash ? (tail.endsWith('…') ? '' : '…') : ''}`
  } catch {
    return `${href.slice(0, LONG_BARE_LINK - 1)}…`
  }
}

chatMarkdownRenderer.link = function link(this: Renderer, token: Tokens.Link): string {
  const target = chatFileHref(token.href)
  if (!target) {
    if (token.text === token.href && token.href.length > LONG_BARE_LINK && /^https?:\/\//i.test(token.href)) {
      return `<a href="${escapeAttr(token.href)}" title="${escapeAttr(token.href)}">${escapeAttr(shortLinkLabel(token.href))}</a>`
    }
    // A target the sanitizer drops is inert text, not a link.
    if (!isUsableHref(token.href)) return deadLinkHtml(this, token)
    return Renderer.prototype.link.call(this, token)
  }
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
    // `data-chat-id` is the raw id of a conversation a message references
    // (see lib/chatLinks.ts). It is named explicitly: the allowlist is not
    // widened to every data attribute.
    // `data-local-path` is the local path of an openable dead link (see
    // lib/openWorkspaceFile.ts): the engine opens it, the browser never
    // navigates to it.
    ADD_ATTR: [
      'target',
      'rel',
      'loading',
      'data-file-path',
      'data-local-path',
      'data-line',
      'data-chat-id',
    ],
    FORBID_ATTR: ['style'],
  })
}

function withExternalLinkAttrs(html: string): string {
  return html.replace(/<a\s+([^>]*?)>/gi, (match, attrs) => {
    if (/\btarget\s*=/i.test(attrs)) return match
    // A file link is handled in-app by the delegated click handler; a
    // `target="_blank"` on it would be a second, wrong answer to the click.
    if (/\bclass\s*=\s*"[^"]*\bfile-link\b/i.test(attrs)) return match
    // So is a link into the app (lib/appLinks.ts): a new tab would open a
    // second copy of the app instead of the task.
    const href = /\bhref\s*=\s*"([^"]*)"/i.exec(attrs)?.[1] ?? ''
    if (isAppRouteHref(href)) return match
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

/**
 * Replace the anchors the sanitizer left without a target. An anchor with no
 * href is not a link but still looks like one — the panes paint `a` with the
 * accent colour and an underline — so it becomes plain text, with the inline
 * formatting it was written with.
 *
 * Only anchors are walked: a dead link that the engine can open is already a
 * button (see `deadLinkHtml`), and this pass must leave it alone.
 *
 * This runs last, after `linkifyHtml`: that pass skips the content of an
 * anchor, so a dead link's label is never turned into a workspace file link.
 */
function neutralizeDeadAnchors(html: string): string {
  if (!html.includes('<a')) return html
  const template = document.createElement('template')
  template.innerHTML = html
  const anchors = Array.from(template.content.querySelectorAll('a'))
  if (!anchors.length) return html
  let changed = false
  for (const anchor of anchors) {
    if (isUsableHref(anchor.getAttribute('href'))) continue
    changed = true
    const span = document.createElement('span')
    span.className = DEAD_LINK_CLASS
    const title = anchor.getAttribute('title')
    if (title) span.setAttribute('title', title)
    span.innerHTML = anchor.innerHTML
    anchor.replaceWith(span)
  }
  return changed ? template.innerHTML : html
}

// No chat is known unless the caller says so: the renderers are used by
// panes that have no chat list of their own.
const NO_KNOWN_CHATS: ReadonlyMap<string, string> = new Map()

/**
 * Render a message for the transcript.
 *
 * `knownChats` maps the ids of the reader's own conversations to their human
 * titles. A reference to one of them becomes a link to the conversation,
 * under its title, before the external attributes are added — so it keeps
 * the app's own link shape: this tab, no `target="_blank"`.
 *
 * `openLocal` opts this surface into the openable dead link: a refused
 * `file:` link becomes a button that asks the engine to open the path. Only
 * a surface whose click chain handles that button (the delegated
 * `handleOpenLocalFileClick`) may opt in — a control that looks clickable and
 * does nothing is the very defect the inert text replaced.
 */
export function renderMarkdown(
  text: string,
  knownPaths: string[] = [],
  knownChats: ReadonlyMap<string, string> = NO_KNOWN_CHATS,
  opts: RenderOptions = {},
): string {
  openLocalFiles = !!opts.openLocal
  try {
    const html = chatMarkdownParser.parse(text) as string
    const linked = linkifyChatIds(html, knownChats)
    return neutralizeDeadAnchors(
      linkifyHtml(sanitizeHtml(withExternalLinkAttrs(linked)), knownPaths),
    )
  } catch {
    return neutralizeDeadAnchors(sanitizeHtml(text))
  } finally {
    openLocalFiles = false
  }
}

// User-typed text is shown as typed: raw HTML is escaped to text instead of
// being parsed (issue #867). The one exception is the four comment tags the
// app writes itself (lib/commentContext.ts), which render as quote cards.
const COMMENT_TAG_NAMES = COMMENT_TAGS.join('|')
const NON_COMMENT_LT = new RegExp(`<(?!/?(?:${COMMENT_TAG_NAMES})>)`, 'gi')
const COMMENT_TAG_ANY = new RegExp(`</?(?:${COMMENT_TAG_NAMES})>`, 'i')

const userMarkdownRenderer = new Renderer()
Object.assign(userMarkdownRenderer, {
  table: chatMarkdownRenderer.table,
  code: chatMarkdownRenderer.code,
  link: chatMarkdownRenderer.link,
})
userMarkdownRenderer.html = function html(token: Tokens.HTML | Tokens.Tag): string {
  const escaped = token.text.replace(NON_COMMENT_LT, '&lt;')
  // Inline fragments sit inside a paragraph already. A block of raw HTML has
  // no paragraph, so give it one and keep its line breaks.
  if (!token.block || COMMENT_TAG_ANY.test(token.text)) return escaped
  return `<p>${escaped.replace(/\n+$/, '').replace(/\n/g, '<br>')}</p>`
}

const userMarkdownParser = new Marked({
  ...MARKDOWN_OPTIONS,
  renderer: userMarkdownRenderer,
})

/**
 * Render a typed message. `openLocal` opts this surface into the openable
 * dead link; see `renderMarkdown` for who may opt in.
 */
export function renderUserMarkdown(
  text: string,
  knownPaths: string[] = [],
  knownChats: ReadonlyMap<string, string> = NO_KNOWN_CHATS,
  opts: RenderOptions = {},
): string {
  openLocalFiles = !!opts.openLocal
  try {
    const html = userMarkdownParser.parse(text) as string
    const linked = linkifyChatIds(html, knownChats)
    return neutralizeDeadAnchors(
      linkifyHtml(sanitizeHtml(withExternalLinkAttrs(linked)), knownPaths),
    )
  } catch {
    return escapeAttr(text)
  } finally {
    openLocalFiles = false
  }
}

/**
 * Render a note for the file viewer. `options.openLocal` opts this surface
 * into the openable dead link; see `renderMarkdown` for who may opt in.
 */
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
  openLocalFiles = !!options.openLocal

  const renderer = {
    image({ href, title, text: alt }: { href: string; title: string | null; text: string }): string {
      const src = href ? options.resolveImageSrc(href) : ''
      const titleAttr = title ? ` title="${escapeAttr(title)}"` : ''
      return `<img src="${escapeAttr(src)}" alt="${escapeAttr(alt ?? '')}"${titleAttr} loading="lazy" />`
    },
    link(this: Renderer, token: Tokens.Link): string {
      // A target the sanitizer drops is inert text, not a link.
      if (!isUsableHref(token.href)) return deadLinkHtml(this, token)
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
    return neutralizeDeadAnchors(sanitizeHtml(parser.parse(text) as string))
  } catch {
    return neutralizeDeadAnchors(sanitizeHtml(text))
  } finally {
    openLocalFiles = false
  }
}
