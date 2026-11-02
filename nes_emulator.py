"""A compact NES emulator foundation: iNES loader, CPU bus, and 6502 core."""
from __future__ import annotations

import argparse
import base64
import binascii
import struct
import zlib
import time
import traceback
import tkinter as tk
from tkinter import filedialog
import pickle
import wave
import io
import queue
import threading
from dataclasses import dataclass
from pathlib import Path

from nes import Bus as CoreBus, PPU as CorePPU, Rom as CoreRom, RomError
from nes.ppu import NES_PALETTE


class LegacyRom:
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
        return cls(prg, chr_data, mapper, "vertical" if flags6 & 1 else "horizontal")


class LegacyPPU:
    """Minimal register-level PPU model suitable for CPU integration tests."""
    def __init__(self):
        self.chr_rom = bytes(0x2000)
        self.vram = bytearray(0x800)
        self.palette = bytearray(0x20)
        self.oam = bytearray(0x100)
        self.frame = bytearray(256 * 240)
        self.address = 0
        self.temp_address = 0
        self.address_latch = False
        self.control = 0
        self.mask = 0x08
        self.mirroring = "horizontal"
        self.scroll_x = 0
        self.scroll_y = 0
        self.data_buffer = 0
        self.oam_address = 0
        self.status = 0
        self.scanline = 0
        self.dot = 0
        self.nmi_pending = False

    def begin_frame(self) -> None:
        self.nmi_pending = False

    def clock(self, dots: int) -> None:
        """Advance PPU timing; one CPU cycle corresponds to three PPU dots."""
        for _ in range(dots):
            self.dot += 1
            if self.dot >= 341:
                self.dot = 0
                self.scanline += 1
                if self.scanline == 241:
                    self.status |= 0x80
                    self.nmi_pending = bool(self.control & 0x80)
                elif self.scanline >= 262:
                    self.scanline = 0
                    self.status &= 0x7F

    def read_register(self, register: int) -> int:
        register &= 7
        if register == 2:  # PPUSTATUS
            self.address_latch = False
            value = self.status
            self.status &= 0x7F
            return value
        if register == 4:  # OAMDATA
            return self.oam[self.oam_address]
        if register == 7:  # PPUDATA
            address = self.address
            value = self._read_vram(address)
            if address < 0x3F00:
                value, self.data_buffer = self.data_buffer, value
            self.address = (self.address + 1) & 0x3FFF
            return value
        return 0

    def write_register(self, register: int, value: int) -> None:
        register &= 7; value &= 0xFF
        if register == 0: self.control = value
        elif register == 1: self.mask = value
        elif register == 3: self.oam_address = value
        elif register == 4:
            self.oam[self.oam_address] = value; self.oam_address = (self.oam_address + 1) & 0xFF
        elif register == 5:
            if not self.address_latch: self.scroll_x = value
            else: self.scroll_y = value
            self.address_latch = not self.address_latch
        elif register == 6:
            if not self.address_latch: self.temp_address = (value & 0x3F) << 8
            else:
                self.temp_address = (self.temp_address & 0x3F00) | value
                self.address = self.temp_address
            self.address_latch = not self.address_latch
        elif register == 7:
            self._write_vram(self.address, value); self.address = (self.address + (32 if self.control & 4 else 1)) & 0x3FFF

    def _read_vram(self, address: int) -> int:
        address &= 0x3FFF
        if address >= 0x3F00:
            index = (address - 0x3F00) & 0x1F
            if index in (0x10, 0x14, 0x18, 0x1C): index -= 0x10
            return self.palette[index]
        return self.vram[address & 0x7FF]

    def _write_vram(self, address: int, value: int) -> None:
        address &= 0x3FFF
        if address >= 0x3F00:
            index = (address - 0x3F00) & 0x1F
            if index in (0x10, 0x14, 0x18, 0x1C): index -= 0x10
            self.palette[index] = value
        elif address >= 0x2000: self.vram[self._nametable_index(address)] = value

    def render_test_pattern(self) -> None:
        """Fill the frame buffer with a visible deterministic diagnostic pattern."""
        for y in range(240):
            for x in range(256): self.frame[y * 256 + x] = ((x // 16) ^ (y // 16)) & 0x3F

    def render_background(self, mirroring: str = "horizontal") -> None:
        """Render the first nametable as 32x30 background tiles into frame."""
        self.mirroring = mirroring
        if not (self.mask & 0x08):
            self.frame[:] = bytes([self.palette[0]]) * len(self.frame)
            return
        name_base = (self.control & 3) * 0x400
        pattern_base = 0x1000 if self.control & 0x10 else 0
        for tile_y in range(30):
            for tile_x in range(32):
                tile = self._read_vram(0x2000 + name_base + tile_y * 32 + tile_x)
                attribute = self._read_vram(0x2000 + name_base + 0x3C0 + (tile_y // 4) * 8 + tile_x // 4)
                quadrant = ((tile_y % 4) // 2) * 2 + ((tile_x % 4) // 2)
                palette_id = (attribute >> (quadrant * 2)) & 3
                for row in range(8):
                    low = self.chr_rom[pattern_base + tile * 16 + row]
                    high = self.chr_rom[pattern_base + tile * 16 + row + 8]
                    for col in range(8):
                        bit = 7 - col
                        colour = ((high >> bit) & 1) * 2 + ((low >> bit) & 1)
                        screen_x = (tile_x * 8 + col - self.scroll_x) % 256
                        screen_y = (tile_y * 8 + row - self.scroll_y) % 240
                        self.frame[screen_y * 256 + screen_x] = self.palette[(palette_id * 4 + colour) & 0x1F]

    def _read_vram(self, address: int) -> int:
        address &= 0x3FFF
        if address >= 0x3F00:
            index = (address - 0x3F00) & 0x1F
            if index in (0x10, 0x14, 0x18, 0x1C): index -= 0x10
            return self.palette[index]
        if address < 0x2000:
            return self.chr_rom[address % len(self.chr_rom)]
        return self.vram[self._nametable_index(address)]

    def _nametable_index(self, address: int) -> int:
        relative = (address - 0x2000) & 0x0FFF
        table = relative // 0x400
        offset = relative & 0x3FF
        if self.mirroring == "vertical":
            physical = table & 1
        else:
            physical = 0 if table in (0, 1) else 1
        return physical * 0x400 + offset

    def render_sprites(self) -> None:
        """Render the 64 8x8 sprites stored in OAM over the background."""
        # Draw in reverse OAM order so lower-numbered sprites have priority.
        for sprite in range(63, -1, -1):
            base = sprite * 4
            y, tile, attributes, x = self.oam[base:base + 4]
            flip_h = bool(attributes & 0x40)
            flip_v = bool(attributes & 0x80)
            behind_background = bool(attributes & 0x20)
            palette_id = attributes & 3
            for row in range(8):
                source_row = 7 - row if flip_v else row
                low = self.chr_rom[tile * 16 + source_row]
                high = self.chr_rom[tile * 16 + source_row + 8]
                screen_y = y + 1 + row
                if screen_y >= 240:
                    continue
                for col in range(8):
                    source_col = col if flip_h else 7 - col
                    colour = (((high >> source_col) & 1) << 1) | ((low >> source_col) & 1)
                    if colour == 0:
                        continue
                    screen_x = x + col
                    if screen_x >= 256:
                        continue
                    index = screen_y * 256 + screen_x
                    if behind_background and self.frame[index] != self.palette[0]:
                        continue
                    self.frame[index] = self.palette[0x10 + palette_id * 4 + colour]

    def save_ppm(self, path: str | Path) -> None:
        """Save the indexed frame buffer as a portable RGB image."""
        nes_palette = [(84,84,84),(0,30,116),(8,16,144),(48,0,136),(68,0,100),(92,0,48),(84,4,0),(60,24,0),(32,42,0),(8,58,0),(0,64,0),(0,60,0),(0,50,60),(0,0,0),(0,0,0),(0,0,0),
                       (152,150,152),(8,76,196),(48,50,236),(92,30,228),(136,20,176),(160,20,100),(152,34,32),(120,60,0),(84,90,0),(40,114,0),(8,124,0),(0,118,40),(0,102,120),(0,0,0),(0,0,0),(0,0,0),
                       (236,238,236),(76,154,236),(120,124,236),(176,98,236),(228,84,236),(236,88,180),(236,106,100),(212,136,32),(160,170,0),(112,196,0),(76,208,32),(56,204,108),(56,180,204),(60,60,60),(0,0,0),(0,0,0),
                       (236,238,236),(168,204,236),(188,188,236),(212,178,236),(236,174,236),(236,174,212),(236,180,176),(228,196,144),(204,210,120),(180,222,120),(168,226,144),(152,226,180),(160,214,228),(160,162,160),(0,0,0),(0,0,0)]
        with Path(path).open("wb") as output:
            output.write(f"P6\n256 240\n255\n".encode())
            output.write(bytes(component for index in self.frame for component in nes_palette[index & 0x3F]))

    def save_png(self, path: str | Path) -> None:
        """Save the indexed frame buffer as a standard RGB PNG."""
        colours = [(84,84,84),(0,30,116),(8,16,144),(48,0,136),(68,0,100),(92,0,48),(84,4,0),(60,24,0),(32,42,0),(8,58,0),(0,64,0),(0,60,0),(0,50,60),(0,0,0),(0,0,0),(0,0,0),(152,150,152),(8,76,196),(48,50,236),(92,30,228),(136,20,176),(160,20,100),(152,34,32),(120,60,0),(84,90,0),(40,114,0),(8,124,0),(0,118,40),(0,102,120),(0,0,0),(0,0,0),(0,0,0),(236,238,236),(76,154,236),(120,124,236),(176,98,236),(228,84,236),(236,88,180),(236,106,100),(212,136,32),(160,170,0),(112,196,0),(76,208,32),(56,204,108),(56,180,204),(60,60,60),(0,0,0),(0,0,0),(236,238,236),(168,204,236),(188,188,236),(212,178,236),(236,174,236),(236,174,212),(236,180,176),(228,196,144),(204,210,120),(180,222,120),(168,226,144),(152,226,180),(160,214,228),(160,162,160),(0,0,0),(0,0,0)]
        raw = b''.join(b'\0' + b''.join(bytes(colours[index & 0x3F]) for index in self.frame[y * 256:(y + 1) * 256]) for y in range(240))
        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', binascii.crc32(kind + data) & 0xFFFFFFFF)
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 256, 240, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
        Path(path).write_bytes(png)


class LegacyBus:
    def __init__(self, rom: Rom | None = None):
        self.rom = rom
        self.ppu_device = PPU()
        if rom:
            self.ppu_device.chr_rom = rom.chr_rom
            self.ppu_device.mirroring = rom.mirroring
        self.prg_bank = 0
        self.chr_bank = 0
        self.ram = bytearray(0x800)
        self.ppu = bytearray(8)
        self.apu_io = bytearray(0x20)
        self.expansion = bytearray(0x1FE0)
        self.controllers = [0, 0]

    def read(self, address: int) -> int:
        address &= 0xFFFF
        if address < 0x2000:
            return self.ram[address & 0x7FF]
        if address < 0x4000:
            return self.ppu_device.read_register(address - 0x2000)
        if address == 0x4016:
            return self.controllers[0] & 1
        if address == 0x4017:
            return self.controllers[1] & 1
        if address < 0x4020:
            return self.apu_io[address - 0x4000]
        if address < 0x6000:
            return self.expansion[address - 0x4020]
        if address < 0x8000:
            return 0
        if self.rom:
            if self.rom.mapper == 66:
                index = (self.prg_bank * 0x8000) + (address - 0x8000)
            else:
                index = address - 0x8000
                if len(self.rom.prg_rom) == 0x4000:
                    index %= 0x4000
            return self.rom.prg_rom[index]
        return 0

    def write(self, address: int, value: int) -> None:
        address &= 0xFFFF
        value &= 0xFF
        if address < 0x2000:
            self.ram[address & 0x7FF] = value
        elif address < 0x4000:
            self.ppu_device.write_register(address - 0x2000, value)
        elif address == 0x4016:
            if value & 1:
                self.controllers = [0, 0]
        elif 0x4000 <= address < 0x4020:
            self.apu_io[address - 0x4000] = value
        elif address >= 0x8000 and self.rom and self.rom.mapper == 66:
            # Mapper 66: bits 4-5 select a 32 KB PRG bank; bits 0-1 select
            # an 8 KB CHR bank. The register is write-only.
            self.prg_bank = (value >> 4) & 0x03
            self.chr_bank = value & 0x03
            start = self.chr_bank * 0x2000
            end = start + 0x2000
            if end <= len(self.rom.chr_rom):
                self.ppu_device.chr_rom = self.rom.chr_rom[start:end]


NTSC_CPU_HZ = 1_789_773          # 21.477272 MHz master clock / 12
NTSC_FPS = NTSC_CPU_HZ / 29_780.5  # ~60.0988 frames per second


class CPU:
    C, Z, I, D, B, U, V, N = (1 << i for i in range(8))

    def __init__(self, bus: Bus):
        self.bus = bus
        self.a = self.x = self.y = 0
        self.sp = 0xFD
        self.p = self.I | self.U
        self.pc = 0
        self.cycles = 0
        bus.apu.now_fn = lambda: self.cycles   # NEW: lets the APU sync itself on register access
        self._cross = 0
        self._read = bus.read
        self._table = self._build_table()

    def reset(self) -> None:
        self.sp, self.p = 0xFD, self.I | self.U
        self.pc = self.read(0xFFFC) | (self.read(0xFFFD) << 8)

    def nmi(self) -> None:
        """Service the PPU vblank NMI when enabled by PPUCTRL bit 7."""
        self.push(self.pc >> 8)
        self.push(self.pc)
        self.push((self.p & ~self.B) | self.U)
        self.p |= self.I
        self.pc = self.read(0xFFFA) | (self.read(0xFFFB) << 8)
        self.cycles += 7

    def read(self, address: int) -> int: return self.bus.read(address)
    def write(self, address: int, value: int) -> None: self.bus.write(address, value)
    def byte(self) -> int:
        value = self.read(self.pc); self.pc = (self.pc + 1) & 0xFFFF; return value
    def word(self, lo: int | None = None) -> int:
        if lo is None: lo = self.byte()
        hi = self.byte(); return lo | (hi << 8)
    def push(self, value: int) -> None:
        self.write(0x100 | self.sp, value); self.sp = (self.sp - 1) & 0xFF
    def pop(self) -> int:
        self.sp = (self.sp + 1) & 0xFF; return self.read(0x100 | self.sp)
    def set_nz(self, value: int) -> int:
        value &= 0xFF; self.p = (self.p & ~(self.Z | self.N)) | (self.Z if value == 0 else 0) | (self.N if value & 0x80 else 0); return value
    def adc(self, value: int) -> None:
        total = self.a + value + (1 if self.p & self.C else 0)
        self.p = (self.p & ~(self.C | self.V)) | (self.C if total > 255 else 0) | (self.V if (~(self.a ^ value) & (self.a ^ total) & 0x80) else 0)
        self.a = self.set_nz(total)

    def sbc(self, value: int) -> None:
        result = self.a - value - (0 if self.p & self.C else 1)
        wrapped = result & 0xFF
        self.p = (self.p & ~(self.C | self.V)) | (self.C if result >= 0 else 0) | (self.V if ((self.a ^ wrapped) & (self.a ^ value) & 0x80) else 0)
        self.a = self.set_nz(wrapped)

    def run_cycles(self, cycle_budget: int) -> int:
        """Execute instructions until approximately cycle_budget cycles elapse."""
        start = self.cycles
        end = start + cycle_budget
        ppu, apu = self.bus.ppu_device, self.bus.apu
        clock, read, table = ppu.clock, self._read, self._table
        while self.cycles < end:
            pc = self.pc
            self.pc = (pc + 1) & 0xFFFF
            elapsed = table[read(pc)]()
            self.cycles += elapsed
            clock(elapsed * 3)
            if ppu.nmi_pending:
                ppu.nmi_pending = False
                self.nmi()
        apu.sync(self.cycles)   # APU catches up once per run
        return self.cycles - start

    # ------------------------------------------------------------------
    # Table-driven instruction dispatch (one closure per opcode).
    # ------------------------------------------------------------------
    def _build_table(self) -> list:
        cpu, read, write = self, self.bus.read, self.bus.write
        table = [None] * 256

        # -- flag helper -------------------------------------------------
        def nz(value):
            cpu.p = (cpu.p & 0x7D) | (value & 0x80) | (0 if value else 2)

        # -- addressing modes: return the effective address ---------------
        def imm():
            address = cpu.pc; cpu.pc = (address + 1) & 0xFFFF; return address
        def zp():
            address = cpu.pc; cpu.pc = (address + 1) & 0xFFFF; return read(address)
        def zpx():
            address = cpu.pc; cpu.pc = (address + 1) & 0xFFFF; return (read(address) + cpu.x) & 0xFF
        def zpy():
            address = cpu.pc; cpu.pc = (address + 1) & 0xFFFF; return (read(address) + cpu.y) & 0xFF
        def ab():
            pc = cpu.pc; cpu.pc = (pc + 2) & 0xFFFF
            return read(pc) | (read((pc + 1) & 0xFFFF) << 8)
        def abx():
            pc = cpu.pc; cpu.pc = (pc + 2) & 0xFFFF
            base = read(pc) | (read((pc + 1) & 0xFFFF) << 8); address = (base + cpu.x) & 0xFFFF
            cpu._cross = 1 if (base ^ address) & 0xFF00 else 0; return address
        def aby():
            pc = cpu.pc; cpu.pc = (pc + 2) & 0xFFFF
            base = read(pc) | (read((pc + 1) & 0xFFFF) << 8); address = (base + cpu.y) & 0xFFFF
            cpu._cross = 1 if (base ^ address) & 0xFF00 else 0; return address
        def inx():
            address = cpu.pc; cpu.pc = (address + 1) & 0xFFFF
            z = (read(address) + cpu.x) & 0xFF
            return read(z) | (read((z + 1) & 0xFF) << 8)
        def iny():
            address = cpu.pc; cpu.pc = (address + 1) & 0xFFFF
            z = read(address); base = read(z) | (read((z + 1) & 0xFF) << 8); address = (base + cpu.y) & 0xFFFF
            cpu._cross = 1 if (base ^ address) & 0xFF00 else 0; return address

        # -- read-type operations (take the operand value) ----------------
        def ora(v): r = cpu.a | v; cpu.a = r; nz(r)
        def and_(v): r = cpu.a & v; cpu.a = r; nz(r)
        def eor(v): r = cpu.a ^ v; cpu.a = r; nz(r)
        def lda(v): cpu.a = v; nz(v)
        def ldx(v): cpu.x = v; nz(v)
        def ldy(v): cpu.y = v; nz(v)
        def adc(v): cpu.adc(v)
        def sbc(v): cpu.sbc(v)
        def compare(reg_name):
            def op(v):
                r = getattr(cpu, reg_name) - v
                cpu.p = (cpu.p & 0x7C) | (1 if r >= 0 else 0) | (r & 0x80) | (2 if not r & 0xFF else 0)
            return op
        def bit(v):
            cpu.p = (cpu.p & 0x3D) | (v & 0xC0) | (0 if cpu.a & v else 2)
        def nop(v): pass

        # -- read-modify-write operations (value -> result) ---------------
        def asl(v):
            cpu.p = (cpu.p & 0xFE) | (v >> 7); r = (v << 1) & 0xFF; nz(r); return r
        def lsr(v):
            cpu.p = (cpu.p & 0xFE) | (v & 1); r = v >> 1; nz(r); return r
        def rol(v):
            r = ((v << 1) | (cpu.p & 1)) & 0xFF; cpu.p = (cpu.p & 0xFE) | (v >> 7); nz(r); return r
        def ror(v):
            r = (v >> 1) | ((cpu.p & 1) << 7); cpu.p = (cpu.p & 0xFE) | (v & 1); nz(r); return r
        def inc(v): r = (v + 1) & 0xFF; nz(r); return r
        def dec(v): r = (v - 1) & 0xFF; nz(r); return r

        # -- handler factories --------------------------------------------
        def reader(op_fn, mode, cycles, penalty):
            if penalty:
                def handler():
                    op_fn(read(mode())); return cycles + cpu._cross
            else:
                def handler():
                    op_fn(read(mode())); return cycles
            return handler
        def rmw(op_fn, mode, cycles):
            def handler():
                address = mode(); write(address, op_fn(read(address))); return cycles
            return handler
        def dcp_indirect_y():
            address = iny(); value = (read(address) - 1) & 0xFF
            write(address, value)
            compare("a")(value)
            return 8
        def dcp_absolute_y():
            address = aby(); value = (read(address) - 1) & 0xFF
            write(address, value)
            compare("a")(value)
            return 7
        def dcp_absolute_x():
            address = abx(); value = (read(address) - 1) & 0xFF
            write(address, value)
            compare("a")(value)
            return 7
        def isc_absolute_x():
            # Unofficial ISC/ISB: INC memory, then SBC the result.
            address = abx(); value = (read(address) + 1) & 0xFF
            write(address, value)
            cpu.sbc(value)
            return 7
        def slo_indirect_x():
            # Unofficial SLO: ASL memory, then ORA the result.
            address = inx(); value = read(address)
            cpu.p = (cpu.p & 0xFE) | (value >> 7)
            value = (value << 1) & 0xFF
            write(address, value)
            cpu.a |= value
            nz(cpu.a)
            return 8
        def slo_zeropage():
            address = zp(); value = read(address)
            cpu.p = (cpu.p & 0xFE) | (value >> 7)
            value = (value << 1) & 0xFF
            write(address, value)
            cpu.a |= value
            nz(cpu.a)
            return 5
        def sre_indirect_y():
            # Unofficial SRE: LSR memory, then EOR the result.
            address = iny(); value = read(address)
            cpu.p = (cpu.p & 0xFE) | (value & 1)
            value >>= 1
            write(address, value)
            cpu.a ^= value
            nz(cpu.a)
            return 8
        def dcp_zeropage_x():
            address = zpx(); value = (read(address) - 1) & 0xFF
            write(address, value)
            compare("a")(value)
            return 6
        def store(reg_name, mode, cycles):
            def handler():
                write(mode(), getattr(cpu, reg_name)); return cycles
            return handler
        def accumulator(op_fn):
            def handler():
                cpu.a = op_fn(cpu.a); return 2
            return handler
        def implied(fn, cycles=2):
            def handler():
                fn(); return cycles
            return handler
        def branch(mask, want):
            def handler():
                address = cpu.pc; offset = read(address); cpu.pc = (address + 1) & 0xFFFF
                if bool(cpu.p & mask) != want: return 2
                if offset & 0x80: offset -= 0x100
                old = cpu.pc; cpu.pc = (old + offset) & 0xFFFF
                return 4 if (old ^ cpu.pc) & 0xFF00 else 3
            return handler

        # -- group 1: ORA AND EOR ADC STA LDA CMP SBC -----------------------
        g1_modes = (("inx", inx, 6, False), ("zp", zp, 3, False), ("imm", imm, 2, False), ("abs", ab, 4, False),
                    ("iny", iny, 5, True), ("zpx", zpx, 4, False), ("aby", aby, 4, True), ("abx", abx, 4, True))
        g1_ops = (ora, and_, eor, adc, None, lda, compare("a"), sbc)
        for aaa in range(8):
            for bbb, (name, mode, cycles, penalty) in enumerate(g1_modes):
                opcode = (aaa << 5) | (bbb << 2) | 1
                if aaa == 4:
                    if name == "imm": continue
                    table[opcode] = store("a", mode, cycles + (1 if name in ("iny", "aby", "abx") else 0))
                else:
                    table[opcode] = reader(g1_ops[aaa], mode, cycles, penalty)

        # -- group 2: shifts, INC/DEC, LDX/STX -----------------------------
        shifts = (asl, rol, lsr, ror)
        for aaa in range(4):
            table[(aaa << 5) | 0x0A] = accumulator(shifts[aaa])
            for bbb, mode, cycles in ((1, zp, 5), (3, ab, 6), (5, zpx, 6), (7, abx, 7)):
                table[(aaa << 5) | (bbb << 2) | 2] = rmw(shifts[aaa], mode, cycles)
        for aaa, fn in ((6, dec), (7, inc)):
            for bbb, mode, cycles in ((1, zp, 5), (3, ab, 6), (5, zpx, 6), (7, abx, 7)):
                table[(aaa << 5) | (bbb << 2) | 2] = rmw(fn, mode, cycles)
        table[0x86], table[0x96], table[0x8E] = store("x", zp, 3), store("x", zpy, 4), store("x", ab, 4)
        table[0xA2] = reader(ldx, imm, 2, False); table[0xA6] = reader(ldx, zp, 3, False)
        table[0xB6] = reader(ldx, zpy, 4, False); table[0xAE] = reader(ldx, ab, 4, False)
        table[0xBE] = reader(ldx, aby, 4, True)
        # Unofficial DCP: DEC memory followed by CMP A, used by this ROM.
        table[0xD3], table[0xD7], table[0xDB], table[0xDF] = dcp_indirect_y, dcp_zeropage_x, dcp_absolute_y, dcp_absolute_x
        # Unofficial ISC/ISB: INC memory followed by SBC A, used by this ROM.
        table[0xFF] = isc_absolute_x
        # Unofficial SLO: ASL memory followed by ORA A, used by this ROM.
        table[0x03], table[0x07] = slo_indirect_x, slo_zeropage
        # Unofficial SRE: LSR memory followed by EOR A, used by this ROM.
        table[0x53] = sre_indirect_y

        # -- group 3: BIT, STY, LDY, CPY, CPX --------------------------------
        table[0x24], table[0x2C] = reader(bit, zp, 3, False), reader(bit, ab, 4, False)
        table[0x84], table[0x94], table[0x8C] = store("y", zp, 3), store("y", zpx, 4), store("y", ab, 4)
        table[0xA0] = reader(ldy, imm, 2, False); table[0xA4] = reader(ldy, zp, 3, False)
        table[0xB4] = reader(ldy, zpx, 4, False); table[0xAC] = reader(ldy, ab, 4, False)
        table[0xBC] = reader(ldy, abx, 4, True)
        cpy, cpx = compare("y"), compare("x")
        table[0xC0], table[0xC4], table[0xCC] = reader(cpy, imm, 2, False), reader(cpy, zp, 3, False), reader(cpy, ab, 4, False)
        table[0xE0], table[0xE4], table[0xEC] = reader(cpx, imm, 2, False), reader(cpx, zp, 3, False), reader(cpx, ab, 4, False)

        # -- branches -------------------------------------------------------
        for opcode, mask, want in ((0x10, 0x80, False), (0x30, 0x80, True), (0x50, 0x40, False), (0x70, 0x40, True),
                                   (0x90, 0x01, False), (0xB0, 0x01, True), (0xD0, 0x02, False), (0xF0, 0x02, True)):
            table[opcode] = branch(mask, want)

        # -- register transfers, counters, flags ---------------------------------
        def set_reg(dst, src_fn):
            def fn():
                v = src_fn(); setattr(cpu, dst, v); nz(v)
            return fn
        table[0xAA] = implied(set_reg("x", lambda: cpu.a)); table[0x8A] = implied(set_reg("a", lambda: cpu.x))
        table[0xA8] = implied(set_reg("y", lambda: cpu.a)); table[0x98] = implied(set_reg("a", lambda: cpu.y))
        table[0xBA] = implied(set_reg("x", lambda: cpu.sp))
        table[0xE8] = implied(set_reg("x", lambda: (cpu.x + 1) & 0xFF)); table[0xC8] = implied(set_reg("y", lambda: (cpu.y + 1) & 0xFF))
        table[0xCA] = implied(set_reg("x", lambda: (cpu.x - 1) & 0xFF)); table[0x88] = implied(set_reg("y", lambda: (cpu.y - 1) & 0xFF))
        def txs(): cpu.sp = cpu.x
        table[0x9A] = implied(txs)
        def set_flag(mask):
            def fn(): cpu.p |= mask
            return fn
        def clear_flag(mask):
            def fn(): cpu.p &= ~mask
            return fn
        table[0x38], table[0x18] = implied(set_flag(0x01)), implied(clear_flag(0x01))
        table[0x78], table[0x58] = implied(set_flag(0x04)), implied(clear_flag(0x04))
        table[0xF8], table[0xD8] = implied(set_flag(0x08)), implied(clear_flag(0x08))
        table[0xB8] = implied(clear_flag(0x40))
        table[0xEA] = implied(lambda: None)

        # -- stack -----------------------------------------------------------------
        def push(value):
            write(0x100 | cpu.sp, value & 0xFF); cpu.sp = (cpu.sp - 1) & 0xFF
        def pull():
            cpu.sp = (cpu.sp + 1) & 0xFF; return read(0x100 | cpu.sp)
        def pha(): push(cpu.a)
        def pla(): v = pull(); cpu.a = v; nz(v)
        def php(): push(cpu.p | 0x30)
        def plp(): cpu.p = (pull() | 0x20) & ~0x10
        table[0x48], table[0x68] = implied(pha, 3), implied(pla, 4)
        table[0x08], table[0x28] = implied(php, 3), implied(plp, 4)

        # -- jumps and subroutines ----------------------------------------------------
        def jmp_abs():
            cpu.pc = ab(); return 3
        def jmp_ind():
            pointer = ab()
            cpu.pc = read(pointer) | (read((pointer & 0xFF00) | ((pointer + 1) & 0xFF)) << 8); return 5
        def jsr():
            target = ab(); ret = (cpu.pc - 1) & 0xFFFF
            push(ret >> 8); push(ret); cpu.pc = target; return 6
        def rts():
            low = pull(); high = pull(); cpu.pc = (((high << 8) | low) + 1) & 0xFFFF; return 6
        def rti():
            cpu.p = (pull() | 0x20) & ~0x10; low = pull(); high = pull(); cpu.pc = (high << 8) | low; return 6
        def brk():
            pc = (cpu.pc + 1) & 0xFFFF; push(pc >> 8); push(pc); push(cpu.p | 0x30); cpu.p |= 0x04
            cpu.pc = read(0xFFFE) | (read(0xFFFF) << 8); return 7
        table[0x4C], table[0x6C], table[0x20], table[0x60], table[0x40], table[0x00] = jmp_abs, jmp_ind, jsr, rts, rti, brk

        # -- common unofficial NOPs (harmless, used by some games) ---------------------
        for opcode in (0x1A, 0x3A, 0x5A, 0x7A, 0xDA, 0xFA): table[opcode] = implied(lambda: None)
        # KIL/JAM opcodes are single-byte instructions on the NES 6502.
        # Keep the emulator running for ROMs that encounter them, without
        # consuming the following byte and shifting the instruction stream.
        for opcode in (0x02, 0x52, 0xF2): table[opcode] = implied(lambda: None)
        for opcode in (0x42, 0x80, 0x82, 0x89, 0xC2, 0xE2): table[opcode] = reader(nop, imm, 2, False)
        for opcode in (0x04, 0x44, 0x64): table[opcode] = reader(nop, zp, 3, False)
        for opcode in (0x14, 0x34, 0x54, 0x74, 0xD4, 0xF4): table[opcode] = reader(nop, zpx, 4, False)
        table[0x0C] = reader(nop, ab, 4, False)
        for opcode in (0x1C, 0x3C, 0x5C, 0x7C, 0xDC, 0xFC): table[opcode] = reader(nop, abx, 4, True)

        # -- anything left is an unimplemented / illegal opcode ----------------------------
        def missing(opcode):
            def handler():
                raise NotImplementedError(f"opcode ${opcode:02X} at ${(cpu.pc - 1) & 0xFFFF:04X}")
            return handler
        for opcode in range(256):
            if table[opcode] is None: table[opcode] = missing(opcode)
        return table

    def step(self) -> int:
        pc = self.pc
        opcode = self._read(pc)
        self.pc = (pc + 1) & 0xFFFF
        cycles = self._table[opcode]()
        self.cycles += cycles
        return cycles


# Public compatibility exports. The implementation now lives in the nes package;
# this module remains the single-file launcher used by existing commands/tests.
Rom = CoreRom
PPU = CorePPU
Bus = CoreBus


def run_window(rom: Rom) -> None:
    """Run the emulator in a small Tk window with keyboard controller input."""
    bus = Bus(rom)
    cpu = CPU(bus)
    cpu.reset()
    root = tk.Tk()
    root.title("KADACHI  /  NES EMULATOR")
    root.configure(bg="#111318")
    try:
        import winsound
    except ImportError:
        winsound = None
    try:
        import sounddevice
    except ImportError:
        sounddevice = None

    audio_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
    audio_stop = threading.Event()
    audio_buffer = bytearray()
    stream_buffer = bytearray()
    stream_lock = threading.Lock()
    audio_stream = None
    # CHANGED: cushion before playback starts / resumes after an underrun.
    prefill_bytes = bus.apu.sample_rate * 2 // 10   # ~100 ms
    max_stream_bytes = bus.apu.sample_rate * 2 // 4  # ~250 ms latency cap
    primed = [False]

    def stream_callback(outdata, frames, _time_info, _status):
        outbytes = memoryview(outdata).cast("B")
        wanted = frames * 2
        with stream_lock:
            # CHANGED: stay silent until enough audio is buffered, and go
            # back to waiting after an underrun instead of stuttering.
            if not primed[0]:
                if len(stream_buffer) >= prefill_bytes:
                    primed[0] = True
                else:
                    outbytes[:wanted] = b"\x00" * wanted
                    return
            available = min(wanted, len(stream_buffer))
            outbytes[:available] = stream_buffer[:available]
            del stream_buffer[:available]
            if available < wanted:
                outbytes[available:wanted] = b"\x00" * (wanted - available)
                primed[0] = False

    if sounddevice is not None:
        try:
            audio_stream = sounddevice.RawOutputStream(
                samplerate=bus.apu.sample_rate,
                channels=1,
                dtype="int16",
                callback=stream_callback,
                blocksize=0,
                latency="high",  # CHANGED
            )
            audio_stream.start()
        except Exception as error:
            print(f"Audio output disabled: {error}", flush=True)
            audio_stream = None
            sounddevice = None

    def audio_worker():
        while not audio_stop.is_set():
            pcm = audio_queue.get()
            try:
                if pcm is None:
                    return
                stream = io.BytesIO()
                with wave.open(stream, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(bus.apu.sample_rate)
                    wav.writeframes(pcm)
                winsound.PlaySound(stream.getvalue(), winsound.SND_MEMORY)
            finally:
                audio_queue.task_done()

    if winsound is not None and sounddevice is None:
        threading.Thread(target=audio_worker, name="kadachi-audio", daemon=True).start()

    def play_audio():
        nonlocal audio_buffer
        if winsound is None and sounddevice is None:
            return
        pcm = bus.apu.take_samples()
        if not pcm:
            return
        # CHANGED: sounddevice path pushes every frame, no 100 ms batching.
        if sounddevice is not None:
            with stream_lock:
                stream_buffer.extend(pcm)
                if len(stream_buffer) > max_stream_bytes:
                    del stream_buffer[:-max_stream_bytes]
            return
        # winsound fallback: still needs ~100 ms clips.
        audio_buffer.extend(pcm)
        chunk_size = bus.apu.sample_rate * 2 // 10
        if len(audio_buffer) < chunk_size:
            return
        pcm = bytes(audio_buffer)
        audio_buffer.clear()
        try:
            audio_queue.put_nowait(pcm)
        except queue.Full:
            pass

    root.resizable(True, True)
    root.minsize(760, 560)
    root.geometry("1080x700")

    # Application chrome is intentionally kept in Tk so the emulator remains
    # dependency-free and the game canvas can still scale cleanly.
    palette = {"bg": "#111318", "panel": "#1a1d24", "panel2": "#222631",
               "line": "#303644", "text": "#edf0f5", "muted": "#8e97a8",
               "accent": "#ffb84d", "green": "#6fe0aa"}
    header = tk.Frame(root, bg=palette["panel"], height=64)
    header.pack(fill="x", side="top")
    header.pack_propagate(False)
    tk.Label(header, text="KADACHI", bg=palette["panel"], fg=palette["accent"],
             font=("Segoe UI", 18, "bold")).pack(side="left", padx=(24, 8))
    tk.Label(header, text="NES EMULATOR", bg=palette["panel"], fg=palette["muted"],
             font=("Segoe UI", 10, "bold")).pack(side="left", pady=(5, 0))
    status = tk.Label(header, text="●  RUNNING", bg=palette["panel"], fg=palette["green"],
                     font=("Segoe UI", 9, "bold"))
    status.pack(side="right", padx=24)

    body = tk.Frame(root, bg=palette["bg"])
    body.pack(fill="both", expand=True)
    sidebar = tk.Frame(body, bg=palette["panel"], width=240)
    sidebar.pack(fill="y", side="left")
    sidebar.pack_propagate(False)

    def action_button(parent, label, command, accent=False):
        return tk.Button(parent, text=label, command=command, anchor="w", padx=14,
                         bg=palette["accent"] if accent else palette["panel2"],
                         fg="#17191d" if accent else palette["text"],
                         activebackground="#ffd07a" if accent else palette["line"],
                         activeforeground=palette["text"], relief="flat", bd=0,
                         font=("Segoe UI", 10, "bold"), cursor="hand2")

    tk.Label(sidebar, text="CARTRIDGE", bg=palette["panel"], fg=palette["muted"],
             font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=20, pady=(24, 8))
    current_rom = tk.StringVar(value=Path(rom.path).name if hasattr(rom, "path") else "Loaded cartridge")
    rom_label = tk.Label(sidebar, textvariable=current_rom, bg=palette["panel"], fg=palette["text"],
                         font=("Segoe UI", 10), anchor="w", wraplength=195)
    rom_label.pack(fill="x", padx=20, pady=(0, 14))
    controls = tk.Frame(sidebar, bg=palette["panel"])
    controls.pack(fill="x", padx=14)
    source_image = tk.PhotoImage(width=256, height=240)
    display_image = source_image
    screen_wrap = tk.Frame(body, bg=palette["bg"])
    screen_wrap.pack(expand=True, fill="both", padx=28, pady=26)
    screen_frame = tk.Frame(screen_wrap, bg="#07080a", highlightthickness=1,
                            highlightbackground=palette["line"])
    screen_frame.pack(expand=True, fill="both")
    label = tk.Label(screen_frame, image=display_image, bd=0, bg="#07080a")
    label.pack(expand=True, fill="both")

    def set_status(text, colour=palette["green"]):
        status.configure(text="●  " + text, fg=colour)

    def save_state(slot):
        try:
            # CPU dispatch and the APU clock hook are runtime closures, so
            # serialize the machine data and CPU registers instead of the CPU
            # object itself.
            apu_now_fn = bus.apu.now_fn
            bus.apu.now_fn = None
            snapshot = {
                "rom": rom,
                "bus": bus,
                "cpu": {name: getattr(cpu, name) for name in
                        ("a", "x", "y", "sp", "p", "pc", "cycles")},
            }
            with open(Path(f"kadachi_state_{slot}.sav"), "wb") as state_file:
                pickle.dump(snapshot, state_file)
            bus.apu.now_fn = apu_now_fn
            set_status(f"STATE {slot} SAVED")
        except Exception as error:
            if 'apu_now_fn' in locals():
                bus.apu.now_fn = apu_now_fn
            print(f"Save state failed: {error}", flush=True)
            traceback.print_exc()

    def load_state(slot):
        nonlocal bus, cpu, rom, cycle_target, deadline
        path = Path(f"kadachi_state_{slot}.sav")
        if not path.exists():
            set_status(f"SLOT {slot} EMPTY", "#ff8f8f")
            return
        try:
            with open(path, "rb") as state_file:
                saved = pickle.load(state_file)
            rom, bus = saved["rom"], saved["bus"]
            cpu = CPU(bus)
            for name, value in saved["cpu"].items():
                setattr(cpu, name, value)
            current_rom.set(Path(rom.path).name if hasattr(rom, "path") else "Loaded cartridge")
            cycle_target, deadline = float(cpu.cycles), time.perf_counter()
            set_status(f"STATE {slot} LOADED")
        except Exception as error:
            print(f"Load state failed: {error}", flush=True)
            traceback.print_exc()

    def open_rom():
        nonlocal bus, cpu, rom, cycle_target, deadline
        path = filedialog.askopenfilename(parent=root, title="Open NES cartridge",
                                          filetypes=[("NES ROM", "*.nes"), ("All files", "*.*")])
        if not path:
            return
        try:
            rom = Rom.load(path)
            bus = Bus(rom); cpu = CPU(bus); cpu.reset()
            current_rom.set(Path(path).name)
            cycle_target, deadline = float(cpu.cycles), time.perf_counter()
            set_status("RUNNING")
        except Exception as error:
            print(f"Could not open ROM: {error}", flush=True)
            traceback.print_exc()

    action_button(controls, "＋  OPEN CARTRIDGE", open_rom, True).pack(fill="x", pady=4)
    action_button(controls, "↻  RESTART GAME", lambda: (cpu.reset(), set_status("RESTARTED"))).pack(fill="x", pady=4)
    tk.Label(sidebar, text="SAVE STATES", bg=palette["panel"], fg=palette["muted"],
             font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=20, pady=(28, 8))
    for slot in range(1, 5):
        row = tk.Frame(sidebar, bg=palette["panel"])
        row.pack(fill="x", padx=14, pady=3)
        tk.Label(row, text=f"SLOT {slot}", width=8, anchor="w", bg=palette["panel"],
                 fg=palette["text"], font=("Segoe UI", 9, "bold")).pack(side="left", padx=6)
        tk.Button(row, text="SAVE", command=lambda s=slot: save_state(s), bg=palette["panel2"], fg=palette["text"], relief="flat", bd=0).pack(side="right", padx=2)
        tk.Button(row, text="LOAD", command=lambda s=slot: load_state(s), bg=palette["panel2"], fg=palette["text"], relief="flat", bd=0).pack(side="right", padx=2)
    tk.Label(sidebar, text="F5–F8  save state   •   Shift+F5–F8  load state", bg=palette["panel"], fg=palette["muted"], font=("Segoe UI", 8), wraplength=200, justify="left").pack(anchor="w", padx=20, pady=(24, 0))

    def scale_image():
        nonlocal display_image
        scale = max(1, min(root.winfo_width() // 256, root.winfo_height() // 240))
        if scale == 1:
            display_image = source_image
        else:
            display_image = source_image.zoom(scale, scale)
        label.configure(image=display_image)

    root.bind("<Configure>", lambda _event: scale_image())

    keys = {
        "z": 0x01, "x": 0x02, "a": 0x01, "b": 0x02,
        "Shift_L": 0x04, "Shift_R": 0x04,
        "Return": 0x08, "KP_Enter": 0x08, "Up": 0x10, "Down": 0x20,
        "Left": 0x40, "Right": 0x80,
    }
    pressed = set()
    pending_buttons = 0

    def key_name(event):
        if event.keysym in keys:
            return event.keysym
        if getattr(event, "keycode", 0) in (13, 36, 108):
            return "Return"
        return event.char.lower() if getattr(event, "char", "").lower() in keys else None

    def key_down(event):
        nonlocal pending_buttons
        name = key_name(event)
        if name:
            pressed.add(name)
            pending_buttons |= keys[name]
            bus.set_controller(0, sum(keys[k] for k in pressed) | pending_buttons)

    def key_up(event):
        name = key_name(event)
        if name:
            pressed.discard(name)
            bus.set_controller(0, sum(keys[k] for k in pressed))

    root.bind_all("<KeyPress>", key_down)
    root.bind_all("<KeyRelease>", key_up)
    root.bind("<KeyPress>", key_down)
    root.bind("<KeyRelease>", key_up)
    for index, key in enumerate(("F5", "F6", "F7", "F8"), start=1):
        root.bind_all(f"<{key}>", lambda _event, s=index: save_state(s))
        root.bind_all(f"<Shift-{key}>", lambda _event, s=index: load_state(s))
    label.bind("<Button-1>", lambda _event: root.focus_force())
    root.focus_force()

    cycles_per_frame = NTSC_CPU_HZ / NTSC_FPS
    frame_time = 1 / NTSC_FPS
    cycle_target = float(cpu.cycles)
    deadline = time.perf_counter()
    skipped = 0

    def draw_frame():
        nonlocal pending_buttons, deadline, skipped, cycle_target
        try:
            bus.ppu_device.begin_frame()
            bus.set_controller(0, sum(keys[k] for k in pressed) | pending_buttons)
            cycle_target += cycles_per_frame
            cpu.run_cycles(max(1, int(cycle_target - cpu.cycles)))
            play_audio()
            pending_buttons = 0
        except Exception as error:
            print(f"NES emulator stopped: {error}", flush=True)
            traceback.print_exc()
            root.title("NES Emulator")
            return
        ppu = bus.ppu_device
        deadline += frame_time
        if time.perf_counter() > deadline + frame_time and skipped < 4:
            skipped += 1
        else:
            skipped = 0
            ppu.render_frame(rom.mirroring)
            try:
                source_image.configure(data=base64.b64encode(ppu.png_bytes()))
                scale_image()
            except tk.TclError:
                ppu.save_png(Path("frame.png"))
                source_image.configure(file="frame.png")
                scale_image()
        now = time.perf_counter()
        if deadline < now - 0.25:
            deadline = now
        root.after(max(1, int((deadline - now) * 1000)), draw_frame)

    def close_window():
        if winsound is not None:
            audio_stop.set()
            try:
                audio_queue.put_nowait(None)
            except queue.Full:
                pass
        if audio_stream is not None:
            audio_stream.stop()
            audio_stream.close()
        bus.ppu_device.render_frame(rom.mirroring)
        bus.ppu_device.save_png("frame.png")
        root.destroy()

    draw_frame()
    root.protocol("WM_DELETE_WINDOW", close_window)
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and step an NES mapper-0 ROM")
    parser.add_argument("rom"); parser.add_argument("--steps", type=int, default=0)
    parser.add_argument("--render", metavar="FILE", help="render the current background to a PPM image")
    parser.add_argument("--frames", type=int, default=1, help="frames of CPU execution before rendering")
    parser.add_argument("--dump-vram", action="store_true", help="print nametable and PPU diagnostics")
    parser.add_argument("--window", action="store_true", help="run the interactive emulator window")
    args = parser.parse_args(); rom = Rom.load(args.rom)
    if args.window:
        run_window(rom)
        return
    bus = Bus(rom); cpu = CPU(bus); cpu.reset()
    print(f"PRG: {len(rom.prg_rom)} bytes | CHR: {len(rom.chr_rom)} bytes | mirroring: {rom.mirroring}")
    print(f"Reset vector: ${cpu.pc:04X}")
    for _ in range(args.steps):
        print(f"${cpu.pc:04X}  A={cpu.a:02X} X={cpu.x:02X} Y={cpu.y:02X} SP={cpu.sp:02X}")
        cpu.step()
    if args.render:
        for _ in range(max(0, args.frames)):
            bus.ppu_device.begin_frame()
            cpu.run_cycles(29780)
        bus.ppu_device.render_background(rom.mirroring)
        bus.ppu_device.render_sprites()
        if str(args.render).lower().endswith('.png'):
            bus.ppu_device.save_png(args.render)
        else:
            bus.ppu_device.save_ppm(args.render)
        print(f"Rendered frame to {args.render}")
    if args.dump_vram:
        ppu = bus.ppu_device
        nonzero = sum(1 for value in ppu.vram if value)
        print(f"VRAM nonzero: {nonzero}/2048 | palette={list(ppu.palette)} | PPUCTRL=${ppu.control:02X} PPUMASK=${ppu.mask:02X}")


if __name__ == "__main__": main()
