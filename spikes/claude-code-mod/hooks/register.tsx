import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Catalog, Chat, ChatLine, MemoryJob, Project, View } from '../types'
import { CATALOG_SCRIPT, CIAO_ROOT } from './catalog'

const APP = 'ciaobot'

const catalog = atom({ plugin: 'ciao', key: 'catalog' } as const, null)
const catalogError = atom({ plugin: 'ciao', key: 'catalogError' } as const, '')
const current = atom({ plugin: 'ciao', key: 'current' } as const, null)
const workspace = atom({ plugin: 'ciao', key: 'workspace' } as const, 'work')
const chats = atom({ plugin: 'ciao', key: 'chats' } as const, [])
const view = atom({ plugin: 'ciao', key: 'view' } as const, { kind: 'home' })
const lines = atom({ plugin: 'ciao', key: 'lines' } as const, [])
const expanded = atom({ plugin: 'ciao', key: 'expanded' } as const, [])
const notice = atom({ plugin: 'ciao', key: 'notice' } as const, '')
const isAppOpen = atom({ plugin: 'ciao', key: 'isAppOpen' } as const, false)
const isRouting = atom({ plugin: 'ciao', key: 'isRouting' } as const, true)
const memoryQueue = atom({ plugin: 'ciao', key: 'memoryQueue' } as const, [])
const QUEUE_KEY = 'memory-queue'
// Set while drawing the app pane: drawing may not write $.state, and a pane kept
// across a reload never runs /ciaobot again to set isAppOpen.
let isAppDrawn = false

// Subagents belong to the host session, so the chat index is kept per host session.
const chatsKey = (sessionId: string) => `chats:${sessionId}`
const CTX_OPEN = '<ciao-chat-context>'
const CTX_CLOSE = '</ciao-chat-context>'

const LAST_PROJECT_KEY = 'last-project'

const oneLine = (s: string, limit: number) => s.split(/\s+/).join(' ').trim().slice(0, limit)

// Mirrors ciao/context/capsule.py so the model sees what a Ciao chat sees.
function capsule(project: Project, cat: Catalog | null): string {
  const ws = cat?.workspaces.find(w => w.name === project.workspace)
  const lines = [`workspace=${project.workspace}`]
  if (ws?.vaultRoot) lines.push(`vault=${ws.vaultRoot}`)
  if (ws?.gwsProfile) lines.push(`gws_profile=${ws.gwsProfile}`)
  if (project.name !== 'General') lines.push(`project="${oneLine(project.name, 180)}"`)
  if (project.context) lines.push(`project_context=${oneLine(project.context, 1200)}`)
  if (project.canonicalDoc) lines.push(`canonical_doc=${project.canonicalDoc}`)
  lines.push(`today=${new Date().toISOString().slice(0, 10)}`)
  return [
    'This session is filed under a Ciao project. Treat the project as the default scope for',
    'questions, notes and vault writes; read canonical_doc before substantive project work.',
    '<ciao-context>',
    ...lines,
    '</ciao-context>',
  ].join('\n')
}

async function loadCatalog($: EngineInterface) {
  const ran = await $.process.run(['python3', `${$.plugin.root}/${CATALOG_SCRIPT}`, CIAO_ROOT], { timeoutMs: 20_000 })
  if (ran.exitCode !== 0) {
    await update($, catalogError, () => oneLine(ran.stderr, 300) || `exit ${ran.exitCode}`)
    return null
  }
  const cat = { ...(JSON.parse(ran.stdout) as Omit<Catalog, 'loadedAt'>), loadedAt: new Date().toISOString() }
  await update($, catalog, () => cat)
  await update($, catalogError, () => '')
  return cat
}

// The project a new chat from Home goes into.
async function choose($: EngineInterface, project: Project) {
  await update($, current, () => project)
  await $.store.set(LAST_PROJECT_KEY, project)
}

