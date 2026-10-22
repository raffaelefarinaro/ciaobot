/**
 * WCAG 2.x contrast maths and the derivation of a custom colour scheme.
 *
 * Pure helpers (no Vue): the Custom colours dialog uses them to refuse an
 * unreadable pair, and `applyAppearanceToRoot` uses them to apply only a pair
 * that still passes. The server validates hex shape only; this is the one
 * implementation of the readability rule.
 */

export const AA_BODY = 4.5

export interface Rgb { r: number; g: number; b: number }
export type ThemeMode = 'dark' | 'light'
/** `#rrggbb`, lowercase. */
export interface ColorPair { bg: string; fg: string }
export interface CustomTokens {
  '--bg': string
  '--bg2': string
  '--bg3': string
  '--bg-elev': string
  '--fg': string
  '--fg2': string
  '--fg3': string
  '--border': string
  '--border-strong': string
}

/** `#rgb` or `#rrggbb`, case-insensitive; anything else is `null`. */
export function parseHex(raw: string): Rgb | null {
  const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(raw.trim())
  if (!match) return null
  let hex = match[1]
  if (hex.length === 3) hex = [...hex].map((c) => c + c).join('')
  return {
    r: parseInt(hex.slice(0, 2), 16),
    g: parseInt(hex.slice(2, 4), 16),
    b: parseInt(hex.slice(4, 6), 16),
  }
}

function channelHex(value: number): string {
  const clamped = Math.min(255, Math.max(0, Math.round(value)))
  return clamped.toString(16).padStart(2, '0')
}

export function toHex(c: Rgb): string {
  return `#${channelHex(c.r)}${channelHex(c.g)}${channelHex(c.b)}`
}

export function relativeLuminance(c: Rgb): number {
  const lin = (v: number): number => {
    const s = v / 255
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b)
}

function parseOrThrow(hex: string): Rgb {
  const rgb = parseHex(hex)
  if (!rgb) throw new Error(`Not a hex colour: ${hex}`)
  return rgb
}

/** Contrast ratio of two hex colours, at least 1. Throws on an unparsable one. */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(parseOrThrow(a))
  const lb = relativeLuminance(parseOrThrow(b))
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la]
  return (hi + 0.05) / (lo + 0.05)
}

/** `t` of `b` mixed into `a`, channel-wise in sRGB. */
export function mix(a: string, b: string, t: number): string {
  const ca = parseOrThrow(a)
  const cb = parseOrThrow(b)
  return toHex({
    r: ca.r + (cb.r - ca.r) * t,
    g: ca.g + (cb.g - ca.g) * t,
    b: ca.b + (cb.b - ca.b) * t,
  })
}

/** Secondary text: fg faded toward bg by at most `start`, kept at AA on bg. */
function fadedText(fg: string, bg: string, start: number): string {
  for (let step = Math.round(start * 100); step > 0; step -= 5) {
    const candidate = mix(fg, bg, step / 100)
    if (contrastRatio(candidate, bg) >= AA_BODY) return candidate
  }
  return fg
}

/** Surfaces, borders and secondary text derived from the user's pair. */
export function deriveCustomTokens(pair: ColorPair): CustomTokens {
  const { bg, fg } = pair
  return {
    '--bg': bg,
    '--bg2': mix(bg, fg, 0.05),
    '--bg3': mix(bg, fg, 0.12),
    '--bg-elev': mix(bg, fg, 0.08),
    '--fg': fg,
    '--fg2': fadedText(fg, bg, 0.25),
    '--fg3': fadedText(fg, bg, 0.45),
    '--border': mix(bg, fg, 0.16),
    '--border-strong': mix(bg, fg, 0.24),
  }
}

export type PairCheck =
  | { ok: true; tokens: CustomTokens; ratio: number }
  | { ok: false; reason: string; ratio: number | null }

/** Whether body text in `pair` is readable on the background and on hover surfaces. */
export function checkCustomPair(pair: ColorPair): PairCheck {
  const bg = parseHex(pair.bg)
  const fg = parseHex(pair.fg)
  if (!bg || !fg) return { ok: false, reason: 'Enter colours like #1a1a2e.', ratio: null }
  const normal: ColorPair = { bg: toHex(bg), fg: toHex(fg) }
  const ratio = contrastRatio(normal.fg, normal.bg)
  if (ratio < AA_BODY) {
    const hint = relativeLuminance(fg) > relativeLuminance(bg)
      ? 'Make the text lighter or the background darker.'
      : 'Make the text darker or the background lighter.'
    return {
      ok: false,
      reason: `Text and background have a contrast of ${ratio.toFixed(1)}:1; body text needs at least 4.5:1 to be readable. ${hint}`,
      ratio,
    }
  }
  const tokens = deriveCustomTokens(normal)
  if (contrastRatio(normal.fg, tokens['--bg3']) < AA_BODY) {
    return {
      ok: false,
      reason: 'Text would be hard to read on hovered and raised surfaces. Increase the contrast a little.',
      ratio,
    }
  }
  return { ok: true, tokens, ratio }
}
