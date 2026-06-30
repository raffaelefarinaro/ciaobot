export type TerminalDiffKind = 'del' | 'ins' | 'skip' | 'empty'

export interface TerminalDiffLine {
  kind: TerminalDiffKind
  text: string
}

type RawDiffLine = { kind: 'eq' | 'del' | 'ins'; text: string }

function rawLineDiff(a: string, b: string): RawDiffLine[] {
  const al = a.split('\n')
  const bl = b.split('\n')
  const m = al.length
  const n = bl.length
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array<number>(n + 1).fill(0))

  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (al[i - 1] === bl[j - 1]) dp[i][j] = dp[i - 1][j - 1] + 1
      else dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1])
    }
  }

  const out: RawDiffLine[] = []
  let i = m
  let j = n
  while (i > 0 && j > 0) {
    if (al[i - 1] === bl[j - 1]) {
      out.unshift({ kind: 'eq', text: al[i - 1] })
      i--
      j--
    } else if (dp[i - 1][j] > dp[i][j - 1]) {
      out.unshift({ kind: 'del', text: al[i - 1] })
      i--
    } else {
      out.unshift({ kind: 'ins', text: bl[j - 1] })
      j--
    }
  }
  while (i > 0) {
    out.unshift({ kind: 'del', text: al[i - 1] })
    i--
  }
  while (j > 0) {
    out.unshift({ kind: 'ins', text: bl[j - 1] })
    j--
  }

  return out
}

function skippedLabel(count: number): string {
  return `… ${count} unchanged line${count === 1 ? '' : 's'} …`
}

export function createTerminalDiffLines(a: string, b: string): TerminalDiffLine[] {
  const raw = rawLineDiff(a, b)
  const out: TerminalDiffLine[] = []
  let skipped = 0

  for (const line of raw) {
    if (line.kind === 'eq') {
      skipped++
      continue
    }
    if (skipped > 0 && out.length > 0) out.push({ kind: 'skip', text: skippedLabel(skipped) })
    skipped = 0
    out.push({ kind: line.kind, text: line.text })
  }

  if (out.length === 0) return [{ kind: 'empty', text: 'No differences.' }]
  return out
}

export function terminalDiffPrefix(kind: TerminalDiffKind): string {
  if (kind === 'del') return '- '
  if (kind === 'ins') return '+ '
  return '  '
}
