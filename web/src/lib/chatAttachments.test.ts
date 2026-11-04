import { describe, expect, it, vi } from 'vitest'

import {
  composeAttachmentBlock,
  formatAttachedFilePath,
  nativeAbsoluteFilePath,
  postFormWithProgress,
  splitAttachmentBlock,
  uploadChatAttachments,
} from './chatAttachments'

class FakeXhr {
  method = ''
  url = ''
  sent: unknown = null
  status = 0
  responseText = ''
  upload: { onprogress: ((event: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null

  open(method: string, url: string): void {
    this.method = method
    this.url = url
  }

  send(body: unknown): void {
    this.sent = body
  }

  respond(status: number, body: unknown): void {
    this.status = status
    this.responseText = JSON.stringify(body)
    this.onload?.()
  }

  progress(loaded: number, total: number): void {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total })
  }
}

const REF = `drop_${'a'.repeat(32)}`

describe('nativeAbsoluteFilePath', () => {
  it('uses an absolute path exposed by a desktop webview', () => {
    const file = {
      name: 'notes.md',
      path: '/Users/ada/Project notes/notes.md',
    } as unknown as File
    expect(nativeAbsoluteFilePath(file)).toBe('/Users/ada/Project notes/notes.md')
  })

  it('rejects browser-relative paths and plain filenames', () => {
    expect(nativeAbsoluteFilePath(
      { name: 'notes.md', path: 'folder/notes.md' } as unknown as File,
    )).toBeNull()
    expect(nativeAbsoluteFilePath({ name: 'notes.md' } as unknown as File)).toBeNull()
  })
})

describe('formatAttachedFilePath', () => {
  it('keeps a path with spaces together as one prompt token', () => {
    expect(formatAttachedFilePath('/Users/ada/Project notes/notes.md'))
      .toBe('`/Users/ada/Project notes/notes.md`')
  })

  it('does not alter a filename containing a backtick', () => {
    expect(formatAttachedFilePath('/Users/ada/a`b.md'))
      .toBe('"/Users/ada/a`b.md"')
  })
})

describe('postFormWithProgress', () => {
  it('reports upload progress and resolves with the parsed body', async () => {
    const xhr = new FakeXhr()
    const onProgress = vi.fn()
    const promise = postFormWithProgress('/api/x', new FormData(), onProgress, () => xhr as unknown as XMLHttpRequest)
    xhr.progress(50, 100)
    expect(onProgress).toHaveBeenCalledWith(50, 100)
    xhr.respond(200, { a: 1 })
    await expect(promise).resolves.toEqual({ status: 200, body: { a: 1 } })
    expect(xhr.method).toBe('POST')
    expect(xhr.url).toBe('/api/x')
  })

  it('rejects when the connection drops', async () => {
    const xhr = new FakeXhr()
    const promise = postFormWithProgress('/api/x', new FormData(), () => {}, () => xhr as unknown as XMLHttpRequest)
    xhr.onerror?.()
    await expect(promise).rejects.toThrow(/interrupted/)
  })

  it('a non-JSON body resolves as an empty object', async () => {
    const xhr = new FakeXhr()
    const promise = postFormWithProgress('/api/x', new FormData(), () => {}, () => xhr as unknown as XMLHttpRequest)
    xhr.status = 500
    xhr.responseText = '<html>proxy error</html>'
    xhr.onload?.()
    await expect(promise).resolves.toEqual({ status: 500, body: {} })
  })
})

describe('uploadChatAttachments', () => {
  it('returns named files and drops malformed refs', async () => {
    const post = vi.fn(async () => ({
      status: 200,
      body: { file_refs: [{ ref: REF, name: 'brief.pdf' }, { ref: 'bad' }] },
    }))
    const result = await uploadChatAttachments('chat-1', [new File(['x'], 'brief.pdf')], undefined, post)
    expect(result.files).toEqual([{ ref: REF, name: 'brief.pdf' }])
    expect(result.imageRefs).toEqual([])
  })

  it('surfaces the server error', async () => {
    const post = vi.fn(async () => ({ status: 409, body: { error: 'no folder' } }))
    await expect(uploadChatAttachments('chat-1', [new File(['x'], 'a.md')], undefined, post))
      .rejects.toThrow('no folder')
  })
})

describe('composeAttachmentBlock / splitAttachmentBlock', () => {
  it('round-trips the token form', () => {
    const block = composeAttachmentBlock([{ ref: REF, name: 'brief.pdf' }])
    expect(splitAttachmentBlock(`Summarise\n\n${block}`)).toEqual({
      text: 'Summarise',
      attachments: [{ name: 'brief.pdf', ref: REF, path: null }],
    })
  })

  it('reads the engine-expanded path form', () => {
    const content = 'Summarise\n\nAttached files:\n- my brief.pdf: `/Users/a/Project x/my brief.pdf`\n- b.md: `C:\\v\\b.md`'
    const parsed = splitAttachmentBlock(content)
    expect(parsed.text).toBe('Summarise')
    expect(parsed.attachments).toEqual([
      { name: 'my brief.pdf', ref: null, path: '/Users/a/Project x/my brief.pdf' },
      { name: 'b.md', ref: null, path: 'C:\\v\\b.md' },
    ])
  })

  it('a files-only message has empty text', () => {
    const block = composeAttachmentBlock([{ ref: REF, name: 'brief.pdf' }])
    expect(splitAttachmentBlock(block).text).toBe('')
  })

  it('leaves text without a block, or with a malformed line, untouched', () => {
    const plain = 'just a note'
    expect(splitAttachmentBlock(plain)).toEqual({ text: plain, attachments: [] })
    const malformed = 'Hi\n\nAttached files:\n- brief.pdf: not a path'
    expect(splitAttachmentBlock(malformed)).toEqual({ text: malformed, attachments: [] })
  })

  it('returns quickly for a long message without a block', () => {
    const input = 'a' + ' '.repeat(50000) + 'b'
    expect(splitAttachmentBlock(input)).toEqual({ text: input, attachments: [] })
  })

  it('sanitises a name with a backtick or newline', () => {
    expect(composeAttachmentBlock([{ ref: REF, name: 'a`b\nc.md' }]))
      .toContain('- a b c.md: `')
  })
})
