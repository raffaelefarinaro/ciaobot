// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref, watch } from 'vue'
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
  surface: (id: string, scope: string) => ReturnType<ReturnType<typeof useDragSort>['surfaceHandlers']>
  root: HTMLElement
  /** Apply `entriesFor` to the DOM, standing in for Vue's async flush. */
  render: () => void
  /** Give an item an in-flight FLIP translation without moving its layout slot. */
  flip: (id: string, dy: number) => void
}

/**
 * `reactive` geometry reads `entriesFor` straight from the drag state, so the
 * placeholder is laid out synchronously. `dom` geometry reads the elements that
 * are actually in the document, exactly as `getBoundingClientRect` would before
 * Vue has flushed the preview; `render()` is the flush.
 */
type Layout = 'reactive' | 'dom'

function setup(overrides: Partial<UseDragSortOptions> = {}, layout: Layout = 'reactive'): Harness {
  document.body.innerHTML = ''
  const root = document.createElement('div')
  document.body.appendChild(root)
  const scopeEls: Record<string, HTMLElement> = {}
  for (const [scope, ids] of Object.entries(lists)) {
    const ul = document.createElement('ul')
    ul.dataset.scope = scope
    // The geometry model, the placeholder's measured height and targetAt's gap
    // must agree.
    ul.style.rowGap = `${GAP}px`
    scopeEls[scope] = ul
    root.appendChild(ul)
    for (const id of ids) {
      const li = document.createElement('li')
      li.dataset.item = id
      const grip = document.createElement('button')
      grip.dataset.dragHandle = id
      li.appendChild(grip)
      const surface = document.createElement('span')
      surface.className = 'surface'
      surface.textContent = id
      li.appendChild(surface)
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

  // The row itself is the surface target, exactly as the view binds it.
  for (const [scope, ids] of Object.entries(lists)) {
    for (const id of ids) {
      root
        .querySelector<HTMLElement>(`[data-item="${id}"]`)!
        .addEventListener('pointerdown', dnd.surfaceHandlers(id, scope).onPointerdown)
    }
  }

  // The `reactive` mode renders synchronously after every drag-state change so
  // its geometry always matches the current preview. The `dom` mode only renders
  // when the test calls `render`, which stands in for Vue's async flush.
  scopeEls.todo.getBoundingClientRect = () => new DOMRect(0, -1, 100, 500)
  scopeEls.doing.getBoundingClientRect = () => new DOMRect(100, -1, 100, 500)
  // Hit testing follows the same rects: a scope is only under the pointer where
  // it is actually drawn.
  document.elementsFromPoint = (x, y) => {
    const hits: Element[] = []
    for (const ul of Object.values(scopeEls)) {
      const rect = ul.getBoundingClientRect()
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) hits.push(ul)
    }
    return hits
  }
  // Flex-like layout read from the elements actually in the document. The
  // `reactive` mode instead lays items out from `entriesFor`, as the old
  // harness did, but its placeholder element is real so the engine can see it.
  const flips = new Map<string, number>()
  const rectForDom = (el: HTMLElement): DOMRect => {
    const ul = el.parentElement as HTMLElement
    let top = 0
    for (const child of Array.from(ul.children) as HTMLElement[]) {
      const height = child.classList.contains('drag-placeholder')
        ? ITEM_HEIGHT
        : child.classList.contains('drag-source')
          ? 0
          : ITEM_HEIGHT
      if (child === el) {
        const flip = el.dataset.item ? (flips.get(el.dataset.item) ?? 0) : 0
        return new DOMRect(ul.dataset.scope === 'todo' ? 0 : 100, top + flip, 100, height)
      }
      top += height + GAP
    }
    return new DOMRect(0, 0, 0, 0)
  }
  const rectForReactive = (id: string): DOMRect => {
    for (const scope of Object.keys(lists)) {
      let top = 0
      for (const entry of dnd.entriesFor(scope)) {
        const height = entry.kind === 'item' && dnd.isSource(entry.id) ? 0 : ITEM_HEIGHT
        if (entry.kind === 'item' && entry.id === id) {
          return new DOMRect(scope === 'todo' ? 0 : 100, top, 100, height)
        }
        top += height + GAP
      }
    }
    return new DOMRect(0, 0, 0, 0)
  }
  root.querySelectorAll<HTMLElement>('[data-item]').forEach((li) => {
    li.getBoundingClientRect =
      layout === 'dom'
        ? () => rectForDom(li)
        : () => rectForReactive(li.dataset.item!)
  })
  const makePlaceholder = (): HTMLElement => {
    const placeholder = document.createElement('li')
    placeholder.className = 'drag-placeholder'
    placeholder.setAttribute('aria-hidden', 'true')
    placeholder.getBoundingClientRect = () => rectForDom(placeholder)
    return placeholder
  }
  const render = (): void => {
    for (const scope of Object.keys(lists)) {
      const ul = scopeEls[scope]
      const nodes: HTMLElement[] = []
      for (const entry of dnd.entriesFor(scope)) {
        if (entry.kind === 'placeholder') {
          nodes.push(makePlaceholder())
        } else {
          const li = root.querySelector<HTMLElement>(`[data-item="${entry.id}"]`)!
          li.classList.toggle('drag-source', dnd.isSource(entry.id))
          nodes.push(li)
        }
      }
      ul.replaceChildren(...nodes)
    }
  }
  // Reactive mode stands in for a view that has already flushed the preview at
  // every step: only the placeholder element is needed for the engine to read a
  // real slot and height; item geometry still comes from `entriesFor`.
  const syncPlaceholder = (): void => {
    const d = dnd.drag.value
    for (const scope of Object.keys(lists)) {
      const ul = scopeEls[scope]
      for (const child of Array.from(ul.children)) {
        if (child.classList.contains('drag-placeholder')) child.remove()
      }
      if (!d || d.mode !== 'pointer' || d.toScope !== scope) continue
      const ids = lists[scope].filter((id) => id !== d.id)
      const before = ids[d.index]
        ? root.querySelector<HTMLElement>(`[data-item="${ids[d.index]}"]`)
        : null
      ul.insertBefore(makePlaceholder(), before)
    }
  }
  if (layout === 'reactive') watch(dnd.drag, syncPlaceholder, { flush: 'sync' })
  const flip = (id: string, dy: number): void => {
    flips.set(id, dy)
    const el = root.querySelector<HTMLElement>(`[data-item="${id}"]`)
    if (el) el.style.transform = `translateY(${dy}px)`
  }
  return {
    dnd,
    onCommit,
    onCancel,
    root,
    render,
    flip,
    grip: (id) => root.querySelector<HTMLElement>(`[data-drag-handle="${id}"]`)!,
    handlers: (id, scope) => dnd.gripHandlers(id, scope),
    surface: (id, scope) => dnd.surfaceHandlers(id, scope),
  }
}

function pointer(type: string, x: number, y: number, pointerType = 'mouse'): PointerEvent {
  return new PointerEvent(type, {
    clientX: x,
    clientY: y,
    pointerId: 1,
    isPrimary: true,
    button: 0,
    pointerType,
    bubbles: true,
  })
}

const surfaceText = (h: Harness, id: string) =>
  h.root.querySelector<HTMLElement>(`[data-item="${id}"] .surface`)!

function pressSurface(h: Harness, id: string, x = 10, y = 10, pointerType = 'mouse'): void {
  surfaceText(h, id).dispatchEvent(pointer('pointerdown', x, y, pointerType))
}

const overlay = () => document.querySelector<HTMLElement>('.drag-overlay')
const flush = async () => {
  await Promise.resolve()
  await new Promise((r) => setTimeout(r, 0))
}

/** A write the test decides when (and how) to answer. */
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
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
    // jsdom has no DOMMatrix; layoutRect only needs the translateY component.
    vi.stubGlobal(
      'DOMMatrix',
      class {
        m42 = 0
        constructor(transform: string) {
          const translateY = /translateY\((-?[\d.]+)px\)/.exec(transform)
          const matrix = /matrix\(([^)]+)\)/.exec(transform)
          const values = matrix?.[1]?.split(',') ?? []
          this.m42 = translateY ? Number(translateY[1] ?? 0) : Number(values[5] ?? 0)
        }
      },
    )
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

  it.each(['cancel', 'dispose'] as const)(
    'removes pointer focus listeners on %s while a commit is pending',
    async (ending) => {
      const added = vi.spyOn(document, 'addEventListener')
      const removed = vi.spyOn(document, 'removeEventListener')
      const write = deferred<boolean>()
      const scope = effectScope()
      const h = scope.run(() => setup({ onCommit: () => write.promise }))!
      try {
        h.grip('a1').focus()
        press(h, 'a1', 'todo')
        move(h, 10, 100)
        h.root.dispatchEvent(pointer('pointerup', 10, 100))
        const focusListener = added.mock.calls.find(([type, , capture]) => type === 'focusin' && capture === true)?.[1]
        const pointerListener = added.mock.calls.find(([type, , capture]) => type === 'pointerdown' && capture === true)?.[1]
        expect(focusListener).toBeDefined()
        expect(pointerListener).toBeDefined()
        if (ending === 'cancel') h.dnd.cancel()
        else scope.stop()
        expect(removed).toHaveBeenCalledWith('focusin', focusListener, true)
        expect(removed).toHaveBeenCalledWith('pointerdown', pointerListener, true)
        write.resolve(false)
        await flush()
      } finally {
        scope.stop()
        added.mockRestore()
        removed.mockRestore()
      }
    },
  )

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

  it('ignores a descendant losing capture as the root takes the pointer over', async () => {
    const h = setup()
    pressSurface(h, 'a1')
    move(h, 10, 30)
    expect(h.dnd.drag.value).toMatchObject({ id: 'a1', mode: 'pointer' })
    // A pen captures the pressed card implicitly; transferring that capture to
    // the root as the drag activates reports the card's loss here.
    h.root
      .querySelector('[data-item="a1"]')!
      .dispatchEvent(new Event('lostpointercapture', { bubbles: true }))
    expect(h.dnd.drag.value).toMatchObject({ id: 'a1', mode: 'pointer' })
    expect(overlay()).not.toBeNull()

    // The root's own capture is the one that matters.
    h.root.dispatchEvent(new Event('lostpointercapture'))
    await flush()
    expect(h.dnd.drag.value).toBeNull()
    expect(overlay()).toBeNull()
    expect(h.onCommit).not.toHaveBeenCalled()
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

  it('only targets a scope the pointer actually hits', () => {
    const h = setup()
    press(h, 'a1', 'todo')
    move(h, 10, 30)
    // The point is inside `doing`'s rect, but a cover sits on top of it: neither
    // an empty hit stack nor one with the lane underneath counts.
    const cover = document.createElement('div')
    const doing = h.root.querySelector('[data-scope="doing"]')!
    const hits = vi.spyOn(document, 'elementsFromPoint')
    hits.mockReturnValue([])
    move(h, 150, 70)
    expect(h.dnd.drag.value?.toScope).toBe('')
    hits.mockReturnValue([cover, doing])
    move(h, 150, 70)
    expect(h.dnd.drag.value?.toScope).toBe('')
    hits.mockRestore()
    move(h, 150, 70)
    expect(h.dnd.drag.value?.toScope).toBe('doing')
    document.dispatchEvent(key('Escape'))
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

  it('drops a fast drag above the first card when the destination move and release share a task', async () => {
    const h = setup({}, 'dom')
    // "Draft" (a1) is first, y 0..40 with midpoint 20; "Plan" (a3) starts last.
    // The destination sits clearly above Draft's original midpoint.
    press(h, 'a3', 'todo', 10, 110)
    move(h, 10, 45)
    move(h, 10, 10)

    // The destination move has not reached the view: the card is not collapsed
    // and no placeholder exists for targetAt to compensate for.
    expect(h.root.querySelector('.drag-placeholder')).toBeNull()
    expect(h.root.querySelector('[data-item="a3"]')!.classList.contains('drag-source')).toBe(false)
    expect(h.root.querySelector('[data-item="a3"]')!.getBoundingClientRect().height).toBe(ITEM_HEIGHT)

    h.root.dispatchEvent(pointer('pointerup', 10, 10))
    await flush()

    expect(h.onCommit).toHaveBeenCalledTimes(1)
    const commit = h.onCommit.mock.calls[0][0]
    expect(commit).toEqual({ id: 'a3', fromScope: 'todo', toScope: 'todo', index: 0 })
    const visible = lists.todo.filter((id) => id !== commit.id)
    const placed = [...visible]
    placed.splice(commit.index, 0, commit.id)
    // The card lands before Draft, so Draft is the card at the commit index.
    expect(visible[commit.index]).toBe('a1')
    expect(placed).toEqual(['a3', 'a1', 'a2'])
  })

  it('drops across lanes before the first card when the destination move and release share a task', async () => {
    const h = setup({}, 'dom')
    // "Plan" (a2) starts second in To do. "Draft" (b1) is first in In progress,
    // y 0..40 with midpoint 20; the pointer ends clearly above it. The first
    // targetAt still names `todo`, so it does not shift the destination lane;
    // the release sees `doing` and assumes a placeholder that was never drawn.
    press(h, 'a2', 'todo', 10, 60)
    move(h, 10, 40)
    move(h, 150, 10)
    expect(h.dnd.drag.value).toMatchObject({ toScope: 'doing', index: 0 })
    expect(h.root.querySelector('[data-scope="doing"] .drag-placeholder')).toBeNull()

    h.root.dispatchEvent(pointer('pointerup', 150, 10))
    await flush()

    expect(h.onCommit).toHaveBeenCalledTimes(1)
    const commit = h.onCommit.mock.calls[0][0]
    expect(commit).toEqual({ id: 'a2', fromScope: 'todo', toScope: 'doing', index: 0 })
    const visible = lists.doing.filter((id) => id !== commit.id)
    expect(visible[commit.index]).toBe('b1')
  })

  it('drops across lanes before the first card when a drawn placeholder is one index stale', async () => {
    const h = setup({}, 'dom')
    press(h, 'a2', 'todo', 10, 60)
    move(h, 10, 40)
    // Enter In progress below Draft's midpoint, so the preview draws the
    // placeholder at index 1 and Vue has flushed it.
    move(h, 150, 45)
    expect(h.dnd.drag.value).toMatchObject({ toScope: 'doing', index: 1 })
    h.render()
    expect(h.root.querySelectorAll('[data-scope="doing"] .drag-placeholder')).toHaveLength(1)
    expect(h.root.querySelector('[data-item="b1"]')!.getBoundingClientRect().top).toBe(0)
    expect(h.root.querySelector('[data-item="b2"]')!.getBoundingClientRect().top).toBe(100)

    // The final move above Draft's midpoint sets index 0. The release arrives in
    // the same task, so the placeholder is still drawn at index 1 while targetAt
    // shifts every slot as though it were at index 0.
    move(h, 150, 10)
    expect(h.dnd.drag.value).toMatchObject({ toScope: 'doing', index: 0 })
    h.root.dispatchEvent(pointer('pointerup', 150, 10))
    await flush()

    expect(h.onCommit).toHaveBeenCalledTimes(1)
    const commit = h.onCommit.mock.calls[0][0]
    expect(commit).toEqual({ id: 'a2', fromScope: 'todo', toScope: 'doing', index: 0 })
    const visible = lists.doing.filter((id) => id !== commit.id)
    expect(visible[commit.index]).toBe('b1')
  })

  it('shifts only the slots that follow the drawn placeholder, not those before it', async () => {
    const h = setup({}, 'dom')
    press(h, 'a3', 'todo', 10, 110)
    move(h, 10, 40)
    move(h, 150, 10)
    h.render()
    expect(h.root.querySelectorAll('[data-scope="doing"] .drag-placeholder')).toHaveLength(1)

    // The drawn placeholder heads the lane, so both cards follow it and their
    // midpoints are read a row higher. Counting the two cards after it instead
    // would apply no shift, and y=45 would stay above b1's live midpoint.
    move(h, 150, 45)
    expect(h.dnd.drag.value).toMatchObject({ toScope: 'doing', index: 1 })
    h.root.dispatchEvent(pointer('pointerup', 150, 45))
    await flush()
    expect(h.onCommit).toHaveBeenCalledWith({
      id: 'a3',
      fromScope: 'todo',
      toScope: 'doing',
      index: 1,
    })
  })

  it('uses the settled midpoint of an earlier item while its FLIP move is in flight', async () => {
    const h = setup({}, 'dom')
    press(h, 'a2', 'todo', 10, 60)
    move(h, 10, 40)
    move(h, 150, 45)
    h.render()
    // b1 is drawn 10px below its layout slot for the move transition, but the
    // insertion point reads its settled position.
    h.flip('b1', 10)
    expect(h.root.querySelector('[data-item="b1"]')!.getBoundingClientRect().top).toBe(10)
    move(h, 150, 25)
    expect(h.dnd.drag.value).toMatchObject({ toScope: 'doing', index: 1 })

    document.dispatchEvent(key('Escape'))
    await flush()
  })

  it('uses the settled midpoint of a later item while its FLIP move is in flight', async () => {
    const h = setup({}, 'dom')
    press(h, 'a2', 'todo', 10, 60)
    move(h, 10, 40)
    move(h, 150, 45)
    h.render()
    // b2's live rect is 10px lower than its layout slot; a release at y=75 is
    // below the settled midpoint (70) but above the translated one (80).
    h.flip('b2', 10)
    expect(h.root.querySelector('[data-item="b2"]')!.getBoundingClientRect().top).toBe(110)
    move(h, 150, 75)
    expect(h.dnd.drag.value).toMatchObject({ toScope: 'doing', index: 2 })
    h.root.dispatchEvent(pointer('pointerup', 150, 75))
    await flush()
    expect(h.onCommit).toHaveBeenCalledWith({
      id: 'a2',
      fromScope: 'todo',
      toScope: 'doing',
      index: 2,
    })
  })

  it('drops that same fast drag above the first card once the preview has flushed', async () => {
    const h = setup({}, 'dom')
    press(h, 'a3', 'todo', 10, 110)
    move(h, 10, 45)
    move(h, 10, 10)
    // The one difference from the failing case: Vue has flushed the preview.
    h.render()
    expect(h.root.querySelector('.drag-placeholder')).not.toBeNull()
    expect(h.root.querySelector('[data-item="a3"]')!.classList.contains('drag-source')).toBe(true)

    h.root.dispatchEvent(pointer('pointerup', 10, 10))
    await flush()
    expect(h.onCommit).toHaveBeenCalledWith({
      id: 'a3',
      fromScope: 'todo',
      toScope: 'todo',
      index: 0,
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

  it('drags from the row surface and swallows the click that follows', async () => {
    const h = setup()
    const clicked = vi.fn()
    document.addEventListener('click', clicked)
    pressSurface(h, 'a1')
    move(h, 10, 30)
    expect(h.dnd.drag.value).toMatchObject({ id: 'a1', mode: 'pointer' })
    expect(overlay()).not.toBeNull()
    expect(h.root.setPointerCapture).toHaveBeenCalledWith(1)
    h.root.dispatchEvent(pointer('pointerup', 10, 30))
    // The click the browser sends after the release lands on the row; a drag
    // must never also be a click-to-open.
    surfaceText(h, 'a1').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    expect(clicked).not.toHaveBeenCalled()
    await flush()
    surfaceText(h, 'a1').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    expect(clicked).toHaveBeenCalledTimes(1)
    document.removeEventListener('click', clicked)
  })

  it('leaves a short surface press as an ordinary click', async () => {
    const h = setup()
    const clicked = vi.fn()
    document.addEventListener('click', clicked)
    pressSurface(h, 'a1')
    move(h, 12, 12)
    expect(h.dnd.drag.value).toBeNull()
    // Nothing captured the pointer: the click the browser sends next still
    // targets the card, so its own click-to-open runs.
    expect(h.root.setPointerCapture).not.toHaveBeenCalled()
    h.root.dispatchEvent(pointer('pointerup', 12, 12))
    surfaceText(h, 'a1').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
    expect(clicked).toHaveBeenCalledTimes(1)
    expect(h.onCommit).not.toHaveBeenCalled()
    document.removeEventListener('click', clicked)
  })

  function selectElsewhere(): Selection {
    const aside = document.createElement('p')
    aside.textContent = 'keep me selected'
    document.body.appendChild(aside)
    const range = document.createRange()
    range.selectNodeContents(aside)
    const selection = window.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)
    return selection
  }

  it('a grip drag leaves an unrelated selection alone', () => {
    const h = setup()
    const selection = selectElsewhere()
    press(h, 'a1', 'todo')
    move(h, 10, 30)
    expect(h.dnd.drag.value).toMatchObject({ id: 'a1', mode: 'pointer' })
    // The sidebar shares this path: its grip must not clear what the user is
    // reading elsewhere.
    expect(selection.toString()).toBe('keep me selected')
    document.dispatchEvent(key('Escape'))
  })

  it('a surface drag clears the selection its press began', () => {
    const h = setup()
    const selection = selectElsewhere()
    pressSurface(h, 'a1')
    move(h, 10, 30)
    expect(h.dnd.drag.value).toMatchObject({ id: 'a1', mode: 'pointer' })
    expect(selection.toString()).toBe('')
    document.dispatchEvent(key('Escape'))
  })

  it('a surface press released outside the row before the threshold ends cleanly', () => {
    const h = setup()
    pressSurface(h, 'a1')
    // No drag yet, and the release lands outside the board: the deferred
    // surface press has no captured root handler, so the document ends it.
    document.dispatchEvent(pointer('pointerup', 900, 900))
    expect(h.dnd.drag.value).toBeNull()
    expect(h.onCommit).not.toHaveBeenCalled()

    // The next press is not blocked by a stuck pending gesture.
    pressSurface(h, 'a2')
    move(h, 10, 30)
    expect(h.dnd.drag.value).toMatchObject({ id: 'a2', mode: 'pointer' })
  })

  it('a surface press that never cleared the threshold ends on window blur', () => {
    const h = setup()
    pressSurface(h, 'a1')
    window.dispatchEvent(new Event('blur'))
    expect(h.dnd.drag.value).toBeNull()

    pressSurface(h, 'a1')
    move(h, 10, 30)
    expect(h.dnd.drag.value).toMatchObject({ id: 'a1', mode: 'pointer' })
  })

  it('does not start a surface drag on an interactive descendant', () => {
    const h = setup()
    const li = h.root.querySelector<HTMLElement>('[data-item="a1"]')!
    const controls = [
      Object.assign(document.createElement('button'), { type: 'button' }),
      Object.assign(document.createElement('a'), { href: '#x' }),
      Object.assign(document.createElement('input'), { type: 'text' }),
    ]
    for (const control of controls) {
      li.appendChild(control)
      control.dispatchEvent(pointer('pointerdown', 10, 10))
      move(h, 10, 30)
      expect(h.dnd.drag.value).toBeNull()
      expect(overlay()).toBeNull()
      control.remove()
    }
    expect(h.root.setPointerCapture).not.toHaveBeenCalled()
  })

  it('does not start a surface drag from touch, so a swipe still scrolls', () => {
    const h = setup()
    pressSurface(h, 'a1', 10, 10, 'touch')
    move(h, 10, 30)
    expect(h.dnd.drag.value).toBeNull()
    expect(overlay()).toBeNull()
    expect(h.root.setPointerCapture).not.toHaveBeenCalled()
  })

  it('reads a first surface move that lands outside the root', () => {
    const h = setup()
    pressSurface(h, 'a1')
    // The deferred press has no capture yet, so this move never reaches the
    // root's own listener. The document sees it and starts the drag.
    document.body.dispatchEvent(pointer('pointermove', 900, 900))
    expect(h.dnd.drag.value).toMatchObject({ id: 'a1', mode: 'pointer', toScope: '' })
    expect(overlay()).not.toBeNull()
    expect(h.root.setPointerCapture).toHaveBeenCalledWith(1)
    document.dispatchEvent(key('Escape'))
  })

  it('a cancelled surface drag writes nothing and leaves the keyboard grip working', async () => {
    const h = setup()
    pressSurface(h, 'a1')
    move(h, 10, 100)
    expect(overlay()).not.toBeNull()
    document.dispatchEvent(key('Escape'))
    await flush()
    expect(h.onCommit).not.toHaveBeenCalled()
    expect(overlay()).toBeNull()

    h.handlers('a1', 'todo').onKeydown(key(' '))
    expect(h.dnd.drag.value).toMatchObject({ id: 'a1', mode: 'keyboard' })
    h.handlers('a1', 'todo').onKeydown(key('ArrowDown'))
    h.handlers('a1', 'todo').onKeydown(key('Enter'))
    await flush()
    expect(h.onCommit).toHaveBeenCalledWith({
      id: 'a1',
      fromScope: 'todo',
      toScope: 'todo',
      index: 1,
    })
  })

  it('a pointer failure does not restore after a press elsewhere', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    press(h, 'a1', 'todo')
    move(h, 10, 100)
    h.root.dispatchEvent(pointer('pointerup', 10, 100))
    await flush()
    expect(h.onCommit).toHaveBeenCalledTimes(1)

    // The pointer source grip still holds focus, but the user pressed another
    // target while the write was in flight.
    h.root.dispatchEvent(pointer('pointerdown', 900, 900))
    expect(document.activeElement).toBe(h.grip('a1'))
    const focus = vi.spyOn(HTMLElement.prototype, 'focus')
    gate.resolve(false)
    await flush()
    expect(focus).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(h.grip('a1'))
    focus.mockRestore()
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

  it('a deferred keyboard drop does not take focus back from a control the user moved to', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    // While the write is in flight the user picks another card to work on.
    h.grip('a2').focus()
    await flush()
    expect(document.activeElement).toBe(h.grip('a2'))

    gate.resolve(true)
    await flush()
    expect(h.onCommit).toHaveBeenCalledTimes(1)
    expect(h.dnd.drag.value).toBeNull()
    expect(document.activeElement).toBe(h.grip('a2'))
  })

  it('a focus change during the deferred reveal tick is kept', async () => {
    const h = setup()
    h.onCommit.mockResolvedValueOnce(true)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))
    // Let the save settle so `finish` has scheduled the reveal's `nextTick`,
    // then move focus before that tick runs.
    await Promise.resolve()
    h.grip('a2').focus()
    await flush()
    expect(document.activeElement).toBe(h.grip('a2'))
  })

  it('a deferred keyboard drop failure does not take focus back either', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))
    h.grip('a2').focus()
    await flush()

    gate.resolve(false)
    await flush()
    expect(h.dnd.announcement.value).toBe('Could not save the new order.')
    expect(document.activeElement).toBe(h.grip('a2'))
  })

  it('moving focus elsewhere cancels a keyboard drag without taking focus back', async () => {
    const h = setup()
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()

    h.grip('a2').focus()
    await flush()
    expect(h.dnd.drag.value).toBeNull()
    expect(h.onCancel).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(h.grip('a2'))
  })

  it('a deferred keyboard drop failure still restores focus to the same grip', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    gate.resolve(false)
    await flush()
    expect(h.dnd.announcement.value).toBe('Could not save the new order.')
    expect(document.activeElement).toBe(h.grip('a1'))
  })

  it('a deferred keyboard drop still restores focus to the same grip', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    gate.resolve(true)
    await flush()
    expect(document.activeElement).toBe(h.grip('a1'))
  })

  it('a remount during a deferred drop restores focus to the card’s new grip', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    // The lane re-renders while the write is in flight: focus falls to the
    // document, which is a remount rather than a decision to leave the drag.
    const fresh = document.createElement('button')
    fresh.dataset.dragHandle = 'a1'
    h.grip('a1').replaceWith(fresh)
    expect(document.activeElement).not.toBe(fresh)

    gate.resolve(true)
    await flush()
    expect(document.activeElement).toBe(fresh)
  })

  it('a new pickup before the deferred reveal keeps the new drag’s grip', async () => {
    const h = setup()
    h.onCommit.mockResolvedValueOnce(true)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    handlers.onKeydown(key('Enter'))
    // The finished drop has scheduled its reveal; start a new drag before it runs.
    await Promise.resolve()
    h.grip('a2').focus()
    h.handlers('a2', 'todo').onKeydown(key(' '))
    await flush()

    expect(h.dnd.drag.value).toMatchObject({ id: 'a2', mode: 'keyboard' })
    expect(document.activeElement).toBe(h.grip('a2'))
    // The old reveal must not have scrolled its card back into view either.
    expect(scroll.mock.contexts).not.toContain(h.root.querySelector('[data-item="a1"]'))
  })

  it('an unmounted engine does not focus the old grip after its deferred reveal', async () => {
    const scope = effectScope()
    const h = scope.run(() => setup())!
    h.onCommit.mockResolvedValueOnce(true)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))
    await Promise.resolve()
    ;(document.activeElement as HTMLElement).blur()
    expect(document.activeElement).toBe(document.body)

    scope.stop()
    await flush()
    expect(document.activeElement).toBe(document.body)
  })

  it('a pointer press on a non-focusable area is a deliberate move, not a remount', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    // Clicking the lane's empty area focuses nothing: focus falls to the
    // document just as a remount would, but this was the user's own move.
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    h.root.dispatchEvent(pointer('pointerdown', 900, 900))
    ;(document.activeElement as HTMLElement).blur()
    expect(document.activeElement).toBe(document.body)

    gate.resolve(true)
    await flush()
    expect(document.activeElement).toBe(document.body)
    expect(scroll).not.toHaveBeenCalled()
  })

  it('a pointer press elsewhere prevents the deferred reveal even while the grip keeps focus', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    // A touch scroll or a click on a non-focusable area leaves focus on the
    // grip, but the user has chosen another target: no reveal, including no
    // scroll, may run.
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    h.root.dispatchEvent(pointer('pointerdown', 900, 900))
    expect(document.activeElement).toBe(h.grip('a1'))

    gate.resolve(true)
    await flush()
    expect(scroll).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(h.grip('a1'))
  })

  it('a deferred failure with a press elsewhere and body focus keeps focus away', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    h.root.dispatchEvent(pointer('pointerdown', 900, 900))
    ;(document.activeElement as HTMLElement).blur()
    expect(document.activeElement).toBe(document.body)

    gate.resolve(false)
    await flush()
    expect(h.dnd.announcement.value).toBe('Could not save the new order.')
    expect(document.activeElement).toBe(document.body)
    expect(scroll).not.toHaveBeenCalled()
  })

  it('a deferred failure with a press elsewhere while the grip stays focused does not refocus it', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    h.root.dispatchEvent(pointer('pointerdown', 900, 900))
    expect(document.activeElement).toBe(h.grip('a1'))

    const focus = vi.spyOn(HTMLElement.prototype, 'focus')
    gate.resolve(false)
    await flush()
    expect(focus).not.toHaveBeenCalled()
    expect(scroll).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(h.grip('a1'))
    focus.mockRestore()
  })

  it('a failure focus queued by the commit is dropped when the interaction ends first', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    gate.resolve(false)
    // Let the commit settle so its failure focus and reveal are queued.
    await Promise.resolve()
    ;(document.activeElement as HTMLElement).blur()
    expect(document.activeElement).toBe(document.body)

    h.dnd.cancel()
    await flush()
    expect(h.dnd.announcement.value).toBe('Could not save the new order.')
    expect(document.activeElement).toBe(document.body)
  })

  it('a silent cancel while a save is pending never focuses the old grip afterwards', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    ;(document.activeElement as HTMLElement).blur()
    // A remote change or navigation ends the drag while the write is in flight.
    h.dnd.cancel()
    gate.resolve(true)
    await flush()

    expect(h.dnd.drag.value).toBeNull()
    expect(document.activeElement).toBe(document.body)
    expect(scroll).not.toHaveBeenCalled()
  })

  it('a new drag started after a silent cancel survives the old commit resolving', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    h.dnd.cancel()
    expect(h.dnd.drag.value).toBeNull()
    h.grip('a2').focus()
    h.handlers('a2', 'todo').onKeydown(key(' '))
    await flush()
    expect(h.dnd.drag.value).toMatchObject({ id: 'a2', mode: 'keyboard' })
    expect(document.activeElement).toBe(h.grip('a2'))

    gate.resolve(true)
    await flush()
    // The stale commit must not clear or refocus the live drag.
    expect(h.dnd.drag.value).toMatchObject({ id: 'a2' })
    expect(document.activeElement).toBe(h.grip('a2'))

    h.handlers('a2', 'todo').onKeydown(key('ArrowDown'))
    h.handlers('a2', 'todo').onKeydown(key('Enter'))
    await flush()
    expect(h.onCommit).toHaveBeenCalledTimes(2)
    expect(h.onCommit).toHaveBeenLastCalledWith({
      id: 'a2',
      fromScope: 'todo',
      toScope: 'todo',
      index: 2,
    })
  })

  it('a deliberate focus move that is later remounted still does not give focus back', async () => {
    const h = setup()
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    // The user moves to another card; its row then remounts while the save is
    // still in flight, so the document focus no longer shows where they went.
    const other = h.grip('a2')
    other.focus()
    other.replaceWith(document.createElement('button'))
    expect(document.activeElement).toBe(document.body)

    gate.resolve(true)
    await flush()
    expect(document.activeElement).toBe(document.body)
  })

  it('an unmount while a save is pending schedules no focus or scroll after it resolves', async () => {
    const scope = effectScope()
    const h = scope.run(() => setup())!
    const gate = deferred<boolean>()
    h.onCommit.mockReturnValueOnce(gate.promise)
    h.grip('a1').focus()
    const handlers = h.handlers('a1', 'todo')
    handlers.onKeydown(key(' '))
    await flush()
    handlers.onKeydown(key('ArrowDown'))
    await flush()
    handlers.onKeydown(key('Enter'))

    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    ;(document.activeElement as HTMLElement).blur()
    scope.stop()
    gate.resolve(true)
    await flush()

    expect(document.activeElement).toBe(document.body)
    expect(scroll).not.toHaveBeenCalled()
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
