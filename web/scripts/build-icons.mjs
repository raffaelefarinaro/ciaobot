#!/usr/bin/env node
// Generate favicon + PWA icon variants from a single source image.
// Source: web/public/favicon.png (square, ideally >= 1024x1024).
// Output: web/public/icons/icon-{16,32,180,192,512}.png
//         web/public/icons/icon-{192,512}-maskable.png (with safe-zone padding)
//
// Maskable icons need ~10% padding on each side so adaptive masks don't clip
// the mark. We composite the source onto a solid background matching the PWA
// theme color (#1a1a2e) at the inner safe zone (80%).
//
// IMPORTANT: This script is NOT wired into `npm run build` on purpose.
// `sharp`'s PNG encoder is not byte-deterministic across platforms (the
// libvips/libpng versions bundled for macOS and Linux produce different
// compression metadata for the same pixels). Running this on each build
// creates a 1-byte ping-pong diff that the local<->cloud sync hooks then
// auto-commit forever. Run `npm run build:icons` manually only when
// `web/public/favicon.png` actually changes, then commit the result once
// (preferably from the same machine each time, e.g. always the cloud box).

import { mkdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import sharp from 'sharp'

const __dirname = dirname(fileURLToPath(import.meta.url))
const webRoot = resolve(__dirname, '..')
const SRC = resolve(webRoot, 'public/favicon.png')
const OUT = resolve(webRoot, 'public/icons')
const BG = { r: 0xf9, g: 0x73, b: 0x16, alpha: 1 }

const ANY_SIZES = [16, 32, 180, 192, 512]
const MASKABLE_SIZES = [192, 512]
// Safe zone for maskable icons: inner content fits within 80% of canvas.
const SAFE_ZONE = 0.8

async function main() {
  if (!existsSync(SRC)) {
    console.error(`[build-icons] source not found: ${SRC}`)
    process.exit(1)
  }
  await mkdir(OUT, { recursive: true })

  for (const size of ANY_SIZES) {
    const out = resolve(OUT, `icon-${size}.png`)
    await sharp(SRC)
      .resize(size, size, { fit: 'cover' })
      .png({ compressionLevel: 9 })
      .toFile(out)
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
