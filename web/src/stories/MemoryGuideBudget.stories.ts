import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import MemoryGuideBudget from '../components/MemoryGuideBudget.vue'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProjectStore } from '../stores/projects'
import {
  defaultRegionSpecs,
  flaggedRegionSpecs,
  guideRoutes,
  guideSecondWorkspace,
  guideWorkspace,
  highUsageRegionSpecs,
  mountGuideFixture,
  overCapRegionSpecs,
  regionExpectation,
  viewerRoutes,
  type GuideStoryState,
} from './memoryGuidanceFixture'

/**
 * The Memory review sections' always-loaded rail over the fail-closed fixture
 * boundary. Every story mounts the production component inside the real rail
 * host and declares exactly the guide reads it makes; the Open story
 * additionally declares what the file viewer store reads, and the Refresh
 * story holds the switched workspace's read.
 */

function mountBudget(host: HTMLElement): ReturnType<typeof mountGuideFixture> {
  return mountGuideFixture(host, MemoryGuideBudget)
}

function renderBudget(state: GuideStoryState) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountBudget>>()
      const error = ref('')
      onMounted(() => {
        try { fixture.value = mountBudget(host.value!) }
        catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => {
        const viewer = fixture.value ? useFileViewerStore(fixture.value.pinia) : undefined
        return h('div', {}, [
          h('p', `Synthetic ${state} fixture: production MemoryGuideBudget with the storybook guide.`),
          h('div', { ref: host }),
          h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
          h('p', { role: 'status' }, `Viewer: ${viewer?.isOpen ? viewer.path : 'closed'}.`),
          state === 'refresh' ? h('label', [
            'Fixture workspace ',
            h('select', {
              'aria-label': 'Fixture workspace',
              onChange: (event: Event) => {
                if (fixture.value) useProjectStore(fixture.value.pinia).activeWorkspace = (event.target as HTMLSelectElement).value
              },
            }, [guideWorkspace, guideSecondWorkspace].map((value) => h('option', { value }, value))),
          ]) : null,
          error.value ? h('p', { role: 'alert' }, error.value) : null,
          ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
        ])
      }
    },
  })
}

const viewportWide = { viewport: { defaultViewport: 'wide' } }

