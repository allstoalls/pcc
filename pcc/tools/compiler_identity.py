"""Content identity of the running native compiler without its source tree."""

import hashlib


def native_executable_identity(path: str) -> str:
    """Use the kernel-validated Mach-O signature, or hash an unsigned image.

    pcc's Mach-O UUID is fixed, so it is not a compiler identity. The embedded
    signature contains hashes of every executable page and is much smaller
    than a self-host image. This helper is for the running, immutable compiler;
    it is not a replacement for validating an arbitrary artifact's signature.
    """
    digest = hashlib.sha256()
    digest.update(b"pcc.running-compiler.v1\0")
    with open(path, "rb") as stream:
        header = stream.read(32)
        if len(header) == 32 and header[:4] == b"\xcf\xfa\xed\xfe":
            command_count = int.from_bytes(header[16:20], "little")
            command_bytes = int.from_bytes(header[20:24], "little")
            if command_bytes > 1048576:
                raise ValueError("compiler load commands exceed the identity bound")
            commands = stream.read(command_bytes)
            if len(commands) != command_bytes:
                raise ValueError("truncated compiler load commands")
            offset = 0
            signature_offset = 0
            signature_size = 0
            for _ in range(command_count):
                if offset + 8 > len(commands):
                    raise ValueError("truncated compiler load command")
                kind = int.from_bytes(commands[offset:offset + 4], "little")
                size = int.from_bytes(commands[offset + 4:offset + 8], "little")
                if size < 8 or offset + size > len(commands):
                    raise ValueError("invalid compiler load command size")
                if kind == 29:  # LC_CODE_SIGNATURE
                    if signature_offset or size != 16:
                        raise ValueError("invalid compiler signature command")
                    signature_offset = int.from_bytes(commands[offset + 8:offset + 12], "little")
                    signature_size = int.from_bytes(commands[offset + 12:offset + 16], "little")
                offset += size
            if offset != len(commands):
                raise ValueError("compiler load command count mismatch")
            if signature_offset:
                if signature_offset < 32 + command_bytes or not 20 <= signature_size <= 16777216:
                    raise ValueError("invalid compiler signature range")
                stream.seek(signature_offset)
                signature = stream.read(signature_size)
                if len(signature) != signature_size or stream.read(1):
                    raise ValueError("compiler signature does not end at the file boundary")
                if signature[:4] != b"\xfa\xde\x0c\xc0":
                    raise ValueError("invalid compiler signature magic")
                digest.update(signature)
                return digest.hexdigest()
        stream.seek(0)
        while True:
            chunk = stream.read(1048576)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
