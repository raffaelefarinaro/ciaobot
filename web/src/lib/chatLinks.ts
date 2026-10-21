/**
 * Chat references in prose.
 *
 * The engine mints a chat id as `chat-` plus eight lowercase hex characters
 * (`ciao/web/chat_service.py`, `_uuid8`). When a message names one, the
 * reader should be able to open that conversation: under its human title,
 * with the raw id kept as metadata.
 *
 * Two rules keep the promise honest. Only an id the reader's own chat list
 * knows is linked — an unknown or deleted id stays plain text, because a
 * link to a conversation that cannot be opened is worse than no link. And
 * the id has to stand alone as a token: `notes/chat-0123abcd`,
 * `chat-0123abcdef` and `chat-0123abcd-extra` are other things, not a
 * reference to a conversation.
 *
 * The label is built as DOM text, so a title the engine produced can never
 * become markup.
 */

/** The id shape the engine mints: `chat-` plus eight lowercase hex digits. */
const CHAT_ID_SOURCE = 'chat-[0-9a-f]{8}'

/** True when the whole string is exactly a chat id. */
const CHAT_ID_RE = new RegExp(`^${CHAT_ID_SOURCE}$`)

/**
 * What may not touch a reference.
 *
 * A word character, a dash or a path separator on either side means the id
 * is part of a longer token: a longer hex run, a suffixed name, a file path
 * or the path part of a URL.
 */
const TOKEN_CHAR = /[\w/\\-]/

/** Elements whose content is never a reference. */
const SKIP_TAGS = new Set(['A', 'PRE', 'SCRIPT', 'STYLE', 'TEXTAREA', 'BUTTON'])

const ELEMENT_NODE = 1
const TEXT_NODE = 3

/** True when the string is exactly a chat id. */
export function isChatId(value: string): boolean {
  return CHAT_ID_RE.test(value || '')
}

type Reference = { index: number; id: string; title: string }

/**
 * The references in one run of text.
 *
 * A reference has to be a word of its own: the characters around it may not
 * be word characters, dashes or path separators, and the token it sits in
 * may not be a URL — a query value like `?to=chat-0123abcd` names a chat
 * inside a link, not a conversation.
 */
function findReferences(text: string, known: ReadonlyMap<string, string>): Reference[] {
  const re = new RegExp(CHAT_ID_SOURCE, 'g')
  const found: Reference[] = []
  let match: RegExpExecArray | null
  while ((match = re.exec(text)) !== null) {
    const id = match[0]
    const start = match.index
    const end = start + id.length
    const before = start > 0 ? text[start - 1] : ''
    const after = end < text.length ? text[end] : ''
    if (before && TOKEN_CHAR.test(before)) continue
    if (after && TOKEN_CHAR.test(after)) continue
    if (isInsideUrl(text, start)) continue
    const title = known.get(id)
    if (title === undefined) continue
    found.push({ index: start, id, title })
  }
  return found
}

/**
 * True when the match sits inside a URL.
 *
 * The token around the reference is read back to its whitespace boundaries;
 * a token that carries a scheme is a link, and an id inside it is part of
 * the address rather than a reference to a conversation.
 */
function isInsideUrl(text: string, index: number): boolean {
  let start = index
  while (start > 0 && !/\s/.test(text[start - 1]!)) start--
  let end = index
  while (end < text.length && !/\s/.test(text[end]!)) end++
  return text.slice(start, end).includes('://')
}

/**
 * The link for one reference.
 *
 * The label is set as text, so a title the engine produced is data and can
 * never become markup; the id stays in the tooltip and in `data-chat-id`,
 * so the destination is the id and the title is only what the reader sees.
 */
function buildLink(doc: Document, id: string, title: string): HTMLAnchorElement {
  const link = doc.createElement('a')
  link.setAttribute('class', 'chat-link')
  link.setAttribute('href', `/chat/${id}`)
  link.setAttribute('title', id)
  link.setAttribute('data-chat-id', id)
  link.textContent = title.trim() || id
  return link
}

/** Replace the references in one text node. Returns true when it changed. */
function linkifyText(node: Text, known: ReadonlyMap<string, string>): boolean {
  const text = node.nodeValue || ''
  const references = findReferences(text, known)
  if (!references.length) return false
  const doc = node.ownerDocument
  const fragment = doc.createDocumentFragment()
  let last = 0
  for (const ref of references) {
    if (ref.index > last) {
      fragment.appendChild(doc.createTextNode(text.slice(last, ref.index)))
    }
    fragment.appendChild(buildLink(doc, ref.id, ref.title))
    last = ref.index + ref.id.length
  }
  if (last < text.length) {
    fragment.appendChild(doc.createTextNode(text.slice(last)))
  }
  if (!node.parentNode) return false
  node.parentNode.replaceChild(fragment, node)
  return true
}

/** Walk the children of a node, skipping the content of excluded elements. */
function linkifyChildren(parent: Node, known: ReadonlyMap<string, string>): boolean {
  let changed = false
  for (const child of Array.from(parent.childNodes)) {
    if (child.nodeType === TEXT_NODE) {
      if (linkifyText(child as Text, known)) changed = true
      continue
    }
    if (child.nodeType !== ELEMENT_NODE) continue
    const element = child as Element
    if (SKIP_TAGS.has(element.tagName)) continue
    if (linkifyChildren(element, known)) changed = true
  }
  return changed
}

/**
 * Turn the known chat ids in a fragment of HTML into links.
 *
 * Only the ids in `known` — the reader's own chat list — are linked, and
 * only when they stand alone in the text. Everything else is left exactly
 * as it was: existing anchors, their attributes, and the content of a code
 * block are never rewritten.
 */
export function linkifyChatIds(html: string, known: ReadonlyMap<string, string>): string {
  if (!html || known.size === 0) return html
  // Most messages name no chat at all, and the DOM round trip below is not
  // free; a plain substring test keeps those out of it.
  if (!html.includes('chat-')) return html
  const template = document.createElement('template')
  template.innerHTML = html
  if (!linkifyChildren(template.content, known)) return html
  return template.innerHTML
}
