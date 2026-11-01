"""A minimal read-only reader for Microsoft PDB files (MSF 7.00): the signature only.

A PDB belongs to a binary when its GUID and age match the binary's CodeView
(RSDS) record, which pe.parse reads as pdb_guid and pdb_age.
"""

import math
import struct

MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"


class NotPDB(ValueError):
    pass


def signature(path):
    """(guid, age) from the PDB info stream (stream 1); guid as the 16 bytes stored in an RSDS record."""
    with open(path, "rb") as f:
        head = f.read(56)
        if len(head) < 56 or head[:32] != MAGIC:
            raise NotPDB(f"{path}: not an MSF 7.00 file")
        block, _, _, dir_bytes, _, map_block = struct.unpack_from("<6I", head, 32)
        if not block or not dir_bytes:
            raise NotPDB(f"{path}: empty stream directory")

        def read_block(i):
            f.seek(i * block)
            return f.read(block)

        n_dir = math.ceil(dir_bytes / block)
        dir_blocks = struct.unpack_from(f"<{n_dir}I", read_block(map_block))
        directory = b"".join(read_block(b) for b in dir_blocks)[:dir_bytes]
        n_streams = struct.unpack_from("<I", directory)[0]
        if n_streams < 2:
            raise NotPDB(f"{path}: no PDB info stream")
        sizes = struct.unpack_from(f"<{n_streams}I", directory, 4)
        stream0 = 0 if sizes[0] == 0xFFFFFFFF else math.ceil(sizes[0] / block)
        first = struct.unpack_from("<I", directory, 4 + 4 * n_streams + 4 * stream0)[0]
        info = read_block(first)
        if sizes[1] < 28 or len(info) < 28:
            raise NotPDB(f"{path}: PDB info stream too short")
        age = struct.unpack_from("<I", info, 8)[0]
        return bytes(info[12:28]), age