async function saveChats($: EngineInterface, list: Chat[]) {
  await update($, chats, () => list)
  await $.store.set(chatsKey(await $.session.id()), list)
}

async function patchChat($: EngineInterface, agentId: string, patch: Partial<Chat>) {
  const list = (await read($, chats)).map(c => (c.agentId === agentId ? { ...c, ...patch } : c))
  await saveChats($, list)
}

// The first prompt carries the project capsule; the pane hides it.
function visibleText(text: string): string {
  const end = text.indexOf(CTX_CLOSE)
  return (end >= 0 ? text.slice(end + CTX_CLOSE.length) : text).trim()
}

async function loadLines($: EngineInterface, agentId: string) {
  const found = await $.session.messages({ agentId })
  if (!Array.isArray(found)) {
    await update($, notice, () => `Cannot read this chat: ${'deny' in found ? String(found.deny) : 'unknown'}`)
    return
  }
  const shown: ChatLine[] = []
  for (const m of found) {
    const text = m.role === 'user' ? visibleText(m.text) : m.text.trim()
    const tools = m.toolUses.length
    const last = shown.at(-1)
    // Collapse tool-only assistant steps into the next visible line, as the PWA's "N tool calls".
    if (m.role === 'user' && !text) continue
    if (m.role === 'assistant' && !text) {
      if (last?.role === 'assistant' && !last.text) last.tools += tools
      else shown.push({ role: 'assistant', text: '', tools })
      continue
    }
    if (m.role === 'assistant' && last?.role === 'assistant' && !last.text) {
      last.text = text
      last.tools += tools
      continue
    }
    shown.push({ role: m.role, text, tools })
  }
  await update($, lines, () => shown)
}

async function openChat($: EngineInterface, agentId: string) {
  await update($, view, () => ({ kind: 'chat', agentId }) as View)
  await update($, lines, () => [])
  await update($, notice, () => '')
  await loadLines($, agentId)
}

async function startChat($: EngineInterface, project: Project, text: string) {
  const cat = await read($, catalog)
  const prompt = `${CTX_OPEN}\n${subagentCapsule(project, cat)}\n${CTX_CLOSE}\n\n${text}`
  const title = oneLine(text, 60)
  const spawned = await $.agent.spawn({ prompt, description: title, subagentType: 'general-purpose' })
  if (!spawned.agentId) {
    await update($, notice, () => `Could not start the chat: ${'deny' in spawned ? String(spawned.deny) : 'no agent id'}`)
    return
  }
  const chat: Chat = {
    agentId: spawned.agentId,
    projectId: project.id,
    projectName: project.name,
    workspace: project.workspace,
    title,
    lastActivity: new Date().toISOString(),
    isRunning: true,
  }
  await saveChats($, [chat, ...(await read($, chats))])
  await update($, expanded, list => (list.includes(project.id) ? list : [...list, project.id]))
  await openChat($, chat.agentId)
  await update($, lines, list => (list.length ? list : [{ role: 'user' as const, text, tools: 0 }]))
}

async function reply($: EngineInterface, agentId: string, text: string) {
  await update($, lines, list => [...list, { role: 'user' as const, text, tools: 0 }])
  await patchChat($, agentId, { isRunning: true, lastActivity: new Date().toISOString() })
  const sent = await $.session.send({ to: { agentId }, text })
  if (!sent.isDelivered) {
    await patchChat($, agentId, { isRunning: false })
    await update($, notice, () => `Not delivered: ${sent.reason}`)
  }
}

async function archiveChat($: EngineInterface, agentId: string) {
  const list = await read($, chats)
  const chat = list.find(c => c.agentId === agentId)
  if (!chat) return
  const job: MemoryJob = {
    agentId,
    projectName: chat.projectName,
    workspace: chat.workspace,
    title: chat.title,
    queuedAt: new Date().toISOString(),
  }
  // Extraction is not wired yet: the job only waits in the queue the Home view shows.
  const queue = [...(await read($, memoryQueue)), job]
  await update($, memoryQueue, () => queue)
  await $.store.set(QUEUE_KEY, queue)
  await saveChats($, list.filter(c => c.agentId !== agentId))
  await update($, view, () => ({ kind: 'home' }) as View)
  $.ui.toast(`Archived "${chat.title}" · queued for memory`)
}

