#!/usr/bin/env swift
import AppKit
import Foundation

// Draws the release disk image's background art: the "Indigo console" option.
//
// Outputs (checked in; the release build only reads them):
//   release/dmg/background.png     640x400 px, 72 dpi
//   release/dmg/background@2x.png  1280x800 px, 144 dpi
// Both are 8-bit sRGB without alpha and are 640x400 pt, so `tiffutil
// -cathidpicheck` can join them into one Retina background.
//
// What it draws, in the tokens of DESIGN.md: a vertical gradient from the
// background (#1a1a2e) to surface-elevated (#23264a), a faint 24 pt dot grid
// in the border colour (#2e3258), the cream wordmark from
// Sources/CiaobotMac/Resources/ciaobot-wordmark-light.png, a brand-orange
// arrow (#fb923c), and one label plate (#6d739c) behind each icon name.
//
// The plates are there for Dark mode: Finder paints the picture as it is in
// both appearances but draws icon labels in the system text colour (black in
// Light, white in Dark). #6d739c holds 4.5:1 against both. No text is drawn,
// because system fonts differ between macOS versions and the wordmark already
// carries the brand.
//
// Every coordinate comes from `scripts/dmg_layout.py geometry`, the single
// source shared with the .DS_Store icon positions.
//
// Usage:
//   swift scripts/make-dmg-background.swift [background.png] [background@2x.png]
//
// Nobody runs this at build time; regenerate only when the art changes, then
// update the pinned hashes in DmgScriptTests.

let scriptDirectory = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
let repositoryRoot = scriptDirectory.deletingLastPathComponent()
let arguments = Array(CommandLine.arguments.dropFirst())
let outputDirectory = repositoryRoot.appendingPathComponent("release/dmg")
let output1x = arguments.count > 0 ? URL(fileURLWithPath: arguments[0]) : outputDirectory.appendingPathComponent("background.png")
let output2x = arguments.count > 1 ? URL(fileURLWithPath: arguments[1]) : outputDirectory.appendingPathComponent("background@2x.png")
let wordmarkURL = repositoryRoot.appendingPathComponent("Sources/CiaobotMac/Resources/ciaobot-wordmark-light.png")

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("\(message)\n".utf8))
    exit(1)
}

func color(_ hex: String) -> NSColor {
    let value = UInt32(hex.dropFirst(), radix: 16) ?? 0
    return NSColor(
        srgbRed: CGFloat((value >> 16) & 0xFF) / 255,
        green: CGFloat((value >> 8) & 0xFF) / 255,
        blue: CGFloat(value & 0xFF) / 255,
        alpha: 1
    )
}

func loadGeometry() -> [String: Any] {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    process.arguments = ["-I", "scripts/dmg_layout.py", "geometry", "--app-name", "Ciaobot.app"]
    process.currentDirectoryURL = repositoryRoot
    let pipe = Pipe()
    process.standardOutput = pipe
    do { try process.run() } catch { fail("Could not run dmg_layout.py: \(error)") }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard process.terminationStatus == 0,
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        fail("dmg_layout.py geometry failed")
    }
    return object
}

func numbers(_ value: Any?) -> [CGFloat] {
    guard let list = value as? [NSNumber] else { fail("geometry is missing a coordinate pair") }
    return list.map { CGFloat($0.doubleValue) }
}

let geometry = loadGeometry()
let content = numbers(geometry["contentSize"])
let arrow = geometry["arrow"] as? [String: Any] ?? [:]
let arrowFrom = numbers(arrow["from"])
let arrowTo = numbers(arrow["to"])
let plates = geometry["plates"] as? [String: [String: NSNumber]] ?? [:]
let plateColor = color(geometry["plateColor"] as? String ?? "")
guard let wordmark = NSImage(contentsOf: wordmarkURL) else { fail("Missing \(wordmarkURL.path)") }

let width = content[0]
let height = content[1]

