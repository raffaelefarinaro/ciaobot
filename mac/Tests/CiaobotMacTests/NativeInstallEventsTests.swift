import CryptoKit
import XCTest
import Foundation

@testable import CiaobotMac

final class NativeInstallEventsTests: XCTestCase {
    private let testRunID = "6f1c2a4e-9b7d-4c3a-8e5f-0a1b2c3d4e5f"

    /// One compact JSON object followed by a newline. `schema` defaults to 1
    /// and `run_id` to the test run id; the caller supplies `seq` and `type`.
    private func line(_ fields: [String: Any], runID: String? = nil) -> Data {
        var object: [String: Any] = ["schema": 1, "run_id": runID ?? testRunID]
        for (key, value) in fields { object[key] = value }
        let json = try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        return json + Data("\n".utf8)
    }

    private func phaseLine(_ seq: Int, _ phase: String) -> Data {
        line(["seq": seq, "type": "phase", "phase": phase])
    }

    private var recordedSuccessfulRun: Data {
        var stream = Data()
        stream.append(phaseLine(1, "preflight"))
        stream.append(phaseLine(2, "bootstrap"))
        stream.append(phaseLine(3, "download"))
        stream.append(phaseLine(4, "install"))
        stream.append(phaseLine(5, "wizard"))
        stream.append(line(["seq": 6, "type": "wizard_ready", "origin": "http://127.0.0.1:8443"]))
        stream.append(line(["seq": 7, "type": "setup_saved"]))
        stream.append(phaseLine(8, "activation"))
        stream.append(phaseLine(9, "readiness"))
        stream.append(line(["seq": 10, "type": "service_ready", "origin": "http://127.0.0.1:8444"]))
        return stream
    }

    private let recordedEvents: [NativeInstallEvent] = [
        .phase(.preflight),
        .phase(.bootstrap),
        .phase(.download),
        .phase(.install),
        .phase(.wizard),
        .wizardReady(Origin("http://127.0.0.1:8443")!),
        .setupSaved,
        .phase(.activation),
        .phase(.readiness),
        .serviceReady(Origin("http://127.0.0.1:8444")!),
    ]

    private func decoder() -> NativeInstallEventDecoder {
        NativeInstallEventDecoder(runID: testRunID)
    }

    // MARK: Pin and arguments

    func testPinMatchesTheEngineContract() {
        XCTAssertEqual(NativeInstallPin.current.version, "1.8.1")
        XCTAssertEqual(
            NativeInstallPin.current.url.absoluteString,
            "https://github.com/raffaelefarinaro/ciaobot/releases/download/v1.8.1/install.sh"
        )
        XCTAssertEqual(
            NativeInstallPin.current.sha256,
            "df5a562bb99894f98d705a6223a5ed1ebef3b24dec44812619ae7e330773effb"
        )
        XCTAssertEqual(NativeInstallPin.eventsFD, 3)
    }

    func testArgumentsAreTheNativeInstallCommand() {
        let runID = UUID(uuidString: "6F1C2A4E-9B7D-4C3A-8E5F-0A1B2C3D4E5F")!
        XCTAssertEqual(
            NativeInstallPin.current.arguments(scriptPath: "/tmp/run/install.sh", runID: runID),
            [
                "/bin/sh", "/tmp/run/install.sh",
                "--version", "1.8.1",
                "--native-install",
                "--events-fd", "3",
                "--run-id", "6f1c2a4e-9b7d-4c3a-8e5f-0a1b2c3d4e5f",
            ]
        )
    }

    func testArgumentsAreTheNativeRepairCommand() {
        let runID = UUID(uuidString: "6F1C2A4E-9B7D-4C3A-8E5F-0A1B2C3D4E5F")!
        let repair = NativeInstallPin.current.arguments(scriptPath: "/tmp/run/install.sh", runID: runID, mode: .repair)
        XCTAssertEqual(
            repair,
            [
                "/bin/sh", "/tmp/run/install.sh",
                "--version", "1.8.1",
                "--native-repair",
                "--events-fd", "3",
                "--run-id", "6f1c2a4e-9b7d-4c3a-8e5f-0a1b2c3d4e5f",
            ]
        )
        XCTAssertFalse(repair.contains("--native-install"))
        XCTAssertFalse(repair.contains("--workspace"))
        XCTAssertEqual(
            NativeInstallPin.current.arguments(scriptPath: "/tmp/run/install.sh", runID: runID, mode: .install),
            NativeInstallPin.current.arguments(scriptPath: "/tmp/run/install.sh", runID: runID)
        )
    }