async function routeTarget($: EngineInterface): Promise<{ label: string; chat?: Chat; project?: Project } | null> {
  if (!(isAppDrawn || (await read($, isAppOpen))) || !(await read($, isRouting))) return null
  const open = await read($, view)
  if (open.kind === 'chat') {
    const chat = (await read($, chats)).find(c => c.agentId === open.agentId)
    return chat ? { label: `chat "${chat.title}"`, chat } : null
  }
  const project = await homeProject($)
  return project ? { label: `a new chat in ${project.name}`, project } : null
}

async function homeProject($: EngineInterface): Promise<Project | undefined> {
  const cat = await read($, catalog)
  const ws = await read($, workspace)
  const chosen = await read($, current)
  const projects = (cat?.projects ?? []).filter(p => p.workspace === ws)
  return chosen && chosen.workspace === ws ? chosen : projects.find(p => p.name === 'General') ?? projects[0]
}

function subagentCapsule(project: Project, cat: Catalog | null): string {
  return capsule(project, cat).replace(
    'This session is filed under a Ciao project.',
    [
      'You are one chat in the Ciaobot pane inside Claude Code, running as a subagent.',
      'Your final reply is shown to the person as your chat message, so answer them directly.',
      'The pane owns the chat lifecycle: archiving, closing and memory extraction happen from its',
      'buttons. Do not call the Ciao engine or `ciao agent chat ...` commands to manage this chat.',
    ].join(' '),
  )
}

// Ciao workspace colors (Tailwind names in workspaces.json) as terminal colors.
const ACCENTS: Record<string, string> = {
  emerald: 'green', green: 'green', teal: 'cyan', sky: 'cyan', cyan: 'cyan', blue: 'blue',
  indigo: 'blue', violet: 'magenta', purple: 'magenta', pink: 'magenta', rose: 'red', red: 'red',
  orange: 'yellow', amber: 'yellow', yellow: 'yellow',
}
function accentOf(cat: Catalog | null, ws: string): string {
  return ACCENTS[cat?.workspaces.find(w => w.name === ws)?.color ?? ''] ?? 'cyan'
}

