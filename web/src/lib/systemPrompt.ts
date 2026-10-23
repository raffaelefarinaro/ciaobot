// Ciaobot's own system prompt, appended to every provider's (ciao/system_prompt.md).

/** The text Ciaobot adds to the provider's system prompt, or '' when unavailable. */
export async function fetchSystemPrompt(): Promise<string> {
  try {
    const resp = await fetch('/api/system-prompt', { credentials: 'same-origin' })
    if (!resp.ok) return ''
    const body = await resp.json() as { text?: unknown }
    return typeof body.text === 'string' ? body.text : ''
  } catch { return '' }
}