func render(scale: Int, to url: URL) {
    let pixelsWide = Int(width) * scale
    let pixelsHigh = Int(height) * scale
    guard let space = CGColorSpace(name: CGColorSpace.sRGB),
          let cg = CGContext(
            data: nil,
            width: pixelsWide,
            height: pixelsHigh,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: space,
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
          ) else { fail("Could not allocate the bitmap") }

    // Top-left origin in points, whatever the scale.
    cg.translateBy(x: 0, y: CGFloat(pixelsHigh))
    cg.scaleBy(x: CGFloat(scale), y: -CGFloat(scale))
    let flipped = NSGraphicsContext(cgContext: cg, flipped: true)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = flipped
    defer { NSGraphicsContext.restoreGraphicsState() }
    cg.setShouldAntialias(true)
    cg.interpolationQuality = .high

    let canvas = NSRect(x: 0, y: 0, width: width, height: height)
    NSGradient(starting: color("#1a1a2e"), ending: color("#23264a"))?.draw(in: canvas, angle: 90)

    color("#2e3258").setFill()
    var dotY: CGFloat = 24
    while dotY < height {
        var dotX: CGFloat = 24
        while dotX < width {
            NSBezierPath(ovalIn: NSRect(x: dotX - 1, y: dotY - 1, width: 2, height: 2)).fill()
            dotX += 24
        }
        dotY += 24
    }

    let markWidth: CGFloat = 140
    let markHeight = markWidth * wordmark.size.height / wordmark.size.width
    wordmark.draw(
        in: NSRect(x: (width - markWidth) / 2, y: 56 - markHeight / 2, width: markWidth, height: markHeight),
        from: .zero,
        operation: .sourceOver,
        fraction: 1,
        respectFlipped: true,
        hints: [.interpolation: NSImageInterpolation.high.rawValue]
    )

    let orange = color("#fb923c")
    let headLength: CGFloat = 20
    let headHalf: CGFloat = 13
    let line = NSBezierPath()
    line.lineWidth = 6
    line.lineCapStyle = .round
    line.move(to: NSPoint(x: arrowFrom[0], y: arrowFrom[1]))
    line.line(to: NSPoint(x: arrowTo[0] - headLength + 2, y: arrowTo[1]))
    orange.setStroke()
    line.stroke()
    let head = NSBezierPath()
    head.move(to: NSPoint(x: arrowTo[0], y: arrowTo[1]))
    head.line(to: NSPoint(x: arrowTo[0] - headLength, y: arrowTo[1] - headHalf))
    head.line(to: NSPoint(x: arrowTo[0] - headLength, y: arrowTo[1] + headHalf))
    head.close()
    head.lineJoinStyle = .round
    head.lineWidth = 2
    orange.setFill()
    orange.setStroke()
    head.fill()
    head.stroke()

    plateColor.setFill()
    for key in ["app", "applications"] {
        guard let plate = plates[key], let x = plate["x"], let y = plate["y"],
              let w = plate["width"], let h = plate["height"] else { fail("geometry is missing a plate") }
        let rect = NSRect(x: CGFloat(x.doubleValue), y: CGFloat(y.doubleValue), width: CGFloat(w.doubleValue), height: CGFloat(h.doubleValue))
        NSBezierPath(roundedRect: rect, xRadius: 8, yRadius: 8).fill()
    }

    guard let image = cg.makeImage(),
          let destination = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else {
        fail("Could not encode the PNG")
    }
    // 72 dpi at 1x, 144 dpi at 2x, so the picture is 640x400 pt either way.
    let dpi = 72 * scale
    CGImageDestinationAddImage(destination, image, [
        kCGImagePropertyDPIWidth: dpi,
        kCGImagePropertyDPIHeight: dpi,
    ] as CFDictionary)
    guard CGImageDestinationFinalize(destination) else { fail("Could not write \(url.path)") }
}

try? FileManager.default.createDirectory(at: output1x.deletingLastPathComponent(), withIntermediateDirectories: true)
render(scale: 1, to: output1x)
render(scale: 2, to: output2x)
