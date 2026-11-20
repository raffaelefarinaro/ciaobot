import AppKit

/// An sRGB colour written as bytes so the generated icon carries the exact
/// swatch value instead of a colour-managed approximation.
struct WorkspaceSwatch: Equatable {
    let red: UInt8
    let green: UInt8
    let blue: UInt8
}

/// The active workspace accent the web UI publishes on
/// `#ciao-app[data-workspace-color]`. The raw values are the exact preset IDs,
/// and `swatch` is the same dark-theme swatch, from the engine's
/// `web/src/lib/workspaceColors.ts`. The client never reads the engine
/// checkout, so this table is its own validated copy: a value that is not one
/// of these IDs is not a colour the Dock may use.
enum WorkspaceColorPreset: String, CaseIterable, Equatable {
    case amber
    case pink
    case cyan
    case emerald
    case violet
    case blue
    case teal
    case lime
    case fuchsia

    var swatch: WorkspaceSwatch {
        switch self {
        case .amber: return WorkspaceSwatch(red: 0xFB, green: 0x92, blue: 0x3C)
        case .pink: return WorkspaceSwatch(red: 0xFF, green: 0x4D, blue: 0x6D)
        case .cyan: return WorkspaceSwatch(red: 0x38, green: 0xBD, blue: 0xF8)
        case .emerald: return WorkspaceSwatch(red: 0x34, green: 0xD3, blue: 0x99)
        case .violet: return WorkspaceSwatch(red: 0xA7, green: 0x8B, blue: 0xFA)
        case .blue: return WorkspaceSwatch(red: 0x60, green: 0xA5, blue: 0xFA)
        case .teal: return WorkspaceSwatch(red: 0x2D, green: 0xD4, blue: 0xBF)
        case .lime: return WorkspaceSwatch(red: 0xA3, green: 0xE6, blue: 0x35)
        case .fuchsia: return WorkspaceSwatch(red: 0xE8, green: 0x79, blue: 0xF9)
        }
    }

    /// The longest preset ID is `fuchsia`; a page message may carry a short
    /// value, never an unbounded string.
    static let maximumMessageLength = 32
}

/// Composes the running app icon: the bundled brand artwork with its cream tile
/// field replaced by the preset colour, the colour-field direction the owner
/// chose when the accent first shipped. The dark C, the tile shape and the
/// transparent exterior are untouched; the two dots take the brand cream on an
/// accent field, because brand orange on the amber or pink field disappears.
protocol WorkspaceDockIconRendering: AnyObject {
    func icon(for preset: WorkspaceColorPreset) -> NSImage?
}

final class WorkspaceDockIconRenderer: WorkspaceDockIconRendering {
    /// The supplied export's cream field. Replacing it subtracts the cream and
    /// adds the swatch, so the mark's antialiased edges keep their coverage
    /// instead of being redrawn over a new field. `scripts/make-icons.swift`
    /// prints these two colours; the asset tests keep them in step.
    private static let brandField: (red: UInt8, green: UInt8, blue: UInt8) = (247, 245, 242)
    /// The dots' accent on the cream tile (the static icon keeps it).
    private static let brandDot: (red: UInt8, green: UInt8, blue: UInt8) = (215, 118, 67)

    private let artwork: NSImage
    private let ink: NSImage
    private let dots: NSImage
    private var cache: [WorkspaceColorPreset: NSImage] = [:]

    /// Fails when a required image is missing, so the caller can fall back to
    /// the original icon rather than a partly drawn tile.
    init?(artwork: NSImage?, ink: NSImage?, dots: NSImage?) {
        guard let artwork, let ink, let dots else { return nil }
        self.artwork = artwork
        self.ink = ink
        self.dots = dots
    }

    func icon(for preset: WorkspaceColorPreset) -> NSImage? {
        if let cached = cache[preset] { return cached }
        guard let image = Self.compose(artwork: artwork, ink: ink, dots: dots, preset: preset) else {
            return nil
        }
        cache[preset] = image
        return image
    }

