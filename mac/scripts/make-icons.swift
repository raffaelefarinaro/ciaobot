#!/usr/bin/env swift
import AppKit
import CryptoKit
import Foundation

// Builds the client's macOS artwork from the approved Ciaobot icon export.
//
// Input (checked in, never read from the engine at build time):
//   Sources/CiaobotMac/Artwork/app-icon-source.png
//     the supplied 1254x1254 brand export: the cream tile with the C mark,
//     flattened onto black outside the rounded tile.
//
// Nothing here redraws the mark. The supplied pixels are the mark, so the C
// keeps the exact shape that was approved; this script only:
//   1. turns the black surround into transparency (the export's edge pixels are
//      cream blended with black, so it divides that blend back out),
//   2. places that artwork on Apple's macOS app-icon grid — a 1024 canvas with
//      an 824x824 rounded tile at (100,100), 185.4 corner radius,
//   3. derives coverage masks for the runtime icons from the composited
//      artwork's own pixels, and
//   4. resamples the ink mask for the 18 pt menu-bar template.
//
// Outputs:
//   Sources/CiaobotMac/Resources/app-icon.png
//     The static Finder, Launchpad and Dock icon.
//   Sources/CiaobotMac/Resources/dock-icon-ink.png
//     Coverage of the C stroke and both dots. The running app replaces the
//     cream field around it with the active workspace accent.
//   Sources/CiaobotMac/Resources/dock-icon-dots.png
//     Coverage of the two dots. On an accent field the running app paints them
//     in the brand cream, because brand orange on the amber field disappears.
//   Sources/CiaobotMac/Resources/mark_template.png
//     The mark alone in black-plus-alpha for the menu-bar item, where macOS
//     applies the light/dark system tint.
//
// Usage:
//   swift scripts/make-icons.swift [source.png] [app-icon.png] [ink.png] [dots.png] [template.png]
//
// The app and the release build never run this script; regenerate only when
// the brand artwork changes.

let scriptDirectory = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
let repositoryRoot = scriptDirectory.deletingLastPathComponent()
let resourcesDirectory = repositoryRoot.appendingPathComponent("Sources/CiaobotMac/Resources")
let defaultSource = repositoryRoot.appendingPathComponent("Sources/CiaobotMac/Artwork/app-icon-source.png")
let arguments = Array(CommandLine.arguments.dropFirst())
let sourceURL = arguments.count > 0 ? URL(fileURLWithPath: arguments[0]) : defaultSource
let appIconURL = arguments.count > 1
    ? URL(fileURLWithPath: arguments[1])
    : resourcesDirectory.appendingPathComponent("app-icon.png")
let inkURL = arguments.count > 2
    ? URL(fileURLWithPath: arguments[2])
    : resourcesDirectory.appendingPathComponent("dock-icon-ink.png")
let dotsURL = arguments.count > 3
    ? URL(fileURLWithPath: arguments[3])
    : resourcesDirectory.appendingPathComponent("dock-icon-dots.png")
let templateURL = arguments.count > 4
    ? URL(fileURLWithPath: arguments[4])
    : resourcesDirectory.appendingPathComponent("mark_template.png")

let canvasSize = 1024
let tileSize: CGFloat = 824
let tileInset = (CGFloat(canvasSize) - tileSize) / 2
let cornerRadius: CGFloat = 185.4
let templatePixels = 36
/// Below this coverage a difference from the field is export noise, not ink.
let inkThreshold = 0.06
/// The export blends the tile edge with black; anything darker is surround.
let blackLevel = 0.02

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("\(message)\n".utf8))
    exit(1)
}

struct Pixel {
    var red: Double
    var green: Double
    var blue: Double
    var alpha: Double

    var maxChannel: Double { Swift.max(red, Swift.max(green, blue)) }
}

/// A straight sRGB RGBA pixel buffer. CoreGraphics owns the bytes, so every
/// read and write below is in one colour space, independent of the display.
final class Canvas {
    let width: Int
    let height: Int
    let context: CGContext

    init(pixels: Int) {
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let context = CGContext(
                data: nil,
                width: pixels,
                height: pixels,
                bitsPerComponent: 8,
                bytesPerRow: pixels * 4,
                space: colorSpace,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
              ), let data = context.data else {
            fail("Could not allocate a \(pixels)px canvas")
        }
        self.width = pixels
        self.height = pixels
        self.context = context
        self.bytes = data.assumingMemoryBound(to: UInt8.self)
        context.clear(CGRect(x: 0, y: 0, width: pixels, height: pixels))
        context.setShouldAntialias(true)
        context.interpolationQuality = .high
    }

