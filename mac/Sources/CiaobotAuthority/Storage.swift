import Darwin
import CryptoKit
import Foundation

public struct StoreError: Error, Equatable, CustomStringConvertible {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var description: String { message }
}

/// A directory the daemon owns: owned by the effective uid, mode exactly 0700,
/// never reached through a symlink. File access inside a directory is relative to
/// its open descriptor. `subdirectory(_:)` reopens by path (`O_NOFOLLOW` on the
/// final component, then the same owner and mode checks), so it is only as safe
/// as its already-verified, owner-only parent: swapping a child needs the owner.
/// descriptor, so swapping the path afterwards changes nothing. Files are 0600,
/// owned by the same uid, and read with `O_NOFOLLOW`.
final class PrivateDirectory {
    let path: String
    private let fd: Int32

    init(path: String) throws {
        self.path = path
        let fd = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { throw StoreError("cannot open \(path): \(String(cString: strerror(errno)))") }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == geteuid(), info.st_mode & 0o7777 == 0o700 else {
            close(fd)
            throw StoreError("\(path) must be owned by the daemon user with mode 0700")
        }
        self.fd = fd
    }

    deinit { close(fd) }

    /// Create (mode 0700) and open. The parent must exist; an existing entry is an error.
    static func create(path: String) throws -> PrivateDirectory {
        guard mkdir(path, 0o700) == 0 else { throw StoreError("cannot create \(path): \(String(cString: strerror(errno)))") }
        // mkdir applies the umask; make the mode exact.
        guard chmod(path, 0o700) == 0 else { throw StoreError("cannot chmod \(path)") }
        return try PrivateDirectory(path: path)
    }

    func subdirectory(_ name: String) throws -> PrivateDirectory {
        try PrivateDirectory(path: path + "/" + name)
    }

    func createSubdirectory(_ name: String) throws -> PrivateDirectory {
        try Self.create(path: path + "/" + name)
    }

