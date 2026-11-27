// Mirrors `_WORKSPACE_FILE_EXTS` in ciao/web/routes_api.py: the only files the
// server accepts as a pin. Anything else would 415 and toast, so the tile
// opens it in the viewer instead of trying to dock it.
const PINNABLE_FILE_EXTS = new Set([
  'md', 'markdown', 'txt', 'py', 'ts', 'tsx', 'js', 'jsx', 'vue', 'css', 'html', 'htm', 'json',
  'yaml', 'yml', 'toml', 'sh', 'rs', 'go', 'java', 'xml', 'sql', 'cfg', 'ini', 'log', 'csv',
])
export function isPinnableFilePath(path: string): boolean {
  const m = /\.([^./\\]+)$/.exec(path.replace(/:\d+$/, ''))
  return !!m && PINNABLE_FILE_EXTS.has(m[1].toLowerCase())
}
