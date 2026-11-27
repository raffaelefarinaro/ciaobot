import XCTest
import AppKit
import CoreGraphics
import ImageIO

@testable import CiaobotMac

/// The menu-bar mark's working loop. The geometry, frame pacing and the
/// animation state machine are pure and run with an injected clock; the
/// animator test uses a hand-fired ticker and recorded images, so nothing here
/// schedules a timer or touches the real menu bar.
final class TrayMarkTests: XCTestCase {
    // MARK: - Geometry (ciaoRing.ts)

    func testRestingShapeIsTheCWithItsTwoDots() {
        let shape = CiaoRing.shape(progress: 0)
        XCTAssertEqual(shape.rotate, 0)
        XCTAssertNil(shape.arcA, "Nothing grows before the dots land.")
        XCTAssertNil(shape.arcB)
        XCTAssertEqual(shape.dotA.radius, 91)
        XCTAssertEqual(shape.dotB.radius, 56)
        XCTAssertEqual(shape.dotA.centre.x, 630 + 361 * cos(-48.4 * .pi / 180), accuracy: 1e-6)
        XCTAssertEqual(shape.dotA.centre.y, 616 + 361 * sin(-48.4 * .pi / 180), accuracy: 1e-6)
    }

    func testClosedShapeIsTheRingWithTheDotsMet() throws {
        let shape = CiaoRing.shape(progress: 1)
        XCTAssertEqual(shape.rotate, 360, accuracy: 1e-9)
        let arcA = try XCTUnwrap(shape.arcA)
        let arcB = try XCTUnwrap(shape.arcB)
        XCTAssertEqual(arcA.end, CiaoRing.meet, accuracy: 1e-9)
        XCTAssertEqual(arcB.start, CiaoRing.meet, accuracy: 1e-9)
        XCTAssertEqual(shape.dotA.radius, CiaoRing.landedDotRadius, accuracy: 1e-9)
        XCTAssertEqual(shape.dotA.centre.x, shape.dotB.centre.x, accuracy: 1e-6)
        XCTAssertEqual(shape.dotA.centre.y, shape.dotB.centre.y, accuracy: 1e-6)
    }

    // MARK: - Frames

    func testTheLoopClosesToTheRingAndPlaysItselfBack() {
        XCTAssertEqual(TrayMarkFrames.frameCount, 24)
        XCTAssertEqual(TrayMarkFrames.cycle, 24 * TrayMarkFrames.step, accuracy: 1e-9)
        let fps = 1 / TrayMarkFrames.step
        XCTAssertTrue((12...15).contains(fps), "\(fps) fps")
        XCTAssertEqual(TrayMarkFrames.progress(forFrame: 0), 0)
        XCTAssertEqual(TrayMarkFrames.progress(forFrame: TrayMarkFrames.ringFrame), 1)
        for frame in 1..<TrayMarkFrames.ringFrame {
            XCTAssertGreaterThan(
                TrayMarkFrames.progress(forFrame: frame),
                TrayMarkFrames.progress(forFrame: frame - 1)
            )
            XCTAssertEqual(
                TrayMarkFrames.progress(forFrame: frame),
                TrayMarkFrames.progress(forFrame: TrayMarkFrames.frameCount - frame),
                accuracy: 1e-12,
                "The way back retraces the way in."
            )
        }
        XCTAssertEqual(TrayMarkFrames.progress(forFrame: TrayMarkFrames.frameCount), 0, "Frames wrap.")
    }

    func testNoFrameStepSpinsTheMarkMoreThanAnEighthOfATurn() {
        for frame in 1...TrayMarkFrames.ringFrame {
            let before = CiaoRing.shape(progress: TrayMarkFrames.progress(forFrame: frame - 1)).rotate
            let after = CiaoRing.shape(progress: TrayMarkFrames.progress(forFrame: frame)).rotate
            XCTAssertLessThanOrEqual(after - before, 46, "frame \(frame)")
        }
    }

    func testEveryFrameDiffersFromTheNextAndStaysInsideTheImage() throws {
        var previous: [UInt8]?
        for frame in 0...TrayMarkFrames.ringFrame {
            let pixels = try alpha(of: frame, pixels: 36)
            if let previous {
                XCTAssertNotEqual(pixels, previous, "frame \(frame) repeats the one before")
            }
            for y in 0..<36 {
                for x in 0..<36 where x == 0 || y == 0 || x == 35 || y == 35 {
                    XCTAssertEqual(pixels[y * 36 + x], 0, "frame \(frame) is clipped at (\(x), \(y))")
                }
            }
            previous = pixels
        }
        XCTAssertNotEqual(try alpha(of: 0, pixels: 36), try alpha(of: TrayMarkFrames.ringFrame, pixels: 36))
    }

