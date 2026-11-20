/**
 * Whether a finished request may move focus back to a control that is still
 * mounted. The element that held focus can unmount when the request ends, so
 * focus has to go somewhere that is still there — but only while it is still
 * ours. While the request was out the user may have moved focus, most likely
 * into the draft to write the next message, and taking it back would interrupt
 * them and could turn their next Enter into a control they did not choose.
 *
 * "Ours" is identity, and only identity: the element that held focus, or
 * nothing in particular holding focus. An element being inside the composer,
 * or the chat pane, or some other container says nothing about whether the
 * user put it there — inferring ownership from where an element sits is what
 * every review round of the browser spec found, so the rule is expressed
 * here, once, as a function of the two elements rather than as a query on
 * the page.
 */
export function shouldRestoreFocus(heldEl: Element | null, active: Element | null): boolean {
  return active === heldEl || active === active?.ownerDocument.body
}
