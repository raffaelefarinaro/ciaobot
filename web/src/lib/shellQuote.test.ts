import { describe, expect, it } from 'vitest'
import { mcpCommand, quoteArg, validServerName, validServerUrl } from './shellQuote'

const PREFIX = 'cd -- /workspace/personal && '
const base = { shell: 'posix' as const, cdPrefix: PREFIX }

describe('quoteArg', () => {
  it('quotes only when needed', () => {
    expect(quoteArg('notion', 'posix')).toBe('notion')
    expect(quoteArg("it's", 'posix')).toBe("'it'\\''s'")
    expect(quoteArg('a b; rm -rf /', 'posix')).toBe("'a b; rm -rf /'")
    expect(quoteArg('', 'posix')).toBe("''")
    expect(quoteArg("it's", 'powershell')).toBe("'it''s'")
  })
})

describe('validators', () => {
  it('validates server names', () => {
    expect(validServerName('notion')).toBe(true)
    expect(validServerName('my-server_1.2')).toBe(true)
    for (const bad of ['', 'a b', '-x', 'a;b', 'a'.repeat(65)]) expect(validServerName(bad)).toBe(false)
  })
  it('validates server urls', () => {
    expect(validServerUrl('https://example.com/mcp')).toBe(true)
    for (const bad of ['ftp://x', 'javascript:alert(1)', 'not a url', 'https://example.com/a b']) expect(validServerUrl(bad)).toBe(false)
  })
})

describe('mcpCommand', () => {
  const args = { name: 'notion', url: 'https://example.com/mcp' }
  it('builds claude commands', () => {
    expect(mcpCommand({ ...base, provider: 'claude', action: 'add', ...args })).toBe(`${PREFIX}claude mcp add --scope local --transport http notion https://example.com/mcp`)
    expect(mcpCommand({ ...base, provider: 'claude', action: 'list' })).toBe(`${PREFIX}claude mcp list`)
    expect(mcpCommand({ ...base, provider: 'claude', action: 'login', name: 'notion' })).toBe(`${PREFIX}claude mcp login notion`)
    expect(mcpCommand({ ...base, provider: 'claude', action: 'remove', name: 'notion' })).toBe(`${PREFIX}claude mcp remove notion`)
    expect(mcpCommand({ ...base, provider: 'claude', action: 'logout', name: 'notion' })).toBe(`${PREFIX}claude mcp logout notion`)
  })
  it('builds opencode commands and has no remove', () => {
    expect(mcpCommand({ ...base, provider: 'opencode', action: 'add', ...args })).toBe(`${PREFIX}opencode mcp add notion --url https://example.com/mcp`)
    expect(mcpCommand({ ...base, provider: 'opencode', action: 'login', name: 'notion' })).toBe(`${PREFIX}opencode mcp auth notion`)
    expect(mcpCommand({ ...base, provider: 'opencode', action: 'logout', name: 'notion' })).toBe(`${PREFIX}opencode mcp logout notion`)
    expect(mcpCommand({ ...base, provider: 'opencode', action: 'remove', name: 'notion' })).toBeNull()
  })
  it('returns null for missing or invalid input and quotes urls safely', () => {
    expect(mcpCommand({ ...base, provider: 'claude', action: 'login' })).toBeNull()
    expect(mcpCommand({ ...base, provider: 'claude', action: 'add', name: 'a b', url: args.url })).toBeNull()
    expect(mcpCommand({ ...base, provider: 'claude', action: 'add', name: 'notion', url: 'ftp://x' })).toBeNull()
    expect(mcpCommand({ ...base, provider: 'claude', action: 'add', name: 'notion', url: "https://example.com/a'b" })).toBe(`${PREFIX}claude mcp add --scope local --transport http notion 'https://example.com/a'\\''b'`)
    expect(mcpCommand({ shell: 'powershell', cdPrefix: "Set-Location -LiteralPath 'C:\\w'; ", provider: 'claude', action: 'add', name: 'notion', url: "https://example.com/a'b" })).toBe("Set-Location -LiteralPath 'C:\\w'; claude mcp add --scope local --transport http notion 'https://example.com/a''b'")
  })
})
