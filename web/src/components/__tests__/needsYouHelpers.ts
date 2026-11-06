import { nextTick } from 'vue'
import type { DOMWrapper, VueWrapper } from '@vue/test-utils'

type Wrapper = Pick<VueWrapper, 'findAll' | 'find'>
type Row = DOMWrapper<Element>

/** Every row of the "Needs you" list, in order. */
export function rows(wrapper: Wrapper): Row[] {
  return wrapper.findAll('.needs-you-row')
}

export function titles(wrapper: Wrapper): string[] {
  return rows(wrapper).map(row => row.find('.needs-you-title-text').text())
}

/** The row's lead controls: one button or link, or none. */
export function leads(row: Row): Row[] {
  return row.findAll('.needs-you-primary button, .needs-you-primary a')
}

export function leadLabels(row: Row): string[] {
  return leads(row).map(el => el.text())
}

export async function openMenu(row: Row): Promise<void> {
  if (row.find('[role="menu"]').exists()) return
  await row.get('.needs-you-more').trigger('click')
  await nextTick()
}

/** The labels in the row's ⋯ menu (opens it). */
export async function menuLabels(row: Row): Promise<string[]> {
  await openMenu(row)
  return row.findAll('[role="menuitem"]').map(el => el.text())
}

/** Choose a menu item by its label (opens the menu first). */
export async function pick(row: Row, label: string): Promise<void> {
  await openMenu(row)
  const item = row.findAll('[role="menuitem"]').find(el => el.text() === label)
  if (!item) throw new Error(`no menu item "${label}"`)
  await item.trigger('click')
  await nextTick()
}

/** Every button and link the row shows without opening the menu. */
export function visibleControls(row: Row): Row[] {
  return row.findAll('button, a').filter(el => el.attributes('role') !== 'menuitem')
}
