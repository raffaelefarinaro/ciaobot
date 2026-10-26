// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { useDragSort, type DragCommit, type UseDragSortOptions } from '../useDragSort'

const ITEM_HEIGHT = 40
const GAP = 10

const lists: Record<string, string[]> = {
  todo: ['a1', 'a2', 'a3'],
  doing: ['b1', 'b2'],
}
const scopeNames: Record<string, string> = { todo: 'To do', doing: 'In progress' }

interface Harness {
  dnd: ReturnType<typeof useDragSort>
  onCommit: ReturnType<typeof vi.fn<(c: DragCommit) => Promise<boolean> | boolean>>
  onCancel: ReturnType<typeof vi.fn>
  grip: (id: string) => HTMLElement
  handlers: (id: string, scope: string) => ReturnType<ReturnType<typeof useDragSort>['gripHandlers']>
  root: HTMLElement
}

function setup(overrides: Partial<UseDragSortOptions> = {}): Harness {
  document.body.innerHTML = ''
  const root = document.createElement('div')
  document.body.appendChild(root)
  const scopeEls: Record<string, HTMLElement> = {}
  for (const [scope, ids] of Object.entries(lists)) {
    const ul = document.createElement('ul')
    ul.dataset.scope = scope
    scopeEls[scope] = ul
    root.appendChild(ul)
    for (const id of ids) {
      const li = document.createElement('li')
      li.dataset.item = id
      const grip = document.createElement('button')
      grip.dataset.dragHandle = id
      li.appendChild(grip)
      ul.appendChild(li)
    }
  }
  root.setPointerCapture = vi.fn()
  root.releasePointerCapture = vi.fn()
  root.hasPointerCapture = vi.fn(() => true)

  const onCommit = vi.fn<(c: DragCommit) => Promise<boolean> | boolean>(() => true)
  const onCancel = vi.fn()
  const dnd = useDragSort({
    root: ref(root),
    scopes: () =>
      Object.keys(lists).map((id) => ({ id, el: () => scopeEls[id] })),
    itemIds: (scope) => lists[scope],
    itemEl: (id) => root.querySelector<HTMLElement>(`[data-item="${id}"]`),
    canDrag: () => true,
    label: (id) => `Task ${id}`,
    scopeLabel: (scope) => scopeNames[scope],
    onCommit,
    onCancel,
    horizontalScopes: true,
    ...overrides,
  })

  // A flex-like layout driven by what the view would render right now.
  scopeEls.todo.getBoundingClientRect = () => new DOMRect(0, -1, 100, 500)
  scopeEls.doing.getBoundingClientRect = () => new DOMRect(100, -1, 100, 500)
  root.querySelectorAll<HTMLElement>('[data-item]').forEach((li) => {
    li.getBoundingClientRect = () => {
      for (const scope of Object.keys(lists)) {
        let top = 0
        for (const entry of dnd.entriesFor(scope)) {
          const height =
            entry.kind === 'item' && dnd.isSource(entry.id) ? 0 : ITEM_HEIGHT
          if (entry.kind === 'item' && entry.id === li.dataset.item) {
            return new DOMRect(scope === 'todo' ? 0 : 100, top, 100, height)
          }
          top += height + GAP
        }
      }
      return new DOMRect(0, 0, 0, 0)
    }
  })
  return {
    dnd,
    onCommit,
    onCancel,
    root,
    grip: (id) => root.querySelector<HTMLElement>(`[data-drag-handle="${id}"]`)!,
    handlers: (id, scope) => dnd.gripHandlers(id, scope),
  }
}

function pointer(type: string, x: number, y: number): PointerEvent {
  return new PointerEvent(type, {
    clientX: x,
    clientY: y,
    pointerId: 1,
    isPrimary: true,
    button: 0,
    bubbles: true,
  })
}

const overlay = () => document.querySelector<HTMLElement>('.drag-overlay')
const flush = async () => {
  await Promise.resolve()
  await new Promise((r) => setTimeout(r, 0))
}

