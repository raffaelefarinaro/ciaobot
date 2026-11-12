import { describe, expect, it } from 'vitest'
import {
  claudeTrimDomId,
  claudeTrimGroupLabel,
  groupClaudeTrimOptions,
  isTrimmed,
  normalizeClaudeTrim,
  setTrimmed,
  trimmedCount,
} from './claudeTrim'
import type { ClaudeTrimKind, ClaudeTrimOption } from './types'

const opt = (o: Partial<ClaudeTrimOption> & { key: string }): ClaudeTrimOption => ({
  kind: o.key.split(':')[0] as ClaudeTrimKind,
  name: o.key.split(':')[1],
  label: o.key,
  reason: `${o.key} reason`,
  group: 'features',
  default_trimmed: false,
  locked: false,
  ...o,
})

const locked = opt({ key: 'tool:CronCreate', group: 'scheduling', default_trimmed: true, locked: true })
const notebook = opt({ key: 'tool:NotebookEdit', group: 'features', default_trimmed: true })
const findings = opt({ key: 'tool:ReportFindings', group: 'review', default_trimmed: false })
const skills = [
  opt({ key: 'skill:a', group: 'skills', default_trimmed: true }),
  opt({ key: 'skill:b', group: 'skills', default_trimmed: true }),
  opt({ key: 'skill:c', group: 'skills', default_trimmed: false }),
  opt({ key: 'skill:d', group: 'skills', default_trimmed: false }),
]
const workflows = opt({ key: 'setting:disableWorkflows', kind: 'setting', group: 'features', default_trimmed: false })
const catalog = [locked, notebook, findings, ...skills, workflows]

describe('claude trim helpers', () => {
  it('isTrimmed reads the override, else the default, and locked is always trimmed', () => {
    expect(isTrimmed(notebook, {})).toBe(true)
    expect(isTrimmed(notebook, { 'tool:NotebookEdit': false })).toBe(false)
    expect(isTrimmed(locked, { 'tool:CronCreate': false })).toBe(true)
  })

  it('setTrimmed writes only differences from the default', () => {
    const off = setTrimmed({}, notebook, false)
    expect(off).toEqual({ 'tool:NotebookEdit': false })
    expect(setTrimmed(off, notebook, true)).toEqual({})
    expect(setTrimmed({}, findings, true)).toEqual({ 'tool:ReportFindings': true })
  })

  it('setTrimmed ignores locked options and never mutates', () => {
    const m = { 'tool:ReportFindings': true }
    expect(setTrimmed(m, locked, false)).toEqual(m)
    setTrimmed(m, findings, false)
    expect(m).toEqual({ 'tool:ReportFindings': true })
  })

  it('normalizeClaudeTrim keeps booleans and sorts keys', () => {
    expect(Object.keys(normalizeClaudeTrim({ b: true, a: false, c: 'x' }))).toEqual(['a', 'b'])
    expect(normalizeClaudeTrim(null)).toEqual({})
    expect(normalizeClaudeTrim([])).toEqual({})
    expect(normalizeClaudeTrim('x')).toEqual({})
  })

  it('groupClaudeTrimOptions keeps catalog order and merges late options into their group', () => {
    const groups = groupClaudeTrimOptions(catalog)
    expect(groups.map((g) => g.id)).toEqual(['scheduling', 'features', 'review', 'skills'])
    expect(groups[1].options.map((o) => o.key)).toEqual(['tool:NotebookEdit', 'setting:disableWorkflows'])
    expect(groups[3].collapsible).toBe(true)
    expect(groups[1].collapsible).toBe(false)
    expect(groups[3].label).toBe('Bundled skills')
  })

  it('claudeTrimGroupLabel falls back to a capitalised id', () => {
    expect(claudeTrimGroupLabel('extras')).toBe('Extras')
  })

  it('trimmedCount counts effective trims', () => {
    expect(trimmedCount(skills, {})).toBe(2)
    expect(trimmedCount(skills, { 'skill:c': true })).toBe(3)
  })

  it('claudeTrimDomId makes a safe id', () => {
    expect(claudeTrimDomId('tool:NotebookEdit')).toBe('trim-tool-NotebookEdit')
  })
})
