import SwiftUI

/// The local-mode engine screen (#3129 S5b): what the user sees while the
/// app's bundled engine starts, and the actions they can take when it will
/// not. `ConnectionView` shows it instead of the connection screen; the
/// connected web view replaces it once the supervisor publishes a running
/// origin.
@MainActor
struct LocalEngineView: View {
    @ObservedObject var supervisor: EngineSupervisor
    /// Every running origin the supervisor publishes. Idempotent: an origin
    /// that is already connected is ignored by the receiver.
    let onRunning: (Origin) -> Void
    /// **Connect to Another Server…** stops the engine, saves the external
    /// choice and shows the ordinary connection screen. S6 adds the
    /// confirmation dialog and the menu command around this.
    let onConnectExternal: () -> Void
    /// Opens the engine log. The default opens it with the system handler.
    var openLog: (URL) -> Void = { NSWorkspace.shared.open($0) }

    var body: some View {
        content
            // A `.running` state that existed before this screen appeared (the
            // connection failed and the web view handed the screen back) has
            // no change to observe, so reconnect here too.
            .onAppear {
                if let origin = runningOrigin { onRunning(origin) }
            }
    }

    @ViewBuilder
    private var content: some View {
        VStack(alignment: .leading, spacing: 16) {
            switch supervisor.state {
            case .stopped, .starting, .running:
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small).accessibilityHidden(true)
                    Text(statusText)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            case .stopping:
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small).accessibilityHidden(true)
                    Text("Stopping the Ciaobot engine…")
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            case .portInUse(let port):
                engineActions(message: Self.portInUseMessage(port), retry: { supervisor.start() })
            case .failed(let message):
                engineActions(message: message, retry: { supervisor.restartAfterFailure() })
            }
        }
        .padding(32)
        .frame(maxWidth: 520)
    }

    private func engineActions(
        message: String,
        retry: @escaping () -> Void
    ) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(message)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("Try Again", action: retry)
                    .accessibilityHint("Starts the engine again.")
                Button("Show Log") { openLog(supervisor.home.logFile) }
                    .accessibilityHint("Opens the engine log from this launch.")
                Spacer(minLength: 8)
                Button("Connect to Another Server…", action: onConnectExternal)
                    .accessibilityHint("Stops the engine and shows the connection screen for another server.")
            }
        }
    }

    private var statusText: String {
        if case .running = supervisor.state { return "Connecting to the Ciaobot engine…" }
        return "Starting Ciaobot…"
    }

    private var runningOrigin: Origin? {
        if case .running(let origin, _, _) = supervisor.state { return origin }
        return nil
    }

    static func portInUseMessage(_ port: Int) -> String {
        "The Ciaobot engine could not start: port \(port) on this Mac is already in use. Quit the program using that port, then try again."
    }
}

/// Silently observes the engine for the whole local-mode session, mounted for
/// as long as the engine mode is local — including while the web view is
/// connected, when the engine status screen itself is not. Every published
/// running origin is handed to the connection: the first connect, an engine
/// restart on a new port (the origin changes), or the return to this screen.
@MainActor
struct EngineRunningWatcher: View {
    @ObservedObject var supervisor: EngineSupervisor
    let onRunning: (Origin) -> Void

    private var runningOrigin: Origin? {
        if case .running(let origin, _, _) = supervisor.state { return origin }
        return nil
    }

    var body: some View {
        Color.clear
            .frame(width: 0, height: 0)
            .accessibilityHidden(true)
            .onChange(of: runningOrigin) { origin in
                guard let origin else { return }
                onRunning(origin)
            }
            .onAppear {
                if let origin = runningOrigin { onRunning(origin) }
            }
    }
}
