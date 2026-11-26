/** One line of `Workspace/Unsure.md`: `- [ ] text` is open, `- [x] text` is ticked. */
export interface UnsureLine {
  text: string
  ticked: boolean
}

const LINE = /^\s*[-*]\s*\[([ xX])\]\s+(.*?)\s*$/
const FENCE = /^\s*(```|~~~)/

/** The item lines of the list, in file order; headings, prose and fenced code are skipped. */
export function parseUnsureLines(markdown: string): UnsureLine[] {
  const out: UnsureLine[] = []
  let fenced = false
  for (const raw of markdown.split(/\r?\n/)) {
    if (FENCE.test(raw)) { fenced = !fenced; continue }
    if (fenced) continue
    const m = LINE.exec(raw)
    if (m) out.push({ text: m[2], ticked: m[1] !== ' ' })
  }
  return out
}
