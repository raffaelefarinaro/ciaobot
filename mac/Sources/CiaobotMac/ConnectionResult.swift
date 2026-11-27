import SwiftUI
import AppKit

/// What the connection screen says about the last "Use Ciaobot on This Mac"
/// check: a running check, or a settled result that stays until the next one.
/// A plain value so the mapping from a takeover outcome to title, reason and
/// next action is tested without rendering the view.
struct ConnectionResult: Equatable, Identifiable {
    enum Kind: Equatable {
        /// The check is running.
        case checking
        /// Nothing is wrong; the existing install stays or the move finished.
        case info
        /// Nothing was lost; trying again may work.
        case attention
        /// The move failed in a way that needs the user to act elsewhere.
        case failure
    }

    let id = UUID()
    let kind: Kind
    let title: String
    /// The fixed copy of the outcome (`TakeoverCopy`), unchanged.
    let reason: String
    /// What to do next when the reason alone does not say it.
    let nextStep: String?
    /// True when another run of the same check can get past this result.
    let canTryAgain: Bool
    /// The existing Ciaobot's address, when it answered.
    let existingAddress: String?
    let checkedAt: Date
    /// True for a completed move: nothing to act on, so the banner clears
    /// itself and shows no "Checked" line.
    let succeeded: Bool

    /// Seconds before a completed-move banner clears itself. Warnings and
    /// errors stay until dismissed.
    static let successAutoClearSeconds: TimeInterval = 8
    var autoClearDelay: TimeInterval? { succeeded ? Self.successAutoClearSeconds : nil }

    static let dismissTitle = "Dismiss"

    static let checkingTitle = "Checking the Ciaobot on this Mac…"
    static let tryAgainTitle = "Try again"

    static func checking(now: Date = Date()) -> ConnectionResult {
        ConnectionResult(kind: .checking, title: checkingTitle, reason: "", nextStep: nil,
                         canTryAgain: false, existingAddress: nil, checkedAt: now, succeeded: false)
    }

    /// The result card for a settled takeover, or nil when the screen says
    /// nothing (no old install, or the user chose Later).
    init?(outcome: EngineTakeover.Outcome, now: Date = Date()) {
        guard let reason = TakeoverCopy.notice(for: outcome) else { return nil }
        let origin: Origin?
        let detail: Detail
        switch outcome {
        case .notNeeded, .deferred:
            return nil
        case .done:
            origin = nil
            detail = Detail(.info, TakeoverCopy.doneTitle)
        case .rolledBack(_, let old):
            origin = old
            detail = Detail(.attention, "Moving into the app did not finish", retry: true)
        case .refused(let code, let old):
            origin = old
            detail = Self.detail(forRefusal: code)
        }
        self.init(kind: detail.kind, title: detail.title, reason: reason, nextStep: detail.nextStep,
                  canTryAgain: detail.retry, existingAddress: origin?.url.absoluteString, checkedAt: now,
                  succeeded: { if case .done = outcome { return true } else { return false } }())
    }

    private init(kind: Kind, title: String, reason: String, nextStep: String?,
                 canTryAgain: Bool, existingAddress: String?, checkedAt: Date, succeeded: Bool) {
        self.succeeded = succeeded
        self.kind = kind
        self.title = title
        self.reason = reason
        self.nextStep = nextStep
        self.canTryAgain = canTryAgain
        self.existingAddress = existingAddress
        self.checkedAt = checkedAt
    }

    private struct Detail {
        let kind: Kind
        let title: String
        let retry: Bool
        let nextStep: String?
        init(_ kind: Kind, _ title: String, retry: Bool = false, nextStep: String? = nil) {
            self.kind = kind
            self.title = title
            self.retry = retry
            self.nextStep = nextStep
        }
    }

    private static let keepUsing = "Keep using your existing Ciaobot: enter its address below."

