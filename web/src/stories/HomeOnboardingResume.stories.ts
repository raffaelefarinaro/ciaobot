import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import HomeOnboardingResume from '../components/HomeOnboardingResume.vue'
import type { OnboardingReceipt } from '../lib/onboarding'
import type { FixtureRoute } from './fixtureBoundary'
import { assertFenceQuiet, homeWorkspace, json, renderHome } from './homeWindowsFixture'

type ReceiptFixture = 'hidden' | 'failed' | 'installation' | 'workspace' | 'focus-refresh'

function receipt(fixture: ReceiptFixture): OnboardingReceipt {
  const done = { status: 'completed', step: 'finish' } as const
  if (fixture === 'installation') {
    return { schema: 1, revision: 1, installation: { status: 'in_progress', step: 'runtime' }, workspaces: {} }
  }
  if (fixture === 'workspace') {
    return {
      schema: 1,
      revision: 1,
      installation: done,
      workspaces: { [homeWorkspace]: { status: 'in_progress', step: 'access_memory' } },
    }
  }
  return { schema: 1, revision: 1, installation: done, workspaces: {} }
}

let focusCalls = 0

function onboardingRoutes(fixture: ReceiptFixture): FixtureRoute[] {
  return [{
    method: 'GET',
    path: '/api/onboarding',
    respond: () => {
      if (fixture === 'failed') return json({ error: 'Synthetic receipt unavailable' }, 503)
      if (fixture === 'focus-refresh') {
        focusCalls++
        return json(receipt(focusCalls === 1 ? 'hidden' : 'workspace'))
      }
      return json(receipt(fixture))
    },
  }]
}

const meta = {
  id: 'home-onboarding-resume',
  title: 'Home windows/HomeOnboardingResume',
  component: HomeOnboardingResume,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof HomeOnboardingResume>
export default meta
type Story = StoryObj<typeof meta>

const label = 'Synthetic onboarding receipt fixture.'

function beforeMount() {
  return () => { focusCalls = 0 }
}

function render(narrow = false) {
  return renderHome(HomeOnboardingResume, {
    label,
    narrow,
    beforeMount,
  })
}

export const Hidden: Story = {
  parameters: { fixture: { routes: onboardingRoutes('hidden') } },
  render: () => render(),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(canvas.queryByRole('region', { name: 'Setup not finished' })).toBeNull()
  },
}

export const InitialFailure: Story = {
  parameters: { fixture: { routes: onboardingRoutes('failed') } },
  render: () => render(),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(canvas.queryByRole('region', { name: 'Setup not finished' })).toBeNull()
  },
}

export const InstallationPending: Story = {
  parameters: { fixture: { routes: onboardingRoutes('installation') } },
  render: () => render(),
  play: async ({ canvas, userEvent }) => {
    const card = await waitFor(() => {
      const found = canvas.queryByRole('region', { name: 'Setup not finished' })
      expect(found).toBeTruthy()
      return found!
    })
    await assertFenceQuiet(canvas)
    expect(card.textContent).toContain('Finish setting up this installation.')
    await userEvent.click(canvas.getByRole('button', { name: 'Resume setup' }))
    await waitFor(() => expect(canvas.getByText(/Workshop destination: \/onboarding$/)).toBeTruthy())
  },
}

export const WorkspacePending: Story = {
  parameters: { fixture: { routes: onboardingRoutes('workspace') } },
  render: () => render(),
  play: async ({ canvas, userEvent }) => {
    const card = await waitFor(() => {
      const found = canvas.queryByRole('region', { name: 'Setup not finished' })
      expect(found).toBeTruthy()
      return found!
    })
    await assertFenceQuiet(canvas)
    expect(card.textContent).toContain(`Finish setting up the workspace “${homeWorkspace}”.`)
    await userEvent.click(canvas.getByRole('button', { name: 'Resume setup' }))
    await waitFor(() => expect(canvas.getByText(/Workshop destination: \/onboarding\?workspace=storybook-alpha/)).toBeTruthy())
  },
}

export const FocusRefresh: Story = {
  parameters: { fixture: { routes: onboardingRoutes('focus-refresh') } },
  render: () => render(),
  play: async ({ canvas }) => {
    await assertFenceQuiet(canvas)
    expect(canvas.queryByRole('region', { name: 'Setup not finished' })).toBeNull()
    window.dispatchEvent(new Event('focus'))
    await waitFor(() => expect(canvas.getByRole('region', { name: 'Setup not finished' }).textContent)
      .toContain(`Finish setting up the workspace “${homeWorkspace}”.`))
    await waitFor(() => expect(canvas.getByRole('status', { name: 'Fixture ledger' }).textContent)
      .toMatch(/Fixture requests: [2-9]/))
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: onboardingRoutes('workspace') } },
  render: () => render(true),
  play: async ({ canvas }) => {
    const card = await waitFor(() => {
      const found = canvas.queryByRole('region', { name: 'Setup not finished' })
      expect(found).toBeTruthy()
      return found!
    })
    await assertFenceQuiet(canvas)
    expect(card.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    expect(canvas.getByRole('button', { name: 'Resume setup' }).getBoundingClientRect().height)
      .toBeGreaterThanOrEqual(44)
  },
}
