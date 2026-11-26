import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import UpdateProgressView from '../components/UpdateProgressView.vue'

const meta = {
  id: 'update-progress-view', title: 'Settings/UpdateProgressView', component: UpdateProgressView,
  tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } },
} satisfies Meta<typeof UpdateProgressView>
export default meta
type Story = StoryObj<typeof meta>
export const Updating: Story = {
  args: { version: '2.0.0' },
  play: async ({ canvas }) => {
    const dialog = canvas.getByRole('alertdialog', { name: 'Updating Ciaobot to 2.0.0' })
    expect(document.activeElement).toBe(dialog)
    expect(canvas.getByRole('progressbar')).toHaveAttribute('aria-valuemax', '100')
    expect(canvas.getByRole('log').textContent).toContain('checking the signed release')
  },
}
export const VersionUnknown: Story = {
  play: async ({ canvas }) => { expect(canvas.getByRole('alertdialog', { name: 'Updating Ciaobot' })).toBeTruthy() },
}
