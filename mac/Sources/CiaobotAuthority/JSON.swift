import Foundation

/// A strict JSON value for the authority protocol.
///
/// The Python reference (`ciao/chat_authority.py`) is the specification. This
/// parser accepts only a subset of what Python's `json.loads` accepts, so
/// anything accepted here means the same thing there: integers only (no
/// floats, exponents or integers beyond Int64), no duplicate object keys, no
/// lone surrogate escapes, no raw control characters, and every string in NFC
/// (Swift compares strings by canonical equivalence, Python by code point, so
/// non-NFC input could make two distinct Python strings one dictionary key
/// here). Anything else is a parse error, never a silent coercion.
public enum JSONValue: Equatable, Sendable {
    case null
    case bool(Bool)
    case int(Int64)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public var stringValue: String? { if case .string(let s) = self { return s }; return nil }
    public var intValue: Int64? { if case .int(let i) = self { return i }; return nil }
    public var boolValue: Bool? { if case .bool(let b) = self { return b }; return nil }
    public var arrayValue: [JSONValue]? { if case .array(let a) = self { return a }; return nil }
    public var objectValue: [String: JSONValue]? { if case .object(let o) = self { return o }; return nil }
    public var isNull: Bool { if case .null = self { return true }; return false }
}

public struct JSONError: Error, Equatable, CustomStringConvertible {
    public let message: String
    public var description: String { "json: \(message)" }
}

public enum JSON {
    public static let maxDepth = 32

    public static func parse(_ bytes: [UInt8]) throws -> JSONValue {
        var parser = Parser(bytes: bytes)
        parser.skipSpace()
        let value = try parser.value(depth: 0)
        parser.skipSpace()
        guard parser.index == bytes.count else { throw JSONError(message: "trailing data") }
        return value
    }

    public static func parse(_ text: String) throws -> JSONValue {
        try parse(Array(text.utf8))
    }

    /// Python's `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
    public static func canonical(_ value: JSONValue) -> [UInt8] {
        var out: [UInt8] = []
        encode(value, into: &out)
        return out
    }

    /// Keys sort by Unicode scalar value, like Python's str ordering.
    public static func sortedKeys(_ keys: some Sequence<String>) -> [String] {
        keys.sorted { Array($0.unicodeScalars.map(\.value)).lexicographicallyPrecedes($1.unicodeScalars.map(\.value)) }
    }

    private static func encode(_ value: JSONValue, into out: inout [UInt8]) {
        switch value {
        case .null: out.append(contentsOf: "null".utf8)
        case .bool(let b): out.append(contentsOf: (b ? "true" : "false").utf8)
        case .int(let i): out.append(contentsOf: String(i).utf8)
        case .string(let s): encodeString(s, into: &out)
        case .array(let items):
            out.append(0x5b)
            for (n, item) in items.enumerated() {
                if n > 0 { out.append(0x2c) }
                encode(item, into: &out)
            }
            out.append(0x5d)
        case .object(let dict):
            out.append(0x7b)
            for (n, key) in sortedKeys(dict.keys).enumerated() {
                if n > 0 { out.append(0x2c) }
                encodeString(key, into: &out)
                out.append(0x3a)
                encode(dict[key]!, into: &out)
            }
            out.append(0x7d)
        }
    }

    private static let hexDigits = Array("0123456789abcdef".utf8)

    private static func encodeString(_ s: String, into out: inout [UInt8]) {
        out.append(0x22)
        func u(_ unit: UInt16) {
            out.append(contentsOf: [0x5c, 0x75])
            for shift in stride(from: 12, through: 0, by: -4) {
                out.append(hexDigits[Int((unit >> UInt16(shift)) & 0xf)])
            }
        }
        for scalar in s.unicodeScalars {
            switch scalar.value {
            case 0x22: out.append(contentsOf: [0x5c, 0x22])
            case 0x5c: out.append(contentsOf: [0x5c, 0x5c])
            case 0x0a: out.append(contentsOf: [0x5c, 0x6e])
            case 0x0d: out.append(contentsOf: [0x5c, 0x72])
            case 0x09: out.append(contentsOf: [0x5c, 0x74])
            case 0x08: out.append(contentsOf: [0x5c, 0x62])
            case 0x0c: out.append(contentsOf: [0x5c, 0x66])
            case 0x20...0x7e: out.append(UInt8(scalar.value))
            case 0..<0x10000: u(UInt16(scalar.value))
            default:
                let v = scalar.value - 0x10000
                u(UInt16(0xd800 + (v >> 10)))
                u(UInt16(0xdc00 + (v & 0x3ff)))
            }
        }
        out.append(0x22)
    }

