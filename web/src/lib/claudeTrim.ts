// Pure helpers for the Settings "Claude context trimming" card. The engine owns
// the catalog (labels, defaults, locks); this module only reads it. The map is
// sparse and key-sorted because JSON.stringify of the workspace form is the dirty check.
import type { ClaudeTrimOption } from './types'

export type ClaudeTrimOverrides = Record<string, boolean>

// Groups of this size or more sit behind a disclosure instead of inline rows.
export const CLAUDE_TRIM_COLLAPSE_AT = 4

const GROUP_LABELS: Record<string, string> = {
  scheduling: 'Scheduling',
  plan: 'Plan mode',
  notifications: 'Notifications',
  design: 'Design',
  features: 'Features',
  review: 'Review',
  skills: 'Bundled skills',
}

export interface ClaudeTrimGroup {
  id: string
  label: string
  options: ClaudeTrimOption[]
  collapsible: boolean
}

export function claudeTrimGroupLabel(id: string): string {
  return GROUP_LABELS[id] ?? (id ? id.charAt(0).toUpperCase() + id.slice(1) : 'Other')
}

/** Keep only boolean entries; keys come out sorted so the form serializes stably. */
export function normalizeClaudeTrim(raw: unknown): ClaudeTrimOverrides {
  const out: ClaudeTrimOverrides = {}
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out
  const source = raw as Record<string, unknown>
  for (const key of Object.keys(source).sort()) {
    const value = source[key]
    if (typeof value === 'boolean') out[key] = value
  }
  return out
}

/** Locked options are always trimmed; otherwise the override wins over the default. */
export function isTrimmed(option: ClaudeTrimOption, overrides: ClaudeTrimOverrides): boolean {
  if (option.locked) return true
  return option.key in overrides ? overrides[option.key] : option.default_trimmed
}

/** A new normalized map. Only a difference from the default is stored. */
export function setTrimmed(overrides: ClaudeTrimOverrides, option: ClaudeTrimOption, trimmed: boolean): ClaudeTrimOverrides {
  if (option.locked) return normalizeClaudeTrim(overrides)
  const copy: ClaudeTrimOverrides = { ...overrides }
  if (trimmed === option.default_trimmed) delete copy[option.key]
  else copy[option.key] = trimmed
  return normalizeClaudeTrim(copy)
}

/** Groups in the order of their first option; options keep catalog order inside a group. */
export function groupClaudeTrimOptions(options: readonly ClaudeTrimOption[]): ClaudeTrimGroup[] {
  const groups = new Map<string, ClaudeTrimOption[]>()
  for (const option of options) {
    const list = groups.get(option.group)
    if (list) list.push(option)
    else groups.set(option.group, [option])
  }
  return [...groups].map(([id, list]) => ({
    id,
    label: claudeTrimGroupLabel(id),
    options: list,
    collapsible: list.length >= CLAUDE_TRIM_COLLAPSE_AT,
  }))
}

export function trimmedCount(options: readonly ClaudeTrimOption[], overrides: ClaudeTrimOverrides): number {
  return options.filter((option) => isTrimmed(option, overrides)).length
}

export function claudeTrimDomId(key: string): string {
  return `trim-${key.replace(/[^A-Za-z0-9_-]/g, '-')}`
}
