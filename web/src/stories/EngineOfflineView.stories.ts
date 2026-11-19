import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, ref } from 'vue'
import EngineOfflineView from '../components/EngineOfflineView.vue'

/**
 * EngineOfflineView (the `components-engine-offline-view` inventory row,
 * #3310): the fixed outage curtain App.vue renders over the app while the
 * engine monitor reports an outage or an announced restart. Props only — no
 * engine, no fixture boundary traffic: `state`, `loopback`, `host` and
 * `retrying` are synthetic props of exactly the shape App.vue hands it, and
 * the panel records the `retry` emit a play performs.
 */

type ViewProps = Required<{
  state: 'updating' | 'unreachable'
  loopback: boolean
  host: string
  retrying: boolean
}>

function offlinePanel(props: ViewProps, config: { clipboard?: boolean } = {}) {
  return defineComponent({
    name: 'EngineOfflineStoryPanel',
    setup() {
      const retries = ref(0)
      let restoreClipboard: (() => void) | undefined
      if (config.clipboard) {
        // Real clipboard access is not a story fact: the stub lets the
        // component's own writeClipboard path record the copy.
        const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
        Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => {} } })
        restoreClipboard = () => {
          if (original) Object.defineProperty(navigator, 'clipboard', original)
          else Reflect.deleteProperty(navigator, 'clipboard')
        }
        onBeforeUnmount(() => restoreClipboard?.())
      }
      return () => h('div', [
        h(EngineOfflineView, {
          state: props.state,
          loopback: props.loopback,
          host: props.host,
          retrying: props.retrying,
          onRetry: () => { retries.value += 1 },
        }),
        h('p', { role: 'status' }, `Retry emits: ${retries.value}.`),
      ])
    },
  })
}

const meta = {
  id: 'engine-offline-view',
  title: 'Startup, offline and login/EngineOfflineView',
  component: EngineOfflineView,
  tags: ['!autodocs'],
  args: { state: 'unreachable', loopback: true, host: 'localhost:8443', retrying: false },
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
  render: (args) => offlinePanel({ state: args.state, loopback: args.loopback, host: args.host, retrying: args.retrying }),
} satisfies Meta<typeof EngineOfflineView>
export default meta
type Story = StoryObj<typeof meta>

const titleOf = (canvasElement: HTMLElement) => canvasElement.querySelector('#engine-offline-title')?.textContent
const retryOf = (canvasElement: HTMLElement) => canvasElement.querySelector('.engine-offline-actions .btn-primary') as HTMLButtonElement

export const UnreachableLoopback: Story = {
  // The default: on loopback the fix is a local command, shown verbatim and
  // copyable, and the curtain takes focus and traps Tab from the keyboard.
  play: async ({ canvasElement, userEvent }) => {
    expect(titleOf(canvasElement)).toBe("Ciaobot isn't running")
    expect(canvasElement.textContent).toContain('The engine on this computer stopped responding. Start it from a terminal:')
    expect(canvasElement.textContent).toContain('ciao service start')
    expect(canvasElement.textContent).toContain('ciao service status')
    expect(canvasElement.textContent).toContain('Logs: .runtime/ciao.stderr.log')
    // The curtain takes focus so the keyboard reaches it, and Retry is it.
    expect(document.activeElement).toBe(retryOf(canvasElement))
    await userEvent.click(retryOf(canvasElement))
    expect(canvasElement.textContent).toContain('Retry emits: 1.')
    // The capture-phase Tab trap wraps focus back into the curtain.
    const buttons = [...canvasElement.querySelectorAll<HTMLButtonElement>('button')]
    expect(buttons.length).toBe(3)
    retryOf(canvasElement).focus()
    await userEvent.tab()
    expect(document.activeElement).toBe(buttons[0])
    await userEvent.tab({ shift: true })
    expect(document.activeElement).toBe(retryOf(canvasElement))
  },
}

export const UnreachableRemote: Story = {
  // Off the host the commands never appear: the title says which computer.
  args: { loopback: false, host: 'mini.storybook.ts.net' },
  play: async ({ canvasElement }) => {
    expect(titleOf(canvasElement)).toBe("Ciaobot on mini.storybook.ts.net isn't reachable")
    expect(canvasElement.textContent).toContain('Check that the computer running Ciaobot is on and connected. Your open chat and drafts are kept.')
    expect(canvasElement.querySelector('.engine-offline-cmd')).toBeNull()
    expect(retryOf(canvasElement).disabled).toBe(false)
    expect(canvasElement.textContent).toContain('Retry emits: 0.')
  },
}

export const Restarting: Story = {
  // An announced restart shows no commands and no host, whatever the origin.
  args: { state: 'updating' },
  play: async ({ canvasElement }) => {
    expect(titleOf(canvasElement)).toBe('Ciaobot is restarting')
    expect(canvasElement.textContent).toContain('This page reconnects by itself.')
    expect(canvasElement.querySelector('.engine-offline-cmd')).toBeNull()
    expect(retryOf(canvasElement).disabled).toBe(false)
  },
}

export const Retrying: Story = {
  // The blocked control, not just a style: disabled while the parent is busy.
  args: { retrying: true },
  play: async ({ canvasElement }) => {
    const retry = retryOf(canvasElement)
    expect(retry.disabled).toBe(true)
    expect(retry.textContent?.trim()).toBe('Checking…')
    expect(retry.classList.contains('btn-primary')).toBe(true)
  },
}

export const CommandCopied: Story = {
  // The stubbed clipboard: the copied row reads Copied, the other row stays Copy.
  render: (args) => offlinePanel({ state: args.state, loopback: args.loopback, host: args.host, retrying: args.retrying }, { clipboard: true }),
  play: async ({ canvasElement, userEvent }) => {
    const rows = canvasElement.querySelectorAll<HTMLButtonElement>('.engine-offline-cmd button')
    const [first, second] = rows
    expect(first.textContent?.trim()).toBe('Copy')
    expect(second.textContent?.trim()).toBe('Copy')
    await userEvent.click(first)
    expect(first.textContent?.trim()).toBe('Copied')
    expect(second.textContent?.trim()).toBe('Copy')
    await userEvent.click(second)
    expect(second.textContent?.trim()).toBe('Copied')
    expect(first.textContent?.trim()).toBe('Copy')
  },
}

export const Narrow: Story = {
  // The curtain is fixed inset-0, so the 390px viewport is the story viewport.
  parameters: { viewport: { defaultViewport: 'narrow' } },
  play: async ({ canvasElement }) => {
    const column = canvasElement.querySelector('.engine-offline-content') as HTMLElement
    const width = column.getBoundingClientRect().width
    expect(width).toBeGreaterThan(0)
    expect(width).toBeLessThanOrEqual(window.innerWidth)
    expect(titleOf(canvasElement)).toBe("Ciaobot isn't running")
    expect(canvasElement.querySelector('.engine-offline-cmd')).toBeTruthy()
  },
}
