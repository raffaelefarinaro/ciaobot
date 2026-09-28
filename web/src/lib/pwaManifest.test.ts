import { describe, expect, it } from 'vitest'
import manifest from '../../public/manifest.json'

describe('PWA manifest', () => {
  it('matches the app shell and exposes only deliberate destinations', () => {
    expect(manifest.id).toBe('/')
    expect(manifest.theme_color).toBe('#1a1a2e')
    expect(manifest.background_color).toBe('#1a1a2e')
    expect(manifest.shortcuts.map(shortcut => shortcut.url)).toEqual(['/?new=1', '/memory/suggested', '/schedules'])
    expect(manifest.share_target.action).toBe('/share-target')
    expect(manifest.share_target.method).toBe('POST')
  })
})