    private struct Parser {
        let bytes: [UInt8]
        var index = 0

        init(bytes: [UInt8]) { self.bytes = bytes }

        mutating func skipSpace() {
            while index < bytes.count, [0x20, 0x09, 0x0a, 0x0d].contains(bytes[index]) { index += 1 }
        }

        mutating func value(depth: Int) throws -> JSONValue {
            guard depth <= JSON.maxDepth else { throw JSONError(message: "too deep") }
            guard index < bytes.count else { throw JSONError(message: "unexpected end") }
            switch bytes[index] {
            case 0x7b: return try object(depth: depth)
            case 0x5b: return try array(depth: depth)
            case 0x22: return .string(try string())
            case 0x74: try literal("true"); return .bool(true)
            case 0x66: try literal("false"); return .bool(false)
            case 0x6e: try literal("null"); return .null
            case 0x2d, 0x30...0x39: return try number()
            default: throw JSONError(message: "unexpected byte")
            }
        }

        mutating func literal(_ word: String) throws {
            let w = Array(word.utf8)
            guard index + w.count <= bytes.count, Array(bytes[index..<index + w.count]) == w else {
                throw JSONError(message: "bad literal")
            }
            index += w.count
        }

        mutating func number() throws -> JSONValue {
            let start = index
            if bytes[index] == 0x2d { index += 1 }
            guard index < bytes.count, (0x30...0x39).contains(bytes[index]) else {
                throw JSONError(message: "bad number")
            }
            if bytes[index] == 0x30 {
                index += 1
            } else {
                while index < bytes.count, (0x30...0x39).contains(bytes[index]) { index += 1 }
            }
            if index < bytes.count, [0x2e, 0x65, 0x45].contains(bytes[index]) {
                throw JSONError(message: "non-integer number")
            }
            guard let text = String(bytes: bytes[start..<index], encoding: .utf8), let n = Int64(text) else {
                throw JSONError(message: "integer out of range")
            }
            return .int(n)
        }

        mutating func hex4() throws -> UInt32 {
            guard index + 4 <= bytes.count else { throw JSONError(message: "bad escape") }
            var v: UInt32 = 0
            for _ in 0..<4 {
                let c = bytes[index]
                index += 1
                let d: UInt32
                switch c {
                case 0x30...0x39: d = UInt32(c - 0x30)
                case 0x61...0x66: d = UInt32(c - 0x61 + 10)
                case 0x41...0x46: d = UInt32(c - 0x41 + 10)
                default: throw JSONError(message: "bad escape")
                }
                v = v << 4 | d
            }
            return v
        }

