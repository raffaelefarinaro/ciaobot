import { describe, expect, it } from 'vitest'
import { projectDeleteMessage, projectDeleteToastBody } from './projectDelete'

describe('projectDeleteMessage', () => {
  it('names the vault folder and the chat count for a vault-backed project', () => {
    const message = projectDeleteMessage({ name: 'Kitchen rebuild', vault_folder: 'kitchen' }, 3)

    expect(message).toContain('Kitchen rebuild')
    // The folder that goes from disk is named, so the user knows what is lost.
    expect(message).toContain('projects/active/kitchen/')
    // The links into the deleted notes are cleaned, not left dangling.
    expect(message).toContain('links')
    expect(message).toContain('3')
    expect(message).toContain('This cannot be undone.')
  })

  it('leaves the vault paragraph out for a project without a vault entry', () => {
    const message = projectDeleteMessage({ name: 'Scratch', vault_folder: '' }, 0)

    expect(message).toContain('Scratch')
    expect(message).not.toContain('projects/active/')
    expect(message).not.toContain('links')
    expect(message).toContain('This cannot be undone.')
  })

  it('is multi-line, so the dialog can lay it out as paragraphs', () => {
    const message = projectDeleteMessage({ name: 'Kitchen rebuild', vault_folder: 'kitchen' }, 2)

    expect(message.split('\n').length).toBeGreaterThan(1)
  })
})

describe('projectDeleteToastBody', () => {
  it('reports the notes, files and cleaned links of a vault-backed delete', () => {
    const body = projectDeleteToastBody({
      ok: true,
      vault_entry_removed: true,
      removed_notes: 2,
      removed_files: 5,
      edited_backlinks: ['memory-vault/personal/Notes/Ref.md'],
    })

    expect(body).toContain('2 notes removed')
    expect(body).toContain('5 files removed')
    expect(body).toContain('1 link cleaned')
  })

  it('says only what happened when no vault entry was there', () => {
    const body = projectDeleteToastBody({
      ok: true,
      vault_entry_removed: false,
      removed_notes: 0,
      removed_files: 0,
      edited_backlinks: [],
    })

    expect(body).not.toContain('notes removed')
    expect(body).toContain('chats are gone')
  })
})