    func testSha256HexMatchesAKnownVector() {
        XCTAssertEqual(
            NativeInstallPin.sha256Hex(Data("abc".utf8)),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )
    }

    // MARK: Decoding

    func testDecodesTheRecordedSuccessfulRun() throws {
        var decoder = decoder()
        XCTAssertEqual(try decoder.feed(recordedSuccessfulRun), recordedEvents)
        XCTAssertEqual(decoder.terminal, .serviceReady(Origin("http://127.0.0.1:8444")!))
        XCTAssertNoThrow(try decoder.finish())
    }

    func testDecodesEventsSplitAcrossArbitraryChunks() throws {
        var decoder = decoder()
        var events: [NativeInstallEvent] = []
        for byte in recordedSuccessfulRun {
            events += try decoder.feed(Data([byte]))
        }
        XCTAssertEqual(events, recordedEvents)
        XCTAssertNoThrow(try decoder.finish())
    }

    func testAcceptsSequenceGaps() throws {
        var decoder = decoder()
        let events = try decoder.feed(phaseLine(1, "preflight") + phaseLine(5, "bootstrap") + phaseLine(9, "download"))
        XCTAssertEqual(events, [.phase(.preflight), .phase(.bootstrap), .phase(.download)])
    }

    func testRejectsRepeatedOrDecreasingSequence() {
        var repeated = decoder()
        XCTAssertThrowsError(try repeated.feed(phaseLine(1, "preflight") + phaseLine(1, "bootstrap"))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .nonIncreasingSequence)
        }

