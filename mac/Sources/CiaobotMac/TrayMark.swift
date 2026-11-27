import CoreGraphics
import Foundation

/// The geometry of the Ciaobot mark's close-into-a-ring motion, ported from
/// `web/src/lib/ciaoRing.ts` (keep the two in step). `shape(progress:)` for
/// a progress in 0...1 describes the mark at that point: 0 is the resting C
/// with its two dots, 1 is the closed ring. Coordinates are the web mark's SVG
/// units (y grows downwards); angles are degrees. Pure: no AppKit.
enum CiaoRing {
    static let centre = CGPoint(x: 630, y: 616)
    static let radius: CGFloat = 295
    static let strokeWidth: CGFloat = 110
    /// The angle of the C's upper end.
    static let upperEnd: CGFloat = -72.48
    /// The angle of the C's lower end.
    static let lowerEnd: CGFloat = 62.0
    static let meet: CGFloat = (upperEnd + lowerEnd) / 2
    /// The share of the motion spent landing the dots on the C's ends.
    static let landShare: CGFloat = 0.35
    /// A landed dot has the stroke's half width.
    static let landedDotRadius: CGFloat = 55

    struct DotSpec {
        let angle: CGFloat
        let distance: CGFloat
        let radius: CGFloat
    }

    static let dotA = DotSpec(angle: -48.4, distance: 361, radius: 91)
    static let dotB = DotSpec(angle: 38.3, distance: 297, radius: 56)

    struct Dot: Equatable {
        let centre: CGPoint
        let radius: CGFloat
    }

    /// An arc of the ring from `start` to `end` degrees, swept towards
    /// increasing angles.
    struct Arc: Equatable {
        let start: CGFloat
        let end: CGFloat
    }

    struct Shape: Equatable {
        /// Rotation of the whole mark, degrees around `centre`.
        let rotate: CGFloat
        /// The arcs growing from each landed dot; nil until the dots land.
        let arcA: Arc?
        let arcB: Arc?
        let dotA: Dot
        let dotB: Dot
    }

    static func shape(progress: Double) -> Shape {
        let p = clamp(CGFloat(progress))
        let land = smooth(p / landShare)
        let grow = inOut(clamp((p - landShare) / (1 - landShare)))
        let tipA = upperEnd + (meet - upperEnd) * grow
        let tipB = lowerEnd + (meet - lowerEnd) * grow
        let growing = grow > 0
        return Shape(
            rotate: 360 * inOut(p),
            arcA: growing ? Arc(start: upperEnd, end: tipA) : nil,
            arcB: growing ? Arc(start: tipB, end: lowerEnd) : nil,
            dotA: dot(dotA, end: upperEnd, land: land, tip: tipA, growing: growing),
            dotB: dot(dotB, end: lowerEnd, land: land, tip: tipB, growing: growing)
        )
    }

    private static func dot(_ spec: DotSpec, end: CGFloat, land: CGFloat, tip: CGFloat, growing: Bool) -> Dot {
        let angle = growing ? tip : lerp(spec.angle, end, land)
        let distance = growing ? radius : lerp(spec.distance, radius, land)
        return Dot(
            centre: CGPoint(
                x: centre.x + distance * cos(radians(angle)),
                y: centre.y + distance * sin(radians(angle))
            ),
            radius: lerp(spec.radius, landedDotRadius, land)
        )
    }

    static func radians(_ degrees: CGFloat) -> CGFloat { degrees * .pi / 180 }
    private static func lerp(_ a: CGFloat, _ b: CGFloat, _ t: CGFloat) -> CGFloat { a + (b - a) * t }
    private static func clamp(_ t: CGFloat) -> CGFloat { max(0, min(1, t)) }
    private static func smooth(_ t: CGFloat) -> CGFloat {
        let c = clamp(t)
        return c * c * (3 - 2 * c)
    }
    private static func inOut(_ t: CGFloat) -> CGFloat {
        t < 0.5 ? 4 * t * t * t : 1 - pow(-2 * t + 2, 3) / 2
    }
}

