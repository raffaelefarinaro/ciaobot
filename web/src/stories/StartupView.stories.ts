import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { defineComponent, h, ref } from 'vue'
import StartupView from '../components/StartupView.vue'

/**
 * StartupView (the `components-startup-view` inventory row, #3310): the fixed
 * startup curtain App.vue renders while the engine's poll reports boot phases.
 * Props only — no engine, no fixture boundary traffic: the phases and
 * readiness a story shows are synthetic props of exactly the shape the App
 * poll hands it, and the panel records the `skip` emit a play performs.
 */

interface Phase {
  name: string
  status: string
  message: string
  started_at: string | null
  finished_at: string | null
}

const phase = (name: string, status: string, message = ''): Phase =>
  ({ name, status, message, started_at: null, finished_at: null })

function startupPanel(props: { phases: Phase[]; overallReady: boolean }) {
  return defineComponent({
    name: 'StartupStoryPanel',
    setup() {
      const skips = ref(0)
      return () => h('div', [
        h(StartupView, {
          phases: props.phases,
          overallReady: props.overallReady,
          onSkip: () => { skips.value += 1 },
        }),
        h('p', { role: 'status' }, `Skip emits: ${skips.value}.`),
      ])
    },
  })
}

const meta = {
  id: 'startup-view',
  title: 'Startup, offline and login/StartupView',
  component: StartupView,
  tags: ['!autodocs'],
  args: { phases: [], overallReady: false },
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
  render: (args) => startupPanel({ phases: args.phases, overallReady: args.overallReady }),
} satisfies Meta<typeof StartupView>
export default meta
type Story = StoryObj<typeof meta>

const phasesOf = (canvasElement: HTMLElement) => canvasElement.querySelector('.startup-phases')
const waitBarOf = (canvasElement: HTMLElement) => canvasElement.querySelector('.startup-wait')
const continueOf = (canvasElement: HTMLElement) => canvasElement.querySelector('button.startup-continue') as HTMLButtonElement | null

export const Connecting: Story = {
  // The empty-phases branch: the one loading geometry, before phases arrive.
  play: async ({ canvasElement, userEvent }) => {
    expect(canvasElement.querySelector('h1')?.textContent).toBe('Connecting to Ciaobot')
    expect(canvasElement.textContent).toContain('Checking the connection to your workspace.')
    expect(phasesOf(canvasElement)).toBeNull()
    expect(waitBarOf(canvasElement)).toBeTruthy()
    const skipped = continueOf(canvasElement)
    expect(skipped?.textContent?.trim()).toBe('Continue to app')
    await userEvent.click(skipped!)
    expect(canvasElement.textContent).toContain('Skip emits: 1.')
  },
}

export const PhaseProgress: Story = {
  // Done, in progress and waiting rows, including the unknown-name fallback
  // and the known labels the phaseName map answers with.
  args: {
    phases: [
      phase('refresh_vault_index', 'done'),
      phase('update_skills', 'in_progress'),
      phase('a_storybook_phase', 'waiting'),
    ],
  },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('h1')?.textContent).toBe('Connecting to Ciaobot')
    expect(canvasElement.textContent).toContain('Getting your workspace ready.')
    expect(phasesOf(canvasElement)).toBeTruthy()
    expect(waitBarOf(canvasElement)).toBeNull()
    expect(canvasElement.textContent).toContain('Preparing your notes')
    expect(canvasElement.textContent).toContain('Ready')
    expect(canvasElement.textContent).toContain('Preparing skills')
    expect(canvasElement.textContent).toContain('In progress')
    expect(canvasElement.textContent).toContain('a storybook phase')
    expect(canvasElement.textContent).toContain('Waiting')
    expect(continueOf(canvasElement)).toBeTruthy()
  },
}

export const PhaseFailed: Story = {
  args: { phases: [phase('connect_claude_code', 'failed')] },
  play: async ({ canvasElement }) => {
    expect(canvasElement.textContent).toContain('Connecting to Claude Code')
    expect(canvasElement.textContent).toContain('Needs attention')
    expect(canvasElement.querySelector('.startup-phase.is-failed')).toBeTruthy()
    expect(continueOf(canvasElement)).toBeTruthy()
  },
}

export const PhaseMessage: Story = {
  // The `v-if="phase.message"` line under a phase row.
  args: { phases: [phase('server_starting', 'in_progress', 'the service came up once already')] },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.startup-phase-message')?.textContent).toBe('the service came up once already')
  },
}

export const Ready: Story = {
  // Ready with phases present: the continue button is gone.
  args: {
    overallReady: true,
    phases: [phase('refresh_vault_index', 'done'), phase('update_skills', 'done')],
  },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('h1')?.textContent).toBe('Connected to Ciaobot')
    expect(canvasElement.textContent).toContain('Getting your workspace ready.')
    expect(phasesOf(canvasElement)).toBeTruthy()
    expect(continueOf(canvasElement)).toBeNull()
    expect(canvasElement.textContent).toContain('Skip emits: 0.')
  },
}

export const Narrow: Story = {
  // The curtain is fixed inset-0, so the 390px viewport is the story viewport.
  parameters: { viewport: { defaultViewport: 'narrow' } },
  play: async ({ canvasElement }) => {
    const column = canvasElement.querySelector('.startup-content') as HTMLElement
    const width = column.getBoundingClientRect().width
    expect(width).toBeGreaterThan(0)
    expect(width).toBeLessThanOrEqual(window.innerWidth)
    expect(canvasElement.querySelector('h1')?.textContent).toBe('Connecting to Ciaobot')
    expect(continueOf(canvasElement)).toBeTruthy()
  },
}
