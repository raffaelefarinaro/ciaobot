export type ShellKind = 'posix'

const SAFE_ARG = /^[A-Za-z0-9_@%+=:,./-]+$/

export function quoteArg(arg: string, _shell: ShellKind): string {
  if (SAFE_ARG.test(arg)) return arg
  return `'${arg.replace(/'/g, "'\\''")}'`
}
