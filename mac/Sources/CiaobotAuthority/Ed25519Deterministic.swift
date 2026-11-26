import CryptoKit
import Foundation

/// Deterministic Ed25519 signing (RFC 8032), for turn events only.
///
/// Why this exists: the shared vectors pin the exact event signature bytes,
/// and the persisted state (whose digest and MAC are pinned too) contains them.
/// The Python reference signs deterministically, but CryptoKit's
/// `Curve25519.Signing` is randomized, so its output can never match. Public
/// key derivation and verification stay with CryptoKit (see `EventSigner`);
/// this file only computes `R || S`.
///
/// It is NOT constant time (array-based big integers, data-dependent
/// branches). The key is the daemon's event seed; only a verified operator
/// command can make the daemon sign, so an attacker cannot choose messages to
/// time, and the daemon runs in a root-private process. That residual is
/// listed in the threat notes. Correctness is pinned by the RFC 8032 vectors,
/// the shared vectors and a cross-check of every signature against CryptoKit's
/// verifier.
enum Ed25519Deterministic {
    static func publicKey(seed: [UInt8]) -> [UInt8] {
        let (a, _) = expand(seed)
        return encode(scalarMult(basePoint, a))
    }

    /// `publicKey` must be `publicKey(seed:)` (cached by the caller; deriving it costs a scalar multiplication).
    static func sign(seed: [UInt8], publicKey pub: [UInt8], message: [UInt8]) -> [UInt8] {
        precondition(seed.count == 32 && pub.count == 32)
        let (a, prefix) = expand(seed)
        let r = Scalar.reduce(littleEndian(Array(SHA512.hash(data: prefix + message))))
        let rEnc = encode(scalarMult(basePoint, r))
        let k = Scalar.reduce(littleEndian(Array(SHA512.hash(data: rEnc + pub + message))))
        // S = (r + k * a) mod L
        var prod = Limbs.mul(k, a)
        prod = Limbs.add(prod, r)
        let s = Scalar.reduce(prod)
        return rEnc + bytes(s, count: 32)
    }

    // MARK: key expansion

    private static func expand(_ seed: [UInt8]) -> (a: [UInt32], prefix: [UInt8]) {
        let h = Array(SHA512.hash(data: seed))
        var lo = Array(h[0..<32])
        lo[0] &= 248
        lo[31] &= 127
        lo[31] |= 64
        return (littleEndian(lo), Array(h[32..<64]))
    }

    private static func littleEndian(_ b: [UInt8]) -> [UInt32] {
        var out = [UInt32](repeating: 0, count: (b.count + 3) / 4)
        for (i, byte) in b.enumerated() { out[i / 4] |= UInt32(byte) << UInt32(8 * (i % 4)) }
        return out
    }

    private static func bytes(_ limbs: [UInt32], count: Int) -> [UInt8] {
        var out = [UInt8](repeating: 0, count: count)
        for i in 0..<count where i / 4 < limbs.count { out[i] = UInt8((limbs[i / 4] >> UInt32(8 * (i % 4))) & 0xff) }
        return out
    }

    // MARK: curve

    struct Point {
        var x: [UInt32], y: [UInt32], z: [UInt32], t: [UInt32]
    }

    private static let d: [UInt32] = {
        // d = -121665 / 121666 mod p
        let num = Field.neg(Field.fromInt(121665))
        return Field.mul(num, Field.inv(Field.fromInt(121666)))
    }()

    private static let basePoint: Point = {
        let y = Field.mul(Field.fromInt(4), Field.inv(Field.fromInt(5)))
        let y2 = Field.mul(y, y)
        let u = Field.sub(y2, Field.fromInt(1))
        let v = Field.add(Field.mul(d, y2), Field.fromInt(1))
        let x2 = Field.mul(u, Field.inv(v))
        var x = Field.pow(x2, Field.pPlus3Over8)
        if Field.mul(x, x) != x2 { x = Field.mul(x, Field.sqrtMinusOne) }
        precondition(Field.mul(x, x) == x2, "base point x is not a square root")
        if x[0] & 1 == 1 { x = Field.neg(x) }  // the base point's x is even
        return Point(x: x, y: y, z: Field.fromInt(1), t: Field.mul(x, y))
    }()

