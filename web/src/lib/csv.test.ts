import { describe, expect, test } from 'vitest'
import { isCsvPath, parseCsv, serializeCsv } from './csv'

describe('isCsvPath', () => {
  test('matches csv extensions and strips line suffixes', () => {
    expect(isCsvPath('wedding-guests.csv')).toBe(true)
    expect(isCsvPath('memory-vault/a/b.CSV')).toBe(true)
    expect(isCsvPath('guests.csv:12')).toBe(true)
    expect(isCsvPath('notes.md')).toBe(false)
  })
})

describe('parseCsv / serializeCsv', () => {
  test('parses headered csv and round-trips', () => {
    const text = 'name,side,status\nAda,Bride,Yes\nBob,Groom,"To do"\n'
    const table = parseCsv(text)
    expect(table.hasHeader).toBe(true)
    expect(table.headers).toEqual(['name', 'side', 'status'])
    expect(table.rows).toEqual([
      ['Ada', 'Bride', 'Yes'],
      ['Bob', 'Groom', 'To do'],
    ])
    const out = serializeCsv(table)
    expect(parseCsv(out).rows).toEqual(table.rows)
    expect(parseCsv(out).headers).toEqual(table.headers)
  })

  test('handles quoted commas and newlines', () => {
    const text = 'name,note\n"Ada, Jr.","line1\nline2"\n'
    const table = parseCsv(text)
    expect(table.rows[0]).toEqual(['Ada, Jr.', 'line1\nline2'])
    const again = parseCsv(serializeCsv(table))
    expect(again.rows[0]).toEqual(['Ada, Jr.', 'line1\nline2'])
  })

  test('synthesizes headers when the first row looks like data', () => {
    const text = '1,2,3\n4,5,6\n'
    const table = parseCsv(text)
    expect(table.hasHeader).toBe(false)
    expect(table.headers).toEqual(['Column 1', 'Column 2', 'Column 3'])
    expect(table.rows).toEqual([
      ['1', '2', '3'],
      ['4', '5', '6'],
    ])
    const out = serializeCsv(table)
    expect(out.trim()).toBe('1,2,3\n4,5,6')
  })

  test('pads short rows to rectangular width', () => {
    const table = parseCsv('a,b,c\n1,2\n')
    expect(table.rows[0]).toEqual(['1', '2', ''])
  })
})
