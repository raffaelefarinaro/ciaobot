import DOMPurify from 'dompurify'
import { Marked, Renderer, type Tokens } from 'marked'

import { CODE_BLOCK_CLASS, codeCopyButtonHtml } from './codeCopy'
import { COMMENT_TAGS } from './commentContext'
import { linkifyHtml } from './filePaths'
import { linkifyWikilinksInMarkdown } from './wikilinks'

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
  const source = options.filePath && options.markdownPaths?.length
    ? linkifyWikilinksInMarkdown(text, options.filePath, options.markdownPaths)
    : text
  const renderer = {
    image({ href, title, text: alt }: { href: string; title: string | null; text: string }): string {
      const src = href ? options.resolveImageSrc(href) : ''
      const titleAttr = title ? ` title="${escapeAttr(title)}"` : ''
      return `<img src="${escapeAttr(src)}" alt="${escapeAttr(alt ?? '')}"${titleAttr} loading="lazy" />`
    },
  }
  const parser = new Marked({ ...MARKDOWN_OPTIONS, renderer })
  try {
    return sanitizeHtml(parser.parse(source) as string)
  } catch {
    return sanitizeHtml(source)
  }
}
