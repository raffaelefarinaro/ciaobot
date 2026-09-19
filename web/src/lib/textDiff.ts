/** Line changes between two versions of a small text body.
 *
 * Used by the proposal review card to show the exact replacement an accept
 * would make. Deliberately not a general diff: the bodies here are a bounded
 * memory region, a person-note stub or a Learnings file, and every change the
 * promotion path makes is a contiguous edit — an appended entry, one line
 * whose recurrence count went up, a replaced entry. Trimming the shared prefix
 * and suffix names exactly that edit, in a few lines, with no dependency.
 *
 * Context lines are not returned. What the card has to answer is *what
 * changes*; reprinting an unchanged region around it buries the one line that
 * does, which is the failure mode of showing the raw bodies side by side.
 */
export interface LineChange {
  op: 'added' | 'removed'
  text: string
}

/**
 * `separator` is what joins the destination's units, which the server names on
 * the preview. A bounded region's entries are joined by `\n§\n`, not by
 * newlines: diffed as lines, appending one entry showed a second added row
 * reading "§" and a multi-line entry was torn into unrelated rows.
 */
export function lineChanges(before: string, after: string, separator = '\n'): LineChange[] {
  if (before === after) return []
  // One trailing newline is a file convention, not a change. Without this the
  // serialized region's terminating newline split into a final empty line and
  // the card showed a blank "+" row under every real addition.
  const oldLines = splitBody(before, separator)
  const newLines = splitBody(after, separator)

  let start = 0
  while (start < oldLines.length && start < newLines.length && oldLines[start] === newLines[start]) {
    start += 1
  }
  let endOld = oldLines.length
  let endNew = newLines.length
  while (endOld > start && endNew > start && oldLines[endOld - 1] === newLines[endNew - 1]) {
    endOld -= 1
    endNew -= 1
  }

  const changes: LineChange[] = []
  for (const text of oldLines.slice(start, endOld)) changes.push({ op: 'removed', text })
  for (const text of newLines.slice(start, endNew)) changes.push({ op: 'added', text })
  return changes
}

function splitBody(text: string, separator: string): string[] {
  if (!text) return []
  return text.replace(/\n$/, '').split(separator)
}