    let bytes: UnsafeMutablePointer<UInt8>

    func read(_ x: Int, _ y: Int) -> Pixel {
        let offset = (y * width + x) * 4
        let alpha = Double(bytes[offset + 3]) / 255
        guard alpha > 0 else { return Pixel(red: 0, green: 0, blue: 0, alpha: 0) }
        return Pixel(
            red: Double(bytes[offset]) / 255 / alpha,
            green: Double(bytes[offset + 1]) / 255 / alpha,
            blue: Double(bytes[offset + 2]) / 255 / alpha,
            alpha: alpha
        )
    }

    func write(_ pixel: Pixel, at index: Int) {
        let offset = index * 4
        bytes[offset] = UInt8((pixel.red * pixel.alpha * 255).rounded())
        bytes[offset + 1] = UInt8((pixel.green * pixel.alpha * 255).rounded())
        bytes[offset + 2] = UInt8((pixel.blue * pixel.alpha * 255).rounded())
        bytes[offset + 3] = UInt8((pixel.alpha * 255).rounded())
    }

    func image() -> CGImage {
        guard let image = context.makeImage() else { fail("Could not read the canvas") }
        return image
    }

    func pngData() -> Data {
        let rep = NSBitmapImageRep(cgImage: image())
        guard let data = rep.representation(using: .png, properties: [:]) else {
            fail("Could not encode the canvas")
        }
        return data
    }
}

func writePNG(_ canvas: Canvas, to url: URL, label: String) {
    let data = canvas.pngData()
    do {
        try data.write(to: url, options: .atomic)
    } catch {
        fail("Could not write \(url.path): \(error)")
    }
    let hash = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    print("Wrote \(url.path)")
    print("  \(label) sha256 \(hash)")
}

/// Decodes any image into straight sRGB RGBA. The supplied export is opaque,
/// so the result matches its bytes exactly.
func decode(_ url: URL) -> Canvas {
    guard let image = NSImage(contentsOf: url),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        fail("Could not read \(url.path)")
    }
    guard cgImage.width == cgImage.height, cgImage.width > 0 else {
        fail("The supplied artwork must be a square image")
    }
    let canvas = Canvas(pixels: cgImage.width)
    canvas.context.draw(cgImage, in: CGRect(x: 0, y: 0, width: cgImage.width, height: cgImage.height))
    return canvas
}

// --- 1. The supplied artwork, black surround removed ----------------------

let source = decode(sourceURL)
let width = source.width
let height = source.height
var artwork = (0..<(width * height)).map { index in
    source.read(index % width, index / width)
}

// The export is flattened onto black. The tile is the only region that is not
// black, so the surround is the black component connected to the border, and
// the ring next to it is cream blended with black: divide it back out.
var exterior = [Bool](repeating: false, count: width * height)
var stack: [Int] = []
for x in 0..<width {
    stack.append(x)
    stack.append((height - 1) * width + x)
}
for y in 0..<height {
    stack.append(y * width)
    stack.append(y * width + width - 1)
}
while let index = stack.popLast() {
    guard !exterior[index], artwork[index].maxChannel <= blackLevel else { continue }
    exterior[index] = true
    let x = index % width
    let y = index / width
    if x > 0 { stack.append(index - 1) }
    if x + 1 < width { stack.append(index + 1) }
    if y > 0 { stack.append(index - width) }
    if y + 1 < height { stack.append(index + width) }
}

// A field patch left of the mark gives the tile's own cream and its brightness.
var fieldSamples: [Pixel] = []
var fieldBrightness = 0.0
for y in stride(from: height / 2 - 40, through: height / 2 + 40, by: 1) {
    for x in stride(from: 120, through: 220, by: 1) {
        let index = y * width + x
        guard !exterior[index] else { continue }
        let pixel = artwork[index]
        fieldSamples.append(pixel)
        fieldBrightness = Swift.max(fieldBrightness, pixel.maxChannel)
    }
}
guard fieldBrightness > 0.5, !fieldSamples.isEmpty else { fail("Could not find the tile field") }

for index in 0..<(width * height) where exterior[index] {
    artwork[index] = Pixel(red: 0, green: 0, blue: 0, alpha: 0)
}

// The export's soft edge is a few pixels wide, and the mark is nowhere near
// it, so every pixel within this band of the surround is a cream/black blend.
let edgeBand = 8
var band = [Bool](repeating: false, count: width * height)
var frontier: [Int] = (0..<(width * height)).filter { exterior[$0] }
for _ in 0..<edgeBand {
    var next: [Int] = []
    for index in frontier {
        let x = index % width
        let y = index / width
        var neighbours: [Int] = []
        if x > 0 { neighbours.append(index - 1) }
        if x + 1 < width { neighbours.append(index + 1) }
        if y > 0 { neighbours.append(index - width) }
        if y + 1 < height { neighbours.append(index + width) }
        for neighbour in neighbours where !exterior[neighbour] && !band[neighbour] {
            band[neighbour] = true
            next.append(neighbour)
        }
    }
    frontier = next
}