    private static let identity = Point(
        x: Field.fromInt(0), y: Field.fromInt(1), z: Field.fromInt(1), t: Field.fromInt(0))

    /// RFC 8032 section 5.1.4 (complete for a = -1); also used for doubling.
    private static func add(_ p: Point, _ q: Point) -> Point {
        let a = Field.mul(Field.sub(p.y, p.x), Field.sub(q.y, q.x))
        let b = Field.mul(Field.add(p.y, p.x), Field.add(q.y, q.x))
        let c = Field.mul(Field.mul(p.t, Field.add(d, d)), q.t)
        let dd = Field.mul(Field.add(p.z, p.z), q.z)
        let e = Field.sub(b, a)
        let f = Field.sub(dd, c)
        let g = Field.add(dd, c)
        let h = Field.add(b, a)
        return Point(x: Field.mul(e, f), y: Field.mul(g, h), z: Field.mul(f, g), t: Field.mul(e, h))
    }

    private static func scalarMult(_ p: Point, _ scalar: [UInt32]) -> Point {
        var result = identity
        for limb in scalar.reversed() {
            for bit in stride(from: 31, through: 0, by: -1) {
                result = add(result, result)
                if (limb >> UInt32(bit)) & 1 == 1 { result = add(result, p) }
            }
        }
        return result
    }

    private static func encode(_ p: Point) -> [UInt8] {
        let zinv = Field.inv(p.z)
        let x = Field.mul(p.x, zinv)
        let y = Field.mul(p.y, zinv)
        var out = bytes(y, count: 32)
        out[31] |= UInt8(x[0] & 1) << 7
        return out
    }
}

/// Fixed-width (8 x 32-bit little-endian limbs) arithmetic modulo 2^255 - 19.
enum Field {
    static let p: [UInt32] = [0xffff_ffed] + [UInt32](repeating: 0xffff_ffff, count: 6) + [0x7fff_ffff]

    static func fromInt(_ v: UInt32) -> [UInt32] { [v, 0, 0, 0, 0, 0, 0, 0] }

    static let pPlus3Over8: [UInt32] = Limbs.shiftRight(Limbs.add(p, [3]), by: 3)
    /// 2^((p-1)/4), a square root of -1.
    static let sqrtMinusOne: [UInt32] = pow(fromInt(2), Limbs.shiftRight(Limbs.sub(p, [1]), by: 2))

    static func add(_ a: [UInt32], _ b: [UInt32]) -> [UInt32] { reduce(Limbs.add(a, b)) }

    static func sub(_ a: [UInt32], _ b: [UInt32]) -> [UInt32] {
        reduce(Limbs.add(a, Limbs.sub(p, b)))
    }

    static func neg(_ a: [UInt32]) -> [UInt32] { sub(fromInt(0), a) }

    static func mul(_ a: [UInt32], _ b: [UInt32]) -> [UInt32] { reduce(Limbs.mul(a, b)) }

    static func pow(_ base: [UInt32], _ exp: [UInt32]) -> [UInt32] {
        var result = fromInt(1)
        for limb in exp.reversed() {
            for bit in stride(from: 31, through: 0, by: -1) {
                result = mul(result, result)
                if (limb >> UInt32(bit)) & 1 == 1 { result = mul(result, base) }
            }
        }
        return result
    }

    static func inv(_ a: [UInt32]) -> [UInt32] { pow(a, Limbs.sub(p, [2])) }

    /// Fold bits above 254 back in (2^255 = 19) until the value fits, then subtract p.
    static func reduce(_ input: [UInt32]) -> [UInt32] {
        var v = input
        while true {
            let hi = Limbs.shiftRight(v, by: 255)
            if hi.allSatisfy({ $0 == 0 }) { break }
            var lo = Array(v.prefix(8))
            while lo.count < 8 { lo.append(0) }
            lo[7] &= 0x7fff_ffff
            v = Limbs.add(lo, Limbs.mul(hi, [19]))
        }
        var out = Array(v.prefix(8))
        while out.count < 8 { out.append(0) }
        while Limbs.compare(out, p) >= 0 { out = Limbs.sub(out, p) }
        return out
    }
}

