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

type FileRef = { ref: string; name?: string }
type UploadFailure = { filename: string; error: string }

export interface AttachmentUploadResult {
  /** Prompt tokens (`ciao-drop:<ref>`, formatted) for the uploaded files. */
  fileRefs: string[]
  /** Image refs to stage on the chat, from a desktop drop. */
  imageRefs: string[]
  /** Per-file failures the server reported alongside a successful request. */
  failures: UploadFailure[]
}

/** The prompt token for an uploaded file ref, or null for anything else. */
export function fileRefText(ref: string | undefined): string | null {
  if (!ref || !/^drop_[0-9a-f]{32}$/.test(ref)) return null
  return formatAttachedFilePath(`ciao-drop:${ref}`)
}

function localControlHeaders(): Record<string, string> {
  return window.location.hostname === '127.0.0.1' ? { 'X-Ciao-Local-Control': '1' } : {}
}

function refsFrom(entries: FileRef[] | undefined): string[] {
  return (entries || [])
    .map(entry => fileRefText(entry.ref))
    .filter((value): value is string => Boolean(value))
}

/** Upload non-image files as a chat's attachments (into its project folder). */
export async function uploadChatAttachments(
  chatId: string,
  files: File[],
  fetchImpl: typeof fetch = (...args) => fetch(...args),
): Promise<AttachmentUploadResult> {
  const form = new FormData()
  files.forEach((file, index) => form.append(`file${index}`, file, file.name))
  const response = await fetchImpl(`/api/chats/${encodeURIComponent(chatId)}/attachments?opaque=1`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: localControlHeaders(),
    redirect: 'manual',
    body: form,
  })
  const result = await response.json().catch(() => ({})) as { file_refs?: FileRef[]; errors?: UploadFailure[]; error?: string }
  if (!response.ok) throw new Error(result.error || `Upload failed (HTTP ${response.status})`)
  return { fileRefs: refsFrom(result.file_refs), imageRefs: [], failures: result.errors || [] }
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
    headers: { 'Content-Type': 'application/json', ...localControlHeaders() },
    redirect: 'manual',
    body: JSON.stringify({ grant_id: grantId, project_id: target.projectId, chat_id: target.chatId }),
  })
  const result = await response.json().catch(() => ({})) as {
    file_refs?: FileRef[]; image_refs?: string[]; errors?: UploadFailure[]; error?: string
  }
  if (!response.ok) throw new Error(result.error || `Native file import failed (HTTP ${response.status})`)
  return { fileRefs: refsFrom(result.file_refs), imageRefs: result.image_refs || [], failures: result.errors || [] }
}
