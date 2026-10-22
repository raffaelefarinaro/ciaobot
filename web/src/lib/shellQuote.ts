export type ShellKind = 'posix' | 'powershell'
export type McpProvider = 'claude' | 'opencode'
export type McpAction = 'add' | 'list' | 'login' | 'remove' | 'logout'

const SAFE_ARG = /^[A-Za-z0-9_@%+=:,./-]+$/

export function quoteArg(arg: string, shell: ShellKind): string {
  if (SAFE_ARG.test(arg)) return arg
  const escaped = shell === 'powershell' ? arg.replace(/['\u2018-\u201B]/g, ch => ch + ch) : arg.replace(/'/g, "'\\''")
  return `'${escaped}'`
}

export function validServerName(name: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(name)
}

export function validServerUrl(url: string): boolean {
  // eslint-disable-next-line no-control-regex
  if (/[\s\u0000-\u001f\u007f]/.test(url)) return false
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    return false
  }
}

const HREF_ALLOWLIST = /^[A-Za-z0-9._~:/?#[\]@!$&'()*+,;=%-]+$/
const CMD_EXE_META = /[&|<>^%"!]/

/** Why a URL cannot be put in a command for this shell, or null when it can. */
export function serverUrlError(raw: string, shell: ShellKind): string | null {
  if (!validServerUrl(raw)) return 'Enter an http:// or https:// URL without spaces.'
  const href = new URL(raw).href
  if (shell === 'powershell' && (CMD_EXE_META.test(raw) || CMD_EXE_META.test(href))) {
    return "This URL has characters the Windows terminal can't pass safely. Use Copy and adjust the command by hand."
  }
  if (!HREF_ALLOWLIST.test(href)) return 'Enter an http:// or https:// URL using plain ASCII characters.'
  return null
}

export function mcpCommand(a: {
  provider: McpProvider
  action: McpAction
  name?: string
  url?: string
  shell: ShellKind
  cdPrefix: string
}): string | null {
  const needsName = a.action === 'add' || a.action === 'login' || a.action === 'remove' || a.action === 'logout'
  let name = ''
  if (needsName) {
    if (!a.name || !validServerName(a.name)) return null
    name = quoteArg(a.name, a.shell)
  }
  let url = ''
  if (a.action === 'add') {
    if (!a.url || serverUrlError(a.url, a.shell)) return null
    url = quoteArg(new URL(a.url).href, a.shell)
  }
  let cmd: string | null
  if (a.provider === 'claude') {
    cmd = {
      add: `claude mcp add --scope local --transport http ${name} ${url}`,
      list: 'claude mcp list',
      login: `claude mcp login ${name}`,
      remove: `claude mcp remove ${name}`,
      logout: `claude mcp logout ${name}`,
    }[a.action]
  } else {
    cmd = {
      add: `opencode mcp add ${name} --url ${url}`,
      list: 'opencode mcp list',
      login: `opencode mcp auth ${name}`,
      remove: null,
      logout: `opencode mcp logout ${name}`,
    }[a.action]
  }
  return cmd === null ? null : a.cdPrefix + cmd
}
