import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import sharp from 'sharp'

function pixel(image: { data: Uint8Array; info: { width: number } }, x: number, y: number) {
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

})
