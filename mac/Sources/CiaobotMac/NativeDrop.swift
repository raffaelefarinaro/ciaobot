import AppKit
import Foundation
import WebKit

/// "Use in place" for a file dropped on, or picked in, the local engine's
/// window: the native half of the engine's desktop-drop grant.
///
/// A browser never learns where a dropped file lives, so the page cannot name
/// a path and the engine never takes one from it. Only this app sees the file
/// URLs (on the drag pasteboard, or from an open panel). It writes them into a
/// single-use grant file in the engine's runtime folder
/// (`<runtime root>/desktop-drop-grants/<uuid>.json`, the file
/// `POST /api/desktop-drop` consumes) and hands the page only the grant id and
/// the display names. The engine validates every path again and returns
/// opaque references; the absolute path reaches the agent's prompt alone.
///
/// Offered only for the app's own local engine: a remote engine cannot read a
/// grant file written on this Mac, so a remote window gets no handler and the
/// page shows only "Attach a copy".
struct NativeDropGrant: Equatable {
    /// Lower-case canonical UUID, as the engine requires.
    let id: String
    let names: [String]

    /// What the page receives: no path, only the id and names.
    var payload: [String: Any] { ["grantId": id, "names": names] }
}

enum NativeDropError: Error, Equatable {
    case noRuntimeRoot
    case invalidPaths
    case writeFailed
}

/// Writes grants the engine's `_consume_desktop_drop_grant` accepts:
/// `{"created_at": <epoch seconds>, "paths": [<absolute path>, ...]}`, at most
/// 100 paths of at most 4096 bytes each, owner-only, renamed into place whole.
struct NativeDropGrantWriter {
    static let maxPaths = 100
    static let maxPathBytes = 4096
    static let maxPointerBytes = 64 * 1024

    /// `EngineHome.pointer`: the app-owned `workspace.json` naming the
    /// engine's runtime root. Read at each write, so a moved workspace is seen.
    let pointer: URL

    func grantsDirectory() throws -> URL {
        guard let data = try? Data(contentsOf: pointer), data.count <= Self.maxPointerBytes,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let root = object["runtime_root"] as? String, root.hasPrefix("/"),
              !root.split(separator: "/").contains("..") else {
            throw NativeDropError.noRuntimeRoot
        }
        return URL(fileURLWithPath: root, isDirectory: true)
            .appendingPathComponent("desktop-drop-grants", isDirectory: true)
    }

    func write(_ urls: [URL], now: Date = Date(), id: UUID = UUID()) throws -> NativeDropGrant {
        let paths = urls.map { $0.standardizedFileURL.path }
        guard !urls.isEmpty, urls.count <= Self.maxPaths,
              urls.allSatisfy(\.isFileURL),
              paths.allSatisfy({ $0.hasPrefix("/") && $0.utf8.count <= Self.maxPathBytes }) else {
            throw NativeDropError.invalidPaths
        }
        let directory = try grantsDirectory()
        let manager = FileManager.default
        let grantID = id.uuidString.lowercased()
        let payload: [String: Any] = ["created_at": now.timeIntervalSince1970, "paths": paths]
        do {
            try manager.createDirectory(at: directory, withIntermediateDirectories: true,
                                        attributes: [.posixPermissions: 0o700])
            let data = try JSONSerialization.data(withJSONObject: payload)
            let staging = directory.appendingPathComponent(".\(grantID).writing")
            guard manager.createFile(atPath: staging.path, contents: data,
                                     attributes: [.posixPermissions: 0o600]) else {
                throw NativeDropError.writeFailed
            }
            do {
                try manager.moveItem(at: staging, to: directory.appendingPathComponent("\(grantID).json"))
            } catch {
                try? manager.removeItem(at: staging)
                throw error
            }
        } catch {
            throw NativeDropError.writeFailed
        }
        return NativeDropGrant(id: grantID, names: urls.map { Self.displayName($0) })
    }

    /// The last path component without control characters, bounded; the page
    /// shows it on a chip and the engine sends its own name back anyway.
    static func displayName(_ url: URL) -> String {
        let name = String(url.lastPathComponent.unicodeScalars
            .filter { !CharacterSet.controlCharacters.contains($0) }
            .map(Character.init))
            .trimmingCharacters(in: .whitespaces)
        return String((name.isEmpty ? "file" : name).prefix(255))
    }
}

/// The file URLs of the last drop on the web view. WebKit gives the page the
/// drop after `performDragOperation` records it, so the page's request for a
/// grant always finds it; it is handed out once and only for a short while.
final class NativeDropCapture {
    static let validity: TimeInterval = 30
    private var pending: (urls: [URL], at: Date)?

    func record(_ urls: [URL], now: Date = Date()) {
        pending = urls.isEmpty ? nil : (urls, now)
    }

    func take(now: Date = Date()) -> [URL]? {
        defer { pending = nil }
        guard let pending else { return nil }
        let age = now.timeIntervalSince(pending.at)
        guard age >= 0, age <= Self.validity else { return nil }
        return pending.urls
    }
}

/// A `WKWebView` that notes a drop's file URLs before WebKit hands the drop to
/// the page. It does not change what the page receives.
final class FileDropWebView: WKWebView {
    var onFileDrop: (([URL]) -> Void)?

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        onFileDrop?(Self.fileURLs(on: sender.draggingPasteboard))
        return super.performDragOperation(sender)
    }

    static func fileURLs(on pasteboard: NSPasteboard) -> [URL] {
        let objects = pasteboard.readObjects(forClasses: [NSURL.self],
                                             options: [.urlReadingFileURLsOnly: true]) ?? []
        return objects.compactMap { $0 as? URL }.filter(\.isFileURL)
    }
}

typealias NativeDropPicker = (@escaping ([URL]?) -> Void) -> Void

/// The window's "Use in place" bridge: the last drop, the grant writer and the
/// open panel for the keyboard path.
@MainActor
final class NativeDropBridge {
    let writer: NativeDropGrantWriter
    let capture = NativeDropCapture()
    private let picker: NativeDropPicker

    init(writer: NativeDropGrantWriter, picker: NativeDropPicker? = nil) {
        self.writer = writer
        self.picker = picker ?? NativeDropBridge.presentOpenPanel
    }

    /// The grant for the drop the page just received; nil when there was none.
    func grantForLastDrop(now: Date = Date()) throws -> NativeDropGrant? {
        guard let urls = capture.take(now: now) else { return nil }
        return try writer.write(urls, now: now)
    }

    /// Files and folders the user picks; nil when the panel was cancelled.
    func pick(completion: @escaping (Result<NativeDropGrant?, Error>) -> Void) {
        picker { [writer] urls in
            guard let urls, !urls.isEmpty else { return completion(.success(nil)) }
            completion(Result { try writer.write(urls) })
        }
    }

    private static var panelOpen = false

    private static func presentOpenPanel(_ completion: @escaping ([URL]?) -> Void) {
        guard !panelOpen else { return completion(nil) }
        panelOpen = true
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.canCreateDirectories = false
        panel.resolvesAliases = true
        panel.prompt = "Use in Place"
        panel.message = "Ciaobot hands the agent these paths. Nothing is copied."
        panel.begin { response in
            panelOpen = false
            completion(response == .OK ? panel.urls : nil)
        }
    }
}
