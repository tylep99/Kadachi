"""CPU address bus and cartridge mapper behavior."""

from __future__ import annotations

from .ppu import PPU
from .rom import Rom


class Bus:
    def __init__(self, rom: Rom | None = None):
        self.rom = rom
        # Cached for the hot read path: PRG bytes and the mask that mirrors a
        # 16 KB image across $8000-$FFFF (32 KB images use the full window).
        self._prg = rom.prg_rom if rom else None
        self._prg_mask = 0x3FFF if rom and len(rom.prg_rom) == 0x4000 else 0x7FFF
        self.ppu_device = PPU()
        if rom:
            # Mapper 66 selects one 8 KB CHR window, including at power-on.
            self.ppu_device.chr_rom = (
                rom.chr_rom[:0x2000] if rom.mapper == 66 else rom.chr_rom
            )
            self.ppu_device.mirroring = rom.mirroring
        self.prg_bank = 0
        self.chr_bank = 0
        self._prg_bank_count = max(1, len(rom.prg_rom) // 0x8000) if rom else 1
        self._chr_bank_count = max(1, len(rom.chr_rom) // 0x2000) if rom else 1
        self.ram = bytearray(0x800)
        self.ppu = bytearray(8)
        self.apu_io = bytearray(0x20)
        self.expansion = bytearray(0x1FE0)
        self.controllers = [0, 0]
        self.controller_state = [0, 0]
        self.controller_strobe = 0

    def read(self, address: int) -> int:
        address &= 0xFFFF
        if address >= 0x8000:
            prg = self._prg
            if prg is None:
                return 0
            # prg_bank is always 0 except on mapper 66 (GxROM, 32 KB banks).
            bank = self.prg_bank % self._prg_bank_count
            return prg[(bank << 15) + (address & self._prg_mask)]
        if address < 0x2000:
            return self.ram[address & 0x7FF]
        if address < 0x4000:
            return self.ppu_device.read_register(address - 0x2000)
        if address == 0x4016:
            return self._controller_read(0)
        if address == 0x4017:
            return self._controller_read(1)
        if address < 0x4020:
            return self.apu_io[address - 0x4000]
        if address < 0x6000:
            return self.expansion[address - 0x4020]
        return 0

    def write(self, address: int, value: int) -> None:
        address &= 0xFFFF
        value &= 0xFF
        if address < 0x2000:
            self.ram[address & 0x7FF] = value
        elif address < 0x4000:
            self.ppu_device.write_register(address - 0x2000, value)
        elif address == 0x4016:
            previous = self.controller_strobe
            self.controller_strobe = value & 1
            # The falling edge copies the live buttons into the serial latch.
            # While strobe is high, reads continuously report A.
            if self.controller_strobe or (previous and not self.controller_strobe):
                self.controllers = list(self.controller_state)
        elif address == 0x4014:
            # OAMDMA: copy one complete 256-byte CPU page into PPU OAM,
            # beginning at the current OAMADDR and wrapping at $FF.
            page = value << 8
            for offset in range(0x100):
                self.ppu_device.oam[(self.ppu_device.oam_address + offset) & 0xff] = self.read(page + offset)
            self.ppu_device.oam_address = (self.ppu_device.oam_address + 0x100) & 0xff
        elif address < 0x4020:
            self.apu_io[address - 0x4000] = value
        elif address >= 0x8000 and self.rom and self.rom.mapper == 66:
            # Mapper 66 exposes 32 KB PRG and 8 KB CHR banks.  The cartridge
            # may contain fewer banks than the four selector bits can name.
            self.prg_bank = ((value >> 4) & 0x03) % self._prg_bank_count
            self.chr_bank = (value & 0x03) % self._chr_bank_count
            start = self.chr_bank * 0x2000
            end = start + 0x2000
            if end <= len(self.rom.chr_rom):
                self.ppu_device.chr_rom = self.rom.chr_rom[start:end]

    def set_controller(self, player: int, buttons: int) -> None:
        """Set an NES controller bitfield: A,B,Select,Start,Up,Down,Left,Right."""
        self.controller_state[player & 1] = buttons & 0xff
        if self.controller_strobe:
            self.controllers[player & 1] = buttons & 0xff

    def _controller_read(self, player: int) -> int:
        player &= 1
        if self.controller_strobe:
            return self.controller_state[player] & 1
        value = self.controllers[player] & 1
        self.controllers[player] = (self.controllers[player] >> 1) | 0x80
        return value
