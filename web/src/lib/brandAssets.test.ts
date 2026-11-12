import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import sharp from 'sharp'

const brand = resolve(process.cwd(), 'public/brand')

async function pixels(name: string) {
  return sharp(resolve(brand, name)).ensureAlpha().raw().toBuffer({ resolveWithObject: true })
}

function pixel(image: Awaited<ReturnType<typeof pixels>>, x: number, y: number) {
  const index = (y * image.info.width + x) * 4
  return [...image.data.subarray(index, index + 4)]
}

describe('small brand mark', () => {
  it('keeps the supplied artwork and removes the black corners', async () => {
    const source = await sharp(resolve(process.cwd(), 'brand/ciaobot-icon-source.png'))
      .ensureAlpha().resize(1024, 1024).raw().toBuffer({ resolveWithObject: true })
    const icon = await sharp(resolve(process.cwd(), 'public/favicon.png'))
      .ensureAlpha().raw().toBuffer({ resolveWithObject: true })
    for (let y = 205; y < 790; y++) {
      const start = (y * 1024 + 215) * 4
      expect(icon.data.subarray(start, start + 580 * 4).equals(source.data.subarray(start, start + 580 * 4))).toBe(true)
    }
    expect(pixel(icon, 0, 0)[3]).toBeLessThan(5)
  })

  it('extracts the tilted C and both dots into separate theme masks', async () => {
    const ink = await pixels('ciaobot-mark-ink.png')
    const dots = await pixels('ciaobot-mark-dots.png')
    for (const [x, y] of [[335, 620], [715, 320], [765, 873]]) {
      expect(pixel(ink, x - 260, y - 250)[3]).toBeGreaterThan(240)
      expect(pixel(dots, x - 260, y - 250)[3]).toBe(0)
    }
    for (const [x, y] of [[870, 346], [863, 800]]) {
      expect(pixel(dots, x - 260, y - 250)[3]).toBeGreaterThan(200)
      expect(pixel(ink, x - 260, y - 250)[3]).toBe(0)
    }
  })
})
