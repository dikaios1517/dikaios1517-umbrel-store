"""npub/nprofile bech32 encode-decode (NIP-19), no external deps."""
CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'


def _polymod(values):
    gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def _bech32_encode(hrp, data5):
    chk = _polymod(_hrp_expand(hrp) + data5 + [0, 0, 0, 0, 0, 0]) ^ 1
    return hrp + '1' + ''.join(CHARSET[d] for d in data5) + \
        ''.join(CHARSET[(chk >> (5 * (5 - i))) & 31] for i in range(6))


def _bech32_decode(hrp, addr):
    addr = addr.lower()
    if not addr.startswith(hrp + '1'):
        return None
    data = [CHARSET.index(c) for c in addr[len(hrp) + 1:]]
    if _polymod(_hrp_expand(hrp) + data) != 1:
        return None
    return bytes(_convertbits(data[:-6], 5, 8, pad=False))


def npub_encode(pubkey_hex: str) -> str:
    return _bech32_encode('npub', _convertbits(bytes.fromhex(pubkey_hex), 8, 5))


def npub_decode(npub: str) -> str | None:
    """npub.../nprofile... -> 64-char hex pubkey, or None."""
    if not isinstance(npub, str):
        return None
    npub = npub.strip()
    if len(npub) == 64:
        try:
            bytes.fromhex(npub)
            return npub.lower()
        except ValueError:
            return None
    for hrp in ('npub', 'nprofile'):
        out = _bech32_decode(hrp, npub)
        if out and len(out) == 32:
            return out.hex()
    return None