/// The menu-bar mark's working loop: the C closes into the ring and opens
/// again, `frameCount` frames per cycle at `step` seconds each. Frame 0 is the
/// resting C, frame `ringFrame` the closed ring; the second half plays the
/// first half back, as the web mark reopens from wherever it got to.
enum TrayMarkFrames {
    static let frameCount = 24
    /// 12.5 frames a second: smooth at 18 points, light on the CPU.
    static let step: TimeInterval = 0.08
    static let cycle: TimeInterval = Double(frameCount) * step
    static let ringFrame = frameCount / 2
    /// The mark's point size in the menu bar, the same as the static template.
    static let pointSize = CGSize(width: 18, height: 18)
    /// The square of the web mark's SVG space drawn into the image. It is the
    /// crop the static `mark_template.png` uses, so frame 0 lands on the
    /// resting C without a jump, and it holds every frame of the motion.
    static let viewBox = CGRect(x: 226, y: 214, width: 788, height: 788)

    /// The ring's progress (0 is the C, 1 the ring) for a frame index; any
    /// integer wraps into the cycle.
    ///
    /// The web mark redraws at 60 fps, where its eased spin turns up to 90
    /// degrees between two of these frames at its fastest. The frames are
    /// therefore spaced evenly in `pacing` rather than in progress, so the
    /// spin's fastest step is half that and the slow ends still ease.
    static func progress(forFrame frame: Int) -> Double {
        let index = ((frame % frameCount) + frameCount) % frameCount
        let mirrored = index <= ringFrame ? index : frameCount - index
        let target = Double(mirrored) / Double(ringFrame)
        // pacing is increasing from 0 to 1; bisect for the progress that reaches target.
        var low = 0.0
        var high = 1.0
        for _ in 0..<40 {
            let mid = (low + high) / 2
            if pacing(mid) < target { low = mid } else { high = mid }
        }
        return mirrored == 0 ? 0 : mirrored == ringFrame ? 1 : (low + high) / 2
    }

    /// Half linear progress, half the spin's own ease (`inOut` in ciaoRing.ts).
    static func pacing(_ progress: Double) -> Double {
        let p = max(0, min(1, progress))
        let eased = p < 0.5 ? 4 * p * p * p : 1 - pow(-2 * p + 2, 3) / 2
        return (p + eased) / 2
    }

    /// Draws the mark at `progress` in black into a context whose drawable
    /// area is `size` (in the context's own units, origin bottom-left). The
    /// caller keeps it a template; the colour comes from the menu bar.
    static func draw(progress: Double, in context: CGContext, size: CGSize) {
        let shape = CiaoRing.shape(progress: progress)
        let centre = CiaoRing.centre
        context.saveGState()
        defer { context.restoreGState() }
        // SVG space has y growing downwards; flip it into the context.
        context.translateBy(x: 0, y: size.height)
        context.scaleBy(x: size.width / viewBox.width, y: -size.height / viewBox.height)
        context.translateBy(x: -viewBox.minX, y: -viewBox.minY)
        context.translateBy(x: centre.x, y: centre.y)
        context.rotate(by: CiaoRing.radians(shape.rotate))
        context.translateBy(x: -centre.x, y: -centre.y)

        let black = CGColor(gray: 0, alpha: 1)
        context.setStrokeColor(black)
        context.setFillColor(black)
        context.setLineWidth(CiaoRing.strokeWidth)
        context.setLineCap(.round)

        let path = CGMutablePath()
        // The resting C runs from its upper end the long way round to its
        // lower end (decreasing angles in SVG space).
        path.addArc(
            center: centre,
            radius: CiaoRing.radius,
            startAngle: CiaoRing.radians(CiaoRing.upperEnd),
            endAngle: CiaoRing.radians(CiaoRing.lowerEnd),
            clockwise: true
        )
        for arc in [shape.arcA, shape.arcB].compactMap({ $0 }) {
            let start = CiaoRing.radians(arc.start)
            path.move(to: CGPoint(
                x: centre.x + CiaoRing.radius * cos(start),
                y: centre.y + CiaoRing.radius * sin(start)
            ))
            path.addArc(
                center: centre,
                radius: CiaoRing.radius,
                startAngle: start,
                endAngle: CiaoRing.radians(arc.end),
                clockwise: false
            )
        }
        context.addPath(path)
        context.strokePath()
        for dot in [shape.dotA, shape.dotB] {
            context.fillEllipse(in: CGRect(
                x: dot.centre.x - dot.radius,
                y: dot.centre.y - dot.radius,
                width: dot.radius * 2,
                height: dot.radius * 2
            ))
        }
    }

