import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { once } from 'node:events'
import path from 'node:path'
import { createInterface } from 'node:readline'
import { expect, test } from '@playwright/test'
import { createServer, loadConfigFromFile } from 'vite'

test('another tab cannot acknowledge or retry a retained server operation', async ({ browser, request }) => {
  test.setTimeout(45_000)
  const web = process.cwd()
  const repo = path.resolve(web, '..')
  const pythonName = process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'
  const python = [path.join(repo, '.venv', pythonName),
    path.resolve(repo, '../../..', '.venv', pythonName)].find(existsSync) || (process.env.CI ? 'python3' : undefined)
  if (!python) throw new Error('Install the repository Python environment before running this test')

  const server = spawn(python, ['e2e/real-server/server.py'], {
    cwd: web, env: { ...process.env, PYTHONPATH: repo }, stdio: ['ignore', 'pipe', 'pipe'],
  })
  let stderr = ''
  server.stderr.on('data', chunk => { stderr += String(chunk) })
  const lines = createInterface({ input: server.stdout })
  let vite: Awaited<ReturnType<typeof createServer>> | undefined
  const context = await browser.newContext()
  try {
    const port = await new Promise<number>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`fixture startup timed out: ${stderr}`)), 15_000)
      server.once('exit', code => { clearTimeout(timer); reject(new Error(`fixture exited ${code}: ${stderr}`)) })
      lines.on('line', line => {
        const match = /^READY (\d+)$/.exec(line)
        if (match) { clearTimeout(timer); resolve(Number(match[1])) }
      })
    })
    const project = await loadConfigFromFile({ command: 'serve', mode: 'development' }, path.join(web, 'vite.config.ts'))
    if (!project) throw new Error('could not load project Vite configuration')
    vite = await createServer({
      // Reuse the app's imports and Vue transforms, but not its /api proxy to the live engine.
      configFile: false, root: web, resolve: project.config.resolve, plugins: project.config.plugins,
      server: { host: '127.0.0.1', port: 0, strictPort: false,
        proxy: { '/api/proposals': `http://127.0.0.1:${port}`, '/__proposal_test__': `http://127.0.0.1:${port}` } },
    })
    await vite.listen()
    const address = vite.httpServer!.address()
    if (!address || typeof address === 'string') throw new Error('no Vite port')
    const origin = `http://127.0.0.1:${address.port}`
    const status = async () => (await request.get(`${origin}/__proposal_test__/status`)).json()
    // Separate page execution contexts in one browser profile share storage and
    // Web Locks; separate Playwright BrowserContexts intentionally do not.
    const first = await context.newPage()
    const second = await context.newPage()
    await Promise.all([first.goto(`${origin}/e2e/real-server/`), second.goto(`${origin}/e2e/real-server/`)])
    await expect(first.getByTestId('ready')).toHaveText('ready')
    await expect(second.getByTestId('ready')).toHaveText('ready')
    expect(await first.evaluate(() => typeof navigator.locks?.request)).toBe('function')

    let secondDispatches = 0
    second.on('request', req => {
      if (req.method() === 'POST' && /\/api\/proposals\/[^/]+\/accept$/.test(req.url())) secondDispatches++
    })
    await first.getByTestId('act').click()
    await expect.poll(async () => (await status()).entered).toBe(true)
    await expect(second.getByTestId('warning')).toHaveText('unresolved')
    await second.getByTestId('inspect').click()
    await second.getByTestId('ack').click()
    await expect(second.getByTestId('lock')).toContainText('another tab')
    await second.getByTestId('act').click()
    expect(secondDispatches).toBe(0)

    await first.close()
    await expect.poll(async () => second.evaluate(async () =>
      (await navigator.locks.query()).held?.some(lock => lock.name === 'ciao:proposal-mutation') ?? false)).toBe(false)
    expect((await status()).active).toBe(true)
    const refusedSeal = second.waitForResponse(response =>
      response.url().endsWith('/api/proposals/reservations/seal') && response.request().method() === 'POST')
    await second.getByTestId('ack').click()
    expect((await refusedSeal).status()).toBe(409)
    await expect(second.getByTestId('warning')).toHaveText('unresolved')
    await second.getByTestId('act').click()
    expect(secondDispatches).toBe(0)
    expect((await status()).active).toBe(true)

    expect((await request.post(`${origin}/__proposal_test__/release`)).ok()).toBe(true)
    await expect.poll(async () => (await status()).active).toBe(false)
    const finished = await status()
    expect(finished.queue).not.toContain('Remember the isolated browser test.')
    expect(finished.guide).toContain('Remember the isolated browser test.')
    const third = await context.newPage()
    await third.goto(`${origin}/e2e/real-server/`)
    await expect(third.getByTestId('warning')).toHaveText('unresolved')
    await second.getByTestId('inspect').click()
    await expect(second.getByTestId('inspection')).toContainText('Remember the isolated browser test.')
    await second.getByTestId('ack').click()
    await expect(second.getByTestId('warning')).toHaveText('clear')
    await expect(third.getByTestId('warning')).toHaveText('clear')
    expect(secondDispatches).toBe(0)
  } finally {
    await context.close()
    await vite?.close()
    lines.close()
    server.kill()
  }
})

