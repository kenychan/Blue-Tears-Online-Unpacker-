#!/usr/bin/env python3
"""
sfa_decrypt.py — Decrypt Nextsoft SFA index files (.SFAi)

Punch Monster / QQ仙境 (QQxj) / Blue Tears archive format.

FINDINGS:
- SFAi file structure:
    [0..7]   8-byte magic: cb f0 f4 d1 9a 99 cc 16
    [8..31]  24-byte header (3 fields, XOR-encrypted)
    [32..]   variable-length body containing N file records
- Body is encrypted with a 300-byte XOR cipher repeating
- The cipher is DIFFERENT per archive (likely derived from the archive name/key)
- For each archive, the cipher can be recovered by "most-common-byte at each
  position mod 300" — IF the archive has enough records (~30+)

For SMALL archives like ClientData.SFAi (only 4 records), per-file cipher
recovery doesn't work — we'd need known plaintext or to derive the cipher
from a larger archive in the same install.

Usage:
    python3 sfa_decrypt.py file1.SFAi [file2.SFAi ...]

Output (per input file):
    <basename>.dec      — decrypted body bytes (without 32-byte header)
    <basename>.cipher   — the 300-byte cipher used
"""

import sys
import os
from collections import Counter

PERIOD = 300
HEADER_SIZE = 32
SFAI_MAGIC = bytes.fromhex("cb f0 f4 d1 9a 99 cc 16")


def derive_cipher(body):
    """Recover the 300-byte XOR cipher by majority vote at each position mod 300."""
    cipher = bytearray(PERIOD)
    conf = [0.0] * PERIOD
    for pos in range(PERIOD):
        vals = [body[i] for i in range(pos, len(body), PERIOD)]
        if not vals:
            continue
        c = Counter(vals)
        most_common, count = c.most_common(1)[0]
        cipher[pos] = most_common
        conf[pos] = count / len(vals)
    return bytes(cipher), conf


def decrypt(body, cipher):
    return bytes(b ^ cipher[i % PERIOD] for i, b in enumerate(body))


def process(path):
    data = open(path, "rb").read()
    if data[:8] != SFAI_MAGIC:
        print(f"[!] {path}: bad magic {data[:8].hex()}", file=sys.stderr)
        return
    body = data[HEADER_SIZE:]
    cipher, conf = derive_cipher(body)
    n_records = len(body) // PERIOD
    confident = sum(1 for c in conf if c > 0.5)
    
    base = os.path.basename(path)
    decrypted = decrypt(body, cipher)
    zero_pct = 100 * decrypted.count(0) / len(decrypted) if decrypted else 0
    
    open(f"{base}.dec", "wb").write(decrypted)
    open(f"{base}.cipher", "wb").write(cipher)
    
    note = ""
    if n_records < 30:
        note = " (WARNING: only %d records, cipher may be unreliable)" % n_records
    print(f"[+] {path}: {n_records} records, {confident}/{PERIOD} confident, "
          f"{zero_pct:.0f}% zeros{note}")
    print(f"    -> {base}.dec, {base}.cipher")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for f in sys.argv[1:]:
        process(f)


if __name__ == "__main__":
    main()
