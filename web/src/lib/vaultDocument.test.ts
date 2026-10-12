// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import {
  buildVaultDocumentMetadata,
  humanizeVaultMetadata,
  isVaultMetadataUrl,
  renderVaultMarkdown,
  resolveVaultImageSrc,
} from './vaultDocument'

describe('vault document helpers', () => {
  it('builds the metadata view while hiding a title repeated by the heading', () => {
    const result = buildVaultDocumentMetadata([
      '---',
      'type: project',
      'status: in-discussion',
      'title: "LV order capture"',
      'description: "Read the order number."',
      'aliases:',
      '  - LV OCR',
      '  - order number OCR',
      'created: 2024-04-01',
      'updated: 2024-04-02',
      '---',
      '',
      '# LV order capture',
      '',
      'Body.',
    ].join('\n'), 'memory-vault/projects/lv.md', [])

    expect(result.frontmatter).not.toBeNull()
    expect(result.body).toContain('# LV order capture')
    expect(result.showName).toBe(false)
    expect(result.type).toBe('project')
    expect(result.status).toBe('in-discussion')
    expect(result.summary).toBe('Read the order number.')
    expect(result.tags).toEqual([])
    expect(result.created).toBe('2024-04-01')
    expect(result.updated).toBe('2024-04-02')
    expect(result.listExtras).toEqual([{
      key: 'aliases',
      items: [{ label: 'LV OCR, order number OCR', path: null }],
    }])
  })

  it('resolves related/link items and leaves unmatched or extra fields truthful', () => {
    const result = buildVaultDocumentMetadata([
      '---',
      'name: "  Separate label  "',
      'summary: ""',
      'notes: Secondary summary',
      'related:',
      '  - "../People/Mo.md|Moses"',
      '  - Missing note',
      '  - Common',
      'links:',
      '  - "[[../Projects/Plan.markdown]]"',
      'custom: [one, two]',
      'blank: ""',
      '---',
      '',
      '# A different heading',
    ].join('\n'), 'memory-vault/work/Current.md', [
      'memory-vault/People/Mo.md',
      'memory-vault/Projects/Plan.markdown',
      'memory-vault/People/Common.md',
      'memory-vault/Projects/Common.md',
    ])

    expect(result.name).toBe('  Separate label  ')
    expect(result.showName).toBe(true)
    expect(result.summary).toBe('Secondary summary')
    expect(result.listExtras).toEqual([
      {
        key: 'related',
        items: [
          { label: 'Moses', path: 'memory-vault/People/Mo.md' },
          { label: 'Missing note', path: null },
          { label: 'Common', path: null },
        ],
      },
      {
        key: 'links',
        items: [{ label: 'Plan.markdown', path: 'memory-vault/Projects/Plan.markdown' }],
      },
    ])
    expect(result.extraEntries).toEqual([{ key: 'custom', value: 'one, two' }])
  })

  it('handles absent frontmatter and title whitespace without inventing a card', () => {
    const plain = buildVaultDocumentMetadata('# Plain note', 'notes/plain.md', [])
    expect(plain.frontmatter).toBeNull()
    expect(plain.body).toBe('# Plain note')
    expect(plain.listExtras).toEqual([])
    expect(plain.extraEntries).toEqual([])

    const whitespaceTitle = buildVaultDocumentMetadata([
      '---',
      'title: "  Existing title  "',
      '---',
      '',
      '# Existing title',
    ].join('\n'), 'notes/other.md', [])
    expect(whitespaceTitle.showName).toBe(false)
  })

  it('resolves relative image paths and preserves explicit URLs and root paths', () => {
    expect(resolveVaultImageSrc('notes/current.md:12', '../images/cover.png'))
      .toBe('/api/workspace-image?path=images%2Fcover.png')
    for (const source of ['https://example.test/a.png', 'data:image/png;base64,abc', '//cdn.test/a.png', '/assets/a.png']) {
      expect(resolveVaultImageSrc('notes/current.md', source)).toBe(source)
    }
    expect(renderVaultMarkdown('![Cover](../images/cover.png)', 'notes/current.md', []))
      .toContain('src="/api/workspace-image?path=images%2Fcover.png"')
    expect(renderVaultMarkdown('![Unsafe](javascript:alert(1))', 'notes/current.md', []))
      .not.toContain('javascript:')
    expect(humanizeVaultMetadata('in-discussion')).toBe('In discussion')
    expect(isVaultMetadataUrl(' https://example.test/path ')).toBe(true)
    expect(isVaultMetadataUrl('javascript:alert(1)')).toBe(false)
  })
})