test('completed seal remains retryable after browser storage failure and server restart', async ({ browser, request }) => {
  test.setTimeout(45_000)
  const web = process.cwd()
  const repo = path.resolve(web, '..')
  const pythonName = process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'
  const python = [path.join(repo, '.venv', pythonName),
    path.resolve(repo, '../../..', '.venv', pythonName)].find(existsSync) || (process.env.CI ? 'python3' : undefined)
  if (!python) throw new Error('Install the repository Python environment before running this test')
  const root = mkdtempSync(path.join(tmpdir(), 'ciao-proposal-browser-'))
  const context = await browser.newContext()
  let server: ReturnType<typeof spawn> | undefined
  let vite: Awaited<ReturnType<typeof createServer>> | undefined
  async function start(port?: number): Promise<number> {
    const child = spawn(python!, ['e2e/real-server/server.py', root, String(port || 0)], {
      cwd: web, env: { ...process.env, PYTHONPATH: repo }, stdio: ['ignore', 'pipe', 'pipe'],
    })
    server = child
    let stderr = ''
    child.stderr!.on('data', chunk => { stderr += String(chunk) })
    const lines = createInterface({ input: child.stdout! })
    return await new Promise<number>((resolve, reject) => {
      const timer = setTimeout(() => { lines.close(); reject(new Error(`fixture startup timed out: ${stderr}`)) }, 15_000)
      child.once('exit', code => { clearTimeout(timer); lines.close(); reject(new Error(`fixture exited ${code}: ${stderr}`)) })
      lines.on('line', line => {
        const match = /^READY (\d+)$/.exec(line)
        if (match) { clearTimeout(timer); lines.close(); resolve(Number(match[1])) }
      })
    })
  }
  async function stop(): Promise<void> {
    if (!server || server.exitCode !== null || server.signalCode !== null) return
    const exited = once(server, 'exit')
    server.kill()
    await exited
  }
  try {
    const port = await start()
    const project = await loadConfigFromFile({ command: 'serve', mode: 'development' }, path.join(web, 'vite.config.ts'))
    if (!project) throw new Error('could not load project Vite configuration')
    vite = await createServer({
      configFile: false, root: web, resolve: project.config.resolve, plugins: project.config.plugins,
      server: { host: '127.0.0.1', port: 0,
        proxy: { '/api/proposals': `http://127.0.0.1:${port}`, '/__proposal_test__': `http://127.0.0.1:${port}` } },
    })
    await vite.listen()
    const address = vite.httpServer!.address()
    if (!address || typeof address === 'string') throw new Error('no Vite port')
    const origin = `http://127.0.0.1:${address.port}`
    const status = async () => (await request.get(`${origin}/__proposal_test__/status`)).json()
    const first = await context.newPage()
    await first.addInitScript(() => {
      const remove = Storage.prototype.removeItem
      Storage.prototype.removeItem = function (key: string) {
        if (key.startsWith('ciao:proposal-intent:')) throw new Error('injected storage failure')
        return remove.call(this, key)
      }
    })
    await first.goto(`${origin}/e2e/real-server/`)
    await expect(first.getByTestId('ready')).toHaveText('ready')
    await first.getByTestId('act').click()
    await expect.poll(async () => (await status()).entered).toBe(true)
    const sealed = first.waitForResponse(response =>
      response.url().endsWith('/api/proposals/reservations/seal') && response.request().method() === 'POST')
    expect((await request.post(`${origin}/__proposal_test__/release`)).ok()).toBe(true)
    const originalSeal = await sealed
    expect(originalSeal.status()).toBe(200)
    const originalBody = originalSeal.request().postDataJSON()
    expect(originalBody.id).toBeTruthy()
    expect(originalBody.scope).toBeTruthy()
    expect(typeof originalBody.inspected).toBe('boolean')
    await expect(first.getByTestId('warning')).toHaveText('unresolved')
    await expect.poll(async () => (await status()).active).toBe(false)
    const finished = await status()
    expect(finished.queue).not.toContain('Remember the isolated browser test.')
    expect(finished.guide).toContain('Remember the isolated browser test.')
    await first.close()
    await stop()
    expect(await start(port)).toBe(port)
    await expect.poll(async () => (await request.get(`${origin}/__proposal_test__/status`)).status()).toBe(200)
    const recovered = await context.newPage()
    await recovered.goto(`${origin}/e2e/real-server/`)
    await expect(recovered.getByTestId('warning')).toHaveText('unresolved')
    await recovered.getByTestId('inspect').click()
    const retried = recovered.waitForResponse(response =>
      response.url().endsWith('/api/proposals/reservations/seal') && response.request().method() === 'POST')
    await recovered.getByTestId('ack').click()
    const retrySeal = await retried
    expect(retrySeal.status()).toBe(200)
    const retryBody = retrySeal.request().postDataJSON()
    expect(retryBody.id).toBe(originalBody.id)
    expect(retryBody.scope).toEqual(originalBody.scope)
    expect(retryBody.inspected).toBe(originalBody.inspected)
    await expect(recovered.getByTestId('warning')).toHaveText('clear')
  } finally {
    await context.close()
    await vite?.close()
    await stop()
    rmSync(root, { recursive: true, force: true })
  }
})
