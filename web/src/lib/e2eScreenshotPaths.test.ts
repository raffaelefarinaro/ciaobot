import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const specsDir = join(import.meta.dirname, '../../e2e/specs')

function specFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    return entry.isDirectory() ? specFiles(path) : entry.name.endsWith('.spec.ts') ? [path] : []
  })
}

describe('Playwright specs', () => {
  it('do not contain machine-specific screenshot paths', () => {
    for (const file of specFiles(specsDir)) {
      const source = readFileSync(file, 'utf8')
      expect(source, file).not.toContain('/private/tmp')
      expect(source, file).not.toContain('scratchpad')
    }
  })
})
