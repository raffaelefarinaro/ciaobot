import { defineComponent, h, ref } from 'vue'
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import OnboardingStack from '../components/onboarding/OnboardingStack.vue'

/**
 * The vertical rhythm primitive of the setup screens (the
 * `components-onboarding-onboarding-stack` inventory row, #3308). Props only:
 * the state list is the `gap` and `as` prop variants. Each story renders the
 * production component with real spacing children.
 */
const meta = {
  id: 'onboarding-stack',
  title: 'Onboarding primitives/OnboardingStack',
  component: OnboardingStack,
} satisfies Meta<typeof OnboardingStack>
export default meta
type Story = StoryObj<typeof meta>

/** Three synthetic content blocks the stack spaces apart. */
function blocks() {
  return [
    h('h2', 'Story heading'),
    h('p', 'Synthetic spacing block for the storybook.'),
    h('p', 'A second synthetic block below it.'),
  ]
}

export const Default: Story = {
  // The default gap is `md`.
  render: () => h(OnboardingStack, null, () => blocks()),
  play: async ({ canvasElement }) => {
    const stack = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-stack')
      expect(found).toBeTruthy()
      return found!
    })
    expect(stack.getAttribute('data-gap')).toBe('md')
    expect(stack.tagName).toBe('DIV')
  },
}

export const Small: Story = {
  render: () => h(OnboardingStack, { gap: 'sm' }, () => blocks()),
  play: async ({ canvasElement }) => {
    const stack = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-stack')
      expect(found).toBeTruthy()
      return found!
    })
    expect(stack.getAttribute('data-gap')).toBe('sm')
  },
}

export const Large: Story = {
  render: () => h(OnboardingStack, { gap: 'xl' }, () => blocks()),
  play: async ({ canvasElement }) => {
    const stack = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-stack')
      expect(found).toBeTruthy()
      return found!
    })
    expect(stack.getAttribute('data-gap')).toBe('xl')
  },
}

export const DoubleLarge: Story = {
  // Production use: OnboardingView separates the Access and Memory stages by
  // 2xl. The play also proves the spacing actually scales with the prop
  // (relative, not an absolute pixel constant).
  render: () => h(OnboardingStack, { gap: '2xl' }, () => [
    h('p', 'First synthetic stage'),
    h(OnboardingStack, {}, () => [h('p', 'Nested default gap')]),
    h('p', 'Second synthetic stage'),
  ]),
  play: async ({ canvasElement }) => {
    const stack = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-stack[data-gap="2xl"]')
      expect(found).toBeTruthy()
      return found!
    })
    const nested = stack.querySelector<HTMLElement>('.ob-stack:not([data-gap="2xl"])')!
    expect(nested.getAttribute('data-gap')).toBe('md')
    // Row gap scales with the gap prop: 2xl leaves more room than md.
    const outerGap = parseFloat(getComputedStyle(stack).rowGap)
    const innerGap = parseFloat(getComputedStyle(nested).rowGap)
    expect(outerGap).toBeGreaterThan(innerGap)
  },
}

export const Section: Story = {
  // Production use: the stages and the summary render a section element.
  render: () => h(OnboardingStack, { as: 'section', 'aria-label': 'Setup summary' }, () => blocks()),
  play: async ({ canvasElement }) => {
    const stack = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-stack')
      expect(found).toBeTruthy()
      return found!
    })
    expect(stack.tagName).toBe('SECTION')
    expect(stack.getAttribute('aria-label')).toBe('Setup summary')
  },
}

export const Form: Story = {
  // Production use: OnboardingLocationStage renders its stage as a form with
  // submit.prevent, so submitting stays in place instead of navigating.
  render: () => h(host()),
  play: async ({ canvasElement }) => {
    const stack = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-stack')
      expect(found).toBeTruthy()
      return found!
    })
    expect(stack.tagName).toBe('FORM')
    expect(stack.getAttribute('data-gap')).toBe('xl')
    // The rendered form carries the submit contract: clicking submit fires
    // the form's prevented submit on the real element.
    await userEvent.click(stack.querySelector<HTMLButtonElement>('button[type="submit"]')!)
    await waitFor(() => expect(stack.getAttribute('data-submitted')).toBe('true'))
  },
}

/** A form stack with an input, the prevented submit recorded on the element. */
function host() {
  return defineComponent({
    name: 'OnboardingStackFormHost',
    setup() {
      const submitted = ref(false)
      return () => h(OnboardingStack, {
        as: 'form',
        gap: 'xl',
        // Vue's h() takes the handler by key, so the .prevent modifier is applied here.
        onSubmit: (event: Event) => { event.preventDefault(); submitted.value = true },
        'data-submitted': submitted.value ? 'true' : 'false',
      }, () => [...blocks(), h('button', { type: 'submit' }, 'Save')])
    },
  })
}
