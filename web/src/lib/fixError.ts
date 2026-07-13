// Builds the prompt that seeds a "Fix this error" chat. When something in
// Ciaobot errors, the user clicks a button and we open a fresh chat in the
// General project pre-filled with this prompt + the error log. The agent is
// asked to diagnose and fix; if the root cause is a bug in Ciaobot's own
// product code and it can't be fixed from here, it should open a GitHub issue
// instead (per the repo contributor guide).

// GitHub repo used for the product-bug fallback. Keep in sync with CLAUDE.md.
export const CIAOBOT_REPO = 'raffaelefarinaro/ciaobot'
export const CIAOBOT_ISSUES_URL = `https://github.com/${CIAOBOT_REPO}/issues/new`

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
    `If the root cause is a bug in the Ciaobot product itself (its own source code) and you cannot fix it from here, do NOT apply a workaround. Prepare a concise GitHub issue instead, with the evidence below. Before publishing anything publicly, ask for my approval.`,
    '',
    `Tell me that submitting an issue requires a GitHub account. I can use ${CIAOBOT_ISSUES_URL} in a browser, where GitHub will let me sign in or create one; I do not need the \`gh\` CLI for that. If I specifically ask you to submit the approved issue with \`gh\`, confirm that it is authenticated first; otherwise ask me to run \`gh auth login\` and do not ask for my credentials.`,
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
