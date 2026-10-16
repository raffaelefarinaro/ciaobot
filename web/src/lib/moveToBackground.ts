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
