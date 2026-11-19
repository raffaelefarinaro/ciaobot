import { h } from 'vue'
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import OnboardingSummary from '../components/onboarding/OnboardingSummary.vue'
import type { OnboardingSummaryItem } from '../lib/onboarding'

/**
 * The setup-summary list (the `components-onboarding-onboarding-summary`
 * inventory row, #3308). Props only: `items` and `loading` are the owner's
 * props, and each item state is one of the four `state` values the row type
 * allows, so no fixture is declared. Each story renders the real component;
 * all data is synthetic.
 */
const meta = {
  id: 'onboarding-summary',
  title: 'Onboarding primitives/OnboardingSummary',
  component: OnboardingSummary,
  args: { items: [], loading: false },
} satisfies Meta<typeof OnboardingSummary>
export default meta
type Story = StoryObj<typeof meta>

function item(patch: Partial<OnboardingSummaryItem>): OnboardingSummaryItem {
  return { id: 'location', label: 'Folder and workspace', value: 'storybook-alpha', state: 'ready', ...patch }
}

function list(...items: OnboardingSummaryItem[]) {
  return () => h('div', { style: 'max-width: 640px' }, [h(OnboardingSummary, { items, loading: false })])
}

export const Loading: Story = {
  // The initial render: a status line instead of the list.
  render: () => h(OnboardingSummary, { items: [], loading: true }),
  play: async ({ canvasElement }) => {
    const status = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('[role="status"]')
      expect(found).toBeTruthy()
      return found!
    })
    expect(status.textContent).toBe('Checking setup…')
    // No list while loading.
    expect(canvasElement.querySelector('.summary-list')).toBeNull()
  },
}

export const Empty: Story = {
  // A loaded list with no rows: the list element renders and nothing is announced.
  render: list(),
  play: async ({ canvasElement }) => {
    const list = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.summary-list')
      expect(found).toBeTruthy()
      return found!
    })
    expect(list.querySelectorAll('.summary-row')).toHaveLength(0)
    expect(canvasElement.querySelector('[role="status"]')).toBeNull()
  },
}

export const Ready: Story = {
  render: list(item({ state: 'ready', href: '/settings/workspace' })),
  play: async ({ canvasElement }) => {
    const state = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.summary-state')
      expect(found).toBeTruthy()
      return found!
    })
    expect(state.getAttribute('data-state')).toBe('ready')
    expect(state.textContent).toBe('ready')
    expect(canvasElement.querySelector<HTMLElement>('.summary-value')!.textContent).toBe('storybook-alpha')
  },
}

export const Deferred: Story = {
  render: list(item({ state: 'deferred', value: 'No model chosen yet', href: '/settings/workspace' })),
  play: async ({ canvasElement }) => {
    const state = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.summary-state')
      expect(found).toBeTruthy()
      return found!
    })
    expect(state.getAttribute('data-state')).toBe('deferred')
    expect(state.textContent).toBe('deferred')
  },
}

export const Attention: Story = {
  render: list(item({ state: 'attention', label: 'Git and backup', value: 'Local history exists; no off-machine backup', href: '/settings' })),
  play: async ({ canvasElement }) => {
    const state = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.summary-state[data-state="attention"]')
      expect(found).toBeTruthy()
      return found!
    })
    // The attention read and the row's own attention data-state agree.
    expect(state.textContent).toBe('attention')
  },
}

export const Unknown: Story = {
  // Production use: an unreadable source becomes an unknown row, never a
  // guessed value.
  render: list(item({ state: 'unknown', label: 'Care timing', value: 'Could not read care timing', href: '/automations' })),
  play: async ({ canvasElement }) => {
    const state = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.summary-state[data-state="unknown"]')
      expect(found).toBeTruthy()
      return found!
    })
    expect(state.textContent).toBe('unknown')
  },
}

export const ReviewLink: Story = {
  // The internal-only href branch: a row with an in-app link gains Review.
  render: list(item({ state: 'ready', href: '/settings/workspace' })),
  play: async ({ canvasElement }) => {
    const link = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLAnchorElement>('.summary-row a')
      expect(found).toBeTruthy()
      return found!
    })
    expect(link.textContent).toBe('Review')
    expect(link.getAttribute('href')).toBe('/settings/workspace')
    // The Review control is a 44px touch target.
    expect(link.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
  },
}

export const ExternalHref: Story = {
  // The href suppression branch: a non-internal link target renders no link
  // at all (internalHref only admits same-app paths).
  render: list(item({ state: 'ready', href: 'https://example.com/storybook' })),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelector('.summary-row')).toBeTruthy())
    expect(canvasElement.querySelector('.summary-row a')).toBeNull()
  },
}
