import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor, within } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import TerminalPane from '../components/TerminalPane.vue'
import { useTerminalStore, type TerminalTarget } from '../stores/terminal'
import type { FixtureConfig, FixtureRoute, FixtureSocketScript } from './fixtureBoundary'
import { mountConnected, type ConnectedFixture } from './mountConnected'
import {
  TERMINAL_CHAT,
  TERMINAL_WORKSPACE,
  exitedShell,
  hold,
  json,
  liveShell,
  session,
  sessionsRoute,
  shellEndingOnce,
  silentShell,
  socketPath,
  statusOnceThenHold,
  statusRoute,
} from './terminalFixtures'

type PaneStory = {
  label: string
  routes: FixtureRoute[]
  sockets?: Array<{ path: string, script: FixtureSocketScript }>
  inTile?: boolean
  chatId?: string
  target?: TerminalTarget
  ensureSession?: boolean
  width?: string
  play?: NonNullable<StoryObj<typeof meta>['play']>
}

// The pane takes its props from its host: mountConnected mounts the owner as
// the route component with no props, so the host passes the layout's props.
function paneHost(story: PaneStory) {
  return defineComponent({
    render: () => h(TerminalPane, { inTile: story.inTile ?? false, chatId: story.chatId ?? '' }),
  })
}

