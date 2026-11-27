import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import CareGlyph from '../components/CareGlyph.vue'

// The Workspace care status shapes, each beside the word every caller shows.
const meta = {
  id: 'care-glyph',
  title: 'Memory/CareGlyph',
  component: CareGlyph,
  render: () => ({
    components: { CareGlyph },
    template: `<ul style="list-style: none; padding: 0; display: grid; gap: 8px">
      <li v-for="kind in ['done', 'blocked', 'open', 'running']" :key="kind" style="display: flex; gap: 8px; align-items: center">
        <CareGlyph :kind="kind" :size="20" /><span>{{ kind }}</span>
      </li>
    </ul>`,
  }),
} satisfies Meta<typeof CareGlyph>
export default meta
type Story = StoryObj<typeof meta>

export const AllShapes: Story = {
  args: { kind: 'done' },
  play: async ({ canvasElement }) => {
    for (const kind of ['done', 'blocked', 'open', 'running']) {
      const glyph = canvasElement.querySelector(`.care-glyph--${kind}`)
      expect(glyph).not.toBeNull()
      expect(glyph?.getAttribute('aria-hidden')).toBe('true')
    }
  },
}
