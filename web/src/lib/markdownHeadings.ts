/**
 * Find a heading in rendered Markdown by the words a person names it with, so
 * a viewer can open a note at a section (`Workspace/Care-Report.md` at
 * "Hygiene"). The rendered note carries no heading ids, so the heading is
 * matched by its text: case, `#` marks, spacing and a trailing colon do not
 * count. Pure DOM reads: no Vue.
 */

/** A heading's text as it is compared: lower case, single spaces, no `#` lead or trailing colon. */
export function headingKey(text: string): string {
  return text.replace(/^\s*#+\s*/, '').replace(/\s+/g, ' ').trim().replace(/\s*:$/, '').toLowerCase()
}

/** The first h1 to h6 under `root` whose text names `heading`, or null (also for an empty name). */
export function findHeading(root: ParentNode, heading: string): HTMLElement | null {
  const key = headingKey(heading)
  if (!key) return null
  for (const el of Array.from(root.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6'))) {
    if (headingKey(el.textContent ?? '') === key) return el
  }
  return null
}
