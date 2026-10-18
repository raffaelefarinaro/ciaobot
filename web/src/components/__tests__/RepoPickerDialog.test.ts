// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import RepoPickerDialog from '../RepoPickerDialog.vue'
import { useProjectStore } from '../../stores/projects'

const apiGet = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', () => ({ api: { get: apiGet } }))

let wrapper: ReturnType<typeof mount> | null = null

function find<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector)
  if (!element) throw new Error(`Missing dialog element: ${selector}`)
  return element
}

async function settle() {
  await nextTick()
  await nextTick()
  await new Promise<void>(resolve => setTimeout(resolve, 0))
  await nextTick()
}

async function mountDialog(currentPath: string | null = null) {
  wrapper = mount(RepoPickerDialog, {
    props: { open: true, projectId: 'p1', currentPath },
    attachTo: document.body,
  })
  await settle()
}

async function type(value: string) {
  const input = find<HTMLInputElement>('#repo-picker-path')
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await settle()
}

function submitButton(): HTMLButtonElement {
  return find<HTMLButtonElement>('button[type="submit"]')
}

describe('RepoPickerDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiGet.mockResolvedValue({
      roots: ['/h/repos'],
      candidates: [
        { path: '/h/repos/alpha', name: 'alpha', root: '/h/repos' },
        { path: '/h/repos/beta', name: 'beta', root: '/h/repos' },
      ],
    })
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
  })

  it('lists candidates and fills the path field from one', async () => {
    await mountDialog()
    const rows = document.querySelectorAll<HTMLButtonElement>('.repo-candidate')
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('alpha')
    rows[1].click()
    await settle()
    expect(find<HTMLInputElement>('#repo-picker-path').value).toBe('/h/repos/beta')
    expect(rows[1].getAttribute('aria-pressed')).toBe('true')
  })

  it('links a typed path through updateProject', async () => {
    const store = useProjectStore()
    const spy = vi.spyOn(store, 'updateProject').mockResolvedValue({} as never)
    await mountDialog()
    await type('  /x/app ')
    submitButton().click()
    await settle()
    expect(spy).toHaveBeenCalledWith('p1', { repo_path: '/x/app' })
    expect(wrapper!.emitted('linked')).toHaveLength(1)
    expect(wrapper!.emitted('close')).toHaveLength(1)
  })

  it("shows the engine's validation error inline and stays open", async () => {
    const store = useProjectStore()
    vi.spyOn(store, 'updateProject').mockRejectedValue(new Error('not a git repository: /x'))
    await mountDialog()
    await type('/x')
    submitButton().click()
    await settle()
    expect(find('#repo-picker-error').textContent).toContain('not a git repository: /x')
    expect(wrapper!.emitted('close')).toBeUndefined()
  })

  it('separates a failed candidate load from an empty list', async () => {
    apiGet.mockRejectedValueOnce(new Error('boom'))
    await mountDialog()
    const alert = find('.repo-note[role="alert"]')
    expect(alert.textContent).toContain('boom')
    expect(alert.querySelector('.repo-retry')).not.toBeNull()
    wrapper!.unmount()
    wrapper = null

    apiGet.mockResolvedValueOnce({ roots: [], candidates: [] })
    await mountDialog()
    expect(document.body.textContent).toContain('No git repositories found')
    expect(document.querySelector('.repo-retry')).toBeNull()
  })

  it('disables submit for an empty or unchanged path', async () => {
    await mountDialog('/x/current')
    expect(submitButton().disabled).toBe(true)
    await type('/x/other')
    expect(submitButton().disabled).toBe(false)
    await type('')
    expect(submitButton().disabled).toBe(true)
  })
})