const meta = {
  id: 'memory-guide-budget',
  title: 'Memory guidance/MemoryGuideBudget',
  component: MemoryGuideBudget,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof MemoryGuideBudget>
export default meta
type Story = StoryObj<typeof meta>

// Under the browser runner the fixture fence sees every transport the owner
// makes; these plays let late reporting arrive and then assert the ledger is
// empty and the story's own state is on screen.
async function assertSettled(canvas: Parameters<NonNullable<Story['play']>>[0]['canvas']) {
  await waitFor(() => expect(canvas.getByText(/Fixture requests: [1-9]/)).toBeTruthy())
  await new Promise((resolve) => setTimeout(resolve, 500))
  expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
  expect(canvas.queryByText(/Story network violation/)).toBeNull()
}

/** The per-region elements, in the owner's fixed memory-then-profile order. */
function regionBlocks(canvasElement: HTMLElement) {
  const regions = Array.from(canvasElement.querySelectorAll<HTMLElement>('.guide-region'))
  const meter = (region: HTMLElement) => region.querySelector<HTMLElement>('[role="meter"]')!
  return {
    memory: { block: regions[0]!, meter: meter(regions[0]!) },
    profile: { block: regions[1]!, meter: meter(regions[1]!) },
  }
}

// The loaded rail: both regions under cap, both actions ready, the path label
// naming the file that resolved, and the real reads the actions make.
export const Default: Story = {
  parameters: { fixture: { routes: [...guideRoutes('default'), ...viewerRoutes(`${guideWorkspace}/AGENTS.md`)] }, ...viewportWide },
  render: () => renderBudget('default'),
  play: async ({ canvas, canvasElement, userEvent }) => {
    const memory = regionExpectation(defaultRegionSpecs().memory, 3000)
    const profile = regionExpectation(defaultRegionSpecs().profile, 1375)
    await waitFor(() => expect(canvas.getByText(memory.count)).toBeTruthy())
    expect(canvas.getByText('Agent memory')).toBeTruthy()
    expect(canvas.getByText('User profile')).toBeTruthy()
    expect(canvas.getByText('storybook-alpha/AGENTS.md')).toBeTruthy()
    const blocks = regionBlocks(canvasElement)
    expect(blocks.memory.meter.getAttribute('aria-label')).toBe(`Agent memory: ${memory.label}`)
    expect(blocks.memory.meter.getAttribute('aria-valuenow')).toBe(String(memory.used))
    expect(blocks.memory.meter.getAttribute('aria-valuemax')).toBe(String(memory.cap))
    expect(blocks.profile.meter.getAttribute('aria-label')).toBe(`User profile: ${profile.label}`)
    // Both actions are ready; Open opens the guide through the viewer store.
    const open = canvas.getByRole('button', { name: 'Open' })
    const discuss = canvas.getByRole('button', { name: 'Discuss' })
    expect(open.getAttribute('disabled')).toBeNull()
    expect(discuss.getAttribute('disabled')).toBeNull()
    for (let step = 0; step < 6 && document.activeElement !== open; step++) await userEvent.tab()
    expect(document.activeElement).toBe(open)
    expect(getComputedStyle(open).outlineWidth).toBe('2px')
    await userEvent.click(open)
    await waitFor(() => expect(canvas.getByText(/Fixture requests: 3\./)).toBeTruthy())
    expect(canvas.getByText('Viewer: storybook-alpha/AGENTS.md.')).toBeTruthy()
    expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
    // Discuss with no General project fails closed in the toast stack, not on the wire.
    await userEvent.click(discuss)
    await waitFor(() => expect(canvas.getByText('Cannot start chat')).toBeTruthy())
    expect(canvas.getByText('No General project found in the storybook-alpha workspace.')).toBeTruthy()
    expect(canvas.getByText(/Fixture requests: 3\./)).toBeTruthy()
    await assertSettled(canvas)
  },
}

// A held initial read: the loading flag in the flag slot, no region list yet,
// and both actions blocked.
export const Loading: Story = {
  parameters: { fixture: { routes: guideRoutes('loading') }, ...viewportWide },
  render: () => renderBudget('loading'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('loading…')).toBeTruthy())
    expect(canvas.getByText('AGENTS.md')).toBeTruthy()
    expect(canvas.queryByText('Agent memory')).toBeNull()
    expect(canvas.queryByRole('button', { name: 'Open' })?.getAttribute('disabled')).not.toBeNull()
    expect(canvas.queryByRole('button', { name: 'Discuss' })?.getAttribute('disabled')).not.toBeNull()
    // Settle first: a read that wrongly answered would have drawn the regions by now.
    await assertSettled(canvas)
    expect(canvas.getByText('loading…')).toBeTruthy()
    expect(canvas.queryByText('Agent memory')).toBeNull()
  },
}