function renderPane(story: PaneStory) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      const error = ref('')
      onMounted(() => {
        try {
          const mounted = mountConnected(host.value!, { component: paneHost(story) })
          fixture.value = mounted
          // The open is the story's own step, the same one the chat's button
          // takes; the pane's reactions to it are the production code.
          if (story.target) void useTerminalStore(mounted.pinia).open(story.target, { ensureSession: story.ensureSession === true })
        } catch (cause) {
          error.value = String(cause)
        }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', { style: `max-width: ${story.width ?? '100%'}; min-width: 0` }, [
        h('p', `Synthetic ${story.label} fixture. Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        h('div', { ref: host }),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map(message => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

const meta = {
  id: 'terminal-pane',
  title: 'Terminal/TerminalPane',
  component: TerminalPane,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof TerminalPane>
export default meta
type Story = StoryObj<typeof meta>

function storyFor(story: PaneStory): Story {
  const fixture: FixtureConfig = { routes: story.routes, sockets: story.sockets ?? [] }
  return {
    parameters: { fixture },
    render: () => renderPane(story),
    play: story.play,
  }
}

const CHAT_QUERY = `chat_id=${TERMINAL_CHAT}`
const CHAT_SESSION = 'storybook-alpha-1'
const CHAT_FOLDER = 'storybook-alpha'
const CHAT_TARGET = { chatId: TERMINAL_CHAT }
const liveRoutes = (): FixtureRoute[] => [
  statusRoute(),
  sessionsRoute(CHAT_QUERY, [session(CHAT_SESSION, CHAT_FOLDER)]),
]
const liveSockets = () => [{ path: socketPath(CHAT_SESSION), script: liveShell(CHAT_SESSION, CHAT_FOLDER) }]

// Shared play steps. Each waits for what the production pane shows, then
// asserts the distinguishing DOM for that state.
type PlayContext = Parameters<NonNullable<StoryObj<typeof meta>["play"]>>[0]

const playLive = async ({ canvasElement, canvas }: PlayContext) => {
  await waitFor(() => expect(canvas.getByRole('tab', { name: /storybook-alpha/ })).toHaveAttribute('aria-selected', 'true'))
  await waitFor(() => expect(canvasElement.querySelector('.tp-surface .xterm')).not.toBeNull())
  expect(canvasElement.querySelector('.tp-phase')).toBeNull()
}

const drawerOrTile = (cls: string) => async (context: PlayContext) => {
  const { canvasElement } = context
  await playLive(context)
  expect(canvasElement.querySelector(`.terminal-pane--${cls}`)).not.toBeNull()
}

// Default: the live shell in the drawer shape. The keyboard belongs to the
// shell, so Shift+Tab is the one key that returns it to the pane's controls.
const defaultStory: PaneStory = {
  label: 'live shell',
  routes: liveRoutes(),
  sockets: liveSockets(),
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: async (context) => {
    const { canvasElement, canvas, userEvent } = context
    await playLive(context)
    const screen = canvasElement.querySelector<HTMLElement>('.xterm-helper-textarea')
    expect(screen).not.toBeNull()
    screen!.focus()
    expect(document.activeElement).toBe(screen)
    await userEvent.keyboard('{Shift>}{Tab}{/Shift}')
    expect(document.activeElement).toBe(canvas.getByRole('button', { name: 'Close' }))
  },
}
export const Default: Story = storyFor(defaultStory)

export const Tile: Story = storyFor({
  label: 'live shell in the tile slot',
  routes: liveRoutes(),
  sockets: liveSockets(),
  inTile: true,
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: (context) => drawerOrTile('tile')(context),
})

export const Drawer: Story = storyFor({
  label: 'live shell at phone width',
  routes: liveRoutes(),
  sockets: liveSockets(),
  width: '390px',
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: (context) => drawerOrTile('drawer')(context),
})

// The chooser: a chat with a linked repository offers two folders and no
// shell yet, so the pane asks. Picking one starts the shell in that folder.
export const Chooser: Story = storyFor({
  label: 'folder chooser',
  routes: [
    statusRoute(),
    sessionsRoute(CHAT_QUERY, [], [
      { location: 'workspace', folder: 'storybook-alpha' },
      { location: 'repo', folder: 'storybook-beta' },
    ]),
    {
      method: 'POST',
      path: '/api/terminal/sessions',
      respond: () => json({ session: session('storybook-beta-1', 'storybook-beta') }),
    },
  ],
  sockets: [{ path: socketPath('storybook-beta-1'), script: liveShell('storybook-beta-1', 'storybook-beta') }],
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: async ({ canvas, userEvent }) => {
    const group = await canvas.findByRole('group', { name: 'Where to open the shell' })
    expect(within(group).getAllByRole('button')).toHaveLength(2)
    await userEvent.click(within(group).getByRole('button', { name: /Repository/ }))
    await waitFor(() => expect(canvas.queryByRole('group', { name: 'Where to open the shell' })).toBeNull())
    await waitFor(() => expect(canvas.getByRole('tab', { name: /storybook-beta/ })).toHaveAttribute('aria-selected', 'true'))
  },
})

export const Connecting: Story = storyFor({
  label: 'connecting',
  routes: liveRoutes(),
  sockets: [{ path: socketPath(CHAT_SESSION), script: silentShell() }],
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Connecting to the terminal…')).toBeTruthy())
  },
})

// A dropped connection is retried after the status read. The read is held, so
// the pane stays in its reconnecting phase; the second status read is the
// retry's own, and the count proves it was made.
export const Reconnecting: Story = storyFor({
  label: 'reconnecting',
  routes: [
    statusOnceThenHold(),
    sessionsRoute(CHAT_QUERY, [session(CHAT_SESSION, CHAT_FOLDER)]),
  ],
  sockets: [{ path: socketPath(CHAT_SESSION), script: shellEndingOnce(CHAT_SESSION, CHAT_FOLDER, 1006) }],
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/Fixture requests: 3\./)).toBeTruthy())
    expect(canvas.getByText('Connecting to the terminal…')).toBeTruthy()
  },
})

// Another connection takes the session. The pane offers a takeover, and the
// takeover reconnects the same shell.
export const Takeover: Story = storyFor({
  label: 'takeover',
  routes: liveRoutes(),
  sockets: [{ path: socketPath(CHAT_SESSION), script: shellEndingOnce(CHAT_SESSION, CHAT_FOLDER, 4409) }],
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: async ({ canvasElement, canvas, userEvent }) => {
    const notice = 'Another connection took over this terminal session.'
    await waitFor(() => expect(canvas.getByText(notice, { selector: '.tp-takeover-text' })).toBeTruthy())
    await userEvent.click(canvas.getByRole('button', { name: 'Take over' }))
    // The takeover prompt goes and the shell is live again. The takeover notice
    // is not asserted here: the store keeps it as an error after the reconnect.
    await waitFor(() => expect(canvasElement.querySelector('.tp-takeover')).toBeNull())
    await waitFor(() => expect(canvasElement.querySelector('.tp-surface .xterm')).not.toBeNull())
    await waitFor(() => expect(canvas.getByRole('tab', { name: /storybook-alpha/ })).toHaveAttribute('aria-selected', 'true'))
  },
})

export const Ended: Story = storyFor({
  label: 'ended session',
  routes: liveRoutes(),
  sockets: [{ path: socketPath(CHAT_SESSION), script: exitedShell(CHAT_SESSION, CHAT_FOLDER, 0) }],
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/This session has ended/)).toBeTruthy())
    expect(canvas.getByRole('tab', { name: /ended \(0\)/ })).toBeTruthy()
  },
})

// The first shell is being created: the create request is held, so the pane
// shows the start and the new-session control is blocked while it is pending.
export const Creating: Story = storyFor({
  label: 'starting a shell',
  routes: [
    statusRoute(),
    sessionsRoute(CHAT_QUERY, []),
    { method: 'POST', path: '/api/terminal/sessions', respond: hold },
  ],
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  ensureSession: true,
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('Starting a shell…')).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'New terminal session' })).toBeDisabled()
    expect(canvas.getByRole('button', { name: 'New terminal session' })).toHaveAttribute('title', 'Starting…')
  },
})

export const Failure: Story = storyFor({
  label: 'status failure',
  routes: [
    { method: 'GET', path: '/api/terminal/status', respond: () => new Response('{}', { status: 500, headers: { 'content-type': 'application/json' } }) },
  ],
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('The engine could not be asked whether the terminal is available.')).toBeTruthy())
    expect(canvas.getByText('The engine could not be asked whether the terminal is available.')).toHaveAttribute('role', 'alert')
  },
})

export const Unavailable: Story = storyFor({
  label: 'unavailable without HTTPS',
  routes: [statusRoute({ available: false, reason: 'https_required', unlock_expires_in: 0 })],
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/needs a secure connection/)).toHaveAttribute('role', 'alert'))
    expect(canvas.queryByRole('tablist')).toBeNull()
  },
})

// Four shells in a workspace, all ended: the engine's slots are full.
export const Capacity: Story = storyFor({
  label: 'workspace slots full',
  routes: [
    statusRoute(),
    sessionsRoute(`workspace=${TERMINAL_WORKSPACE}`, [
      session('storybook-slot-1', 'storybook-slot-1', { exited: true, exit_code: 0 }),
      session('storybook-slot-2', 'storybook-slot-2', { exited: true, exit_code: 0 }),
      session('storybook-slot-3', 'storybook-slot-3', { exited: true, exit_code: 0 }),
      session('storybook-slot-4', 'storybook-slot-4', { exited: true, exit_code: 0 }),
    ]),
  ],
  target: { workspace: TERMINAL_WORKSPACE },
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/All four terminal slots are occupied/)).toBeTruthy())
    expect(canvas.getByRole('button', { name: 'New terminal session' })).toBeDisabled()
    expect(canvas.getByRole('button', { name: 'End terminal session storybook-slot-1' })).toBeEnabled()
  },
})

export const Truncated: Story = storyFor({
  label: 'truncated replay',
  routes: liveRoutes(),
  sockets: [{ path: socketPath(CHAT_SESSION), script: liveShell(CHAT_SESSION, CHAT_FOLDER, true) }],
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/output buffer was truncated/)).toHaveAttribute('role', 'status'))
    expect(canvas.getByRole('tab', { name: /storybook-alpha/ })).toHaveAttribute('aria-selected', 'true')
  },
})

// No shell and nothing asked for one: the pane says so and creates nothing.
export const NoShell: Story = storyFor({
  label: 'no shell yet',
  routes: [statusRoute(), sessionsRoute(CHAT_QUERY, [])],
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText('No shell is open for this chat yet.')).toBeTruthy())
    await waitFor(() => expect(canvas.getByText(/Fixture requests: 2\./)).toBeTruthy())
  },
})

// The status read is still pending: the pane is in its loading phase. The
// request stays open, and the pane shows the same no-shell line it shows when
// empty, because it has no separate loading indicator.
export const Loading: Story = storyFor({
  label: 'initial load',
  routes: [{ method: 'GET', path: '/api/terminal/status', respond: hold }],
  chatId: TERMINAL_CHAT,
  target: CHAT_TARGET,
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getByText(/Fixture requests: 1\./)).toBeTruthy())
    await waitFor(() => expect(canvas.getByText('No shell is open for this chat yet.')).toBeTruthy())
  },
})

// Closed: the pane is mounted but not open, so it fetches nothing and the
// open class is absent.
export const Closed: Story = storyFor({
  label: 'closed',
  routes: [],
  play: async ({ canvasElement, canvas }) => {
    await waitFor(() => expect(canvasElement.querySelector('.terminal-pane')).not.toBeNull())
    expect(canvasElement.querySelector('.terminal-pane--open')).toBeNull()
    expect(canvas.getByText(/Fixture requests: 0\./)).toBeTruthy()
  },
})
