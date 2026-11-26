import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import MemoryHowItWorks from '../components/MemoryHowItWorks.vue'
import { mountExplainerFixture } from './memoryGuidanceFixture'

/**
 * The "How memory works" disclosure under the Memory header over the
 * fail-closed fixture boundary. The production component makes no request at
 * all; the connected mount supplies the memory router its Categories
 * RouterLink resolves through, and every story seeds the per-browser storage
 * key before mounting so each story's initial state is its own.
 */

function mountExplainer(host: HTMLElement): ReturnType<typeof mountExplainerFixture> {
  return mountExplainerFixture(host, MemoryHowItWorks)
}

function renderExplainer(open: '0' | '1') {
  return defineComponent({
    setup() {
      // Seed the owner's own storage key: mount reads it, so every story starts
      // from the state its name names no matter what ran before it. The owner
      // guards blocked storage; the workshop seeds the same guarded way.
      try { localStorage.setItem('ciao.memory.howOpen', open) } catch { /* page-local choice */ }
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountExplainer>>()
      const error = ref('')
      onMounted(() => {
        try { fixture.value = mountExplainer(host.value!) }
        catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', {}, [
        h('p', `Synthetic ${open === '1' ? 'open' : 'closed'} fixture: production MemoryHowItWorks, the disclosure choice pre-seeded.`),
        h('div', { ref: host }),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

const viewportWide = { viewport: { defaultViewport: 'wide' } }

const meta = {
  id: 'memory-how-it-works',
  title: 'Memory guidance/MemoryHowItWorks',
  component: MemoryHowItWorks,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof MemoryHowItWorks>
export default meta
type Story = StoryObj<typeof meta>

const body = (canvasElement: HTMLElement) => canvasElement.querySelector<HTMLElement>('#mhw-body')!

// Collapsed as the browser never saw it opened: the body stays out of the tab
// order, and the keyboard opens it and reaches its Categories link.
export const Collapsed: Story = {
  parameters: { fixture: { routes: [] }, ...viewportWide },
  render: () => renderExplainer('0'),
  play: async ({ canvas, canvasElement, userEvent }) => {
    const toggle = await canvas.findByRole('button', { name: 'How memory works' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(toggle.getAttribute('aria-controls')).toBe('mhw-body')
    expect(canvasElement.querySelector<HTMLElement>('#mhw-body')).toBeTruthy()
    expect(body(canvasElement).style.display).toBe('none')
    // The keyboard path: Tab reaches only the toggle, Enter opens the body,
    // focus stays on the control and its focus ring is drawn.
    for (let step = 0; step < 6 && document.activeElement !== toggle; step++) await userEvent.tab()
    expect(document.activeElement).toBe(toggle)
    expect(getComputedStyle(toggle).outlineWidth).toBe('2px')
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(toggle.getAttribute('aria-expanded')).toBe('true'))
    expect(body(canvasElement).style.display).not.toBe('none')
    expect(localStorage.getItem('ciao.memory.howOpen')).toBe('1')
    // Inside the open body the next stop is its Categories link.
    await userEvent.tab()
    const categories = document.activeElement as HTMLAnchorElement
    expect(categories.textContent).toBe('Categories tab')
    expect(categories.getAttribute('href')).toBe('/memory/categories')
    expect(getComputedStyle(categories).outlineWidth).toBe('2px')
  },
}

// The open disclosure the browser remembers: the full body, and the toggle
// that closes and re-opens it, storing the choice.
export const Expanded: Story = {
  parameters: { fixture: { routes: [] }, ...viewportWide },
  render: () => renderExplainer('1'),
  play: async ({ canvas, canvasElement, userEvent }) => {
    const toggle = await canvas.findByRole('button', { name: 'How memory works' })
    await waitFor(() => expect(toggle.getAttribute('aria-expanded')).toBe('true'))
    expect(body(canvasElement).style.display).not.toBe('none')
    expect(canvas.getByText('What gets saved')).toBeTruthy()
    expect(canvas.getByText('Undo')).toBeTruthy()
    expect(canvasElement.querySelector('table')).toBeNull()
    const siteLink = canvasElement.querySelector<HTMLAnchorElement>('a[target="_blank"]')!
    expect(siteLink.rel).toContain('noopener')
    expect(siteLink.rel).toContain('noreferrer')
    // The toggle collapses and re-opens the body, storing each choice.
    await userEvent.click(toggle)
    await waitFor(() => expect(toggle.getAttribute('aria-expanded')).toBe('false'))
    expect(body(canvasElement).style.display).toBe('none')
    expect(localStorage.getItem('ciao.memory.howOpen')).toBe('0')
    await userEvent.click(toggle)
    await waitFor(() => expect(toggle.getAttribute('aria-expanded')).toBe('true'))
    expect(body(canvasElement).style.display).not.toBe('none')
    expect(localStorage.getItem('ciao.memory.howOpen')).toBe('1')
  },
}

// The 390px viewport: the disclosure open and inside the pane.
export const Narrow: Story = {
  parameters: { fixture: { routes: [] }, viewport: { defaultViewport: 'narrow' } },
  render: () => renderExplainer('1'),
  play: async ({ canvas, canvasElement, userEvent }) => {
    const toggle = await canvas.findByRole('button', { name: 'How memory works' })
    await waitFor(() => expect(toggle.getAttribute('aria-expanded')).toBe('true'))
    expect(body(canvasElement).style.display).not.toBe('none')
    const section = canvasElement.querySelector<HTMLElement>('section.mhw')!
    expect(section.getBoundingClientRect().width).toBeGreaterThan(0)
    expect(section.getBoundingClientRect().width).toBeLessThanOrEqual(window.innerWidth)
    // The keyboard still drives the disclosure at this size.
    for (let step = 0; step < 6 && document.activeElement !== toggle; step++) await userEvent.tab()
    expect(document.activeElement).toBe(toggle)
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(toggle.getAttribute('aria-expanded')).toBe('false'))
    expect(body(canvasElement).style.display).toBe('none')
  },
}