        var decreasing = decoder()
        XCTAssertThrowsError(try decreasing.feed(phaseLine(5, "preflight") + phaseLine(3, "bootstrap"))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .nonIncreasingSequence)
        }
    }

    func testRejectsWrongRunIDAndSchema() {
        var wrongRun = decoder()
        XCTAssertThrowsError(try wrongRun.feed(line(["seq": 1, "type": "phase", "phase": "preflight"], runID: "00000000-0000-4000-8000-000000000000"))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .wrongRunID)
        }

        var wrongSchema = decoder()
        XCTAssertThrowsError(try wrongSchema.feed(line(["schema": 2, "seq": 1, "type": "phase", "phase": "preflight"]))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .wrongSchema)
        }
    }

    func testLineLimitIsTenTwentyFourBytesIncludingNewline() throws {
        let prefix = #"{"schema":1,"run_id":"\#(testRunID)","seq":1,"type":"phase","phase":"preflight","pad":""#
        let suffix = #""}"#
        let baseLength = prefix.utf8.count + suffix.utf8.count

        let paddingForExactLimit = String(repeating: "x", count: 1024 - 1 - baseLength)
        let exact = Data((prefix + paddingForExactLimit + suffix).utf8) + Data("\n".utf8)
        XCTAssertEqual(exact.count, 1024)
        var acceptingDecoder = decoder()
        XCTAssertEqual(try acceptingDecoder.feed(exact), [.phase(.preflight)])

        let paddingOneOver = String(repeating: "x", count: 1024 - baseLength)
        let oneOver = Data((prefix + paddingOneOver + suffix).utf8) + Data("\n".utf8)
        XCTAssertEqual(oneOver.count, 1025)
        var rejectingDecoder = decoder()
        XCTAssertThrowsError(try rejectingDecoder.feed(oneOver)) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .lineTooLong)
        }

        var noNewlineDecoder = decoder()
        XCTAssertThrowsError(try noNewlineDecoder.feed(Data(repeating: UInt8(ascii: "x"), count: 1024))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .lineTooLong)
        }
    }

    func testRejectsMoreThanThirtyTwoEvents() {
        XCTAssertEqual(NativeInstallEventDecoder.maximumEvents, 32)

        var decoder = NativeInstallEventDecoder(runID: testRunID, maximumEvents: 2)
        XCTAssertThrowsError(
            try decoder.feed(phaseLine(1, "preflight") + phaseLine(2, "bootstrap") + phaseLine(3, "download"))
        ) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .tooManyEvents)
        }
    }

    func testPhasesMustBeInOrderAndUnique() {
        var reordered = decoder()
        XCTAssertThrowsError(try reordered.feed(phaseLine(1, "wizard") + phaseLine(2, "install"))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .phaseOutOfOrder)
        }

        var repeated = decoder()
        XCTAssertThrowsError(try repeated.feed(phaseLine(1, "preflight") + phaseLine(2, "preflight"))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .phaseOutOfOrder)
        }

        var skipping = decoder()
        XCTAssertNoThrow(try skipping.feed(phaseLine(1, "preflight") + phaseLine(2, "install")))
    }

    func testWizardReadyNeedsTheWizardPhase() {
        var decoder = decoder()
        XCTAssertThrowsError(
            try decoder.feed(phaseLine(1, "install") + line(["seq": 2, "type": "wizard_ready", "origin": "http://127.0.0.1:8443"]))
        ) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .unexpectedEvent)
        }
    }

    func testSetupSavedNeedsWizardReady() {
        var decoder = decoder()
        XCTAssertThrowsError(try decoder.feed(phaseLine(1, "wizard") + line(["seq": 2, "type": "setup_saved"]))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .unexpectedEvent)
        }
    }

    func testServiceReadyNeedsTheReadinessPhase() {
        var decoder = decoder()
        XCTAssertThrowsError(
            try decoder.feed(phaseLine(1, "activation") + line(["seq": 2, "type": "service_ready", "origin": "http://127.0.0.1:8444"]))
        ) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .unexpectedEvent)
        }
    }

    func testRejectsNonLoopbackOrigins() {
        let origins = [
            "https://127.0.0.1:8443",
            "http://localhost:8443",
            "http://[::1]:8443",
            "http://127.0.0.1",
            "http://127.0.0.1:8443/",
            "http://127.0.0.1:0",
            "http://127.0.0.1:70000",
            "http://127.0.0.1:08443",
            "http://10.0.0.1:8443",
            "http://127.0.0.1:8443/path",
        ]
        for origin in origins {
            var decoder = decoder()
            XCTAssertThrowsError(
                try decoder.feed(phaseLine(1, "wizard") + line(["seq": 2, "type": "wizard_ready", "origin": origin])),
                origin
            ) {
                XCTAssertEqual($0 as? NativeInstallProtocolError, .invalidOrigin, origin)
            }
        }
    }

    func testDecodesEveryFailureCode() throws {
        let rawCodes = [
            "invalid_arguments", "existing_install", "partial_install", "unknown_state",
            "install_in_progress", "download_failed", "verification_failed", "install_failed",
            "wizard_failed", "activation_refused", "service_not_ready", "cancelled",
            "event_channel_failed", "internal_error", "not_installed",
        ]
        XCTAssertEqual(NativeInstallFailureCode.allCases.map(\.rawValue).sorted(), rawCodes.sorted())

        for code in NativeInstallFailureCode.allCases {
            var decoder = decoder()
            XCTAssertEqual(
                try decoder.feed(line(["seq": 1, "type": "failed", "code": code.rawValue])),
                [.failed(code)]
            )
            XCTAssertEqual(decoder.terminal, .failed(code))
        }
    }

    func testRejectsUnknownTypeAndFailureCode() {
        var unknownType = decoder()
        XCTAssertThrowsError(try unknownType.feed(line(["seq": 1, "type": "celebrate"]))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .unknownType)
        }

        var unknownCode = decoder()
        XCTAssertThrowsError(try unknownCode.feed(line(["seq": 1, "type": "failed", "code": "on_fire"]))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .unknownFailureCode)
        }
    }

    func testRejectsInvalidUTF8AndMalformedJSON() {
        var invalidUTF8 = decoder()
        XCTAssertThrowsError(try invalidUTF8.feed(Data([0xFF, 0x0A]))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .invalidUTF8)
        }

        var missingFields = decoder()
        XCTAssertThrowsError(try missingFields.feed(Data(#"{"schema":1}"#.utf8) + Data("\n".utf8))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .malformedJSON)
        }

        var boolSeq = decoder()
        let boolSeqLine = #"{"schema":1,"run_id":"\#(testRunID)","seq":true,"type":"setup_saved"}"#
        XCTAssertThrowsError(try boolSeq.feed(Data(boolSeqLine.utf8) + Data("\n".utf8))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .malformedJSON)
        }
    }

    func testRejectsAnEventAfterTheTerminalEvent() throws {
        var decoder = decoder()
        XCTAssertEqual(try decoder.feed(line(["seq": 1, "type": "failed", "code": "cancelled"])), [.failed(.cancelled)])
        XCTAssertThrowsError(try decoder.feed(phaseLine(2, "preflight"))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .eventAfterTerminal)
        }
    }

    func testFinishRequiresATerminalEventAndACompleteLine() throws {
        var noTerminal = decoder()
        _ = try noTerminal.feed(phaseLine(1, "preflight") + phaseLine(2, "bootstrap"))
        XCTAssertThrowsError(try noTerminal.finish()) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .missingTerminalEvent)
        }

        var truncated = decoder()
        _ = try truncated.feed(line(["seq": 1, "type": "failed", "code": "internal_error"]) + Data(#"{"sch"#.utf8))
        XCTAssertThrowsError(try truncated.finish()) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .truncatedLine)
        }
    }

    func testDecoderStaysFailedAfterAViolation() {
        var decoder = decoder()
        XCTAssertThrowsError(try decoder.feed(Data("not json\n".utf8))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .malformedJSON)
        }
        XCTAssertThrowsError(try decoder.feed(phaseLine(1, "preflight"))) {
            XCTAssertEqual($0 as? NativeInstallProtocolError, .malformedJSON)
        }
    }

    // MARK: Download causes

    func testDownloadCauseClassification() {
        XCTAssertEqual(NativeInstallDownloadCause.classify(URLError(.notConnectedToInternet)), .offline)
        XCTAssertEqual(NativeInstallDownloadCause.classify(URLError(.timedOut)), .timedOut)
        XCTAssertEqual(NativeInstallDownloadCause.classify(URLError(.serverCertificateUntrusted)), .insecureConnection)
        XCTAssertEqual(NativeInstallDownloadCause.classify(NativeInstallFetchError.unexpectedStatus(500)), .httpStatus(500))
        XCTAssertEqual(
            NativeInstallDownloadCause.classify(NativeInstallFetchError.responseTooLarge(bytes: 2_000_000, limit: 1_048_576)),
            .oversized(bytes: 2_000_000, limit: 1_048_576)
        )
        XCTAssertEqual(NativeInstallDownloadCause.classify(URLError(.cannotFindHost)), .other)
    }

    func testDownloadFailureCopyNamesTheCause() {
        let causes: [NativeInstallDownloadCause] = [
            .offline, .timedOut, .httpStatus(500),
            .oversized(bytes: 2_000_000, limit: 1_048_576),
            .insecureConnection, .other,
        ]
        let messages = causes.map { NativeInstallCopy.message(for: .downloadFailed($0)) }

        XCTAssertTrue(messages[0].contains("offline"))
        XCTAssertTrue(messages[1].contains("timed out"))
        XCTAssertTrue(messages[2].contains("HTTP 500"))
        XCTAssertTrue(messages[3].contains("larger than expected"))
        XCTAssertTrue(messages[4].contains("securely"))
        XCTAssertTrue(messages.allSatisfy { $0.contains("native-install.log") })
        XCTAssertEqual(Set(messages).count, messages.count)
    }

    // MARK: States and copy

    func testEveryFailureHasItsOwnMessage() {
        let downloadFailures: [NativeInstallFailure] = [
            .offline, .timedOut, .httpStatus(500),
            .oversized(bytes: 2_000_000, limit: 1_048_576),
            .insecureConnection, .other,
        ].map { NativeInstallFailure.downloadFailed($0) }
        let failures: [NativeInstallFailure] =
            NativeInstallFailureCode.allCases.map { .installer($0) }
            + downloadFailures
            + [.checksumMismatch, .launchFailed, .cancelledBeforeLaunch, .releaseUnverified, .repairUnavailable]
            + [.protocolViolation(.missingTerminalEvent)]
        let messages = failures.map { NativeInstallCopy.message(for: $0) }

        XCTAssertEqual(messages.count, 27)
        XCTAssertTrue(messages.allSatisfy { !$0.isEmpty })
        XCTAssertEqual(Set(messages).count, messages.count)
    }

    func testPortFailuresTellTheUserToPickAnotherPort() {
        let portFailures: [NativeInstallFailure] = [.installer(.activationRefused), .installer(.serviceNotReady)]
        let others: [NativeInstallFailure] =
            NativeInstallFailureCode.allCases.filter { $0 != .activationRefused && $0 != .serviceNotReady }.map { .installer($0) }
            + [.downloadFailed(.other), .checksumMismatch, .launchFailed, .cancelledBeforeLaunch, .protocolViolation(.missingTerminalEvent)]

        for failure in portFailures {
            XCTAssertTrue(NativeInstallCopy.message(for: failure).lowercased().contains("pick another port"))
        }
        for failure in others {
            XCTAssertFalse(NativeInstallCopy.message(for: failure).lowercased().contains("pick another port"))
        }
    }

    func testPortFailureCopyNeverClaimsTheUserChoseAPort() {
        let portFailures: [NativeInstallFailure] = [.installer(.activationRefused), .installer(.serviceNotReady)]
        for failure in portFailures {
            let message = NativeInstallCopy.message(for: failure)
            XCTAssertTrue(message.lowercased().contains("pick another port"), message)
            XCTAssertFalse(message.contains("you chose"), message)
        }

        let downloadFailures: [NativeInstallFailure] = [
            .offline, .timedOut, .httpStatus(500),
            .oversized(bytes: 2_000_000, limit: 1_048_576),
            .insecureConnection, .other,
        ].map { NativeInstallFailure.downloadFailed($0) }
        let failures: [NativeInstallFailure] =
            NativeInstallFailureCode.allCases.map { .installer($0) }
            + downloadFailures
            + [.checksumMismatch, .launchFailed, .cancelledBeforeLaunch, .releaseUnverified, .repairUnavailable]
            + [.protocolViolation(.missingTerminalEvent)]
        for failure in failures {
            XCTAssertFalse(NativeInstallCopy.message(for: failure).contains("the port you chose"),
                           NativeInstallCopy.message(for: failure))
        }
    }

    func testLockAndCancelCopyNeverPromiseCleanup() {
        let lock = NativeInstallCopy.message(for: .installer(.installInProgress))
        XCTAssertTrue(lock.contains("Recovering a native-install lock"))
        XCTAssertTrue(lock.contains("never removed automatically"))

        let cancelled = NativeInstallCopy.message(for: .installer(.cancelled))
        XCTAssertTrue(cancelled.contains("left running"))
    }

    func testOnlyTheWizardStateTrustsAnOrigin() {
        let origin = Origin("http://127.0.0.1:8443")!
        XCTAssertEqual(NativeInstallState.wizard(origin).wizardOrigin, origin)
        let untrusted: [NativeInstallState] = [
            .idle, .downloading, .verifying, .running(nil), .running(.wizard),
            .cancelling, .succeeded(origin), .failed(.cancelledBeforeLaunch),
        ]
        for state in untrusted {
            XCTAssertNil(state.wizardOrigin)
        }

        let active: [NativeInstallState] = [.downloading, .verifying, .running(nil), .running(.wizard), .wizard(origin), .cancelling]
        let inactive: [NativeInstallState] = [.idle, .succeeded(origin), .failed(.cancelledBeforeLaunch)]
        XCTAssertTrue(active.allSatisfy(\.isActive))
        XCTAssertTrue(inactive.allSatisfy { !$0.isActive })
    }

    // MARK: Native repair

    private func assertRejects(_ stream: Data, mode: NativeInstallMode, _ expected: NativeInstallProtocolError,
                               file: StaticString = #filePath, line: UInt = #line) {
        var decoder = NativeInstallEventDecoder(runID: testRunID, mode: mode)
        XCTAssertThrowsError(try decoder.feed(stream), file: file, line: line) { error in
            XCTAssertEqual(error as? NativeInstallProtocolError, expected, file: file, line: line)
        }
    }

    func testDecodesTheRepairSequence() throws {
        var stream = Data()
        stream.append(phaseLine(1, "preflight"))
        stream.append(phaseLine(2, "bootstrap"))
        stream.append(phaseLine(3, "download"))
        stream.append(phaseLine(4, "install"))
        stream.append(phaseLine(5, "activation"))
        stream.append(phaseLine(6, "readiness"))
        stream.append(line(["seq": 7, "type": "service_ready", "origin": "http://127.0.0.1:8443"]))

        var decoder = NativeInstallEventDecoder(runID: testRunID, mode: .repair)
        XCTAssertEqual(try decoder.feed(stream), [
            .phase(.preflight),
            .phase(.bootstrap),
            .phase(.download),
            .phase(.install),
            .phase(.activation),
            .phase(.readiness),
            .serviceReady(Origin("http://127.0.0.1:8443")!),
        ])
        XCTAssertEqual(decoder.terminal, .serviceReady(Origin("http://127.0.0.1:8443")!))
        XCTAssertNoThrow(try decoder.finish())
    }

    func testRepairRejectsTheWizardPhaseAndWizardEvents() throws {
        var wizardPhase = Data()
        wizardPhase.append(phaseLine(1, "install"))
        wizardPhase.append(phaseLine(2, "wizard"))
        assertRejects(wizardPhase, mode: .repair, .unexpectedEvent)

        var wizardReady = phaseLine(1, "install")
        wizardReady.append(line(["seq": 2, "type": "wizard_ready", "origin": "http://127.0.0.1:8443"]))
        assertRejects(wizardReady, mode: .repair, .unexpectedEvent)

        var setupSaved = phaseLine(1, "install")
        setupSaved.append(line(["seq": 2, "type": "setup_saved"]))
        assertRejects(setupSaved, mode: .repair, .unexpectedEvent)

        // Install mode is unchanged: the wizard phase and its ready event are accepted.
        var install = phaseLine(1, "wizard")
        install.append(line(["seq": 2, "type": "wizard_ready", "origin": "http://127.0.0.1:8443"]))
        var installDecoder = NativeInstallEventDecoder(runID: testRunID)
        XCTAssertEqual(try installDecoder.feed(install), [
            .phase(.wizard),
            .wizardReady(Origin("http://127.0.0.1:8443")!),
        ])
    }

    func testRepairDecodesNotInstalled() throws {
        var stream = phaseLine(1, "preflight")
        stream.append(line(["seq": 2, "type": "failed", "code": "not_installed"]))

        var decoder = NativeInstallEventDecoder(runID: testRunID, mode: .repair)
        XCTAssertEqual(try decoder.feed(stream), [.phase(.preflight), .failed(.notInstalled)])
        XCTAssertEqual(decoder.terminal, .failed(.notInstalled))
    }

    func testRepairFailureCopy() {
        let notInstalled = NativeInstallCopy.message(for: .installer(.notInstalled))
        XCTAssertTrue(notInstalled.contains(NativeInstallFlowCopy.installButton))
        XCTAssertTrue(notInstalled.contains("nothing to repair"))

        let unavailable = NativeInstallCopy.message(for: .repairUnavailable)
        XCTAssertTrue(unavailable.contains("Try again"))
        XCTAssertNotEqual(unavailable, NativeInstallCopy.message(for: .releaseUnverified))
    }

    func testRepairProgressChangesOnlyTheInstallPhase() {
        XCTAssertEqual(NativeInstallCopy.progress(for: .running(.install), mode: .repair), "Repairing Ciaobot…")
        XCTAssertEqual(NativeInstallCopy.progress(for: .running(.install)), "Installing Ciaobot…")
        for phase in NativeInstallPhase.allCases where phase != .install {
            XCTAssertEqual(
                NativeInstallCopy.progress(for: .running(phase), mode: .repair),
                NativeInstallCopy.progress(for: .running(phase))
            )
        }
        let states: [NativeInstallState] = [.idle, .downloading, .verifying, .running(nil), .cancelling]
        for state in states {
            XCTAssertEqual(
                NativeInstallCopy.progress(for: state, mode: .repair),
                NativeInstallCopy.progress(for: state)
            )
        }
    }

    func testRepairCancelMessageDoesNotPromiseARunningService() {
        let repair = NativeInstallCopy.message(for: .installer(.cancelled), mode: .repair)
        XCTAssertTrue(repair.contains("run Repair again"))
        XCTAssertFalse(repair.contains("left running"))

        let install = NativeInstallCopy.message(for: .installer(.cancelled), mode: .install)
        XCTAssertEqual(install, NativeInstallCopy.message(for: .installer(.cancelled)))
        XCTAssertTrue(install.contains("left running"))
    }
}
