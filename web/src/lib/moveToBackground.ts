/** How long a backgroundable call runs before the composer offers to move it (#1413). */
export const MOVE_TO_BACKGROUND_DELAY_MS = 3000

/** Running backgroundable calls of one chat: tool_use_id → first-seen time (ms). */
export type BackgroundableCalls = Record<string, number>

interface ToolFrame {
  type?: string
  tool_use_id?: string
  parent_tool_use_id?: string
  backgroundable?: boolean
}

/** The chat's running backgroundable calls after one `tool_use` or `tool_result` frame. */
export function nextBackgroundableCalls(
  calls: BackgroundableCalls | undefined, frame: ToolFrame, now: number,
): BackgroundableCalls {
  const current = calls ?? {}
  const id = frame.tool_use_id
  if (!id || frame.parent_tool_use_id) return current
  if (frame.type === 'tool_result') {
    if (!(id in current)) return current
    const rest = { ...current }
    delete rest[id]
    return rest
  }
  if (!frame.backgroundable || id in current) return current
  return { ...current, [id]: now }
}

/** True once any running backgroundable call is at least the delay old. */
export function canOfferMoveToBackground(calls: BackgroundableCalls | undefined, now: number): boolean {
  return Object.values(calls ?? {}).some(t => now - t >= MOVE_TO_BACKGROUND_DELAY_MS)
}

/**
 * Whether a call that finished moving to the background may hand focus on. The
 * move's chip is unmounted the moment the turn ends, so focus has to go
 * somewhere that is still there — but only while it is still ours. While the
 * request was out the user may have moved focus, most likely into the draft to
 * write the next message, and taking it back would interrupt them and could turn
 * their next Enter into a control they did not choose (#1413 review round 2).
 *
 * "Ours" is identity, and only identity: the chip itself, or nothing in
 * particular holding focus. An element being inside the composer, or the chat
 * pane, or some other container says nothing about whether the user put it
 * there — inferring ownership from where an element sits is what every review
 * round of the browser spec found, so the rule is expressed here, once, as a
 * function of the two elements rather than as a query on the page.
 */
export function shouldRestoreFocus(chipEl: Element | null, active: Element | null): boolean {
  return active === chipEl || active === active?.ownerDocument.body
}