    private static func detail(forRefusal code: String) -> Detail {
        if code == "rollback_failed" {
            return Detail(.failure, "Your existing Ciaobot could not be restored")
        }
        if TakeoverRefusal.stateUnknown.contains(code) {
            return Detail(.attention, "Could not check your existing Ciaobot", retry: true)
        }
        switch code {
        case "downgrade":
            return Detail(.info, "Your Ciaobot on this Mac is newer", nextStep: keepUsing)
        case "engine_too_old":
            return Detail(.attention, "Your Ciaobot on this Mac is too old to move",
                          retry: true, nextStep: "Update it to the latest Ciaobot, then try again, or keep using it below.")
        case "custom_data_location":
            return Detail(.info, "Your data is in a custom location", nextStep: keepUsing)
        case "port_mismatch":
            return Detail(.info, "Your Ciaobot uses a different port", nextStep: keepUsing)
        case "legacy_desktop", "unknown_program":
            return Detail(.info, "Your Ciaobot was installed another way", nextStep: keepUsing)
        case "no_workspace":
            return Detail(.info, "Your Ciaobot data was not found", nextStep: keepUsing)
        case "unfinished_migration":
            return Detail(.attention, "A Ciaobot 1.10 update is not finished", retry: true)
        case "already_settled":
            return Detail(.info, "The move already completed", retry: true)
        case "plist_changed":
            return Detail(.attention, "Your Ciaobot changed during the check", retry: true)
        case "plist_changed_stopped":
            return Detail(.failure, "Your previous Ciaobot was changed")
        case "moved_not_ready":
            return Detail(.failure, "Ciaobot moved in but could not start", retry: true)
        case "cancelled_past_trash":
            return Detail(.attention, "The move did not finish", retry: true)
        default:
            return Detail(.attention, "Ciaobot could not be moved in just now", retry: true)
        }
    }

    /// "Checked just now" is what tells a repeated, identical result from a
    /// stale one. Past a minute the clock time stays put.
    func checkedLabel(now: Date) -> String {
        let seconds = now.timeIntervalSince(checkedAt)
        if seconds < 10 { return "Checked just now" }
        if seconds < 60 { return "Checked \(Int(seconds)) seconds ago" }
        return "Checked at " + checkedAt.formatted(date: .omitted, time: .shortened)
    }

    /// One announcement for VoiceOver when the result changes.
    var announcement: String {
        kind == .checking ? title : [title, reason, nextStep].compactMap { $0 }.joined(separator: " ")
    }
}

/// What the sign-in persistence control shows for the address in the field.
/// The toggle appears only where it can matter; elsewhere one line says why.
enum SignInPersistenceHint: Equatable {
    case hidden
    /// The toggle, with the full explanation as its help text.
    case toggle(help: String)
    case line(String)

    static let httpsOnlyLine = "Signed-in sessions are kept only for https addresses."

    static func make(origin: Origin?, canPersist: Bool, explanation: String) -> SignInPersistenceHint {
        guard let origin else { return .hidden }
        if !origin.usesTLS { return .line(httpsOnlyLine) }
        return canPersist ? .toggle(help: explanation) : .line(explanation)
    }
}

/// The result card of the connection screen: an icon, a title, one reason, the
/// next step, and Try again where a retry can help. System fonts and colors.
struct ConnectionResultCard: View {
    let result: ConnectionResult
    /// Try again is offered only where running the check is safe: not over a
    /// live connection.
    var onTryAgain: (() -> Void)?
    var tryAgainDisabled = false
    /// Set where the card is a banner above the session: adds a close button.
    var onDismiss: (() -> Void)?

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            icon.frame(width: 22, height: 22)
            VStack(alignment: .leading, spacing: 6) {
                Text(result.title).font(.headline)
                if result.kind != .checking {
                    Text(result.reason).foregroundStyle(.secondary)
                    if let nextStep = result.nextStep { Text(nextStep) }
                    if let address = result.existingAddress {
                        Text("Your existing Ciaobot: \(address)").foregroundStyle(.secondary)
                    }
                    HStack(spacing: 12) {
                        if result.canTryAgain, let onTryAgain {
                            Button(ConnectionResult.tryAgainTitle, action: onTryAgain)
                                .disabled(tryAgainDisabled)
                                .accessibilityHint("Runs the check on the Ciaobot on this Mac again.")
                        }
                        if !result.succeeded {
                            TimelineView(.periodic(from: result.checkedAt, by: 5)) { context in
                                Text(result.checkedLabel(now: context.date))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                    .padding(.top, 2)
                }
            }
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            if let onDismiss {
                Button(action: onDismiss) {
                    Image(systemName: "xmark")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .frame(width: 28, height: 28)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.borderless)
                .help(ConnectionResult.dismissTitle)
                .accessibilityLabel(ConnectionResult.dismissTitle)
            }
        }
        .padding(12)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(.separator))
        .accessibilityElement(children: .contain)
        .onAppear { announce() }
    }

    @ViewBuilder
    private var icon: some View {
        switch result.kind {
        case .checking: ProgressView().controlSize(.small)
        case .info: Image(systemName: "info.circle.fill").foregroundStyle(.secondary).imageScale(.large)
        case .attention: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange).imageScale(.large)
        case .failure: Image(systemName: "xmark.octagon.fill").foregroundStyle(.red).imageScale(.large)
        }
    }

    private func announce() {
        NSAccessibility.post(
            element: NSApp as Any,
            notification: .announcementRequested,
            userInfo: [.announcement: result.announcement, .priority: NSAccessibilityPriorityLevel.high.rawValue]
        )
    }
}