for index in 0..<(width * height) where band[index] {
    let coverage = Swift.min(Swift.max(artwork[index].maxChannel / fieldBrightness, 0), 1)
    if coverage < 0.02 {
        artwork[index] = Pixel(red: 0, green: 0, blue: 0, alpha: 0)
    } else {
        artwork[index] = Pixel(
            red: Swift.min(artwork[index].red / coverage, 1),
            green: Swift.min(artwork[index].green / coverage, 1),
            blue: Swift.min(artwork[index].blue / coverage, 1),
            alpha: coverage
        )
    }
}

func median(_ values: [Double]) -> Double {
    guard !values.isEmpty else { return 0 }
    let sorted = values.sorted()
    return sorted[sorted.count / 2]
}
// The field colour is the export's own, so the icon matches the supplied image.
let fieldColour = Pixel(
    red: median(fieldSamples.map(\.red)),
    green: median(fieldSamples.map(\.green)),
    blue: median(fieldSamples.map(\.blue)),
    alpha: 1
)

// --- 2. The app icon on Apple's macOS grid --------------------------------

let appIcon = Canvas(pixels: canvasSize)
let tileRect = CGRect(x: tileInset, y: tileInset, width: tileSize, height: tileSize)
// The tile is the only opaque region. Everything outside it stays
// transparent, which is what gives Finder and the Dock the rounded shape.
appIcon.context.addPath(CGPath(
    roundedRect: tileRect,
    cornerWidth: cornerRadius,
    cornerHeight: cornerRadius,
    transform: nil
))
appIcon.context.clip()
appIcon.context.setFillColor(CGColor(
    srgbRed: fieldColour.red,
    green: fieldColour.green,
    blue: fieldColour.blue,
    alpha: 1
))
appIcon.context.fill(tileRect)

let artworkCanvas = Canvas(pixels: width)
for index in 0..<(width * height) {
    artworkCanvas.write(artwork[index], at: index)
}
appIcon.context.draw(artworkCanvas.image(), in: tileRect)
writePNG(appIcon, to: appIconURL, label: "app-icon.png")

// --- 3. Masks from the app icon's own pixels ------------------------------

func difference(_ pixel: Pixel, _ other: Pixel) -> (red: Double, green: Double, blue: Double) {
    (pixel.red - other.red, pixel.green - other.green, pixel.blue - other.blue)
}

func distance(_ pixel: Pixel, _ other: Pixel) -> Double {
    let v = difference(pixel, other)
    return (v.red * v.red + v.green * v.green + v.blue * v.blue).squareRoot()
}

// The two ink colours, as cluster averages of the supplied artwork: the C is
// green-dominant and the dots are red-dominant.
var greenSum = (red: 0.0, green: 0.0, blue: 0.0, count: 0.0)
var orangeSum = (red: 0.0, green: 0.0, blue: 0.0, count: 0.0)
for y in 0..<canvasSize {
    for x in 0..<canvasSize {
        let pixel = appIcon.read(x, y)
        guard pixel.alpha > 0.99, distance(pixel, fieldColour) > 0.35 else { continue }
        if pixel.red - pixel.green > 0.1 {
            orangeSum.red += pixel.red
            orangeSum.green += pixel.green
            orangeSum.blue += pixel.blue
            orangeSum.count += 1
        } else if pixel.green - pixel.blue > 0.02 {
            greenSum.red += pixel.red
            greenSum.green += pixel.green
            greenSum.blue += pixel.blue
            greenSum.count += 1
        }
    }
}
guard greenSum.count > 100, orangeSum.count > 100 else {
    fail("The artwork has no recognisable C stroke and dots")
}
let inkColour = Pixel(
    red: greenSum.red / greenSum.count,
    green: greenSum.green / greenSum.count,
    blue: greenSum.blue / greenSum.count,
    alpha: 1
)
let dotColour = Pixel(
    red: orangeSum.red / orangeSum.count,
    green: orangeSum.green / orangeSum.count,
    blue: orangeSum.blue / orangeSum.count,
    alpha: 1
)

