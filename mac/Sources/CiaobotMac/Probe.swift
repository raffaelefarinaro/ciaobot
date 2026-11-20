import Foundation

/// The one pre-connection check the connection screen needs. Split from the
/// URLSession implementation so a test can decide the outcome without
/// contacting any address.
protocol ProbeChecking {
    func check(_ origin: Origin, completion: @escaping (Bool) -> Void)
}

/// What a valid `/api/startup-status` answer says about the server. Only
/// display fields are kept, plus the latest-version hint for display; the
/// startup identifier is a shape check, never stored or shown.
struct StartupInfo: Equatable {
    static let maximumVersionLength = 40

    let version: String?
    let ready: Bool
    let pid: Int32?
    /// The newest engine version the server knows, when it reported one.
    var latestVersion: String? = nil
    /// True only when the server reported a JSON boolean `true`.
    var updateAvailable: Bool = false

    /// A version string that is short, printable and version-like, or nil.
    /// Shared by the running version and the latest-version hint, since both
    /// come from whatever answered the address.
    static func sanitizedVersion(_ raw: Any?) -> String? {
        guard let text = (raw as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) else { return nil }
        if text.isEmpty || text.count > maximumVersionLength
            || !text.unicodeScalars.allSatisfy({ versionScalars.contains($0) }) {
            return nil
        }
        return text
    }

    /// ASCII only: a version string is passed as a command argument, and
    /// non-ASCII digits or letters that `CharacterSet.alphanumerics` admits
    /// have no place in one.
    private static let versionScalars = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-+_".unicodeScalars)
}

final class Probe: NSObject, URLSessionDataDelegate, ProbeChecking {
    static let maximumResponseBytes = 64 * 1024
    private var body = Data()
    private var completion: ((StartupInfo?) -> Void)?

    static func valid(status: Int, data: Data) -> Bool {
        startupInfo(status: status, data: data) != nil
    }

    /// Parses a successful startup response, or nil when it is not one. A
    /// version that is not a short printable string is dropped rather than
    /// shown, since the text comes from whatever answered the address.
    static func startupInfo(status: Int, data: Data) -> StartupInfo? {
        guard (200...299).contains(status),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let id = object["startup_id"] as? String,
              !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        let version = StartupInfo.sanitizedVersion(object["version"])
        let pid = (object["pid"] as? NSNumber).flatMap { number -> Int32? in
            let value = number.int64Value
            return value > 0 && value <= Int64(Int32.max) ? Int32(value) : nil
        }
        let latest = StartupInfo.sanitizedVersion(object["latest_version"])
        // Only a JSON boolean counts: NSNumber(1) would otherwise bridge to `true`.
        let updateAvailable = (object["update_available"] as? NSNumber)
            .map { CFGetTypeID($0) == CFBooleanGetTypeID() && $0.boolValue } ?? false
        return StartupInfo(version: version, ready: object["overall_ready"] as? Bool ?? false, pid: pid,
                           latestVersion: latest, updateAvailable: updateAvailable)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }

    func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge, completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        if challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust {
            completionHandler(.performDefaultHandling, nil)
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard data.count <= Self.maximumResponseBytes - body.count else {
            dataTask.cancel()
            return
        }
        body.append(data)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let status = (task.response as? HTTPURLResponse)?.statusCode ?? 0
        let info = error == nil ? Self.startupInfo(status: status, data: body) : nil
        let callback = completion
        completion = nil
        session.finishTasksAndInvalidate()
        DispatchQueue.main.async { callback?(info) }
    }

    func check(_ origin: Origin, completion: @escaping (Bool) -> Void) {
        check(origin, configuration: .ephemeral, completion: completion)
    }

    func check(_ origin: Origin, configuration: URLSessionConfiguration, completion: @escaping (Bool) -> Void) {
        inspect(origin, configuration: configuration) { completion($0 != nil) }
    }

    /// The same credential-free request as `check`, returning the parsed
    /// startup response instead of a yes/no.
    func inspect(_ origin: Origin, configuration: URLSessionConfiguration = .ephemeral, completion: @escaping (StartupInfo?) -> Void) {
        // A fresh delegate owns each request's bounded buffer and callback.
        let delegate = Probe()
        delegate.completion = completion
        let config = configuration.copy() as! URLSessionConfiguration
        // The probe is an identity-shape check, never an authenticated
        // request. Drop anything a caller-supplied configuration could use to
        // attach a cookie or credential implicitly.
        config.httpShouldSetCookies = false
        config.httpCookieAcceptPolicy = .never
        config.httpCookieStorage = nil
        config.httpAdditionalHeaders = nil
        config.urlCredentialStorage = nil
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = min(config.timeoutIntervalForRequest, 8)
        config.timeoutIntervalForResource = min(config.timeoutIntervalForResource, 10)
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        var request = URLRequest(url: origin.url.appendingPathComponent("api/startup-status"))
        request.httpShouldHandleCookies = false
        request.cachePolicy = .reloadIgnoringLocalCacheData
        session.dataTask(with: request).resume()
    }
}