// A region over its advisory cap: the flag in the slot, the warn count and
// bar, and the section's over-cap class, while the other region stays quiet.
export const OverCap: Story = {
  parameters: {
    fixture: { routes: guideRoutes('over-cap') },
    ...viewportWide,
  },
  render: () => renderBudget('over-cap'),
  play: async ({ canvas, canvasElement }) => {
    const memory = regionExpectation(overCapRegionSpecs().memory, 3000)
    await waitFor(() => expect(canvas.getByText(memory.count)).toBeTruthy())
    expect(canvas.getByText('over cap')).toBeTruthy()
    const section = canvasElement.querySelector<HTMLElement>('.guide-budget')!
    expect(section.className).toContain('guide-budget--over')
    const blocks = regionBlocks(canvasElement)
    expect(blocks.memory.block.querySelector<HTMLElement>('.guide-region-count')!.className).toContain('guide-region-count--warn')
    const memoryBar = blocks.memory.meter
    expect(memoryBar.className).toContain('guide-region-bar--warn')
    expect(memoryBar.className).not.toContain('guide-region-bar--high')
    expect(memoryBar.getAttribute('aria-label')).toBe(`Agent memory: ${memory.label}`)
    expect(memoryBar.getAttribute('aria-valuenow')).toBe(String(memory.used))
    expect(memoryBar.getAttribute('aria-valuemax')).toBe(String(memory.cap))
    const barFill = memoryBar.querySelector<HTMLElement>('span')!
    expect(barFill.style.width).toBe('100%')
    // The under-cap region carries no warn classes and the actions still work.
    expect(blocks.profile.meter.className).not.toContain('guide-region-bar--warn')
    expect(blocks.profile.meter.className).not.toContain('guide-region-bar--high')
    expect(blocks.profile.meter.className).toContain('guide-region-bar')
    expect(canvas.getByRole('button', { name: 'Open' }).getAttribute('disabled')).toBeNull()
    expect(canvas.getByRole('button', { name: 'Discuss' }).getAttribute('disabled')).toBeNull()
    await assertSettled(canvas)
  },
}

// A region at the warn threshold (80% and up) without crossing its cap: the
// high bar tint, no over-cap flag and no warn count.
export const HighUsage: Story = {
  parameters: { fixture: { routes: guideRoutes('high-usage') }, ...viewportWide },
  render: () => renderBudget('high-usage'),
  play: async ({ canvas, canvasElement }) => {
    const memory = regionExpectation(highUsageRegionSpecs().memory, 3000)
    await waitFor(() => expect(canvas.getByText(memory.count)).toBeTruthy())
    expect(canvas.queryByText('over cap')).toBeNull()
    const section = canvasElement.querySelector<HTMLElement>('.guide-budget')!
    expect(section.className).not.toContain('guide-budget--over')
    const blocks = regionBlocks(canvasElement)
    const memoryBar = blocks.memory.meter
    expect(memoryBar.className).toContain('guide-region-bar--high')
    expect(memoryBar.className).not.toContain('guide-region-bar--warn')
    expect(blocks.memory.block.querySelector<HTMLElement>('.guide-region-count')!.className)
      .not.toContain('guide-region-count--warn')
    expect(memoryBar.getAttribute('aria-label')).toBe(`Agent memory: ${memory.label}`)
    expect(canvas.getByRole('button', { name: 'Open' }).getAttribute('disabled')).toBeNull()
    await assertSettled(canvas)
  },
}

// Expiry and malformed tags: the region meta line gains its counts, and the
// malformed one carries the warn span class.
export const ExpiredAndMalformed: Story = {
  parameters: { fixture: { routes: guideRoutes('expired-and-malformed') }, ...viewportWide },
  render: () => renderBudget('expired-and-malformed'),
  play: async ({ canvas, canvasElement }) => {
    const memory = regionExpectation(flaggedRegionSpecs().memory, 3000)
    const profile = regionExpectation(flaggedRegionSpecs().profile, 1375)
    await waitFor(() => expect(canvas.getByText(memory.count)).toBeTruthy())
    expect(canvas.getByText(profile.count)).toBeTruthy()
    const memoryMeta = canvasElement.querySelectorAll<HTMLElement>('.guide-region')[0]!.querySelector<HTMLElement>('.guide-region-meta')!
    expect(memoryMeta.textContent).toContain('1 expired')
    const profileMeta = canvasElement.querySelectorAll<HTMLElement>('.guide-region')[1]!.querySelector<HTMLElement>('.guide-region-meta')!
    expect(profileMeta.textContent).toContain('1 malformed tag')
    expect(profileMeta.querySelector<HTMLElement>('.guide-region-meta--warn')).toBeTruthy()
    expect(memoryMeta.querySelector<HTMLElement>('.guide-region-meta--warn')).toBeNull()
    await assertSettled(canvas)
  },
}

