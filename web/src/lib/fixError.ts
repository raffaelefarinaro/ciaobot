// Builds the prompt that seeds a "Fix this error" chat. When something in
// Ciaobot errors, the user clicks a button and we open a fresh chat in the
// General project pre-filled with this prompt + the error log. The agent is
// asked to diagnose and fix; if the root cause is a bug in Ciaobot's own
// product code and it can't be fixed from here, it should open a GitHub issue
// instead (per the repo contributor guide).

// GitHub repo used for the product-bug fallback. Keep in sync with CLAUDE.md.
export const CIAOBOT_REPO = 'raffaelefarinaro/ciaobot'

export interface FixPromptInput {
  // Raw error text / log to diagnose.
  errorText: string
  // Optional context (e.g. what the user was doing when it failed).
  context?: string
}

export function buildFixPrompt({ errorText, context }: FixPromptInput): string {
  const parts: string[] = [
    'Something went wrong in Ciaobot and I want you to fix it.',
    '',
    'Diagnose the root cause from the error log below and fix it.',
    '',
    `If the root cause is a bug in the Ciaobot product itself (its own source code) and you cannot fix it from here, do NOT apply a workaround. Instead, open a GitHub issue in \`${CIAOBOT_REPO}\` using the \`gh\` CLI, for example:`,
    '',
    '```bash',
    `gh issue create --repo ${CIAOBOT_REPO} --title "[Agent] <short summary>" --body "<what failed, repro steps, relevant code locations, and the log below>"`,
    '```',
    '',
    'Explain clearly what you found and what you did.',
    '',
    '## Error log',
    '```',
    (errorText || '').trim() || '(no error text captured)',
    '```',
  ]
  if (context && context.trim()) {
    parts.push('', '## What I was doing', context.trim())
  }
  return parts.join('\n')
}
