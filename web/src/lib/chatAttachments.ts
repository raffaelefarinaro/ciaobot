type FileWithDesktopPath = File & { path?: string }

/** Return a desktop-provided absolute path, ignoring browser-relative paths. */
export function nativeAbsoluteFilePath(file: File): string | null {
  const path = (file as FileWithDesktopPath).path
  if (!path) return null
  if (path.startsWith('/')) return path
  if (/^[A-Za-z]:[\\/]/.test(path)) return path
  if (/^\\\\[^\\]/.test(path)) return path
  return null
}

/** Keep paths containing spaces or punctuation intact in the chat prompt. */
export function formatAttachedFilePath(path: string): string {
  if (/[\r\n`]/.test(path)) return JSON.stringify(path)
  return `\`${path}\``
}

const FILE_REF_RE = /^drop_[0-9a-f]{32}$/
const TOKEN_RE = /^ciao-drop:(drop_[0-9a-f]{32})$/
const ATTACHED_HEADER = 'Attached files:'
const BLOCK_RE = /(?:^|\n\n)Attached files:\n((?:- .+(?:\n|$))+)$/
const LINE_RE = /^- (.+?): `(.+)`$/

type FileRef = { ref: string; name?: string }
type UploadFailure = { filename: string; error: string }

export type UploadProgressHandler = (loaded: number, total: number) => void
export interface FormPostResult { status: number; body: unknown }
export type FormPoster = (url: string, form: FormData, onProgress?: UploadProgressHandler) => Promise<FormPostResult>
/** A file the server stored for a chat, staged in the composer before send. */
export interface AttachedFile { ref: string; name: string }
/** An attachment parsed back out of a sent message (token before expansion, path after). */
export interface MessageAttachment { name: string; ref: string | null; path: string | null }

export interface AttachmentUploadResult {
  /** Files the server stored, ready to stage on the chat. */
  files: AttachedFile[]
  /** Image refs to stage on the chat, from a desktop drop. */
  imageRefs: string[]
  /** Per-file failures the server reported alongside a successful request. */
  failures: UploadFailure[]
}

/** POST a multipart form with upload progress (fetch cannot report it). */
export function postFormWithProgress(
  url: string,
  form: FormData,
  onProgress: UploadProgressHandler = () => {},
  createXhr: () => XMLHttpRequest = () => new XMLHttpRequest(),
): Promise<FormPostResult> {
  return new Promise((resolve, reject) => {
    const xhr = createXhr()
    xhr.open('POST', url)
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(event.loaded, event.total) }
    xhr.onload = () => {
      let body: unknown = {}
      try { body = JSON.parse(xhr.responseText) } catch { body = {} }
      resolve({ status: xhr.status, body })
    }
    xhr.onerror = () => reject(new Error('Upload failed: the connection was interrupted.'))
    xhr.onabort = () => reject(new Error('Upload cancelled.'))
    xhr.send(form)
  })
}

function cleanName(name: unknown): string {
  const text = typeof name === 'string' ? name.replace(/[\r\n`]/g, ' ').trim() : ''
  return text || 'file'
}

function attachedFiles(entries: FileRef[] | undefined): AttachedFile[] {
  return (entries || [])
    .filter(entry => typeof entry.ref === 'string' && FILE_REF_RE.test(entry.ref))
    .map(entry => ({ ref: entry.ref, name: cleanName(entry.name) }))
}

/** The labelled block a send appends; the engine expands each token in place. */
export function composeAttachmentBlock(files: AttachedFile[]): string {
  if (!files.length) return ''
  return [ATTACHED_HEADER, ...files.map(f => `- ${cleanName(f.name)}: ${formatAttachedFilePath(`ciao-drop:${f.ref}`)}`)].join('\n')
}

/** Split a user message into its text and the trailing attachment block (token or expanded-path form). */
export function splitAttachmentBlock(content: string): { text: string; attachments: MessageAttachment[] } {
  const trimmed = content.replace(/\s+$/, '')
  const match = BLOCK_RE.exec(trimmed)
  if (!match) return { text: content, attachments: [] }
  const attachments: MessageAttachment[] = []
  for (const line of match[1].split('\n')) {
    if (!line) continue
    const parts = LINE_RE.exec(line)
    if (!parts) return { text: content, attachments: [] }
    const token = TOKEN_RE.exec(parts[2])
    attachments.push({ name: parts[1], ref: token ? token[1] : null, path: token ? null : parts[2] })
  }
  return { text: trimmed.slice(0, match.index).trimEnd(), attachments }
}

/** Upload non-image files as a chat's attachments (into its project folder). */
export async function uploadChatAttachments(
  chatId: string,
  files: File[],
  onProgress?: UploadProgressHandler,
  post: FormPoster = postFormWithProgress,
): Promise<AttachmentUploadResult> {
  const form = new FormData()
  files.forEach((file, index) => form.append(`file${index}`, file, file.name))
  const { status, body } = await post(`/api/chats/${encodeURIComponent(chatId)}/attachments?opaque=1`, form, onProgress)
  const result = (body || {}) as { file_refs?: FileRef[]; errors?: UploadFailure[]; error?: string }
  if (status < 200 || status >= 300) throw new Error(result.error || `Upload failed (HTTP ${status})`)
  return { files: attachedFiles(result.file_refs), imageRefs: [], failures: result.errors || [] }
}

/** Import a desktop (Finder) drop grant into a chat. */
export async function importDesktopDrop(
  grantId: string,
  target: { chatId: string; projectId: string },
  fetchImpl: typeof fetch = (...args) => fetch(...args),
): Promise<AttachmentUploadResult> {
  const response = await fetchImpl('/api/desktop-drop', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    redirect: 'manual',
    body: JSON.stringify({ grant_id: grantId, project_id: target.projectId, chat_id: target.chatId }),
  })
  const result = await response.json().catch(() => ({})) as {
    file_refs?: FileRef[]; image_refs?: string[]; errors?: UploadFailure[]; error?: string
  }
  if (!response.ok) throw new Error(result.error || `Native file import failed (HTTP ${response.status})`)
  return { files: attachedFiles(result.file_refs), imageRefs: result.image_refs || [], failures: result.errors || [] }
}
