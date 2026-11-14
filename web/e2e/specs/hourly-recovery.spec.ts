import { expect, test } from '@playwright/test'
import { isolate } from '../support/app'

test('recovery uses the captured failure identity without the request or images', async ({ page }, testInfo) => {
  await isolate(page, `recovery-${testInfo.workerIndex}`)
  const failure = { failure_id: 'synthetic-failure', recovery_eligible: true, user_turn_count: 1, last_error: 'You have reached your usage limit' }
  const rows = [
    { role: 'user', content: 'Synthetic private request', images: ['synthetic.png'], turn_index: 0, i: 0 },
    { role: 'assistant', content: 'You have reached your usage limit', is_error: true, i: 1 },
  ]
  const posts: unknown[] = []
  let conflicted = false
  await page.route('**/api/chats', async route => {
    const response = await route.fetch()
    const chats = await response.json()
    await route.fulfill({ response, json: chats.map((chat: { chat_id: string }) => chat.chat_id === 'alpha-chat-1' ? { ...chat, ...failure } : chat) })
  })
  await page.route('**/api/chats/alpha-chat-1/messages?*', route => route.fulfill({ json: { items: rows, total: 2, offset: 0, limit: 50, hasMore: false, nextOffset: null } }))
  await page.route('**/api/chats/alpha-chat-1', async route => {
    const response = await route.fetch()
    const chat = await response.json()
    await route.fulfill({ response, json: { ...chat, ...failure, ...(conflicted ? {
      retry: { status: 'pending', mode: 'literal', last_error: 'manual', next_at: '', attempts: 0, interval_seconds: 3600 },
    } : {}) } })
  })
  await page.route('**/api/chats/alpha-chat-1/retry', async route => {
    const body = route.request().postDataJSON()
    if (body.action === 'issue_recovery_request') {
      await route.fulfill({ json: { request_id: 'issued-recovery-id', expires: Date.now() / 1000 + 900 } })
      return
    }
    posts.push(body)
    conflicted = true
    await route.fulfill({ json: { request_id: body.request_id, status: 'completed', result: { http_status: 409 } } })
  })
  await page.goto('/chat/alpha-chat-1')
  const hourly = page.getByRole('button', { name: 'Retry every hour' })
  await expect(hourly).toBeEnabled()
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 844 })
    await page.screenshot({ path: testInfo.outputPath(`recovery-${width}.png`) })
  }
  await hourly.click()
  await expect.poll(() => posts).toEqual([{ action: 'continue_recovery', failure_id: 'synthetic-failure', request_id: expect.any(String) }])
  await expect(page.getByText(/another retry is pending/i)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Stop trying' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run scheduled prompt' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('recovery-conflict-390.png') })
})
