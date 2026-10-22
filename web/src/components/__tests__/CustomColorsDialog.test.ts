// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import CustomColorsDialog from '../settings/CustomColorsDialog.vue'
import { api } from '../../lib/api'
import { applyAppearanceSettings } from '../../composables/useAppearanceSettings'

const engine = {
  text_contrast: 'normal' as const,
  body_font: 'system' as const,
  theme: 'dark' as const,
  font_scale: 1.2,
  custom_colors: {},
  revision: 'r0',
}

let wrapper: ReturnType<typeof mount> | null = null

function find<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector)
  if (!element) throw new Error(`Missing element: ${selector}`)
  return element
}

async function settle() {
  await nextTick()
  await nextTick()
  await new Promise<void>((resolve) => setTimeout(resolve, 0))
}

function hex(mode: 'dark' | 'light', key: 'bg' | 'fg'): HTMLInputElement {
  const label = `${mode === 'dark' ? 'Dark' : 'Light'} theme ${key === 'bg' ? 'background' : 'text'} hex`
  return find<HTMLInputElement>(`input[aria-label="${label}"]`)
}

async function type(input: HTMLInputElement, value: string) {
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await settle()
}

function button(label: string): HTMLButtonElement {
  const found = [...document.querySelectorAll('button')].find((b) => b.textContent?.trim() === label)
  if (!found) throw new Error(`Missing button: ${label}`)
  return found as HTMLButtonElement
}

async function open(): Promise<void> {
  wrapper = mount(CustomColorsDialog, { props: { open: true }, attachTo: document.body })
  await settle()
}

beforeEach(() => {
  applyAppearanceSettings(engine)
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.restoreAllMocks()
  localStorage.clear()
  document.body.innerHTML = ''
  document.documentElement.removeAttribute('style')
  document.documentElement.removeAttribute('data-custom-colors')
})

describe('CustomColorsDialog', () => {
  it('seeds from a saved pair and from the defaults for a mode without one', async () => {
    applyAppearanceSettings({ ...engine, custom_colors: { dark: { bg: '#101018', fg: '#e8e8f0' } }, revision: 'r1' })
    await open()
    expect(hex('dark', 'bg').value).toBe('#101018')
    expect(hex('light', 'bg').value).toBe('#f4f4fa')
    expect(hex('light', 'fg').value).toBe('#1a1a2e')
    expect(find('[role="dialog"]').getAttribute('aria-labelledby')).toBeTruthy()
    expect(document.body.textContent).toContain('Custom colours')
  })

  it('refuses an unreadable pair, keeps Apply disabled and says why', async () => {
    await open()
    expect(button('Apply').disabled).toBe(true)
    await type(hex('dark', 'fg'), '#5a5a70')
    const status = document.querySelectorAll('[role="status"]')[0]
    expect(status.textContent).toContain('4.5:1')
    expect(status.textContent).toContain('lighter')
    expect(button('Apply').disabled).toBe(true)
  })

  it('enables Apply for a passing pair and patches only the edited section', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({
      ...engine, custom_colors: { dark: { bg: '#101018', fg: '#e8e8f0' } }, revision: 'r1',
    })
    await open()
    await type(hex('dark', 'bg'), '#101018')
    expect(document.querySelectorAll('[role="status"]')[0].textContent).toContain('Readable')
    expect(button('Apply').disabled).toBe(false)
    button('Apply').click()
    await settle()
    expect(patch).toHaveBeenCalledWith('/api/settings/appearance', {
      custom_colors: { dark: { bg: '#101018', fg: '#e8e8f0' } },
      revision: 'r0',
    })
    expect(wrapper!.emitted('update:open')?.at(-1)).toEqual([false])
  })

  it('keeps the saved pair of the other mode when applying one', async () => {
    applyAppearanceSettings({ ...engine, custom_colors: { light: { bg: '#fdf6e3', fg: '#2b2b2b' } }, revision: 'r1' })
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({ ...engine, revision: 'r2' })
    await open()
    await type(hex('dark', 'bg'), '#101018')
    button('Apply').click()
    await settle()
    expect(patch).toHaveBeenCalledWith('/api/settings/appearance', {
      custom_colors: {
        light: { bg: '#fdf6e3', fg: '#2b2b2b' },
        dark: { bg: '#101018', fg: '#e8e8f0' },
      },
      revision: 'r1',
    })
  })

  it('shows the format hint for an invalid hex and never saves it', async () => {
    const patch = vi.spyOn(api, 'patch')
    await open()
    await type(hex('light', 'bg'), 'zzz')
    expect(document.querySelectorAll('[role="status"]')[1].textContent).toContain('Enter colours like #1a1a2e.')
    expect(button('Apply').disabled).toBe(true)
    expect(patch).not.toHaveBeenCalled()
  })

  it('keeps a save error inside the dialog', async () => {
    vi.spyOn(api, 'patch').mockRejectedValue(new Error('Appearance settings changed on another device. Reload and try again.'))
    await open()
    await type(hex('dark', 'bg'), '#101018')
    button('Apply').click()
    await settle()
    expect(find('[role="alert"]').textContent).toContain('another device')
    expect(wrapper!.emitted('update:open')).toBeUndefined()
  })

  it('Reset patches an empty map', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({ ...engine, revision: 'r1' })
    await open()
    button('Reset').click()
    await settle()
    expect(patch).toHaveBeenCalledWith('/api/settings/appearance', { custom_colors: {}, revision: 'r0' })
  })

  it('Cancel and Escape close without a patch', async () => {
    const patch = vi.spyOn(api, 'patch')
    await open()
    button('Cancel').click()
    await settle()
    expect(wrapper!.emitted('update:open')?.at(-1)).toEqual([false])
    await wrapper!.setProps({ open: true })
    await settle()
    find('[role="dialog"]').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await settle()
    expect(wrapper!.emitted('update:open')?.length).toBe(2)
    expect(patch).not.toHaveBeenCalled()
  })
})
