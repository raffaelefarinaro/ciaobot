/** Human message for a failed workspace file/image load. `limit` is the size cap shown for 413. */
export function fileLoadErrorMessage(status: number, limit = '2 MB'): string {
  if (status === 404) return 'File not found.'
  if (status === 403) return 'Forbidden — path is outside the workspace.'
  if (status === 413) return `File is too large to preview (>${limit}).`
  if (status === 415) return 'Unsupported file type.'
  return `Failed to load file (HTTP ${status}).`
}

/** Message for an <img> that failed: fetches `url` once to learn why. Never throws. */
export async function imageLoadErrorMessage(url: string): Promise<string> {
  let resp: Response
  try {
    resp = await fetch(url, { credentials: 'same-origin' })
  } catch {
    return 'Could not load this image.'
  }
  if (resp.ok) return 'This image could not be displayed (unsupported or damaged file).'
  return fileLoadErrorMessage(resp.status, '15 MB')
}