    /// nil when the file does not exist.
    func read(_ name: String, maxBytes: Int) throws -> [UInt8]? {
        let file = openat(fd, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        if file < 0 {
            if errno == ENOENT { return nil }
            throw StoreError("cannot open \(name): \(String(cString: strerror(errno)))")
        }
        defer { close(file) }
        var info = stat()
        guard fstat(file, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_uid == geteuid(),
            info.st_mode & 0o077 == 0, Int(info.st_size) <= maxBytes
        else { throw StoreError("\(name) is not an owner-private regular file within its size limit") }
        let size = Int(info.st_size)
        var out = [UInt8](repeating: 0, count: size)
        var done = 0
        while done < size {
            let n = out.withUnsafeMutableBytes { Darwin.read(file, $0.baseAddress! + done, size - done) }
            if n < 0 { if errno == EINTR { continue }; throw StoreError("read failed: \(name)") }
            if n == 0 { throw StoreError("short read: \(name)") }
            done += n
        }
        return out
    }

    private func fullSync(_ descriptor: Int32) throws {
        // F_FULLFSYNC flushes the drive cache; plain fsync does not on macOS.
        if fcntl(descriptor, F_FULLFSYNC) != 0 && fsync(descriptor) != 0 { throw StoreError("sync failed") }
    }

    private func writeTemp(_ data: [UInt8]) throws -> String {
        let name = ".tmp-" + Hex.encode((0..<8).map { _ in UInt8.random(in: 0...255) })
        let file = openat(fd, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard file >= 0 else { throw StoreError("cannot create temp file: \(String(cString: strerror(errno)))") }
        var ok = true
        var done = 0
        while done < data.count {
            let n = data.withUnsafeBytes { Darwin.write(file, $0.baseAddress! + done, data.count - done) }
            if n < 0 { if errno == EINTR { continue }; ok = false; break }
            done += n
        }
        if ok { ok = (try? fullSync(file)) != nil }
        close(file)
        if !ok { unlinkat(fd, name, 0); throw StoreError("cannot write temp file") }
        return name
    }

    /// Replace `name` atomically and durably: temp file, sync, rename, directory sync.
    func writeAtomic(_ name: String, _ data: [UInt8]) throws {
        let temp = try writeTemp(data)
        guard renameat(fd, temp, fd, name) == 0 else {
            unlinkat(fd, temp, 0)
            throw StoreError("cannot rename into place: \(String(cString: strerror(errno)))")
        }
        try fullSync(fd)
    }

    /// Create `name` atomically and durably, failing if it already exists.
    func createExclusive(_ name: String, _ data: [UInt8]) throws {
        let temp = try writeTemp(data)
        defer { unlinkat(fd, temp, 0) }
        guard linkat(fd, temp, fd, name, 0) == 0 else {
            throw StoreError(errno == EEXIST ? "\(name) already exists" : "cannot create \(name): \(String(cString: strerror(errno)))")
        }
        try fullSync(fd)
    }

    /// Exclusive, non-blocking `flock` on a file in this directory, held until the returned descriptor is closed.
    func lockFile(_ name: String) throws -> Int32 {
        let file = openat(fd, name, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard file >= 0 else { throw StoreError("cannot open lock file") }
        guard flock(file, LOCK_EX | LOCK_NB) == 0 else {
            close(file)
            throw StoreError("state directory is locked by another process")
        }
        return file
    }
}

/// The counter anchor as a root-private file. In production it lives in a
/// separate root-owned process; here it is this file under `anchor/`.
public final class FileCounterAnchor: CounterAnchor {
    private let dir: PrivateDirectory
    private static let name = "counter"

    init(dir: PrivateDirectory) { self.dir = dir }

    static func record(_ r: AnchorRecord) -> [UInt8] {
        func digest(_ d: [UInt8]?) -> JSONValue { d.map { .string(Hex.encode($0)) } ?? .null }
        return JSON.canonical(
            .object([
                "counter": .int(r.counter), "current_sha256": digest(r.currentDigest),
                "previous_sha256": digest(r.previousDigest),
            ]))
    }

    public func read() throws -> AnchorRecord {
        let data: [UInt8]?
        do { data = try dir.read(Self.name, maxBytes: 4096) } catch { throw AuthorityError("anchor_invalid") }
        guard let data else { throw AuthorityError("anchor_missing") }
        guard let obj = (try? JSON.parse(data))?.objectValue, obj.count == 3, let counter = obj["counter"]?.intValue,
            counter >= 0
        else { throw AuthorityError("anchor_invalid") }
        func digest(_ key: String) throws -> [UInt8]? {
            guard let v = obj[key] else { throw AuthorityError("anchor_invalid") }
            if v.isNull { return nil }
            guard let text = v.stringValue, let bytes = Hex.decode(text), bytes.count == 32 else {
                throw AuthorityError("anchor_invalid")
            }
            return bytes
        }
        let record = AnchorRecord(counter: counter, currentDigest: try digest("current_sha256"), previousDigest: try digest("previous_sha256"))
        guard Self.record(record) == data else { throw AuthorityError("anchor_invalid") }
        return record
    }

    public func advance(to: Int64, blobSHA256: [UInt8]) throws {
        let current = try read()
        guard to == current.counter + 1, blobSHA256.count == 32 else { throw StoreError("anchor only moves forward by one") }
        try dir.writeAtomic(
            Self.name, Self.record(AnchorRecord(counter: to, currentDigest: blobSHA256, previousDigest: current.currentDigest)))
    }
}

public final class FileBlobStorage: AuthorityStorage {
    private let dir: PrivateDirectory
    private static let name = "state.blob"
    static let maxBlobBytes = 32 * 1024 * 1024

    init(dir: PrivateDirectory) { self.dir = dir }

    public func read() throws -> [UInt8]? { try dir.read(Self.name, maxBytes: Self.maxBlobBytes) }
    public func write(_ data: [UInt8]) throws { try dir.writeAtomic(Self.name, data) }
}

public struct Enrollment: Equatable {
    public var operatorPublicKeyX963: [UInt8]
    /// The console user whose euid may read (the enrolling user).
    public var readerUID: UInt32
}

/// The owner-private layout under a configurable root:
///
///     <root>/            0700  (production: /var/ciao-authority, root-owned)
///       state/           0700  state.blob, event.seed, state.key, enrollment.json, .lock
///       anchor/          0700  counter
///
/// Opening verifies ownership and modes, takes an exclusive `flock` held for
/// the store's lifetime, and never creates or repairs anything: a missing key,
/// anchor or directory is an error and the daemon stays closed.
public final class AuthorityStore {
    public static let productionRoot = "/var/ciao-authority"

    public let anchor: FileCounterAnchor
    public let storage: FileBlobStorage
    public let eventSeed: [UInt8]
    public let stateKey: [UInt8]
    public let enrollment: Enrollment?
    private let lockFD: Int32

    public init(root: String) throws {
        let rootDir = try PrivateDirectory(path: root)
        let state = try rootDir.subdirectory("state")
        let anchorDir = try rootDir.subdirectory("anchor")
        lockFD = try state.lockFile(".lock")
        guard let seed = try state.read("event.seed", maxBytes: 32), seed.count == 32,
            let key = try state.read("state.key", maxBytes: 32), key.count == 32
        else {
            close(lockFD)
            throw StoreError("keys are missing: run provision")
        }
        eventSeed = seed
        stateKey = key
        enrollment = try Self.loadEnrollment(state)
        anchor = FileCounterAnchor(dir: anchorDir)
        storage = FileBlobStorage(dir: state)
    }

    deinit { close(lockFD) }

    private static func loadEnrollment(_ state: PrivateDirectory) throws -> Enrollment? {
        guard let data = try state.read("enrollment.json", maxBytes: 4096) else { return nil }
        guard let obj = (try? JSON.parse(data))?.objectValue, obj.count == 2,
            let hex = obj["operator_public_key_hex"]?.stringValue, let key = Hex.decode(hex),
            let uid = obj["reader_uid"]?.intValue, (0...Int64(UInt32.max)).contains(uid),
            (try? OperatorVerifier(publicKeyX963: key)) != nil
        else { throw StoreError("enrollment.json is invalid") }
        return Enrollment(operatorPublicKeyX963: key, readerUID: UInt32(uid))
    }

    /// Create the layout, both keys and the anchor at counter 0. Refuses an existing root.
    public static func provision(root: String) throws {
        let rootDir = try PrivateDirectory.create(path: root)
        let state = try rootDir.createSubdirectory("state")
        let anchorDir = try rootDir.createSubdirectory("anchor")
        func random32() -> [UInt8] { SymmetricKey(size: .bits256).withUnsafeBytes { Array($0) } }
        let seed = random32()
        var key = random32()
        while key == seed { key = random32() }
        try state.createExclusive("event.seed", seed)
        try state.createExclusive("state.key", key)
        try anchorDir.createExclusive("counter", FileCounterAnchor.record(AnchorRecord(counter: 0)))
    }

    /// Store the operator public key and the reader uid, once. There is no
    /// replace: changing the key means destroying the root and provisioning again.
    /// (The XPC `enroll_operator_key` op and its admin authorization are a later slice.)
    public static func enroll(root: String, operatorPublicKeyX963: [UInt8], readerUID: UInt32) throws {
        _ = try OperatorVerifier(publicKeyX963: operatorPublicKeyX963)
        let state = try PrivateDirectory(path: root).subdirectory("state")
        let body = JSON.canonical(
            .object([
                "operator_public_key_hex": .string(Hex.encode(operatorPublicKeyX963)),
                "reader_uid": .int(Int64(readerUID)),
            ]))
        try state.createExclusive("enrollment.json", body)
    }

    /// Build the authority. Throws `not_enrolled` before enrollment, or any load refusal.
    public func openAuthority(newID: @escaping () -> String = AuthorityStore.randomID) throws -> ChatAuthority {
        guard let enrollment else { throw AuthorityError("not_enrolled") }
        return try ChatAuthority(
            eventSigner: try EventSigner(seed: eventSeed), stateMAC: try StateMAC(key: stateKey),
            operatorKey: try OperatorVerifier(publicKeyX963: enrollment.operatorPublicKeyX963),
            storage: storage, anchor: anchor, newID: newID)
    }

    /// 128 random bits, never repeated across restarts in practice.
    public static func randomID() -> String { Hex.encode(SymmetricKey(size: .bits128).withUnsafeBytes { Array($0) }) }
}
