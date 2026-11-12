#!/usr/bin/env node
// Generate favicon + PWA icon variants from the brand app icon.
// Source: web/brand/ciaobot-icon-source.png (the supplied artwork).
// Output: web/public/brand/ciaobot-mark-{ink,dots}.png (transparent masks)
// Output: web/public/favicon.png (1024, transparent corners)
//         web/public/icons/icon-{16,32,180,192,512}.png
//         web/public/icons/icon-{192,512}-maskable.png (with safe-zone padding)
//         site/assets/img/{favicon,logo}.png (32 and 192, transparent corners)
//
// Maskable icons need ~10% padding on each side so adaptive masks don't clip
// the mark. We composite the source onto the tile's cream (BG) at the inner
// safe zone (80%).
//
// The larger "any" icons get that same background flattened in. They are what
// macOS/iOS use for Web Push notifications, the home screen and the installed
// app, and those surfaces composite transparent corners onto plain white (or
// black on iOS) and apply their own mask. The 16/32 favicons keep the rounded
// tile with transparent corners, which is conventional in a browser tab.
//
// IMPORTANT: This script is NOT wired into `npm run build` on purpose.
// `sharp`'s PNG encoder is not byte-deterministic across platforms (the
// libvips/libpng versions bundled for macOS and Linux produce different
// compression metadata for the same pixels). Running this on each build
// creates a 1-byte ping-pong diff that the local<->cloud sync hooks then
// auto-commit forever. Run `npm run build:icons` manually only when
// `web/brand/ciaobot-icon-source.png` actually changes, then commit the result once
// (preferably from the same machine each time, e.g. always the cloud box).

import { mkdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import sharp from 'sharp'

const __dirname = dirname(fileURLToPath(import.meta.url))
const webRoot = resolve(__dirname, '..')
const BRAND = resolve(webRoot, 'public/brand')
const SOURCE = resolve(webRoot, 'brand/ciaobot-icon-source.png')
const FAVICON = resolve(webRoot, 'public/favicon.png')
const OUT = resolve(webRoot, 'public/icons')
const SITE_IMG = resolve(webRoot, '../site/assets/img')
// The tile's cream.
const BG = { r: 0xf8, g: 0xf4, b: 0xef, alpha: 1 }
// Site favicon and standalone icon: [file, size].
const SITE_SIZES = [['favicon.png', 32], ['logo.png', 192]]

const ANY_SIZES = [16, 32, 180, 192, 512]
// "any" sizes that land on an opaque OS surface and therefore need BG baked in.
const OPAQUE_ANY_SIZES = new Set([180, 192, 512])
const MASKABLE_SIZES = [192, 512]
// Safe zone for maskable icons: inner content fits within 80% of canvas.
const SAFE_ZONE = 0.8

async function main() {
  if (!existsSync(SOURCE)) {
    console.error(`[build-icons] source not found: ${SOURCE}`)
    process.exit(1)
  }
  await mkdir(OUT, { recursive: true })

  // The reference has black outside its rounded tile; remove only those corner
  // pixels, keeping the supplied artwork and its antialiased edges intact.
  const { data, info } = await sharp(SOURCE).ensureAlpha().raw().toBuffer({ resolveWithObject: true })
  if (info.width !== 1254 || info.height !== 1254) throw new Error('Expected the 1254×1254 brand reference')
  const ink = Buffer.alloc(720 * 720 * 4)
  const dots = Buffer.alloc(720 * 720 * 4)
  const alpha = n => Math.round(Math.max(0, Math.min(1, n)) * 255)
  for (let y = 0; y < info.height; y++) {
    for (let x = 0; x < info.width; x++) {
      const i = (y * info.width + x) * 4
      const [r, g, b] = data.subarray(i, i + 3)
      if ((x < 250 || x >= 1004) && (y < 250 || y >= 1004)) {
        const level = (r + g + b) / 3
        if (level < 225) {
          data[i] = 248
          data[i + 1] = 244
          data[i + 2] = 239
          data[i + 3] = alpha(level / 245)
        }
      }
      if (x >= 260 && x < 980 && y >= 250 && y < 970) {
        const j = ((y - 250) * 720 + x - 260) * 4
        ink[j] = ink[j + 1] = ink[j + 2] = 255
        dots[j] = dots[j + 1] = dots[j + 2] = 255
        ink[j + 3] = g - r > 3 ? alpha((245 - (r + g + b) / 3) / 205) : 0
        dots[j + 3] = r - b > 4 ? alpha((245 - (r + g + b) / 3) / 100) : 0
      }
    }
  }
  await sharp(ink, { raw: { width: 720, height: 720, channels: 4 } }).png().toFile(resolve(BRAND, 'ciaobot-mark-ink.png'))
  await sharp(dots, { raw: { width: 720, height: 720, channels: 4 } }).png().toFile(resolve(BRAND, 'ciaobot-mark-dots.png'))
  const SRC = await sharp(data, { raw: info }).resize(1024, 1024).png().toBuffer()
  await sharp(SRC).png({ compressionLevel: 9 }).toFile(FAVICON)
  console.log(`[build-icons] wrote ${FAVICON}`)

  for (const [name, size] of SITE_SIZES) {
    const out = resolve(SITE_IMG, name)
    await sharp(SRC).resize(size, size).png({ compressionLevel: 9 }).toFile(out)
    console.log(`[build-icons] wrote ${out}`)
  }

  for (const size of ANY_SIZES) {
    const out = resolve(OUT, `icon-${size}.png`)
    const pipeline = sharp(SRC).resize(size, size, { fit: 'cover' })
    if (OPAQUE_ANY_SIZES.has(size)) {
      pipeline.flatten({ background: BG })
    }
    await pipeline.png({ compressionLevel: 9 }).toFile(out)
    console.log(`[build-icons] wrote ${out}`)
  }

  for (const size of MASKABLE_SIZES) {
    const inner = Math.round(size * SAFE_ZONE)
    const offset = Math.round((size - inner) / 2)
    const resized = await sharp(SRC)
      .resize(inner, inner, { fit: 'contain', background: BG })
      .png()
      .toBuffer()
    const out = resolve(OUT, `icon-${size}-maskable.png`)
    await sharp({
      create: { width: size, height: size, channels: 4, background: BG },
    })
      .composite([{ input: resized, top: offset, left: offset }])
      .png({ compressionLevel: 9 })
      .toFile(out)
    console.log(`[build-icons] wrote ${out}`)
  }
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