function press(h: Harness, id: string, scope: string, x = 10, y = 10): void {
  h.handlers(id, scope).onPointerdown(pointer('pointerdown', x, y))
}

function move(h: Harness, x: number, y: number): void {
  h.root.dispatchEvent(pointer('pointermove', x, y))
}

function key(k: string): KeyboardEvent {
  return new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true })
}

describe('useDragSort', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', () => 1)
    vi.stubGlobal('cancelAnimationFrame', () => undefined)
    Element.prototype.scrollIntoView = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('does not activate below the threshold and lets a tap through', () => {
    const h = setup()
    const clicked = vi.fn()
    document.addEventListener('click', clicked)
    press(h, 'a1', 'todo')
    move(h, 12, 12)
    expect(h.dnd.drag.value).toBeNull()
    expect(overlay()).toBeNull()
    h.root.dispatchEvent(pointer('pointerup', 12, 12))
    h.root.querySelector('[data-item="a1"]')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(clicked).toHaveBeenCalledTimes(1)
    expect(h.onCommit).not.toHaveBeenCalled()
    document.removeEventListener('click', clicked)
  })

  it('activates past the threshold and creates an overlay that follows the pointer', () => {
    const h = setup()
    press(h, 'a1', 'todo')
    move(h, 10, 30)
    expect(h.dnd.drag.value).toMatchObject({ id: 'a1', mode: 'pointer', fromScope: 'todo' })
    const el = overlay()!
    expect(el).not.toBeNull()
    expect(el.getAttribute('aria-hidden')).toBe('true')
    expect(el.hasAttribute('inert')).toBe(true)
    expect(el.style.pointerEvents).toBe('none')
    expect(el.style.position).toBe('fixed')
    expect(el.querySelector('[data-drag-handle]')).toBeNull()
    expect(el.style.transform).toBe('translate3d(0px, 20px, 0)')
    move(h, 60, 80)
    expect(el.style.transform).toBe('translate3d(50px, 70px, 0)')
    expect(h.dnd.isSource('a1')).toBe(true)
    expect(h.root.style.getPropertyValue('--drag-item-height')).toBe('40px')
    expect(h.root.setPointerCapture).toHaveBeenCalledWith(1)
  })

  it('commits exactly once on pointerup with the computed index', async () => {
    const h = setup()
    press(h, 'a1', 'todo')
    move(h, 10, 30)
    move(h, 10, 100)
    expect(h.dnd.drag.value).toMatchObject({ toScope: 'todo', index: 2 })
    h.root.dispatchEvent(pointer('pointerup', 10, 100))
    await flush()
    expect(h.onCommit).toHaveBeenCalledTimes(1)
    expect(h.onCommit).toHaveBeenCalledWith({
      id: 'a1',
      fromScope: 'todo',
      toScope: 'todo',
      index: 2,
    })
    expect(h.dnd.drag.value).toBeNull()
    expect(overlay()).toBeNull()
    expect(h.root.releasePointerCapture).toHaveBeenCalled()
  })

  it('no-op drop (same scope and index) does not call onCommit', async () => {
    const h = setup()
    press(h, 'a1', 'todo')
    move(h, 10, 30)
    h.root.dispatchEvent(pointer('pointerup', 10, 30))
    await flush()
    expect(h.onCommit).not.toHaveBeenCalled()
    expect(h.dnd.drag.value).toBeNull()
    expect(overlay()).toBeNull()
  })

  it('Escape cancels without commit and removes the overlay', async () => {
    const h = setup()
    press(h, 'a1', 'todo')
    move(h, 10, 100)
    expect(overlay()).not.toBeNull()
    document.dispatchEvent(key('Escape'))
    await flush()
    expect(h.onCommit).not.toHaveBeenCalled()
    expect(h.onCancel).toHaveBeenCalledTimes(1)
    expect(h.dnd.drag.value).toBeNull()
    expect(overlay()).toBeNull()
    // Listeners are gone: a later move does nothing.
    move(h, 10, 120)
    expect(overlay()).toBeNull()
  })

  it('pointercancel and lostpointercapture cancel without commit', async () => {
    for (const type of ['pointercancel', 'lostpointercapture']) {
      const h = setup()
      press(h, 'a1', 'todo')
      move(h, 10, 100)
      h.root.dispatchEvent(new Event(type))
      await flush()
      expect(h.onCommit).not.toHaveBeenCalled()
      expect(h.dnd.drag.value).toBeNull()
      expect(overlay()).toBeNull()
    }
  })

  it('release outside any valid scope cancels', async () => {
    const h = setup()
    press(h, 'a1', 'todo')
    move(h, 10, 30)
    move(h, 900, 900)
    expect(h.dnd.drag.value?.toScope).toBe('')
    h.root.dispatchEvent(pointer('pointerup', 900, 900))
    await flush()
    expect(h.onCommit).not.toHaveBeenCalled()
    expect(h.onCancel).toHaveBeenCalledTimes(1)
    expect(overlay()).toBeNull()
  })

  it('canDropIn false (pinned) cancels', async () => {
    const h = setup({ canDropIn: (_id, _from, to) => to !== 'doing' })
    press(h, 'a1', 'todo')
    move(h, 10, 30)
    move(h, 150, 20)
    expect(h.dnd.drag.value?.toScope).toBe('')
    h.root.dispatchEvent(pointer('pointerup', 150, 20))
    await flush()
    expect(h.onCommit).not.toHaveBeenCalled()
    expect(h.dnd.drag.value).toBeNull()
  })

  it('drops into another scope at the computed index', async () => {
    const h = setup()
    press(h, 'a1', 'todo')
    move(h, 10, 30)
    move(h, 150, 70)
    expect(h.dnd.drag.value).toMatchObject({ toScope: 'doing', index: 1 })
    expect(h.dnd.entriesFor('doing').map((e) => e.kind)).toEqual(['item', 'placeholder', 'item'])
    h.root.dispatchEvent(pointer('pointerup', 150, 70))
    await flush()
    expect(h.onCommit).toHaveBeenCalledWith({
      id: 'a1',
      fromScope: 'todo',
      toScope: 'doing',
      index: 1,
    })
  })

  it('swallows the click after a completed drag', async () => {
    const h = setup()
    const clicked = vi.fn()
    document.addEventListener('click', clicked)
    press(h, 'a1', 'todo')
    move(h, 10, 30)
    h.root.dispatchEvent(pointer('pointerup', 10, 30))
    h.grip('a1').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    expect(clicked).not.toHaveBeenCalled()
    await flush()
    h.grip('a1').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    expect(clicked).toHaveBeenCalledTimes(1)
    document.removeEventListener('click', clicked)
  })

  it('keyboard pickup, arrows, drop commits once; Escape cancels; focus returns to the grip', async () => {
    const h = setup()
    const handlers = h.handlers('a1', 'todo')
    const pick = key(' ')
    handlers.onKeydown(pick)
    expect(pick.defaultPrevented).toBe(true)
    expect(h.dnd.drag.value).toMatchObject({ id: 'a1', mode: 'keyboard', index: 0 })
    expect(overlay()).toBeNull()
    await flush()
    expect(document.activeElement).toBe(h.grip('a1'))

    handlers.onKeydown(key('ArrowDown'))
    handlers.onKeydown(key('ArrowDown'))
    handlers.onKeydown(key('ArrowDown'))
    expect(h.dnd.drag.value?.index).toBe(2)
    handlers.onKeydown(key('Enter'))
    await flush()
    expect(h.onCommit).toHaveBeenCalledTimes(1)
    expect(h.onCommit).toHaveBeenCalledWith({
      id: 'a1',
      fromScope: 'todo',
      toScope: 'todo',
      index: 2,
    })
    expect(h.dnd.drag.value).toBeNull()
    expect(document.activeElement).toBe(h.grip('a1'))

    handlers.onKeydown(key(' '))
    handlers.onKeydown(key('ArrowDown'))
    handlers.onKeydown(key('Escape'))
    await flush()
    expect(h.onCommit).toHaveBeenCalledTimes(1)
    expect(h.dnd.drag.value).toBeNull()
    expect(document.activeElement).toBe(h.grip('a1'))
  })

  it('keyboard left and right change scope and clamp the index', async () => {
    const h = setup()
    const handlers = h.handlers('a3', 'todo')
    handlers.onKeydown(key(' '))
    handlers.onKeydown(key('ArrowRight'))
    expect(h.dnd.drag.value).toMatchObject({ toScope: 'doing', index: 2 })
    handlers.onKeydown(key('ArrowRight'))
    expect(h.dnd.drag.value?.toScope).toBe('doing')
    handlers.onKeydown(key('Enter'))
    await flush()
    expect(h.onCommit).toHaveBeenCalledWith({
      id: 'a3',
      fromScope: 'todo',
      toScope: 'doing',
      index: 2,
    })
  })

  it('keyboard arrows are not intercepted when nothing is picked up', () => {
    const h = setup()
    const parent = vi.fn()
    h.root.addEventListener('keydown', parent)
    const grip = h.grip('a1')
    grip.addEventListener('keydown', h.handlers('a1', 'todo').onKeydown)
    for (const k of ['ArrowDown', 'ArrowLeft', 'Escape']) {
      const e = key(k)
      grip.dispatchEvent(e)
      expect(e.defaultPrevented).toBe(false)
    }
    expect(parent).toHaveBeenCalledTimes(3)
    const shifted = new KeyboardEvent('keydown', { key: ' ', shiftKey: true, cancelable: true })
    h.handlers('a1', 'todo').onKeydown(shifted)
    expect(h.dnd.drag.value).toBeNull()
  })

  it('announcements for pickup, move, drop, cancel and failure', async () => {
    const h = setup()
    const handlers = h.handlers('a2', 'todo')
    handlers.onKeydown(key(' '))
    expect(h.dnd.announcement.value).toBe('Picked up Task a2, position 2 of 3 in To do.')
    handlers.onKeydown(key('ArrowRight'))
    expect(h.dnd.announcement.value).toBe('Task a2, position 2 of 3 in In progress.')
    handlers.onKeydown(key('Enter'))
    await flush()
    expect(h.dnd.announcement.value).toBe('Dropped Task a2 at position 2 of 3 in In progress.')

    handlers.onKeydown(key(' '))
    handlers.onKeydown(key('Escape'))
    await flush()
    expect(h.dnd.announcement.value).toBe(
      'Move cancelled. Task a2 is back at position 2 of 3 in To do.',
    )

    h.onCommit.mockReturnValueOnce(false)
    handlers.onKeydown(key(' '))
    handlers.onKeydown(key('ArrowDown'))
    handlers.onKeydown(key('Enter'))
    await flush()
    expect(h.dnd.announcement.value).toBe('Could not save the new order.')
  })

  it('move() at a boundary resolves false without commit', async () => {
    const h = setup()
    expect(await h.dnd.move('a1', 'todo', 'up')).toBe(false)
    expect(await h.dnd.move('a3', 'todo', 'down')).toBe(false)
    expect(await h.dnd.move('a1', 'todo', 'left')).toBe(false)
    expect(await h.dnd.move('b1', 'doing', 'right')).toBe(false)
    expect(h.onCommit).not.toHaveBeenCalled()

    expect(await h.dnd.move('a2', 'todo', 'down')).toBe(true)
    expect(h.onCommit).toHaveBeenLastCalledWith({
      id: 'a2',
      fromScope: 'todo',
      toScope: 'todo',
      index: 2,
    })
    expect(h.dnd.announcement.value).toBe('Dropped Task a2 at position 3 of 3 in To do.')
    expect(await h.dnd.move('a2', 'todo', 'right')).toBe(true)
    expect(h.onCommit).toHaveBeenLastCalledWith({
      id: 'a2',
      fromScope: 'todo',
      toScope: 'doing',
      index: 1,
    })
  })

  it('external cancel() (workspace change) cancels silently', () => {
    const h = setup()
    press(h, 'a1', 'todo')
    move(h, 10, 100)
    const before = h.dnd.announcement.value
    h.dnd.cancel()
    expect(h.onCommit).not.toHaveBeenCalled()
    expect(h.dnd.drag.value).toBeNull()
    expect(overlay()).toBeNull()
    expect(h.dnd.announcement.value).toBe(before)
    expect(h.dnd.announcement.value).not.toContain('cancelled')
  })

  it('onCommit returning false announces the failure and ends the drag', async () => {
    const h = setup()
    h.onCommit.mockResolvedValueOnce(false)
    press(h, 'a1', 'todo')
    move(h, 10, 30)
    move(h, 10, 100)
    h.root.dispatchEvent(pointer('pointerup', 10, 100))
    await flush()
    expect(h.onCommit).toHaveBeenCalledTimes(1)
    expect(h.dnd.announcement.value).toBe('Could not save the new order.')
    expect(h.dnd.drag.value).toBeNull()
    expect(overlay()).toBeNull()
  })

  it('keyboard drag keeps the source visible and moves it in entriesFor', async () => {
    const h = setup()
    const handlers = h.handlers('a2', 'todo')
    handlers.onKeydown(key(' '))
    handlers.onKeydown(key('ArrowDown'))
    expect(h.dnd.isSource('a2')).toBe(false)
    expect(h.dnd.isLifted('a2')).toBe(true)
    expect(h.dnd.isLifted('a1')).toBe(false)
    expect(h.dnd.entriesFor('todo')).toEqual([
      { kind: 'item', id: 'a1' },
      { kind: 'item', id: 'a3' },
      { kind: 'item', id: 'a2' },
    ])
    handlers.onKeydown(key('Escape'))
    await flush()
    expect(h.dnd.isLifted('a2')).toBe(false)
    expect(h.dnd.entriesFor('todo').map((e) => (e.kind === 'item' ? e.id : '-'))).toEqual([
      'a1',
      'a2',
      'a3',
    ])
  })

  it('keyboard drag across scopes moves the source between entriesFor lists', () => {
    const h = setup()
    const handlers = h.handlers('a2', 'todo')
    handlers.onKeydown(key(' '))
    handlers.onKeydown(key('ArrowRight'))
    expect(h.dnd.isSource('a2')).toBe(false)
    expect(h.dnd.isLifted('a2')).toBe(true)
    expect(h.dnd.entriesFor('todo')).toEqual([
      { kind: 'item', id: 'a1' },
      { kind: 'item', id: 'a3' },
    ])
    expect(h.dnd.entriesFor('doing')).toEqual([
      { kind: 'item', id: 'b1' },
      { kind: 'item', id: 'a2' },
      { kind: 'item', id: 'b2' },
    ])
  })

  it('overlay clone strips id, name and for', () => {
    const h = setup()
    const li = h.root.querySelector('[data-item="a1"]')!
    li.id = 'card-a1'
    li.insertAdjacentHTML(
      'beforeend',
      '<label for="chk">x</label><input id="chk" name="grp" type="radio">',
    )
    press(h, 'a1', 'todo')
    move(h, 10, 30)
    const el = overlay()!
    expect(el.id).toBe('')
    expect(el.querySelectorAll('[id],[name],[for]').length).toBe(0)
    expect(li.id).toBe('card-a1')
    expect(li.querySelector('input')!.name).toBe('grp')
  })
})
