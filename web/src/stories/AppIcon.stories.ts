import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import AppIcon from '../components/AppIcon.vue'

const meta = {
  id: 'app-icon',
  title: 'Brand and glyphs/AppIcon',
  component: AppIcon,
  args: { name: 'doc', size: 16 },
} satisfies Meta<typeof AppIcon>
export default meta
type Story = StoryObj<typeof meta>

// Each name is one `v-if` / `v-else-if` branch. The play asserts that branch's
// own shape, the decorative default (hidden, no role) and the 16px box, so
// swapping two stories' names fails both plays.
function decorative(shape: string): NonNullable<Story['play']> {
  return async ({ canvasElement }) => {
    const icon = canvasElement.querySelector('svg.app-icon')
    expect(icon?.getAttribute('aria-hidden')).toBe('true')
    expect(icon?.getAttribute('role')).toBeNull()
    expect(icon?.getAttribute('width')).toBe('16')
    expect(icon?.querySelector(shape)).not.toBeNull()
  }
}

export const Image: Story = {
  args: { name: 'image' },
  play: decorative('path[d="M3 16l5-5 4 4 3-3 6 6"]'),
}

export const Doc: Story = {
  args: { name: 'doc' },
  play: decorative('line[x1="8"][y1="16"][x2="13"]'),
}

export const File: Story = {
  args: { name: 'file' },
  play: decorative('path[d="M6 3h8l4 4v14H6z"]'),
}

export const Model: Story = {
  args: { name: 'model' },
  play: decorative('rect[x="7"][y="7"][width="10"]'),
}

export const Question: Story = {
  args: { name: 'question' },
  play: decorative('path[d="M9 9a3 3 0 1 1 3 3v2"]'),
}

export const Comment: Story = {
  args: { name: 'comment' },
  play: decorative('polyline[points="8 17 8 20 11 17"]'),
}

export const Clock: Story = {
  args: { name: 'clock' },
  play: decorative('polyline[points="12 8 12 12 15 14"]'),
}

export const Activity: Story = {
  args: { name: 'activity' },
  play: decorative('polyline[points="3 16 7 16 9 8 13 20 15 14 21 14"]'),
}

export const Shield: Story = {
  args: { name: 'shield' },
  play: decorative('path[d^="M12 3l8 3.5"]'),
}

export const Spark: Story = {
  args: { name: 'spark' },
  play: decorative('path[d^="M12 3l2.2 6.8"]'),
}

export const Check: Story = {
  args: { name: 'check' },
  play: decorative('polyline[points="4 12 10 18 20 6"]'),
}

export const Terminal: Story = {
  args: { name: 'terminal' },
  play: decorative('polyline[points="7 9 10 12 7 15"]'),
}

export const Arrow: Story = {
  args: { name: 'arrow' },
  play: decorative('polyline[points="13 6 19 12 13 18"]'),
}

export const Chevron: Story = {
  args: { name: 'chevron' },
  play: decorative('polyline[points="9 6 15 12 9 18"]'),
}

export const Skill: Story = {
  args: { name: 'skill' },
  play: decorative('path[d^="M12 3l2.6 5.9"]'),
}

export const Plug: Story = {
  args: { name: 'plug' },
  play: decorative('path[d="M6 7h12v4a6 6 0 0 1-12 0z"]'),
}

export const Folder: Story = {
  args: { name: 'folder' },
  play: decorative('path[d="M3 5h6l2 3h10v11H3z"]'),
}

export const Branch: Story = {
  args: { name: 'branch' },
  play: decorative('rect[x="16"][y="5"]'),
}

export const Code: Story = {
  args: { name: 'code' },
  play: decorative('polyline[points="16 7 21 12 16 17"]'),
}

export const Copy: Story = {
  args: { name: 'copy' },
  play: decorative('path[d="M15 9V3H3v12h6"]'),
}

export const Warning: Story = {
  args: { name: 'warning' },
  play: decorative('path[d="M12 3l10 18H2z"]'),
}

export const Task: Story = {
  args: { name: 'task' },
  play: decorative('polyline[points="8 12 11 15 16 9"]'),
}

// A label makes the icon the control's only name: it becomes an image with
// that accessible name and is no longer hidden.
export const Labelled: Story = {
  args: { name: 'image', label: 'Attached image' },
  play: async ({ canvas }) => {
    const icon = canvas.getByRole('img', { name: 'Attached image' })
    expect(icon.getAttribute('aria-hidden')).toBeNull()
    expect(icon.querySelector('path[d="M3 16l5-5 4 4 3-3 6 6"]')).not.toBeNull()
  },
}

// The size prop sets the pixel box; the artwork keeps the 24-unit viewBox.
export const Sized: Story = {
  args: { name: 'warning', size: 24 },
  play: async ({ canvasElement }) => {
    const icon = canvasElement.querySelector('svg.app-icon')
    expect(icon?.getAttribute('aria-hidden')).toBe('true')
    expect(icon?.getAttribute('width')).toBe('24')
    expect(icon?.getAttribute('height')).toBe('24')
    expect(icon?.getAttribute('viewBox')).toBe('0 0 24 24')
    expect(icon?.querySelector('path[d="M12 3l10 18H2z"]')).not.toBeNull()
  },
}