    /// Frame 0 hands over to the bundled static mark at both ends of the loop,
    /// so it must cover the same pixels.
    func testTheFirstFrameLandsOnTheStaticMark() throws {
        let url = try XCTUnwrap(AppAssets.menuBarImageURL())
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(url as CFURL, nil))
        let template = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        XCTAssertEqual(template.width, 36)
        let mark = rasterAlpha(template, pixels: 36)
        let first = try alpha(of: 0, pixels: 36)
        let second = try alpha(of: 1, pixels: 36)
        XCTAssertGreaterThan(overlap(mark, first), 0.85)
        XCTAssertGreaterThan(overlap(mark, first), overlap(mark, second))
    }

    @MainActor
    func testTheImageCacheMakesTwoScaleTemplates() throws {
        let image = try XCTUnwrap(TrayMarkImageCache.makeImage(progress: 0.5))
        XCTAssertTrue(image.isTemplate, "A template follows the light or dark menu bar.")
        XCTAssertEqual(image.size, NSSize(width: 18, height: 18))
        let widths = image.representations.map(\.pixelsWide).sorted()
        XCTAssertEqual(widths, [18, 36])
    }

    // MARK: - State machine

    func testIdleShowsTheStaticMarkAndNeedsNoTimer() {
        var animation = TrayMarkAnimation()
        XCTAssertEqual(animation.display(at: 10), .idle)
        XCTAssertFalse(animation.needsTicks)
        animation.report(working: false, at: 10)
        XCTAssertEqual(animation.display(at: 11), .idle)
        XCTAssertFalse(animation.needsTicks)
    }

    func testWorkingPlaysTheLoopFromTheRestingC() {
        var animation = TrayMarkAnimation()
        animation.report(working: true, at: 100)
        XCTAssertTrue(animation.needsTicks)
        XCTAssertEqual(animation.display(at: 100), .frame(0))
        XCTAssertEqual(animation.display(at: 100 + TrayMarkFrames.step), .frame(1))
        XCTAssertEqual(animation.display(at: 100 + 12 * TrayMarkFrames.step), .frame(12))
        XCTAssertEqual(animation.display(at: 100 + TrayMarkFrames.cycle), .frame(0), "The loop repeats.")
        animation.report(working: true, at: 100 + 3 * TrayMarkFrames.step)
        XCTAssertEqual(
            animation.display(at: 100 + 3 * TrayMarkFrames.step),
            .frame(3),
            "A repeated working report keeps the phase."
        )
    }

    func testABriefGapBetweenTurnsDoesNotBlink() {
        var animation = TrayMarkAnimation()
        animation.report(working: true, at: 0)
        animation.report(working: false, at: 0.4)
        XCTAssertEqual(animation.display(at: 0.4 + 0.5), .frame(11), "Still inside the debounce.")
        animation.report(working: true, at: 0.95)
        XCTAssertEqual(animation.display(at: 0.4 + 0.6 + 0.5), .frame(18), "The loop never stopped.")
        XCTAssertTrue(animation.needsTicks)
    }

    func testIdlePlaysOnToTheRestingCThenStops() {
        var animation = TrayMarkAnimation()
        animation.report(working: true, at: 0)
        animation.report(working: false, at: 0.2)
        // Debounce ends at 0.8 (frame 10); the loop runs out to the next cycle boundary.
        XCTAssertEqual(animation.display(at: 0.8), .frame(10))
        XCTAssertEqual(animation.display(at: TrayMarkFrames.cycle - TrayMarkFrames.step), .frame(23))
        XCTAssertTrue(animation.needsTicks)
        XCTAssertEqual(animation.display(at: TrayMarkFrames.cycle), .idle)
        XCTAssertFalse(animation.needsTicks)
        XCTAssertFalse(animation.showsWork)
    }

    func testIdleRightOnACycleBoundaryStopsAtOnce() {
        var animation = TrayMarkAnimation()
        animation.report(working: true, at: 0)
        animation.report(working: false, at: TrayMarkFrames.cycle - 0.6)
        XCTAssertEqual(animation.display(at: TrayMarkFrames.cycle), .idle)
    }

    func testWorkAfterTheMarkRestedStartsAFreshLoop() {
        var animation = TrayMarkAnimation()
        animation.report(working: true, at: 0)
        animation.report(working: false, at: 0.1)
        XCTAssertEqual(animation.display(at: 5), .idle)
        animation.report(working: true, at: 7.3)
        XCTAssertEqual(animation.display(at: 7.3), .frame(0))
    }

    func testReduceMotionShowsTheStillRingWithoutATimer() {
        var animation = TrayMarkAnimation(reduceMotion: true)
        animation.report(working: true, at: 0)
        XCTAssertEqual(animation.display(at: 0), .still)
        XCTAssertEqual(animation.display(at: 3.33), .still)
        XCTAssertFalse(animation.needsTicks, "A still mark needs no frames.")

        animation.report(working: false, at: 4)
        XCTAssertTrue(animation.needsTicks, "Only the debounce deadline needs a tick.")
        XCTAssertEqual(animation.display(at: 4.5), .still)
        XCTAssertEqual(animation.display(at: 4.6), .idle, "No run-out without motion.")
        XCTAssertFalse(animation.needsTicks)
    }

    func testReduceMotionFollowsTheSettingLive() {
        var animation = TrayMarkAnimation()
        animation.report(working: true, at: 0)
        XCTAssertEqual(animation.display(at: 0.16), .frame(2))
        animation.setReduceMotion(true)
        XCTAssertEqual(animation.display(at: 0.24), .still)
        XCTAssertFalse(animation.needsTicks)
        animation.setReduceMotion(false)
        XCTAssertEqual(animation.display(at: 0.32), .frame(4))

        animation.report(working: false, at: 0.4)
        XCTAssertEqual(animation.display(at: 1.0), .frame(12), "Run-out started.")
        animation.setReduceMotion(true)
        XCTAssertEqual(animation.display(at: 1.1), .idle, "Reduce Motion skips the run-out.")
    }

    // MARK: - Animator

    @MainActor
    func testTheAnimatorTicksOnlyWhileAFrameCanChange() {
        let ticker = MarkTicker()
        let images = MarkImages()
        let idle = NSImage(size: NSSize(width: 18, height: 18))
        var clock: TimeInterval = 50
        var reduce = false
        var applied: [NSImage] = []
        let animator = TrayMarkAnimator(
            idleImage: idle,
            images: images,
            ticker: ticker,
            clock: { clock },
            reduceMotion: { reduce },
            apply: { applied.append($0) }
        )

        animator.setWorking(false)
        XCTAssertFalse(ticker.isRunning, "Idle runs no timer.")
        XCTAssertTrue(applied.isEmpty, "The static mark is already showing.")

        animator.setWorking(true)
        XCTAssertTrue(ticker.isRunning)
        XCTAssertEqual(ticker.intervals, [TrayMarkFrames.step])
        XCTAssertEqual(animator.display, .frame(0))
        clock += TrayMarkFrames.step
        ticker.fire()
        XCTAssertEqual(animator.display, .frame(1))
        XCTAssertTrue(applied.last === images.frames[1])
        ticker.fire()
        XCTAssertEqual(applied.count, 2, "An unchanged frame is not re-applied.")

        reduce = true
        animator.reduceMotionChanged()
        XCTAssertEqual(animator.display, .still)
        XCTAssertFalse(ticker.isRunning, "The still ring needs no timer.")

        reduce = false
        animator.reduceMotionChanged()
        XCTAssertTrue(ticker.isRunning)

        animator.setWorking(false)
        clock += 0.6 + TrayMarkFrames.cycle
        ticker.fire()
        XCTAssertEqual(animator.display, .idle)
        XCTAssertTrue(applied.last === idle, "The bundled static mark comes back.")
        XCTAssertFalse(ticker.isRunning)

        animator.setWorking(true)
        animator.stop()
        XCTAssertFalse(ticker.isRunning)
    }

    // MARK: - Helpers

    private func alpha(of frame: Int, pixels: Int) throws -> [UInt8] {
        let image = try XCTUnwrap(TrayMarkFrames.bitmap(
            progress: TrayMarkFrames.progress(forFrame: frame),
            pixels: pixels
        ))
        return rasterAlpha(image, pixels: pixels)
    }

    private func rasterAlpha(_ image: CGImage, pixels: Int) -> [UInt8] {
        var buffer = [UInt8](repeating: 0, count: pixels * pixels * 4)
        buffer.withUnsafeMutableBytes { raw in
            let context = CGContext(
                data: raw.baseAddress,
                width: pixels,
                height: pixels,
                bitsPerComponent: 8,
                bytesPerRow: pixels * 4,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            )
            context?.draw(image, in: CGRect(x: 0, y: 0, width: pixels, height: pixels))
        }
        return stride(from: 3, to: buffer.count, by: 4).map { buffer[$0] }
    }

    /// Intersection over union of the half-covered pixels.
    private func overlap(_ a: [UInt8], _ b: [UInt8]) -> Double {
        var both = 0.0
        var either = 0.0
        for (x, y) in zip(a, b) {
            if x > 127 && y > 127 { both += 1 }
            if x > 127 || y > 127 { either += 1 }
        }
        return either == 0 ? 0 : both / either
    }
}

@MainActor
private final class MarkTicker: MenuTicking {
    private(set) var isRunning = false
    private(set) var intervals: [TimeInterval] = []
    private var tick: (() -> Void)?

    func start(interval: TimeInterval, tick: @escaping () -> Void) {
        isRunning = true
        intervals.append(interval)
        self.tick = tick
    }

    func stop() {
        isRunning = false
        tick = nil
    }

    func fire() {
        guard isRunning else { return }
        tick?()
    }
}

@MainActor
private final class MarkImages: TrayMarkImageProviding {
    let frames = (0..<TrayMarkFrames.frameCount).map { _ in NSImage(size: NSSize(width: 18, height: 18)) }
    let ring = NSImage(size: NSSize(width: 18, height: 18))

    func frame(_ index: Int) -> NSImage? { frames[index] }
    func still() -> NSImage? { ring }
}
