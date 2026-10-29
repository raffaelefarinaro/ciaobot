// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'
import { useMenuFocusReturn, type MenuFocusReturn } from '../useMenuFocusReturn'

function control(label: string): HTMLButtonElement {
  const button = document.createElement('button')
  button.type = 'button'
  button.textContent = label
  document.body.appendChild(button)
  return button
}

const press = (target: EventTarget): void => {
  target.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))
}

describe('useMenuFocusReturn', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    Element.prototype.scrollIntoView = vi.fn()
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  function setup(): { scope: ReturnType<typeof effectScope>; focus: MenuFocusReturn } {
    const scope = effectScope()
    const focus = scope.run(() => useMenuFocusReturn())!
    return { scope, focus }
  }

  it('returns focus to the trigger and scrolls it into view after a settled action', async () => {
    const { scope, focus } = setup()
    const trigger = control('Actions')
    const scroll = vi.fn()
    trigger.scrollIntoView = scroll
    trigger.focus()
    const action = focus.arm(trigger)

    await action.restore(() => trigger)

    expect(document.activeElement).toBe(trigger)
    expect(scroll).toHaveBeenCalledWith({ block: 'nearest', inline: 'nearest' })
    scope.stop()
  })

  it('does not restore after focus moved to another control during the write', async () => {
    const { scope, focus } = setup()
    const trigger = control('Actions')
    const other = control('Other')
    trigger.focus()
    const action = focus.arm(trigger)
    other.focus()

    await action.restore(() => trigger)

    expect(document.activeElement).toBe(other)
    scope.stop()
  })

  it('does not restore after a press elsewhere, even while the trigger keeps focus', async () => {
    const { scope, focus } = setup()
    const trigger = control('Actions')
    const moved = control('Moved row')
    trigger.focus()
    const action = focus.arm(trigger)
    press(document.body)

    await action.restore(() => moved)

    expect(document.activeElement).toBe(trigger)
    expect(document.activeElement).not.toBe(moved)
    scope.stop()
  })

  it('treats a press on the trigger itself as staying, not leaving', async () => {
    const { scope, focus } = setup()
    const trigger = control('Actions')
    const moved = control('Moved row')
    trigger.focus()
    const action = focus.arm(trigger)
    press(trigger)

    await action.restore(() => moved)

    expect(document.activeElement).toBe(moved)
    scope.stop()
  })

  it('finds the fresh trigger after the row was remounted in another lane', async () => {
    const { scope, focus } = setup()
    const old = control('Old actions')
    old.focus()
    const action = focus.arm(old)
    old.remove()
    ;(document.activeElement as HTMLElement | null)?.blur()

    const fresh = control('Fresh actions')
    await action.restore(() => fresh)

    expect(document.activeElement).toBe(fresh)
    scope.stop()
  })

  it('a failed write follows the same no-steal rule', async () => {
    const { scope, focus } = setup()
    const trigger = control('Actions')
    const other = control('Other')
    trigger.focus()
    const action = focus.arm(trigger)
    other.focus()

    await action.restore(() => trigger)

    expect(document.activeElement).toBe(other)
    scope.stop()
  })

  it('does nothing after unmount, even if the write settles later', async () => {
    const { scope, focus } = setup()
    const trigger = control('Actions')
    const moved = control('Moved row')
    trigger.focus()
    const action = focus.arm(trigger)
    scope.stop()

    await action.restore(() => moved)

    expect(document.activeElement).toBe(trigger)
    expect(document.activeElement).not.toBe(moved)
  })

  it('cancel() ends the current action without moving focus', async () => {
    const { scope, focus } = setup()
    const trigger = control('Actions')
    const moved = control('Moved row')
    trigger.focus()
    const action = focus.arm(trigger)
    action.cancel()

    await action.restore(() => moved)

    expect(document.activeElement).toBe(trigger)
    expect(document.activeElement).not.toBe(moved)
    scope.stop()
  })

  it('an older action cannot cancel a newer watch', async () => {
    const { scope, focus } = setup()
    const first = control('First')
    const second = control('Second')
    const secondTarget = control('Second target')
    first.focus()
    const firstAction = focus.arm(first)
    second.focus()
    const secondAction = focus.arm(second)
    firstAction.cancel()

    await secondAction.restore(() => secondTarget)

    expect(document.activeElement).toBe(secondTarget)
    scope.stop()
  })

  it('a newer action owns the watch; the older action cannot end it or move focus', async () => {
    const { scope, focus } = setup()
    const first = control('First')
    const second = control('Second')
    const firstTarget = control('First target')
    const secondTarget = control('Second target')
    first.focus()
    const firstAction = focus.arm(first)
    const older = firstAction.restore(() => firstTarget)
    second.focus()
    const secondAction = focus.arm(second)

    expect(firstAction.departed()).toBe(true)
    expect(secondAction.departed()).toBe(false)
    await older
    expect(document.activeElement).toBe(second)

    await secondAction.restore(() => secondTarget)
    expect(document.activeElement).toBe(secondTarget)
    scope.stop()
  })

  it('does not restore when the trigger has disappeared', async () => {
    const { scope, focus } = setup()
    const trigger = control('Actions')
    trigger.focus()
    const action = focus.arm(trigger)
    trigger.remove()
    ;(document.activeElement as HTMLElement | null)?.blur()

    await action.restore(() => null)

    expect(document.activeElement).toBe(document.body)
    scope.stop()
  })
})
