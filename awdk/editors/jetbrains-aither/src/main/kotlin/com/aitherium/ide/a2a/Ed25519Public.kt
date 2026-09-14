package com.aitherium.ide.a2a

import java.math.BigInteger
import java.security.KeyFactory
import java.security.PrivateKey
import java.security.spec.PKCS8EncodedKeySpec

/**
 * RFC 8032 Ed25519 public-key derivation — the part of A2A signing the JDK does
 * not expose (the public key is not recoverable from a PKCS8 private via the
 * standard KeyFactory API). We derive it from the 32-byte seed with the standard
 * base-point scalar multiplication on the twisted Edwards curve (a = -1).
 *
 * Verified against awdk's cryptography-based derivation on 2026-08-08:
 * the derived 64-char public hex matches `private_key.public_key().public_bytes(
 * Encoding.Raw, PublicFormat.Raw).hex()`.
 */
object Ed25519Public {

    private val P = BigInteger("2").pow(255).subtract(BigInteger("19"))
    private val D = BigInteger("-121665")
        .multiply(BigInteger("121666").modInverse(P))
        .mod(P)

    private val BY = BigInteger("46316835694926478169428394003475163141307993866256225615783033603165251855960")
    private val BX = BigInteger("15112221349535400772501151409588531511454012693041857206046113283949847762202")

    /** Point on the Edwards curve, affine (x, y) mod P. */
    private class Pt(val x: BigInteger, val y: BigInteger)

    private val B = Pt(BX, BY)

    fun fromPkcs8(priv: PrivateKey): ByteArray {
        val der = priv.encoded
        require(der.size >= 32) { "PKCS8 Ed25519 key too short: ${der.size} bytes" }
        val seed = der.copyOfRange(der.size - 32, der.size)
        return fromSeed(seed)
    }

    fun fromSeed(seed: ByteArray): ByteArray {
        require(seed.size == 32) { "Ed25519 seed must be 32 bytes, got ${seed.size}" }
        // RFC 8032: the scalar is the clamped first half of SHA-512(seed).
        val hash = java.security.MessageDigest.getInstance("SHA-512").digest(seed)
        val scalarBytes = hash.copyOfRange(0, 32)
        scalarBytes[0] = (scalarBytes[0].toInt() and 0xf8).toByte()
        scalarBytes[31] = ((scalarBytes[31].toInt() and 0x7f) or 0x40).toByte()
        val scalar = BigInteger(1, scalarBytes.reversedArray()) // little-endian -> big-endian int
        val a = scalarMult(scalar, B)
        return encodePoint(a)
    }

    // ------------------------------------------------------- curve arithmetic

    private fun mod(x: BigInteger): BigInteger = x.mod(P)

    private fun ptAdd(p: Pt, q: Pt): Pt {
        val dxy = mod(D.multiply(p.x).multiply(q.x).multiply(p.y).multiply(q.y))
        val xNum = mod(p.x.multiply(q.y).add(p.y.multiply(q.x)))
        val xDen = mod(BigInteger.ONE.add(dxy)).modInverse(P)
        val yNum = mod(p.y.multiply(q.y).add(p.x.multiply(q.x)))
        val yDen = mod(BigInteger.ONE.subtract(dxy)).modInverse(P)
        return Pt(mod(xNum.multiply(xDen)), mod(yNum.multiply(yDen)))
    }

    private fun ptDouble(p: Pt): Pt = ptAdd(p, p)

    private fun scalarMult(scalar: BigInteger, base: Pt): Pt {
        var result: Pt? = null
        var addend = base
        var k = scalar
        while (k.signum() > 0) {
            if (k.testBit(0)) result = if (result == null) addend else ptAdd(result, addend)
            addend = ptDouble(addend)
            k = k.shiftRight(1)
        }
        return result ?: Pt(BigInteger.ZERO, BigInteger.ONE)
    }

    /** 32-byte little-endian compressed point: y (LE) with the sign of x in bit 255. */
    private fun encodePoint(p: Pt): ByteArray {
        val yLe = toLe(p.y, 32)
        val xIsNegative = p.x.mod(P).testBit(0)
        if (xIsNegative) yLe[31] = (yLe[31].toInt() or 0x80).toByte()
        return yLe
    }

    private fun toLe(v: BigInteger, size: Int): ByteArray {
        val out = ByteArray(size)
        var tmp = v
        for (i in 0 until size) {
            out[i] = tmp.and(BigInteger("255")).toByte()
            tmp = tmp.shiftRight(8)
        }
        return out
    }
}
