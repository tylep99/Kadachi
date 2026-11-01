"""iNES ROM loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class RomError(ValueError):
    """Raised when a ROM cannot be loaded by the emulator."""


@dataclass
class Rom:
    prg_rom: bytes
    chr_rom: bytes
    mapper: int
    mirroring: str

    @classmethod
    def load(cls, path: str | Path) -> "Rom":
        data = Path(path).read_bytes()
        if len(data) < 16 or data[:4] != b"NES\x1a":
            raise RomError("not an iNES ROM")
        flags6, flags7 = data[6], data[7]
        if flags6 & 0x04:
            raise RomError("trainer data is not supported")
        mapper = (flags6 >> 4) | (flags7 & 0xF0)
        if mapper not in (0, 66):
            raise RomError(f"mapper {mapper} is not supported (supported: 0, 66)")
        prg_size = data[4] * 16_384
        chr_size = (data[5] or 1) * 8_192
        end = 16 + prg_size + (0 if data[5] == 0 else chr_size)
        if len(data) < end:
            raise RomError("ROM is truncated")
        prg = data[16:16 + prg_size]
        chr_data = data[16 + prg_size:end] if data[5] else bytes(chr_size)
        mirroring = "vertical" if flags6 & 1 else "horizontal"
        return cls(prg, chr_data, mapper, mirroring)
