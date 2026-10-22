import { describe, expect, it } from 'vitest'
import { mcpCommand, quoteArg, serverUrlError, validServerName, validServerUrl } from './shellQuote'

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

describe('url hardening', () => {
  const add = (url: string, shell: 'posix' | 'powershell' = 'posix', cdPrefix = PREFIX) =>
    mcpCommand({ shell, cdPrefix, provider: 'claude', action: 'add', name: 'notion', url })
  const tail = (cmd: string | null) => cmd?.split('http notion ')[1]

  it('percent-encodes curly quotes and braces instead of passing them through', () => {
    for (const ch of ['\u2018', '\u2019', '\u201A', '\u201B']) {
      const cmd = add(`https://example.com/a${ch}b`)
      expect(cmd, ch).not.toBeNull()
      expect(cmd!).not.toContain(ch)
      // Percent-encoding puts a % in the href, which PowerShell refuses.
      expect(add(`https://example.com/a${ch}b`, 'powershell'), ch).toBeNull()
    }
    expect(tail(add('https://example.com/{a,b}'))).toBe('https://example.com/%7Ba,b%7D')
  })
  it('turns a backslash into a slash so it cannot escape the quote', () => {
    expect(tail(add("https://example.com/a\\'b"))).toBe("'https://example.com/a/'\\''b'")
  })
  it('uses the ascii href for non-ascii hosts', () => {
    expect(tail(add('https://exämple.com/mcp'))).toBe('https://xn--exmple-cua.com/mcp')
  })
  it('rejects cmd.exe metacharacters under powershell only', () => {
    for (const ch of ['&', '|', '<', '>', '^', '%', '"', '!']) {
      const url = `https://example.com/a${ch}b`
      expect(add(url, 'powershell'), ch).toBeNull()
      expect(serverUrlError(url, 'powershell'), ch).toContain("Windows terminal can't pass safely")
    }
    expect(add('https://example.com/a&b')).toBe(`${PREFIX}claude mcp add --scope local --transport http notion 'https://example.com/a&b'`)
    expect(serverUrlError('https://example.com/a&b', 'posix')).toBeNull()
  })
  it('doubles every curly quote in a powershell argument', () => {
    expect(quoteArg('a\u2018b\u2019c\u201Ad\u201Be', 'powershell')).toBe("'a\u2018\u2018b\u2019\u2019c\u201A\u201Ad\u201B\u201Be'")
  })
  it('handles a very long url without breaking the quoting', () => {
    const url = `https://example.com/${'a'.repeat(2500)}`
    expect(tail(add(url))).toBe(url)
  })
})
