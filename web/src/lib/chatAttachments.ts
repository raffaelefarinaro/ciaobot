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
