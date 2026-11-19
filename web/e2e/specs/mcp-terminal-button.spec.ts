import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate, openChatDetails, settled } from '../support/app'

/**
 * Chat details MCP, in a real browser.
 *
 * Ciaobot keeps no MCP inventory. The section is one button that types the
 * chat's agent CLI MCP command into this chat's terminal. The command is the
 * provider row's `mcp_command`; the page never builds it.
 */
const CHAT_PATH = '/chat/alpha-chat-1'

test.describe('Chat details MCP button', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('types the agent MCP command into the chat terminal', async ({ page }, testInfo) => {
    await isolate(page, `mcp-terminal-button-${testInfo.workerIndex}`)
    await boot(page, CHAT_PATH, COMPOSER)

    const details = page.locator('aside').filter({ has: page.getByRole('heading', { name: 'Chat details' }) })
    if (!(await details.isVisible())) {
      await settled(page.locator('.chat-layout'))
      if (!(await details.isVisible())) await openChatDetails(page)
    }
    const section = details.locator('.chat-rail-mcp')
    await expect(section.getByRole('heading', { name: 'MCP' })).toBeVisible()
    await expect(section.locator('code')).toHaveText('claude mcp')

    await section.getByRole('button', { name: 'Open MCP settings in Claude Code', exact: true }).click()
    await expect(page.locator('.terminal-pane')).toBeVisible()
    // The terminal takes the right-hand space at this width, so the rail
    // closes: assert the typed command in the terminal itself, not the note.
    await expect(page.locator('.terminal-pane')).toContainText('claude mcp')
  })
})
