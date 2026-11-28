// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { findHeading, headingKey } from './markdownHeadings'

function rendered(html: string): HTMLElement {
  const root = document.createElement('div')
  root.innerHTML = html
  return root
}

describe('headingKey', () => {
  it('ignores case, # marks, spacing and a trailing colon', () => {
    expect(headingKey('## Hygiene')).toBe('hygiene')
    expect(headingKey('  What   needs you: ')).toBe('what needs you')
    expect(headingKey('')).toBe('')
  })
})

describe('findHeading', () => {
  const root = rendered('<h1>Care report</h1><p>Hygiene is a word here.</p><h2>What needs you</h2><h2>Hygiene:</h2><h3>Hygiene</h3>')

  it('finds the first heading that names the section, never body text', () => {
    const found = findHeading(root, 'hygiene')
    expect(found?.tagName).toBe('H2')
    expect(found?.textContent).toBe('Hygiene:')
    expect(findHeading(root, '## What needs you')?.textContent).toBe('What needs you')
  })

  it('finds nothing for a missing or empty name', () => {
    expect(findHeading(root, 'Notes')).toBeNull()
    expect(findHeading(root, '  ')).toBeNull()
    expect(findHeading(root, 'Hyg')).toBeNull()
  })
})
