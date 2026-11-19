import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted } from 'vue'
import HomeNoticeWindow from '../components/HomeNoticeWindow.vue'
import { resetHomeNoticeWindows } from '../composables/useHomeNoticeWindows'
import { narrowStyle } from './homeWindowsFixture'

type WindowArgs = {
  title: string
  noticeKey: string
  blocking?: boolean
  closeLabel?: string
}

function renderWindow(args: WindowArgs, narrow = false) {
  return defineComponent({
    setup() {
      onMounted(() => resetHomeNoticeWindows())
      onBeforeUnmount(() => resetHomeNoticeWindows())
      return () => h('div', narrow ? { style: narrowStyle } : {}, [
        h(HomeNoticeWindow, args, {
          default: () => h('p', { 'data-testid': 'slot-content' }, 'Synthetic slot content for the workshop.'),
        }),
        // The app's explicit recovery control, as a harness target for focus.
        h('button', { type: 'button', 'data-home-notices-reopen': '' }, 'Recover notices'),
      ])
    },
  })
}

const meta = {
  id: 'home-notice-window',
  title: 'Home windows/HomeNoticeWindow',
  component: HomeNoticeWindow,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof HomeNoticeWindow>
export default meta
type Story = StoryObj<typeof meta>

type PlayContext = Parameters<NonNullable<Story['play']>>[0]

async function closeAndFocus({ canvas, userEvent }: PlayContext, closeLabel: string) {
  const close = canvas.getByRole('button', { name: closeLabel })
  await waitFor(() => expect(close.getAttribute('aria-label')).toBe(closeLabel))
  await userEvent.click(close)
  const recovery = canvas.getByRole('button', { name: 'Recover notices' })
  await waitFor(() => expect(document.activeElement).toBe(recovery))
}

export const Default: Story = {
  args: {
    title: 'Synthetic notice',
    noticeKey: 'storybook:notice',
  },
  render: args => renderWindow(args),
  play: async (context) => {
    const { canvas } = context
    expect(canvas.getByRole('heading', { name: 'Synthetic notice' })).toBeTruthy()
    expect(canvas.getByTestId('slot-content').textContent).toContain('Synthetic slot content')
    await closeAndFocus(context, 'Close Synthetic notice window')
  },
}

export const Blocking: Story = {
  args: {
    title: 'Synthetic blocking notice',
    noticeKey: 'storybook:blocking',
    blocking: true,
  },
  render: args => renderWindow(args),
  play: async (context) => {
    const { canvas } = context
    const article = canvas.getByRole('article')
    expect(article.classList.contains('home-notice-window--blocking')).toBe(true)
    expect(canvas.getByRole('heading', { name: 'Synthetic blocking notice' })).toBeTruthy()
    await closeAndFocus(context, 'Close Synthetic blocking notice window')
  },
}

export const CustomCloseLabel: Story = {
  args: {
    title: 'Synthetic notice',
    noticeKey: 'storybook:custom-label',
    closeLabel: 'Dismiss this card on every device',
  },
  render: args => renderWindow(args),
  play: async (context) => {
    const { canvas } = context
    expect(canvas.getByRole('button', { name: 'Dismiss this card on every device' }).getAttribute('title'))
      .toBe('Dismiss this card on every device')
    await closeAndFocus(context, 'Dismiss this card on every device')
  },
}

export const CloseKeyboardFocus: Story = {
  args: {
    title: 'Synthetic keyboard notice',
    noticeKey: 'storybook:keyboard',
  },
  render: args => renderWindow(args),
  play: async ({ canvas, userEvent }) => {
    const close = canvas.getByRole('button', { name: 'Close Synthetic keyboard notice window' })
    for (let step = 0; step < 6 && document.activeElement !== close; step++) await userEvent.tab()
    expect(document.activeElement).toBe(close)
    expect(getComputedStyle(close).outlineWidth).toBe('2px')
    await userEvent.keyboard('{Enter}')
    const recovery = canvas.getByRole('button', { name: 'Recover notices' })
    await waitFor(() => expect(document.activeElement).toBe(recovery))
    expect(getComputedStyle(recovery).outlineWidth).toBe('2px')
  },
}

export const Narrow: Story = {
  args: {
    title: 'Synthetic notice in a narrow rail',
    noticeKey: 'storybook:narrow',
    blocking: true,
  },
  render: args => renderWindow(args, true),
  play: async (context) => {
    const { canvas } = context
    expect(canvas.getByRole('article').getBoundingClientRect().width).toBeLessThanOrEqual(390)
    await closeAndFocus(context, 'Close Synthetic notice in a narrow rail window')
  },
}
