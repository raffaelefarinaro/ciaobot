import SwiftUI

/// The local-mode engine screen (#3129 S5b): what the user sees while the
/// app's bundled engine starts, connects, or fails. Shown instead of the
/// connection screen, and in place of the web view when the engine goes down
/// while connected. It only presents; it never reconnects on its own. What
/// each state shows is `LocalEnginePresentation`.
@MainActor
struct LocalEngineView: View {
    @ObservedObject var supervisor: EngineSupervisor
    /// The connection's phase and whether a manual retry is still allowed.
    let connection: LocalConnectionModel.Phase
    let canRetry: Bool
    let onTryAgain: () -> Void
    /// **Connect to Another Server…** runs through the mode-switch
    /// confirmation (S6) and then stops the engine, saves the external choice
    /// and shows the ordinary connection screen.
    let onConnectExternal: () -> Void
    /// Opens the engine log. The default opens it with the system handler.
    var openLog: (URL) -> Void = { NSWorkspace.shared.open($0) }

    private var presentation: LocalEnginePresentation {
        LocalEnginePresentation.make(engine: supervisor.state, connection: connection, canRetry: canRetry)
    }

    var body: some View {
        let shown = presentation
        VStack(alignment: .leading, spacing: 16) {
            if shown.showsProgress {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small).accessibilityHidden(true)
                    Text(shown.message)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                VStack(alignment: .leading, spacing: 14) {
                    Text(shown.message)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 8) {
                        if shown.actions.contains(.tryAgain) {
                            Button("Try Again", action: onTryAgain)
                                .accessibilityHint("Tries the engine again.")
                        }
                        if shown.actions.contains(.showLog) {
                            Button("Show Log") { openLog(supervisor.home.logFile) }
                                .accessibilityHint("Opens the engine log from this launch.")
                        }
                        Spacer(minLength: 8)
                        if shown.actions.contains(.connectElsewhere) {
                            Button("Connect to Another Server…", action: onConnectExternal)
                                .accessibilityHint("Stops the engine and shows the connection screen for another server.")
                        }
                    }
                }
            }
        }
        .padding(32)
        .frame(maxWidth: 520)
    }
}

/// Silently observes the engine for the whole local-mode session, mounted for
/// as long as the engine mode is local, including while the web view is
/// connected, when the status screen itself is not. Every published running
/// origin is handed to the connection (the first connect, or a restart on a
/// new port), and whether the engine is down is reported so the web view can
/// give way to the status screen.
@MainActor
struct EngineRunningWatcher: View {
    @ObservedObject var supervisor: EngineSupervisor
    let onRunning: (Origin) -> Void
    let onEngineDown: (Bool) -> Void

    private var runningOrigin: Origin? {
        if case .running(let origin, _, _) = supervisor.state { return origin }
        return nil
    }

    private var isDown: Bool { LocalEnginePresentation.engineIsDown(supervisor.state) }

    var body: some View {
        Color.clear
            .frame(width: 0, height: 0)
            .accessibilityHidden(true)
            .onChange(of: runningOrigin) { origin in
                guard let origin else { return }
                onRunning(origin)
            }
            .onChange(of: isDown) { onEngineDown($0) }
            .onAppear {
                onEngineDown(isDown)
                if let origin = runningOrigin { onRunning(origin) }
            }
    }
}
