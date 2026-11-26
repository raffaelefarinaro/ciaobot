import CryptoKit
import XCTest

@testable import CiaobotAuthority

final class JSONTests: XCTestCase {
    func testCanonicalMatchesPythonDumps() throws {
        // json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        let value = JSONValue.object([
            "b": .array([.int(1), .null, .bool(true)]), "a": .string("caf\u{e9} \u{1F600} \u{7f}\n\t\"\\ \u{1}"),
            "\u{1F600}": .int(-5), "z": .object([:]), "\u{ff5e}": .int(0),
        ])
        XCTAssertEqual(
            String(decoding: JSON.canonical(value), as: UTF8.self),
            #"{"a":"caf\u00e9 \ud83d\ude00 \u007f\n\t\"\\ \u0001","b":[1,null,true],"z":{},"\uff5e":0,"\ud83d\ude00":-5}"#)
    }

    func testKeysSortByCodePointNotUTF16() {
        // U+FF5E (BMP) sorts before U+1F600 by code point; UTF-16 order would reverse them.
        XCTAssertEqual(JSON.sortedKeys(["\u{1F600}", "\u{ff5e}"]), ["\u{ff5e}", "\u{1F600}"])
    }

    func testParserRoundTripsEscapes() throws {
        let parsed = try JSON.parse(#"{"k":"\ud83d\ude00 \u00e9 \/ \b\f"}"#)
        XCTAssertEqual(parsed.objectValue?["k"], .string("\u{1F600} \u{e9} / \u{8}\u{c}"))
    }

    func testParserRefusesWhatPythonWouldReadDifferently() {
        let bad = [
            "1.5", "1e3", "9223372036854775808", #"{"a":1,"a":2}"#, #""\ud83d""#, #""\ude00""#, "\"a\tb\"".replacingOccurrences(of: "\\t", with: "\t"),
            "[1,]", "{\"a\":}", "NaN", "01", "-", "[1] x", #""\x""#, "\"e\u{301}\"",  // NFD é
        ]
        for text in bad { XCTAssertThrowsError(try JSON.parse(text), text) }
        XCTAssertThrowsError(try JSON.parse([0x22, 0xff, 0x22]))  // invalid UTF-8
        XCTAssertThrowsError(try JSON.parse(String(repeating: "[", count: 100) + String(repeating: "]", count: 100)))
    }

    func testNegativeZeroAndBigButValidIntegers() throws {
        XCTAssertEqual(try JSON.parse("-0"), .int(0))
        XCTAssertEqual(try JSON.parse("9007199254740991"), .int(maxCounter))
    }
}

final class Ed25519Tests: XCTestCase {
    private func hex(_ s: String) -> [UInt8] { Hex.decode(s)! }

    func testRFC8032Vector1() throws {
        let seed = hex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        let signer = try EventSigner(seed: seed)
        XCTAssertEqual(Hex.encode(signer.publicKey), "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        XCTAssertEqual(
            Hex.encode(signer.sign([])),
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    }

    func testDerivedPublicKeyMatchesCryptoKit() {
        for n in 0..<3 {
            let seed = (0..<32).map { _ in UInt8.random(in: 0...255) }
            let expected = Array(try! Curve25519.Signing.PrivateKey(rawRepresentation: Data(seed)).publicKey.rawRepresentation)
            XCTAssertEqual(Ed25519Deterministic.publicKey(seed: seed), expected, "seed \(n)")
        }
    }

    func testSignaturesAreDeterministicAndVerifyWithCryptoKit() throws {
        let seed = (0..<32).map { _ in UInt8.random(in: 0...255) }
        let signer = try EventSigner(seed: seed)
        let key = try Curve25519.Signing.PublicKey(rawRepresentation: Data(signer.publicKey))
        for length in [0, 1, 31, 64, 1000] {
            let message = (0..<length).map { _ in UInt8.random(in: 0...255) }
            let sig = signer.sign(message)
            XCTAssertEqual(sig, signer.sign(message))
            XCTAssertTrue(key.isValidSignature(Data(sig), for: Data(message)), "length \(length)")
            var bad = message
            bad.append(0)
            XCTAssertFalse(key.isValidSignature(Data(sig), for: Data(bad)))
        }
    }
}
