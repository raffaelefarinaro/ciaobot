import DOMPurify from 'dompurify'
import { Marked, marked } from 'marked'

import { linkifyHtml } from './filePaths'

type FileMarkdownOptions = {
  resolveImageSrc: (href: string) => string
}

const MARKDOWN_OPTIONS = { breaks: true }

function sanitizeHtml(html: string): string {
  return DOMPurify.sanitize(html, {
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
    const html = marked.parse(text, MARKDOWN_OPTIONS) as string
    return linkifyHtml(sanitizeHtml(withExternalLinkAttrs(html)), knownPaths)
  } catch {
    return sanitizeHtml(text)
  }
}

export function renderFileMarkdown(text: string, options: FileMarkdownOptions): string {
  const renderer = {
    image({ href, title, text: alt }: { href: string; title: string | null; text: string }): string {
      const src = href ? options.resolveImageSrc(href) : ''
      const titleAttr = title ? ` title="${escapeAttr(title)}"` : ''
      return `<img src="${escapeAttr(src)}" alt="${escapeAttr(alt ?? '')}"${titleAttr} loading="lazy" />`
    },
  }
  const parser = new Marked({ ...MARKDOWN_OPTIONS, renderer })
  try {
    return sanitizeHtml(parser.parse(text) as string)
  } catch {
    return sanitizeHtml(text)
  }
}
