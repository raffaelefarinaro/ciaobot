/** One line of `Workspace/Unsure.md`: `- [ ] text` is open, `- [x] text` is ticked. */
export interface UnsureLine {
  text: string
  ticked: boolean
}

const LINE = /^\s*-\s*\[([ xX])\]\s+(.*?)\s*$/

/** The item lines of the list, in file order; headings and prose are skipped. */
export function parseUnsureLines(markdown: string): UnsureLine[] {
  const out: UnsureLine[] = []
  for (const raw of markdown.split(/\r?\n/)) {
    const m = LINE.exec(raw)
    if (m) out.push({ text: m[2], ticked: m[1] !== ' ' })
  }
  return out
}