        mutating func string() throws -> String {
            index += 1
            var raw: [UInt8] = []
            while true {
                guard index < bytes.count else { throw JSONError(message: "unterminated string") }
                let c = bytes[index]
                index += 1
                switch c {
                case 0x22:
                    let s = String(decoding: raw, as: UTF8.self)
                    guard Array(s.utf8) == raw else { throw JSONError(message: "invalid UTF-8") }
                    guard s.precomposedStringWithCanonicalMapping.unicodeScalars.elementsEqual(s.unicodeScalars) else {
                        throw JSONError(message: "string is not NFC")
                    }
                    return s
                case 0x5c:
                    guard index < bytes.count else { throw JSONError(message: "bad escape") }
                    let e = bytes[index]
                    index += 1
                    switch e {
                    case 0x22: raw.append(0x22)
                    case 0x5c: raw.append(0x5c)
                    case 0x2f: raw.append(0x2f)
                    case 0x62: raw.append(0x08)
                    case 0x66: raw.append(0x0c)
                    case 0x6e: raw.append(0x0a)
                    case 0x72: raw.append(0x0d)
                    case 0x74: raw.append(0x09)
                    case 0x75:
                        var code = try hex4()
                        if (0xd800..<0xdc00).contains(code) {
                            guard index + 1 < bytes.count, bytes[index] == 0x5c, bytes[index + 1] == 0x75 else {
                                throw JSONError(message: "lone surrogate")
                            }
                            index += 2
                            let low = try hex4()
                            guard (0xdc00..<0xe000).contains(low) else { throw JSONError(message: "lone surrogate") }
                            code = 0x10000 + ((code - 0xd800) << 10) + (low - 0xdc00)
                        } else if (0xdc00..<0xe000).contains(code) {
                            throw JSONError(message: "lone surrogate")
                        }
                        guard let scalar = Unicode.Scalar(code) else { throw JSONError(message: "bad escape") }
                        raw.append(contentsOf: Array(String(Character(scalar)).utf8))
                    default: throw JSONError(message: "bad escape")
                    }
                case 0..<0x20: throw JSONError(message: "control character in string")
                default: raw.append(c)
                }
            }
        }

        mutating func array(depth: Int) throws -> JSONValue {
            index += 1
            var items: [JSONValue] = []
            skipSpace()
            if index < bytes.count, bytes[index] == 0x5d { index += 1; return .array(items) }
            while true {
                skipSpace()
                items.append(try value(depth: depth + 1))
                skipSpace()
                guard index < bytes.count else { throw JSONError(message: "unterminated array") }
                if bytes[index] == 0x2c { index += 1; continue }
                if bytes[index] == 0x5d { index += 1; return .array(items) }
                throw JSONError(message: "bad array")
            }
        }

        mutating func object(depth: Int) throws -> JSONValue {
            index += 1
            var dict: [String: JSONValue] = [:]
            skipSpace()
            if index < bytes.count, bytes[index] == 0x7d { index += 1; return .object(dict) }
            while true {
                skipSpace()
                guard index < bytes.count, bytes[index] == 0x22 else { throw JSONError(message: "bad object key") }
                let key = try string()
                skipSpace()
                guard index < bytes.count, bytes[index] == 0x3a else { throw JSONError(message: "missing colon") }
                index += 1
                skipSpace()
                guard dict[key] == nil else { throw JSONError(message: "duplicate key") }
                dict[key] = try value(depth: depth + 1)
                skipSpace()
                guard index < bytes.count else { throw JSONError(message: "unterminated object") }
                if bytes[index] == 0x2c { index += 1; continue }
                if bytes[index] == 0x7d { index += 1; return .object(dict) }
                throw JSONError(message: "bad object")
            }
        }
    }
}

public enum Hex {
    public static func encode(_ bytes: some Sequence<UInt8>) -> String {
        let digits = Array("0123456789abcdef".utf8)
        var out: [UInt8] = []
        for b in bytes { out.append(digits[Int(b >> 4)]); out.append(digits[Int(b & 0xf)]) }
        return String(decoding: out, as: UTF8.self)
    }

    public static func decodeHex(_ text: String) -> [UInt8]? { decode(text) }

    public static func decode(_ text: String) -> [UInt8]? {
        let chars = Array(text.utf8)
        guard chars.count % 2 == 0 else { return nil }
        func nibble(_ c: UInt8) -> UInt8? {
            switch c {
            case 0x30...0x39: return c - 0x30
            case 0x61...0x66: return c - 0x61 + 10
            case 0x41...0x46: return c - 0x41 + 10
            default: return nil
            }
        }
        var out: [UInt8] = []
        out.reserveCapacity(chars.count / 2)
        var i = 0
        while i < chars.count {
            guard let hi = nibble(chars[i]), let lo = nibble(chars[i + 1]) else { return nil }
            out.append(hi << 4 | lo)
            i += 2
        }
        return out
    }
}