/// How much of `pixel` is `ink` over the field, and how well that explains it.
/// The export is antialiased, so a blended edge pixel projects onto the ink
/// vector; the residual says which ink the pixel belongs to (C or dot).
func coverage(_ pixel: Pixel, over ink: Pixel) -> (amount: Double, residual: Double) {
    let v = difference(pixel, fieldColour)
    let w = difference(ink, fieldColour)
    let denominator = w.red * w.red + w.green * w.green + w.blue * w.blue
    guard denominator > 0 else { return (0, .infinity) }
    let amount = (v.red * w.red + v.green * w.green + v.blue * w.blue) / denominator
    let residualRed = v.red - amount * w.red
    let residualGreen = v.green - amount * w.green
    let residualBlue = v.blue - amount * w.blue
    let residual = (residualRed * residualRed
        + residualGreen * residualGreen
        + residualBlue * residualBlue).squareRoot()
    return (amount, residual)
}

let inkMask = Canvas(pixels: canvasSize)
let dotsMask = Canvas(pixels: canvasSize)
for y in 0..<canvasSize {
    for x in 0..<canvasSize {
        let pixel = appIcon.read(x, y)
        // The 8-bit premultiplied round-trip loses colour in near-transparent
        // pixels, so only the opaque tile interior can carry ink.
        guard pixel.alpha > 0.5 else { continue }
        let green = coverage(pixel, over: inkColour)
        let orange = coverage(pixel, over: dotColour)
        let isDot = orange.residual < green.residual
        var amount = Swift.min(Swift.max(isDot ? orange.amount : green.amount, 0), 1)
        guard amount >= inkThreshold else { continue }
        amount = (amount - inkThreshold) / (1 - inkThreshold)
        let alpha = UInt8((amount * 255).rounded())
        let index = y * canvasSize + x
        let inkOffset = index * 4
        inkMask.bytes[inkOffset + 3] = alpha
        if isDot { dotsMask.bytes[index * 4 + 3] = alpha }
    }
}
writePNG(inkMask, to: inkURL, label: "dock-icon-ink.png")
writePNG(dotsMask, to: dotsURL, label: "dock-icon-dots.png")

// --- 4. The 18 pt menu-bar template --------------------------------------

// The ink mask is the size of the whole app icon, where the mark fills only
// about 44% of the canvas. A menu-bar item must fill its box, so crop to the
// mark's own bounds and scale it uniformly: that keeps the supplied tilt and
// the dots' places relative to each other exactly.
var minX = canvasSize
var minY = canvasSize
var maxX = -1
var maxY = -1
for y in 0..<canvasSize {
    for x in 0..<canvasSize where inkMask.bytes[(y * canvasSize + x) * 4 + 3] > 8 {
        minX = Swift.min(minX, x)
        minY = Swift.min(minY, y)
        maxX = Swift.max(maxX, x)
        maxY = Swift.max(maxY, y)
    }
}
guard maxX > minX, maxY > minY else { fail("The ink mask is empty") }

let markImage = inkMask.image()
guard let markCrop = markImage.cropping(to: CGRect(
    x: minX,
    y: minY,
    width: maxX - minX + 1,
    height: maxY - minY + 1
)) else { fail("Could not crop the mark") }

let template = Canvas(pixels: templatePixels)
template.context.interpolationQuality = .high
let inset = 1.0
let available = CGFloat(templatePixels) - inset * 2
let scale = Swift.min(
    available / CGFloat(markCrop.width),
    available / CGFloat(markCrop.height)
)
let drawnWidth = CGFloat(markCrop.width) * scale
let drawnHeight = CGFloat(markCrop.height) * scale
template.context.draw(markCrop, in: CGRect(
    x: (CGFloat(templatePixels) - drawnWidth) / 2,
    y: (CGFloat(templatePixels) - drawnHeight) / 2,
    width: drawnWidth,
    height: drawnHeight
))
writePNG(template, to: templateURL, label: "mark_template.png")

// --- Summary --------------------------------------------------------------

func hex(_ pixel: Pixel) -> String {
    String(
        format: "#%02X%02X%02X",
        Int((pixel.red * 255).rounded()),
        Int((pixel.green * 255).rounded()),
        Int((pixel.blue * 255).rounded())
    )
}
let sourceHash = SHA256.hash(data: (try? Data(contentsOf: sourceURL)) ?? Data())
    .map { String(format: "%02x", $0) }.joined()
print("  canvas \(canvasSize)x\(canvasSize), tile \(Int(tileSize))x\(Int(tileSize)) at \(Int(tileInset)), radius \(cornerRadius)")
print("  source \(width)x\(height) sha256 \(sourceHash)")
print("  sampled field \(hex(fieldColour)), C \(hex(inkColour)), dots \(hex(dotColour))")
print("  menu-bar template \(templatePixels)x\(templatePixels) black-plus-alpha")
