import XCTest
import AppKit

@testable import CiaobotMac

/// The menu's working glyph is the web UI's `ActivityGlyph` chase. These tests
/// pin the frame model to `ActivityGlyph.vue`'s `WORKING_CHASE`: `chase[cell]`
/// is the animation step of that row-major cell, the centre is fixed, and the
/// cycle is eight 0.15 s steps of perimeter chase. Nothing here opens a menu,
/// and the renderer test only draws an image in memory.
@MainActor
final class WorkingGlyphFrameTests: XCTestCase {
    func testChaseOrderMatchesTheWebConstant() {
        XCTAssertEqual(
            WorkingGlyphFrame.chase,
            [0, 1, 2, 7, -1, 3, 6, 5, 4],
            "The menu must animate the same grid Home does."
        )
        XCTAssertEqual(WorkingGlyphFrame.cycle, 1.2, accuracy: 1e-9)
        XCTAssertEqual(WorkingGlyphFrame.step, 0.15, accuracy: 1e-9)
        XCTAssertEqual(
            WorkingGlyphFrame.cycle,
            8 * WorkingGlyphFrame.step,
            accuracy: 1e-9,
            "Eight perimeter cells are one cycle; the dim centre has no step."
        )
    }

    func testOneFullCycleReturnsToTheFirstLitCell() throws {
        let first = try XCTUnwrap(WorkingGlyphFrame.litCell(at: 0))
        XCTAssertEqual(first, 0, "The top-left cell leads the chase.")

        XCTAssertEqual(
            WorkingGlyphFrame.litCell(at: WorkingGlyphFrame.cycle),
            first,
            "A full linear cycle must land on the same cell."
        )
        XCTAssertEqual(WorkingGlyphFrame.litCell(at: 2 * WorkingGlyphFrame.cycle), first)
    }

    func testTheChaseFollowsThePerimeterOneStepAtATime() {
        let sequence = (0..<8).map { step in
            WorkingGlyphFrame.litCell(at: Double(step) * WorkingGlyphFrame.step)
        }
        XCTAssertEqual(
            sequence,
            [0, 1, 2, 5, 8, 7, 6, 3],
            "The lit pixel walks the top row, down the right edge, back along the bottom, and up the left edge."
        )
    }

    func testTheDimCentreNeverLights() {
        var litCells = Set<Int>()
        for step in 0..<8 {
            let cell = WorkingGlyphFrame.litCell(at: Double(step) * WorkingGlyphFrame.step)
            XCTAssertNotEqual(cell, WorkingGlyphFrame.dimCentre, "The centre is the fixed dim cell.")
            if let cell {
                litCells.insert(cell)
            }
        }
        XCTAssertEqual(
            litCells,
            [0, 1, 2, 3, 5, 6, 7, 8],
            "Every perimeter cell takes its turn and only the centre stays dim."
        )
    }

    func testLitCellIsStableAcrossACycleBoundary() {
        for offset in stride(from: 0.0, to: WorkingGlyphFrame.cycle, by: 0.05) {
            XCTAssertEqual(
                WorkingGlyphFrame.litCell(at: WorkingGlyphFrame.cycle + offset),
                WorkingGlyphFrame.litCell(at: offset),
                "The frame at cycle + \(offset) must match the frame at \(offset)."
            )
        }
    }

    func testEveryStepBoundarySelectsTheNextCellRatherThanFlickeringBack() {
        for step in 0..<16 {
            let exact = Double(step) * WorkingGlyphFrame.step
            XCTAssertEqual(
                WorkingGlyphFrame.litCell(at: exact + 0.001),
                WorkingGlyphFrame.litCell(at: exact + 0.05),
                "A whole step must hold one cell."
            )
        }
    }

    func testReducedMotionCellsAreTheEvenCells() {
        XCTAssertEqual(
            WorkingGlyphFrame.reducedMotionCells(),
            Set([0, 2, 4, 6, 8]),
            "Reduced motion freezes the grid into the web's even-cell checker."
        )
    }

    func testRendererDrawsAnElevenPointTemplateGrid() throws {
        let image = try XCTUnwrap(
            WorkingGlyphRenderer().glyphImage(
                litCell: 0,
                size: WorkingGlyphRenderer.pointSize,
                reduceMotion: false
            )
        )
        XCTAssertEqual(image.size, NSSize(width: 11, height: 11))
        XCTAssertTrue(image.isTemplate, "The menu-bar tint must apply to the drawn grid.")
    }

    func testReducedMotionRendererIgnoresTheChaseFrame() throws {
        let renderer = WorkingGlyphRenderer()
        let lit = try XCTUnwrap(renderer.glyphImage(litCell: 0, size: 11, reduceMotion: true))
        let dim = try XCTUnwrap(renderer.glyphImage(litCell: nil, size: 11, reduceMotion: true))
        XCTAssertEqual(lit.size, dim.size)
        XCTAssertTrue(lit.isTemplate)
        XCTAssertTrue(dim.isTemplate)
    }
}