// A pre-migration install: AGENTS.md misses and the legacy CLAUDE.md guides
// the rail, its label and its actions.
export const LegacyClaudeGuide: Story = {
  parameters: { fixture: { routes: guideRoutes('legacy-claude-guide') }, ...viewportWide },
  render: () => renderBudget('legacy-claude-guide'),
  play: async ({ canvas }) => {
    const memory = regionExpectation(defaultRegionSpecs().memory, 3000)
    await waitFor(() => expect(canvas.getByText(memory.count)).toBeTruthy())
    expect(canvas.getByText('storybook-alpha/CLAUDE.md')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Open' }).getAttribute('disabled')).toBeNull()
    expect(canvas.getByRole('button', { name: 'Discuss' }).getAttribute('disabled')).toBeNull()
    expect(canvas.getByText(/Fixture requests: 2\./)).toBeTruthy()
    await assertSettled(canvas)
  },
}

// A workspace with no guide yet: silent (no failed read), the label
// placeholder stays, and both actions are blocked.
export const NoGuide: Story = {
  parameters: { fixture: { routes: guideRoutes('no-guide') }, ...viewportWide },
  render: () => renderBudget('no-guide'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('No guide file found for this workspace.')).toBeTruthy())
    expect(canvas.queryByText('Agent memory')).toBeNull()
    expect(canvas.getByText('AGENTS.md')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Open' }).getAttribute('disabled')).not.toBeNull()
    expect(canvas.getByRole('button', { name: 'Discuss' }).getAttribute('disabled')).not.toBeNull()
    expect(canvas.getByText(/Fixture requests: 2\./)).toBeTruthy()
    await assertSettled(canvas)
    expect(canvas.queryByText(/HTTP/)).toBeNull()
  },
}

// Every qualified probe errored: the failure note names the last failed read,
// both actions are blocked.
export const Error: Story = {
  parameters: { fixture: { routes: guideRoutes('error') }, ...viewportWide },
  render: () => renderBudget('error'),
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Failed to load storybook-alpha/CLAUDE.md (HTTP 503)')).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'Open' }).getAttribute('disabled')).not.toBeNull()
    expect(canvas.getByRole('button', { name: 'Discuss' }).getAttribute('disabled')).not.toBeNull()
    expect(canvas.getByText(/Fixture requests: 2\./)).toBeTruthy()
    await assertSettled(canvas)
    expect(canvas.getByText('Failed to load storybook-alpha/CLAUDE.md (HTTP 503)')).toBeTruthy()
  },
}

// A refresh over data already on screen: the workspace watcher re-reads and
// the rail keeps its regions with the loading flag in the slot while the
// switched workspace's read is held.
export const Refresh: Story = {
  parameters: { fixture: { routes: guideRoutes('refresh') }, ...viewportWide },
  render: () => renderBudget('refresh'),
  play: async ({ canvas, userEvent }) => {
    const memory = regionExpectation(defaultRegionSpecs().memory, 3000)
    await waitFor(() => expect(canvas.getByText(memory.count)).toBeTruthy())
    const open = canvas.getByRole('button', { name: 'Open' })
    expect(open.getAttribute('disabled')).toBeNull()
    await userEvent.selectOptions(canvas.getByRole('combobox', { name: 'Fixture workspace' }), guideSecondWorkspace)
    await waitFor(() => expect(canvas.getByText('loading…')).toBeTruthy())
    expect(canvas.getByText(memory.count)).toBeTruthy()
    expect(open.getAttribute('disabled')).not.toBeNull()
    await waitFor(() => expect(canvas.getByText(/Fixture requests: 2\./)).toBeTruthy())
    await assertSettled(canvas)
    expect(canvas.getByText('loading…')).toBeTruthy()
    expect(canvas.getByText(memory.count)).toBeTruthy()
  },
}
