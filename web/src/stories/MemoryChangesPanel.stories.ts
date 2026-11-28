import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { defineComponent, h } from 'vue'
import { expect, waitFor } from 'storybook/test'
import MemoryChangesPanel from '../components/MemoryChangesPanel.vue'
import MemoryChangesRail from '../components/MemoryChangesRail.vue'
import type { ChangesFeed } from '../lib/changesFeed'
import { changesFeed, emptyChangesFeed } from '../lib/changesFeedFixture'
import { changesRoute, CARE_WORKSPACE } from './careFixture'
import { assertFenceQuiet, renderHome } from './homeWindowsFixture'

/**
 * Memory · Changes over the fail-closed fixture: the production page and its
 * This week window reading one synthetic feed. Undo opens a chat and Review
 * goes to the care page, which the fixture does not declare, so no play presses them.
 */
function page(narrow: boolean) {
  return defineComponent({
    setup: () => () => h('div', {
      style: narrow
        ? 'display: flex; flex-direction: column; gap: 16px'
        : 'display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 24px; align-items: start',
    }, [h(MemoryChangesPanel), h(MemoryChangesRail)]),
  })
}

function story(feed: ChangesFeed | 'failed', options: { narrow?: boolean } = {}) {
  const owner = renderHome(page(!!options.narrow), {
    label: 'Changes workshop: one synthetic workspace and a synthetic feed.',
    workspace: CARE_WORKSPACE,
    narrow: options.narrow,
  })
  return {
    parameters: { fixture: { routes: [changesRoute(feed)] } },
    render: () => ({ components: { owner }, template: '<owner />' }),
  }
}

const meta = {
  id: 'memory-changes-panel',
  title: 'Memory/MemoryChangesPanel',
  component: MemoryChangesPanel,
  tags: ['!autodocs'],
  parameters: { docs: { disable: true, story: { inline: false } }, controls: { disable: true } },
} satisfies Meta<typeof MemoryChangesPanel>
export default meta
type Story = StoryObj<typeof meta>

export const Loaded: Story = {
  ...story(changesFeed),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('heading', { name: /Workspace care, \d\d:\d\d$/ })).toBeTruthy())
    expect(canvas.getByRole('heading', { name: 'This week' })).toBeTruthy()
    const undo = canvas.getAllByRole('button', { name: /^Undo in a chat: / })
    expect(undo).toHaveLength(3)
    for (const button of undo) expect(button.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    expect(canvas.getByRole('button', { name: 'All' }).getAttribute('aria-pressed')).toBe('true')
    await assertFenceQuiet(canvas, 1)
  },
}

export const FilteredToTasks: Story = {
  ...story(changesFeed),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Tasks' })).toBeTruthy())
    await waitFor(() => expect(canvas.getAllByRole('button', { name: /^Undo in a chat: / }).length).toBeGreaterThan(0))
    await userEvent.click(canvas.getByRole('button', { name: 'Tasks' }))
    await waitFor(() => expect(canvas.queryAllByRole('button', { name: /^Undo in a chat: / })).toHaveLength(0))
    expect(canvas.getByText('Read 2 tasks, wrote 1 fact (People/User.md)')).toBeTruthy()
  },
}

export const Phone: Story = {
  ...story(changesFeed, { narrow: true }),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('heading', { name: 'This week' })).toBeTruthy())
    for (const name of ['All', 'Memory', 'Notes', 'Tasks', 'Skills']) {
      expect(canvas.getByRole('button', { name }).getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    }
  },
}

export const Empty: Story = {
  ...story(emptyChangesFeed),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/^Nothing recorded yet\./)).toBeTruthy())
  },
}

export const Failed: Story = {
  ...story('failed'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByRole('alert').textContent).toContain('Could not read the changes'))
  },
}
