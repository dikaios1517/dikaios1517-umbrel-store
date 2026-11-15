"""BIP-340 Schnorr verify (and sign, for tests) — pure Python reference
implementation, with a coincurve fast path when the wheel is available.

Verify is on the hot path (every EVENT admitted), so the fast path matters in
production; the pure fallback is correct everywhere and fast enough for a
personal relay (~1-2 ms/verify).
"""
import hashlib
import secrets

p = 2**256 - 2**32 - 977
n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)

try:
    from coincurve import PublicKey as _CCPublicKey  # noqa

    def verify(pubkey: bytes, msg: bytes, sig: bytes) -> bool:
        if len(pubkey) != 32 or len(msg) != 32 or len(sig) != 64:
            return False
        try:
            pk = _CCPublicKey(pubkey)
        except Exception:
            return False
        # coincurve exposes BIP-340 schnorr verify via verify_schnorr
        try:
            return bool(pk.verify_schnorr(sig, msg))
        except Exception:
            return False
except ImportError:
    def verify(pubkey: bytes, msg: bytes, sig: bytes) -> bool:
        if len(pubkey) != 32 or len(sig) != 64:
            return False
        r = int.from_bytes(sig[:32], 'big')
        s = int.from_bytes(sig[32:], 'big')
        if r >= p or s >= n:
            return False
        P = lift_x(int.from_bytes(pubkey, 'big'))
        if P is None:
            return False
        rx_bytes = sig[:32]
        e = int.from_bytes(
            tagged_hash('BIP0340/challenge', rx_bytes + pubkey + msg), 'big') % n
        R = point_add(point_mul(G, s), point_neg(point_mul(P, e)))
        if R is None or not is_y_even(R) or R[0] != r:
            return False
        return True


def tagged_hash(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def lift_x(x: int):
    if x >= p:
        return None
    y_sq = (pow(x, 3, p) + 7) % p
    y = pow(y_sq, (p + 1) // 4, p)
    if pow(y, 2, p) != y_sq:
        return None
    return (x, y if y % 2 == 0 else p - y)


def point_neg(P):
    if P is None:
        return None
    return (P[0], (p - P[1]) % p)


def is_y_even(P):
    return P[1] % 2 == 0


def point_add(P1, P2):
    if P1 is None:
        return P2
    if P2 is None:
        return P1
    if P1[0] == P2[0] and P1[1] != P2[1]:
        return None  # vertical line -> infinity
    if P1 == P2:
        lam = (3 * P1[0] * P1[0]) * pow(2 * P1[1], -1, p) % p
    else:
        lam = (P2[1] - P1[1]) * pow(P2[0] - P1[0], -1, p) % p
    x3 = (lam * lam - P1[0] - P2[0]) % p
    return (x3, (lam * (P1[0] - x3) - P1[1]) % p)


def point_mul(P, k: int):
    R = None
    for i in range(256):
        if (k >> i) & 1:
            R = point_add(R, P)
        P = point_add(P, P)
    return R


def sign(seckey: bytes, msg: bytes, aux: bytes = None) -> bytes:
    """Deterministic-enough signing (aux = random when omitted). Test-only."""
    d0 = int.from_bytes(seckey, 'big')
    if not 1 <= d0 <= n - 1:
        raise ValueError('bad seckey')
    P = point_mul(G, d0)
    d = d0 if P[1] % 2 == 0 else n - d0
    if aux is None:
        aux = secrets.token_bytes(32)
    t = bytes(a ^ b for a, b in zip(d.to_bytes(32, 'big'),
                                    tagged_hash('BIP0340/aux', aux)))
    k0 = int.from_bytes(
        tagged_hash('BIP0340/nonce',
                    t + P[0].to_bytes(32, 'big') + msg), 'big') % n
    if not 1 <= k0 <= n - 1:
        raise ValueError('bad nonce')
    R = point_mul(G, k0)
    k = k0 if R[1] % 2 == 0 else n - k0
    e = int.from_bytes(
        tagged_hash('BIP0340/challenge', R[0].to_bytes(32, 'big')
                    + P[0].to_bytes(32, 'big') + msg), 'big') % n
    s = (k + e * d) % n
    return R[0].to_bytes(32, 'big') + s.to_bytes(32, 'big')


def pubkey_for(seckey: bytes) -> bytes:
    P = point_mul(G, int.from_bytes(seckey, 'big'))
    return P[0].to_bytes(32, 'big')
