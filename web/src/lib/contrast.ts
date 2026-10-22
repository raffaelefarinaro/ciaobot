/** WCAG 2.x contrast helpers (hex colours only). */

export interface Rgb {
  r: number
  g: number
  b: number
}

/** Parse `#rgb` or `#rrggbb` (case-insensitive); null when the text is not a hex colour. */
export function parseHex(raw: string): Rgb | null {
  const text = raw.trim().toLowerCase()
  const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(text)
  if (!match) return null
  let digits = match[1]
  if (digits.length === 3) digits = digits.split('').map((c) => c + c).join('')
  return {
    r: parseInt(digits.slice(0, 2), 16),
    g: parseInt(digits.slice(2, 4), 16),
    b: parseInt(digits.slice(4, 6), 16),
  }
}

export function toHex({ r, g, b }: Rgb): string {
  return '#' + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, '0')).join('')
}

export function relativeLuminance({ r, g, b }: Rgb): number {
  const channel = (v: number): number => {
    const s = v / 255
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4)
  }
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

/** Contrast ratio of two hex colours, 1 to 21. Throws on a value that is not a hex colour. */
export function contrastRatio(a: string, b: string): number {
  const first = parseHex(a)
  const second = parseHex(b)
  if (!first || !second) throw new Error(`not a hex colour: ${!first ? a : b}`)
  const la = relativeLuminance(first)
  const lb = relativeLuminance(second)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}