    /// One frame as a square RGBA bitmap of `pixels` per side, black on
    /// transparent. Pure Core Graphics, so tests can compare frames without
    /// AppKit.
    static func bitmap(progress: Double, pixels: Int) -> CGImage? {
        guard pixels > 0,
              let context = CGContext(
                  data: nil,
                  width: pixels,
                  height: pixels,
                  bitsPerComponent: 8,
                  bytesPerRow: pixels * 4,
                  space: CGColorSpaceCreateDeviceRGB(),
                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
              ) else { return nil }
        context.setShouldAntialias(true)
        draw(progress: progress, in: context, size: CGSize(width: pixels, height: pixels))
        return context.makeImage()
    }
}

/// When the menu-bar mark animates, as a value with an injected clock: no
/// timer, AppKit or system setting lives here.
///
/// - Something working starts the loop at once, from the resting C.
/// - Nothing working stops it only after `idleDebounce`, so the short gap
///   between two turns does not blink the mark, and then the loop plays on to
///   the next resting C so the mark never snaps out of the ring.
/// - Reduce Motion shows the closed ring, still, for as long as something
///   works, and the plain C otherwise.
struct TrayMarkAnimation: Equatable {
    enum Display: Equatable {
        /// The static mark.
        case idle
        /// A frame of the loop.
        case frame(Int)
        /// The reduced-motion working mark: the closed ring, not moving.
        case still
    }

    static let idleDebounce: TimeInterval = 0.6

    private(set) var reduceMotion: Bool
    /// The loop's time origin while the mark shows work; nil when idle.
    private var startedAt: TimeInterval?
    /// When the feed last said nothing is working, while the mark still shows work.
    private var idleSince: TimeInterval?
    /// Once the debounce has passed: the resting-C boundary the loop ends on.
    private var stopsAt: TimeInterval?

    init(reduceMotion: Bool = false) {
        self.reduceMotion = reduceMotion
    }

    /// True while the mark shows work, including the debounce and the run-out
    /// to the resting C.
    var showsWork: Bool { startedAt != nil }

    /// Whether a timer must call `display(at:)`: the loop is playing, or a
    /// reduced-motion still is waiting for its debounce to end. A still mark
    /// with nothing pending needs none.
    var needsTicks: Bool {
        guard showsWork else { return false }
        return !reduceMotion || idleSince != nil
    }

    mutating func report(working: Bool, at now: TimeInterval) {
        if working {
            idleSince = nil
            stopsAt = nil
            if startedAt == nil { startedAt = now }
        } else if startedAt != nil, idleSince == nil {
            idleSince = now
        }
    }

    mutating func setReduceMotion(_ on: Bool) {
        reduceMotion = on
    }

    /// Settles every deadline that has passed by `now` and says what to draw.
    mutating func display(at now: TimeInterval) -> Display {
        guard let start = startedAt else { return .idle }
        if stopsAt == nil, let since = idleSince, now - since >= Self.idleDebounce - Self.tolerance {
            if reduceMotion {
                reset()
                return .idle
            }
            // The first resting C at or after the debounce deadline, however
            // late this call reads the clock.
            let deadline = since + Self.idleDebounce
            let cycles = ((deadline - start) / TrayMarkFrames.cycle - Self.tolerance).rounded(.up)
            stopsAt = start + max(0, cycles) * TrayMarkFrames.cycle
        }
        if let end = stopsAt, now >= end - Self.tolerance {
            reset()
            return .idle
        }
        if reduceMotion {
            // Reduce Motion turned on during the run-out: there is no loop to
            // finish, so the mark rests at once.
            if stopsAt != nil {
                reset()
                return .idle
            }
            return .still
        }
        let steps = Int(((now - start) / TrayMarkFrames.step + Self.tolerance).rounded(.down))
        return .frame(max(0, steps) % TrayMarkFrames.frameCount)
    }

    private mutating func reset() {
        startedAt = nil
        idleSince = nil
        stopsAt = nil
    }

    /// Absorbs the rounding of a clock read as a double, far below a frame.
    private static let tolerance: Double = 1e-6
}
