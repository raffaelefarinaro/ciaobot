import { describe, expect, it } from 'vitest'
import { buildFixPrompt, CIAOBOT_ISSUES_URL } from './fixError'

describe('buildFixPrompt', () => {
  it('embeds the error log inside a fenced block', () => {
    const prompt = buildFixPrompt({ errorText: 'Error: boom at step 3' })
    expect(prompt).toContain('## Error log')
    expect(prompt).toContain('```\nError: boom at step 3\n```')
  })

  it('instructs the GitHub-issue fallback for product bugs', () => {
    const prompt = buildFixPrompt({ errorText: 'x' })
    expect(prompt).toContain('bug in the Ciaobot product itself')
    expect(prompt).toContain('ask for my approval')
    expect(prompt).toContain(CIAOBOT_ISSUES_URL)
    expect(prompt).toContain('gh auth login')
  })

  it('includes context only when provided', () => {
    const withCtx = buildFixPrompt({ errorText: 'x', context: 'I clicked send' })
    expect(withCtx).toContain('## What I was doing')
    expect(withCtx).toContain('I clicked send')

    const noCtx = buildFixPrompt({ errorText: 'x' })
    expect(noCtx).not.toContain('## What I was doing')
  })

  it('falls back to a placeholder when the error text is empty', () => {
    const prompt = buildFixPrompt({ errorText: '   ' })
    expect(prompt).toContain('(no error text captured)')
  })
})
