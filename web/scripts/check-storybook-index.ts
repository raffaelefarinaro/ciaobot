import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import type { InventoryEntry } from '../src/stories/inventory'

export function checkStorybookIndex(
  inventory: readonly InventoryEntry[],
  entries: Readonly<Record<string, { type?: string }>>,
): void {
  for (const owner of inventory) {
    for (const state of owner.states) {
      const context = `${owner.owner.path}/${state.name}`
      if (state.coverage === 'covered' && !state.storyIds.length) throw new Error(`${context}: covered state has no story IDs`)
      if (state.coverage !== 'covered' && state.storyIds.length) throw new Error(`${context}: noncovered state has story IDs`)
      for (const id of state.storyIds) {
        if (!id.trim() || id.endsWith('--docs') || id === 'inventory--all' || entries[id]?.type !== 'story') {
          throw new Error(`${context}: invalid or missing story ${id}`)
        }
      }
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const { inventory } = await import(new URL('../src/stories/inventory.ts', import.meta.url).href)
  const index = JSON.parse(readFileSync(new URL('../dist/storybook/index.json', import.meta.url), 'utf8'))
  if (!index.entries || typeof index.entries !== 'object' || Array.isArray(index.entries)) throw new Error('Invalid Storybook index entries')
  checkStorybookIndex(inventory, index.entries)
}
