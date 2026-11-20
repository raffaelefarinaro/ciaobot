import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import ShellCard from '../components/ShellCard.vue'
import { shellCardFromHistory, type ShellCard as Card } from '../lib/shellCard'

/**
 * One shell call as a card in the activity trace. The data is the shape the live
 * stream and reloaded history both produce (`lib/shellCard.ts`), so the stories hand
 * the component the card directly. Plays check the disclosure state, the state word,
 * the exit code, the background badge and the output tail.
 */

const sample = (overrides: Partial<Card> = {}): Card => ({
  tool_use_id: 'sh-1',
  command: 'npm run build',
  background: false,
  state: 'running',
  exit_code: null,
  output_tail: 'vite v6 building for production...\nmodules transformed: 412',
  ...overrides,
})

const meta = {
  id: 'shell-card',
  title: 'Chat activity/ShellCard',
  component: ShellCard,
  args: { card: sample() },
  render: (args) => ({
    components: { ShellCard },
    setup: () => ({ args }),
    template: '<div style="max-width: 520px; padding: var(--space-3); color: var(--fg); font-size: var(--text-sm)"><ShellCard v-bind="args" /></div>',
  }),
} satisfies Meta<typeof ShellCard>
export default meta
type Story = StoryObj<typeof meta>

// A foreground shell still running: open, with the working glyph and its output so far.
export const Running: Story = {
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.shell-card-toggle')?.getAttribute('aria-expanded')).toBe('true')
    expect(canvasElement.querySelector('.shell-card-state-label')?.textContent).toBe('Running')
    expect(canvasElement.querySelector('.activity-glyph--working')).not.toBeNull()
    expect(canvasElement.querySelector('.shell-card-output')?.textContent).toContain('modules transformed')
    expect(canvasElement.querySelector('.shell-card-badge')).toBeNull()
  },
}

// A finished shell folds itself; its exit code shows beside the state word.
export const Done: Story = {
  args: { card: sample({ state: 'done', exit_code: 0 }) },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.shell-card-toggle')?.getAttribute('aria-expanded')).toBe('false')
    expect(canvasElement.querySelector('.shell-card-state-label')?.textContent).toBe('Done')
    expect(canvasElement.querySelector('.shell-card-exit')?.textContent).toBe('exit 0')
    expect(canvasElement.querySelector('.shell-card-body')).toBeNull()
  },
}

// A non-zero exit: the card carries the failed border and the code.
export const Failed: Story = {
  args: { card: sample({ state: 'failed', exit_code: 2, output_tail: 'Error: Cannot find module ./app' }) },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.shell-card--failed')).not.toBeNull()
    expect(canvasElement.querySelector('.shell-card-state-label')?.textContent).toBe('Failed')
    expect(canvasElement.querySelector('.shell-card-exit')?.textContent).toBe('exit 2')
  },
}

// A backgrounded shell: the launch acknowledgement keeps it running, with the badge.
export const BackgroundRunning: Story = {
  args: {
    card: sample({
      command: 'ping -c 8 127.0.0.1',
      background: true,
      output_tail: '',
    }),
  },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.shell-card-state-label')?.textContent).toBe('Running')
    expect(canvasElement.querySelector('.shell-card-badge')?.textContent).toBe('running in the background')
    expect(canvasElement.querySelector('.shell-card-badge')?.getAttribute('title')).toContain('reports back')
    expect(canvasElement.querySelector('.shell-card-exit')).toBeNull()
  },
}

// Long output: the tail is a scrolling preformatted block, and a long command wraps inside the card.
export const LongOutput: Story = {
  args: {
    card: sample({
      state: 'done',
      exit_code: 0,
      command: 'git log --oneline --all --graph --decorate -n 200 --format="%h %s (%an, %ar)"',
      output_tail: Array.from({ length: 40 }, (_, n) => `line ${n + 1}: ${'output '.repeat(12)}`).join('\n'),
    }),
  },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.shell-card-toggle')?.getAttribute('aria-expanded')).toBe('false')
    expect(canvasElement.querySelector('.shell-card-output')).toBeNull()
  },
}

// A journal row from before the card existed carries no flags: it reads as a plain done shell.
export const OldRow: Story = {
  args: {
    card: shellCardFromHistory({ tool_use_id: 'sh-old', command: 'ls -la', state: 'done' }) as Card,
  },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.shell-card-command')?.textContent).toBe('ls -la')
    expect(canvasElement.querySelector('.shell-card-state-label')?.textContent).toBe('Done')
    expect(canvasElement.querySelector('.shell-card-exit')).toBeNull()
  },
}
