// Ciaobot's own system prompt, appended to every provider's (ciao/system_prompt.md).

/**
 * The text Ciaobot adds to the provider's system prompt: '' for a 200 with no
 * text, null when the read failed (non-OK response or exception), so a caller
 * can retry instead of treating a cold engine as "no prompt".
 */
export async function fetchSystemPrompt(): Promise<string | null> {
  try {
    const resp = await fetch('/api/system-prompt', { credentials: 'same-origin' })
    if (!resp.ok) return null
    const body = await resp.json() as { text?: unknown }
    return typeof body.text === 'string' ? body.text : ''
  } catch { return null }
}