function ago(iso: string): string {
  const mins = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60_000))
  if (mins < 60) return `${mins}m`
  if (mins < 60 * 24) return `${Math.round(mins / 60)}h`
  return `${Math.round(mins / 1440)}d`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({ name: 'ciaobot', description: 'Open or close Ciaobot: projects, chats and home' })
    const saved = (await $.store.get(LAST_PROJECT_KEY)) as Project | undefined
    const kept = (await $.store.get(chatsKey(await $.session.id()))) as Chat[] | undefined
    if (kept) await update($, chats, () => kept.map(c => ({ ...c, isRunning: false })))
    const queued = (await $.store.get(QUEUE_KEY)) as MemoryJob[] | undefined
    if (queued) await update($, memoryQueue, () => queued)
    if (saved) {
      await update($, current, () => saved)
      await update($, workspace, () => saved.workspace)
    }
    void loadCatalog($)
    return started
  })

  on('command.run', { command: 'ciaobot' }, async $ => {
    if (isAppDrawn || (await read($, isAppOpen))) {
      await $.ui.close({ id: APP })
      return { text: 'Ciaobot closed.' }
    }
    if (!(await read($, catalog))) await loadCatalog($)
    await $.ui.open({ id: APP, closeOnEscape: true, title: 'Ciaobot', focus: true, columns: 110 })
    await update($, isAppOpen, () => true)
    await update($, isRouting, () => true)
    return { text: 'Ciaobot opened.' }
  })

  on('ui.close', async ($, e, next) => {
    const closed = await next(e)
    if (e.id === APP) {
      isAppDrawn = false
      await update($, isAppOpen, () => false)
    }
    return closed
  })

  on('prompt.submit', async ($, e, next) => {
    // A pane chat finishing would otherwise wake the main session with its result.
    if (e.origin.kind === 'task-notification') {
      const ours = (await read($, chats)).find(c => e.text.includes(c.agentId))
      if (ours) return { drop: `Ciao chat "${ours.title}" finished; shown in the pane` }
      return next(e)
    }
    if (e.origin.kind !== 'composer' || e.text.trimStart().startsWith('/')) return next(e)
    const target = await routeTarget($)
    if (!target) return next(e)
    const text = e.text.trim()
    if (!text) return next(e)
    if (target.chat) await reply($, target.chat.agentId, text)
    else if (target.project) await startChat($, target.project, text)
    return { drop: `Sent to ${target.label}` }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || !(isAppDrawn || (await read($, isAppOpen)))) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const routing = await read($, isRouting)
    const target = await routeTarget($)
    const accent = accentOf(await read($, catalog), await read($, workspace))
    return (
      <Box flexDirection="row" gap={1}>
        {routing && target ? (
          <Text color={accent} bold>{`✎ Typing below goes to ${target.label}`}</Text>
        ) : (
          <Text dimColor>✎ Typing below goes to the main Claude session</Text>
        )}
        <Button key="route" plain dimColor onPress={() => void update($, isRouting, v => !v)}>
          {routing ? '(talk to main instead)' : '(send to Ciao instead)'}
        </Button>
      </Box>
    )
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    const agentId = e.agentId
    if (agentId && (await read($, chats)).some(c => c.agentId === agentId)) {
      await patchChat($, agentId, { isRunning: false, lastActivity: new Date().toISOString() })
      const open = await read($, view)
      if (open.kind === 'chat' && open.agentId === agentId) await loadLines($, agentId)
    }
    return done
  })

  on('ui.render', { component: 'Pane', requestId: APP }, async ($, e) => {
    isAppDrawn = true
    const ui = $.ui.resolve(e)
    const { Box, Text, Button, Markdown } = ui
    const cat = await read($, catalog)
    const error = await read($, catalogError)
    const ws = await read($, workspace)
    const chosen = await read($, current)
    const list = await read($, chats)
    const open = await read($, view)
    const shown = await read($, lines)
    const opened = await read($, expanded)
    const note = await read($, notice)
    const queue = await read($, memoryQueue)
    const routing = await read($, isRouting)
    const rows = e.viewport?.rows ?? 40
    const columns = e.viewport?.columns ?? 110

    if (!cat) {
      return (
        <Box flexDirection="column" paddingX={1}>
          <Text dimColor>{error ? `Could not read the Ciao workspace: ${error}` : '◌ Loading the Ciao workspace…'}</Text>
          {error && <Button key="retry" plain hotkey="r" onPress={() => void loadCatalog($)}>Retry</Button>}
        </Box>
      )
    }

    const accent = accentOf(cat, ws)
    const projects = cat.projects.filter(p => p.workspace === ws)
    const target = chosen && chosen.workspace === ws ? chosen : projects.find(p => p.name === 'General') ?? projects[0]
    const wsChats = list.filter(c => c.workspace === ws)
    const SIDEBAR = 32
    const mainWidth = Math.max(30, columns - SIDEBAR - 4)
    const rule = <Text dimColor>{'─'.repeat(mainWidth)}</Text>

    const sidebar = (
      <Box flexDirection="column" width={SIDEBAR} flexShrink={0} borderStyle="round" borderDimColor paddingX={1}>
        <Box flexDirection="row" justifyContent="space-between">
          <Text color={accent} bold>◆ ciaobot</Text>
          <Button key="close-app" plain dimColor role="dismiss" onPress={() => void $.ui.close({ id: APP })}>✕</Button>
        </Box>
        <Box flexDirection="row" gap={2} marginBottom={1}>
          {cat.workspaces.map(w => (
            <Button key={`ws-${w.name}`} plain dimColor={w.name !== ws} onPress={() => void update($, workspace, () => w.name)}>
              {`${w.name === ws ? '●' : '○'} ${w.name}`}
            </Button>
          ))}
        </Box>
        <Box flexDirection="row">
          <Text color={accent}>{open.kind === 'home' ? '▌' : ' '}</Text>
          <Button key="home" plain dimColor={open.kind !== 'home'} onPress={() => void update($, view, () => ({ kind: 'home' }) as View)}>
            ⌂ Home
          </Button>
        </Box>
        <Box marginTop={1}>
          <Text dimColor bold>PROJECTS</Text>
        </Box>
        {projects.map(p => {
          const mine = wsChats.filter(c => c.projectId === p.id)
          const isOpen = opened.includes(p.id) || mine.length > 0
          const isTarget = target?.id === p.id
          return (
            <Box key={`proj-${p.id}`} flexDirection="column">
              <Box flexDirection="row" justifyContent="space-between">
                <Box flexDirection="row">
                  <Text color={accent}>{isTarget ? '▌' : ' '}</Text>
                  <Button
                    key={`p-${p.id}`}
                    plain
                    dimColor={!isTarget}
                    onPress={async () => {
                      await choose($, p)
                      await update($, expanded, l => (l.includes(p.id) ? l.filter(x => x !== p.id) : [...l, p.id]))
                    }}
                  >
                    {`${mine.length ? (isOpen ? '▾' : '▸') : ' '} ${p.name.slice(0, SIDEBAR - 10)}`}
                  </Button>
                </Box>
                {mine.length > 0 && <Text dimColor>{String(mine.length)}</Text>}
              </Box>
              {isOpen && mine.map(c => {
                const isOpenChat = open.kind === 'chat' && open.agentId === c.agentId
                return (
                  <Box key={`cr-${c.agentId}`} flexDirection="row">
                    <Text color={accent}>{isOpenChat ? '▌' : ' '}</Text>
                    <Text color={c.isRunning ? 'yellow' : undefined} dimColor={!c.isRunning}>{c.isRunning ? '  ◌ ' : '  · '}</Text>
                    <Button key={`c-${c.agentId}`} plain dimColor={!isOpenChat} onPress={() => void openChat($, c.agentId)}>
                      {c.title.slice(0, SIDEBAR - 9)}
                    </Button>
                  </Box>
                )
              })}
            </Box>
          )
        })}
      </Box>
    )

    let main
    if (open.kind === 'home') {
      const recent = [...wsChats].sort((a, b) => b.lastActivity.localeCompare(a.lastActivity)).slice(0, 8)
      main = (
        <Box flexDirection="column" flexGrow={1} paddingX={2} gap={1}>
          <Box flexDirection="column">
            <Box flexDirection="row" justifyContent="space-between">
              <Text bold>Home</Text>
              <Text dimColor>{`${ws} · ${projects.length} projects · ${wsChats.length} open chats`}</Text>
            </Box>
            {rule}
          </Box>
          <Box flexDirection="column" borderStyle="round" borderColor={accent} paddingX={1}>
            <Text>
              <Text dimColor>New chat in </Text>
              <Text color={accent} bold>{target ? target.name : 'no project'}</Text>
            </Text>
            {target?.context ? <Text dimColor wrap="truncate-end">{target.context}</Text> : null}
            <Text dimColor>{routing ? '↓ Type in the prompt box below and press Enter' : 'Routing is off: the prompt box talks to the main session'}</Text>
          </Box>
          {queue.length > 0 && (
            <Text>
              <Text color="yellow">✦ </Text>
              <Text>{`${queue.length} archived chat${queue.length === 1 ? '' : 's'} waiting for memory extraction`}</Text>
            </Text>
          )}
          <Box flexDirection="column">
            <Text dimColor bold>CONTINUE WHERE YOU LEFT OFF</Text>
            {recent.length === 0 && <Text dimColor>No chats yet in this session. Start one from the prompt box.</Text>}
            {recent.map(c => (
              <Box key={`r-${c.agentId}`} flexDirection="row" justifyContent="space-between">
                <Box flexDirection="row">
                  <Text color={c.isRunning ? 'yellow' : accent}>{c.isRunning ? '◌ ' : '› '}</Text>
                  <Button key={`rb-${c.agentId}`} plain onPress={() => void openChat($, c.agentId)}>
                    {c.title.slice(0, Math.max(20, mainWidth - 32))}
                  </Button>
                </Box>
                <Text dimColor>{`${c.projectName.slice(0, 18)} · ${c.isRunning ? 'working' : ago(c.lastActivity)}`}</Text>
              </Box>
            ))}
          </Box>
        </Box>
      )
    } else {
      const chat = list.find(c => c.agentId === open.agentId)
      const room = Math.max(3, Math.floor((rows - 10) / 4))
      main = (
        <Box flexDirection="column" flexGrow={1} paddingX={2} gap={1}>
          <Box flexDirection="column">
            <Box flexDirection="row" justifyContent="space-between" gap={2}>
              <Text bold wrap="truncate-end">{chat?.title ?? open.agentId}</Text>
              {chat && (
                <Text>
                  <Text color={accent}>{chat.projectName}</Text>
                  <Text dimColor>{' · '}</Text>
                  {chat.isRunning ? <Text color="yellow">◌ working</Text> : <Text dimColor>{ago(chat.lastActivity)}</Text>}
                </Text>
              )}
            </Box>
            {rule}
          </Box>
          {shown.length > room && <Text dimColor>{`↑ ${shown.length - room} earlier message${shown.length - room === 1 ? '' : 's'}`}</Text>}
          {shown.length === 0 && !chat?.isRunning && <Text dimColor>No messages yet.</Text>}
          {shown.slice(-room).map((l, i) => (
            <Box key={`l-${i}`} flexDirection="column">
              {l.role === 'user' ? (
                <Box flexDirection="row">
                  <Text color={accent} bold>{'you › '}</Text>
                  <Box flexShrink={1}><Text>{l.text}</Text></Box>
                </Box>
              ) : (
                <Box flexDirection="column">
                  <Text>
                    <Text bold>ciao</Text>
                    {l.tools > 0 ? <Text dimColor>{`  ⚙ ${l.tools} tool call${l.tools === 1 ? '' : 's'}`}</Text> : null}
                  </Text>
                  {l.text ? <Markdown key={`m-${i}`} text={l.text} /> : null}
                </Box>
              )}
            </Box>
          ))}
          {chat?.isRunning && <Text color="yellow">◌ working…</Text>}
          <Box flexDirection="column">
            {rule}
            <Box flexDirection="row" justifyContent="space-between">
              <Text color={routing ? accent : undefined} dimColor={!routing}>
                {routing ? '↓ Reply in the prompt box below' : 'Prompt box → main session'}
              </Text>
              <Box flexDirection="row" gap={2}>
                <Button key="back" plain hotkey="h" onPress={() => void update($, view, () => ({ kind: 'home' }) as View)}>Home</Button>
                <Button key="refresh" plain hotkey="r" onPress={() => void loadLines($, open.agentId)}>Refresh</Button>
                <Button key="archive" plain hotkey="a" onPress={() => void archiveChat($, open.agentId)}>Archive</Button>
              </Box>
            </Box>
          </Box>
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        {note && (
          <Box borderStyle="round" borderColor="red" paddingX={1}>
            <Text color="red">{note}</Text>
          </Box>
        )}
        <Box flexDirection="row">
          {sidebar}
          {main}
        </Box>
      </Box>
    )
  })
}