/// Reduction modulo the group order L = 2^252 + 27742317777372353535851937790883648493.
enum Scalar {
    static let l: [UInt32] = [
        0x5cf5_d3ed, 0x5812_631a, 0xa2f7_9cd6, 0x14de_f9de, 0, 0, 0, 0x1000_0000,
    ]

    /// Bitwise shift-and-subtract; only a few calls per signature.
    static func reduce(_ x: [UInt32]) -> [UInt32] {
        var r = [UInt32](repeating: 0, count: 9)
        let lw = l + [0]
        for limb in x.reversed() {
            for bit in stride(from: 31, through: 0, by: -1) {
                r = Limbs.shiftLeftOne(r, carryIn: (limb >> UInt32(bit)) & 1)
                if Limbs.compare(r, lw) >= 0 { r = Limbs.sub(r, lw) }
            }
        }
        return Array(r.prefix(8))
    }
}

/// Little-endian 32-bit-limb unsigned integers of arbitrary length.
enum Limbs {
    static func trim(_ a: [UInt32]) -> [UInt32] {
        var a = a
        while a.count > 1, a.last == 0 { a.removeLast() }
        return a
    }

    static func compare(_ a: [UInt32], _ b: [UInt32]) -> Int {
        let n = max(a.count, b.count)
        for i in stride(from: n - 1, through: 0, by: -1) {
            let x = i < a.count ? a[i] : 0
            let y = i < b.count ? b[i] : 0
            if x != y { return x < y ? -1 : 1 }
        }
        return 0
    }

    static func add(_ a: [UInt32], _ b: [UInt32]) -> [UInt32] {
        let n = max(a.count, b.count)
        var out = [UInt32](repeating: 0, count: n + 1)
        var carry: UInt64 = 0
        for i in 0..<n {
            let s = UInt64(i < a.count ? a[i] : 0) + UInt64(i < b.count ? b[i] : 0) + carry
            out[i] = UInt32(truncatingIfNeeded: s)
            carry = s >> 32
        }
        out[n] = UInt32(carry)
        return out
    }

    /// a - b, requires a >= b.
    static func sub(_ a: [UInt32], _ b: [UInt32]) -> [UInt32] {
        var out = [UInt32](repeating: 0, count: a.count)
        var borrow: Int64 = 0
        for i in 0..<a.count {
            var d = Int64(a[i]) - Int64(i < b.count ? b[i] : 0) - borrow
            if d < 0 { d += 1 << 32; borrow = 1 } else { borrow = 0 }
            out[i] = UInt32(d)
        }
        precondition(borrow == 0, "negative result")
        return out
    }

    static func mul(_ a: [UInt32], _ b: [UInt32]) -> [UInt32] {
        var t = [UInt64](repeating: 0, count: a.count + b.count + 1)
        for i in 0..<a.count {
            var carry: UInt64 = 0
            for j in 0..<b.count {
                let cur = t[i + j] + UInt64(a[i]) * UInt64(b[j]) + carry
                t[i + j] = cur & 0xffff_ffff
                carry = cur >> 32
            }
            t[i + b.count] += carry
        }
        return t.map { UInt32(truncatingIfNeeded: $0) }
    }

    static func shiftRight(_ a: [UInt32], by bits: Int) -> [UInt32] {
        let limbShift = bits / 32
        let bitShift = UInt32(bits % 32)
        guard limbShift < a.count else { return [0] }
        var out = [UInt32](repeating: 0, count: a.count - limbShift)
        for i in 0..<out.count {
            let lo = a[i + limbShift] >> bitShift
            let hi: UInt32 = (bitShift > 0 && i + limbShift + 1 < a.count) ? a[i + limbShift + 1] << (32 - bitShift) : 0
            out[i] = lo | hi
        }
        return out
    }

    static func shiftLeftOne(_ a: [UInt32], carryIn: UInt32) -> [UInt32] {
        var out = a
        var carry = carryIn
        for i in 0..<out.count {
            let next = out[i] >> 31
            out[i] = out[i] << 1 | carry
            carry = next
        }
        return out
    }
}