    /// Pixels are handled in premultiplied 8-bit sRGB RGBA, so every replacement
    /// is scaled by the artwork's own alpha and the tile edge keeps its
    /// antialiasing. The ink mask (C plus dots) says how much of the pixel is
    /// mark rather than cream field; the dots mask says how much of it is a dot.
    /// Subtracting the cream the artwork already carries and adding the swatch
    /// gives each pixel exactly one of: its untouched artwork ink, the swatch, or
    /// the cream dot.
    static func compose(
        artwork: NSImage,
        ink: NSImage,
        dots: NSImage,
        preset: WorkspaceColorPreset
    ) -> NSImage? {
        guard let source = pixels(of: artwork),
              let mask = pixels(of: ink),
              let dotMask = pixels(of: dots),
              source.width == mask.width,
              source.height == mask.height,
              source.width == dotMask.width,
              source.height == dotMask.height else { return nil }

        var output = source.data
        let swatch = preset.swatch
        let swatchComponents = [swatch.red, swatch.green, swatch.blue]
        let fieldComponents = [brandField.red, brandField.green, brandField.blue]
        let dotComponents = [brandDot.red, brandDot.green, brandDot.blue]
        for offset in stride(from: 0, to: output.count, by: 4) {
            let alpha = Int(source.data[offset + 3])
            guard alpha > 0 else { continue }
            let field = (255 - Int(mask.data[offset + 3])) * alpha / 255
            let dot = Int(dotMask.data[offset + 3]) * alpha / 255
            guard field > 0 || dot > 0 else { continue }
            for channel in 0..<3 {
                let index = offset + channel
                var value = Int(source.data[index])
                if field > 0 {
                    value += shift(
                        Int(swatchComponents[channel]) - Int(fieldComponents[channel]),
                        coverage: field
                    )
                }
                if dot > 0 {
                    value += shift(
                        Int(fieldComponents[channel]) - Int(dotComponents[channel]),
                        coverage: dot
                    )
                }
                output[index] = UInt8(min(max(value, 0), 255))
            }
        }

        guard let provider = CGDataProvider(data: Data(output) as CFData),
              let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let image = CGImage(
                  width: source.width,
                  height: source.height,
                  bitsPerComponent: 8,
                  bitsPerPixel: 32,
                  bytesPerRow: source.width * 4,
                  space: colorSpace,
                  bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                  provider: provider,
                  decode: nil,
                  shouldInterpolate: false,
                  intent: .defaultIntent
              ) else { return nil }

        let result = NSImage(size: NSSize(width: source.width, height: source.height))
        result.addRepresentation(NSBitmapImageRep(cgImage: image))
        return result
    }

    /// `(new - brand) * coverage`, rounded to the nearest byte so a fully
    /// covered field reproduces the swatch (or the cream dot) exactly.
    private static func shift(_ delta: Int, coverage: Int) -> Int {
        let scaled = delta * coverage
        return scaled >= 0 ? (scaled + 127) / 255 : (scaled - 127) / 255
    }

    private static func pixels(of image: NSImage) -> (data: [UInt8], width: Int, height: Int)? {
        var proposed = CGRect(origin: .zero, size: image.size)
        guard let cgImage = image.cgImage(forProposedRect: &proposed, context: nil, hints: nil) else {
            return nil
        }
        let width = cgImage.width
        let height = cgImage.height
        guard width > 0, height > 0, let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
            return nil
        }

        var data = [UInt8](repeating: 0, count: width * height * 4)
        let drawn: Bool = data.withUnsafeMutableBytes { buffer in
            guard let context = CGContext(
                data: buffer.baseAddress,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: width * 4,
                space: colorSpace,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            ) else { return false }
            context.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard drawn else { return nil }
        return (data, width, height)
    }
}

/// Owns the running app's Dock icon. Production sets `NSApp.applicationIconImage`;
/// tests inject a setter, so no test touches the real Dock. The controller also
/// owns the launch fallback: the bundled icon until a page reports a valid
/// workspace colour, and again after a disconnect or an unknown value.
@MainActor
protocol WorkspaceDockIconControlling: AnyObject {
    /// The colour currently drawn, or nil for the bundled artwork.
    var current: WorkspaceColorPreset? { get }
    func apply(_ preset: WorkspaceColorPreset?)
    /// Show the bundled artwork, including at launch when nothing was applied.
    func reset()
}

@MainActor
final class WorkspaceDockIconController: WorkspaceDockIconControlling {
    typealias Setter = @MainActor (NSImage?) -> Void

    private let artwork: NSImage?
    private let renderer: WorkspaceDockIconRendering?
    private let setter: Setter

    private(set) var current: WorkspaceColorPreset?
    private var hasApplied = false

    init(
        artwork: NSImage? = AppAssets.appIconImage,
        ink: NSImage? = AppAssets.dockIconInkImage,
        dots: NSImage? = AppAssets.dockIconDotsImage,
        renderer: WorkspaceDockIconRendering? = nil,
        setter: Setter? = nil
    ) {
        self.artwork = artwork
        self.renderer = renderer ?? WorkspaceDockIconRenderer(artwork: artwork, ink: ink, dots: dots)
        self.setter = setter ?? Self.systemSetter
    }

    /// Production writes the real Dock tile. Tests inject their own setter and
    /// never reach this.
    private static func systemSetter(_ image: NSImage?) {
        NSApp.applicationIconImage = image
    }

    /// Applies the colour once. A repeated value is a no-op, and a failed
    /// render falls back to the bundled icon instead of a half-painted tile.
    func apply(_ preset: WorkspaceColorPreset?) {
        guard !hasApplied || current != preset else { return }
        let image = preset.flatMap { renderer?.icon(for: $0) } ?? artwork
        current = preset
        hasApplied = true
        setter(image)
    }

    func reset() {
        apply(nil)
    }
}
