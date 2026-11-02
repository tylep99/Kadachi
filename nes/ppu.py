"""Minimal PPU register, timing, and software rendering model."""

from __future__ import annotations

import binascii
import struct
import zlib
from pathlib import Path


NES_PALETTE = [
    (84,84,84),(0,30,116),(8,16,144),(48,0,136),(68,0,100),(92,0,48),(84,4,0),(60,24,0),(32,42,0),(8,58,0),(0,64,0),(0,60,0),(0,50,60),(0,0,0),(0,0,0),(0,0,0),
    (152,150,152),(8,76,196),(48,50,236),(92,30,228),(136,20,176),(160,20,100),(152,34,32),(120,60,0),(84,90,0),(40,114,0),(8,124,0),(0,118,40),(0,102,120),(0,0,0),(0,0,0),(0,0,0),
    (236,238,236),(76,154,236),(120,124,236),(176,98,236),(228,84,236),(236,88,180),(236,106,100),(212,136,32),(160,170,0),(112,196,0),(76,208,32),(56,204,108),(56,180,204),(60,60,60),(0,0,0),(0,0,0),
    (236,238,236),(168,204,236),(188,188,236),(212,178,236),(236,174,236),(236,174,212),(236,180,176),(228,196,144),(204,210,120),(180,222,120),(168,226,144),(152,226,180),(160,214,228),(160,162,160),(0,0,0),(0,0,0),
]


_ROW_CACHE: dict[int, bytes] = {}
_RGB_R = bytes(NES_PALETTE[i & 0x3F][0] for i in range(256))
_RGB_G = bytes(NES_PALETTE[i & 0x3F][1] for i in range(256))
_RGB_B = bytes(NES_PALETTE[i & 0x3F][2] for i in range(256))


def pixel_row(low: int, high: int) -> bytes:
    """Decode one 8-pixel row of a 2bpp tile into colour indexes 0-3 (cached)."""
    key = (high << 8) | low
    row = _ROW_CACHE.get(key)
    if row is None:
        row = bytes((((high >> b) & 1) << 1) | ((low >> b) & 1) for b in range(7, -1, -1))
        _ROW_CACHE[key] = row
    return row


class PPU:
    """Minimal register-level PPU model suitable for CPU integration tests."""

    @property
    def chr_rom(self) -> bytes:
        return self._chr_rom

    @chr_rom.setter
    def chr_rom(self, value: bytes) -> None:
        if value is self.__dict__.get("_chr_rom"):
            return  # same bank object: nothing changed, keep decoded tiles
        self._select_chr(value)
        if hasattr(self, "_chr_events"):
            self._chr_events.append((self.scanline, value))

    def _select_chr(self, value: bytes) -> None:
        """Make `value` the active CHR bank, reusing its decoded-tile cache."""
        self._chr_rom = value
        caches = self.__dict__.setdefault("_tile_caches", {})
        entry = caches.get(id(value))
        if entry is None or entry[0] is not value:
            if len(caches) >= 8:  # bound memory if a mapper hands out many banks
                caches.clear()
            entry = caches[id(value)] = (value, {})
        self._tile_cache = entry[1]

    def _tile_rows(self, address: int) -> tuple:
        """Eight decoded pixel rows for the tile whose pattern data starts at address."""
        rows = self._tile_cache.get(address)
        if rows is None:
            chr_rom, size = self._chr_rom, len(self._chr_rom)
            rows = tuple(pixel_row(chr_rom[(address + r) % size], chr_rom[(address + r + 8) % size]) for r in range(8))
            self._tile_cache[address] = rows
        return rows

    def __init__(self):
        self.chr_rom = bytes(0x2000)
        self.vram = bytearray(0x800)
        self.palette = bytearray(0x20)
        self.oam = bytearray(0x100)
        self.frame = bytearray(256 * 240)
        self.address = self.temp_address = 0
        self.address_latch = False
        self.control = 0
        self.mask = 0x08
        self.mirroring = "horizontal"
        self.scroll_x = self.scroll_y = 0
        self.data_buffer = self.oam_address = self.status = 0
        self.scanline = self.dot = 0
        self._ppu_scanlines = 0
        self._chr_frame_origin = 0
        self._last_chr_events: list[tuple[int, bytes]] = []
        self._chr_events: list[tuple[int, bytes]] = [(0, self._chr_rom)]
        self._render_chr_events = self._chr_events
        self.nmi_pending = False
        self.sprite_zero_hit = False
        self.sprite_zero_hit_at: tuple[int, int] | None = None
        self.frame_ready = False
        self.frame_counter = 0
        self.split_line: int | None = None
        self.hud_scroll: tuple[int, int, int] | None = None
        # Per-frame scroll history: (first_scanline, scroll_x, scroll_y, base_nametable).
        # Games such as Super Mario Bros. rewrite the scroll registers mid-frame
        # (status bar vs. playfield), so rendering must replay these writes instead
        # of using whatever the registers hold when the host happens to draw.
        self._scroll_events: list[tuple[int, int, int, int]] = [(0, 0, 0, 0)]
        # Copy of everything the picture depends on, taken when a PPU frame ends.
        self._snapshot: dict | None = None

    def begin_frame(self) -> None:
        self.nmi_pending = False
        self.frame_ready = False
        # Some cartridges change CHR banks between scanlines. The event list
        # is reset on the PPU's real frame boundary, not the host timer.
        if not self._chr_events:
            self._chr_events = [(self.scanline, self._chr_rom)]

    def clock(self, dots: int) -> None:
        self.dot += dots
        # NEW fast path: no scanline boundary and no sprite-0 hit pending.
        if self.dot < 341 and (self.sprite_zero_hit_at is None or self.sprite_zero_hit):
            return
        while True:
            hit_at = self.sprite_zero_hit_at
            if (hit_at is not None and not self.sprite_zero_hit
                    and (self.scanline > hit_at[0] or (self.scanline == hit_at[0] and self.dot >= hit_at[1]))):
                self.sprite_zero_hit = True
                self.status |= 0x40
            if self.dot < 341:
                return
            self.dot -= 341
            self.scanline += 1
            self._ppu_scanlines += 1
            if self.scanline == 241:
                self.status |= 0x80
                self.nmi_pending = bool(self.control & 0x80)
                self.frame_ready = True
                self.frame_counter += 1
                self._snapshot = {
                    "oam": bytearray(self.oam),
                    "vram": bytearray(self.vram),
                    "palette": bytearray(self.palette),
                    "control": self.control,
                    "mask": self.mask,
                    "scroll_events": list(self._scroll_events),
                    "chr_events": list(self._chr_events),
                }
            elif self.scanline == 261:
                # Pre-render line: vblank, sprite-0 hit and overflow all clear.
                self.status &= 0x1F
                self.sprite_zero_hit = False
                self.sprite_zero_hit_at = None
            elif self.scanline >= 262:
                self._last_chr_events = self._chr_events
                self._chr_events = [(0, self._chr_rom)]
                self.scanline = 0
                self._scroll_events = [(0, self.scroll_x, self.scroll_y, self._base_nametable())]
                # OAM/VRAM/scroll were prepared during vblank, so work out
                # now where sprite 0 will overlap the background this frame.
                self.sprite_zero_hit_at = self._find_sprite_zero_hit()
                # Remember the HUD-area scroll so rendering can split at sprite 0.
                self.hud_scroll = (self.scroll_x, self.scroll_y, self._base_nametable())
                self.split_line = (self.sprite_zero_hit_at[0] + 1) if self.sprite_zero_hit_at else None

    def read_register(self, register: int) -> int:
        register &= 7
        if register == 2:
            self.address_latch = False
            value = self.status
            # Reading PPUSTATUS clears vblank only, not sprite-zero hit.
            self.status &= ~0x80
            return value
        if register == 4:
            return self.oam[self.oam_address]
        if register == 7:
            address = self.address
            value = self._read_vram(address)
            if address < 0x3F00:
                value, self.data_buffer = self.data_buffer, value
            self.address = (self.address + 1) & 0x3FFF
            return value
        return 0

    def write_register(self, register: int, value: int) -> None:
        register &= 7; value &= 0xFF
        if register == 0:
            self.control = value
            # PPUCTRL bits 0-1 load t bits 10-11; a later $2006 write overwrites them.
            self.temp_address = (self.temp_address & ~0x0C00) | ((value & 3) << 10)
            if self.status & 0x80 and value & 0x80:
                self.nmi_pending = True
            self._record_scroll()
        elif register == 1: self.mask = value
        elif register == 3: self.oam_address = value
        elif register == 4:
            self.oam[self.oam_address] = value; self.oam_address = (self.oam_address + 1) & 0xFF
        elif register == 5:
            if not self.address_latch: self.scroll_x = value
            else: self.scroll_y = value
            self.address_latch = not self.address_latch
            self._record_scroll()
        elif register == 6:
            if not self.address_latch: self.temp_address = (value & 0x3F) << 8
            else: self.temp_address = (self.temp_address & 0x3F00) | value; self.address = self.temp_address
            self.address_latch = not self.address_latch
        elif register == 7:
            self._write_vram(self.address, value); self.address = (self.address + (32 if self.control & 4 else 1)) & 0x3FFF

    def _record_scroll(self) -> None:
        """Remember the scroll state from the scanline where a register write takes effect."""
        if self.scanline >= 240:
            return  # vblank writes are picked up when the next frame starts
        # A write after the end of the visible part of a line only affects the next line.
        line = self.scanline + (1 if self.dot >= 257 else 0)
        if line >= 240:
            return
        state = (line, self.scroll_x, self.scroll_y, self._base_nametable())
        events = self._scroll_events
        if events[-1][0] == line:
            events[-1] = state
        elif events[-1][1:] != state[1:]:
            events.append(state)

    def _read_vram(self, address: int) -> int:
        address &= 0x3FFF
        if address >= 0x3F00:
            index = (address - 0x3F00) & 0x1F
            if index in (0x10, 0x14, 0x18, 0x1C): index -= 0x10
            return self.palette[index]
        if address < 0x2000: return self.chr_rom[address % len(self.chr_rom)]
        return self.vram[self._nametable_index(address)]

    def _write_vram(self, address: int, value: int) -> None:
        address &= 0x3FFF
        if address >= 0x3F00:
            index = (address - 0x3F00) & 0x1F
            if index in (0x10, 0x14, 0x18, 0x1C): index -= 0x10
            self.palette[index] = value
        elif address >= 0x2000: self.vram[self._nametable_index(address)] = value

    def render_test_pattern(self) -> None:
        for y in range(240):
            for x in range(256): self.frame[y * 256 + x] = ((x // 16) ^ (y // 16)) & 0x3F

    def render_background(self, mirroring: str = "horizontal") -> None:
        self.mirroring = mirroring
        if not (self.mask & 0x08):
            self.frame[:] = bytes([self.palette[0]]) * len(self.frame); return
        name_base = self._base_nametable() * 0x400
        pattern_base = 0x1000 if self.control & 0x10 else 0
        for tile_y in range(30):
            for tile_x in range(32):
                tile = self._read_vram(0x2000 + name_base + tile_y * 32 + tile_x)
                attribute = self._read_vram(0x2000 + name_base + 0x3C0 + (tile_y // 4) * 8 + tile_x // 4)
                quadrant = ((tile_y % 4) // 2) * 2 + ((tile_x % 4) // 2)
                palette_id = (attribute >> (quadrant * 2)) & 3
                for row in range(8):
                    low = self.chr_rom[pattern_base + tile * 16 + row]; high = self.chr_rom[pattern_base + tile * 16 + row + 8]
                    for col in range(8):
                        bit = 7 - col; colour = ((high >> bit) & 1) * 2 + ((low >> bit) & 1)
                        screen_x = (tile_x * 8 + col - self.scroll_x) % 256; screen_y = (tile_y * 8 + row - self.scroll_y) % 240
                        self.frame[screen_y * 256 + screen_x] = self.palette[(palette_id * 4 + colour) & 0x1F] if colour else self.palette[0]

    def render_frame(self, mirroring: str = "horizontal") -> None:
        """Render the most recently completed PPU frame.

        The picture is built from a snapshot taken at the end of the visible
        frame, with the background drawn in horizontal bands that follow the
        scroll writes the game made while the frame was being displayed
        (e.g. a fixed status bar above a scrolling playfield).
        """
        self.mirroring = mirroring
        snap = self._snapshot
        if snap is None:
            snap = {
                "oam": self.oam, "vram": self.vram, "palette": self.palette,
                "control": self.control, "mask": self.mask,
                "scroll_events": [(0, self.scroll_x, self.scroll_y, self._base_nametable())],
                "chr_events": self._chr_events,
            }
        live = (self.oam, self.vram, self.palette, self.control, self.mask)
        self.oam, self.vram, self.palette = snap["oam"], snap["vram"], snap["palette"]
        self.control, self.mask = snap["control"], snap["mask"]
        try:
            self._render_chr_events = snap["chr_events"]
            self.frame[:] = bytes([self.palette[0]]) * len(self.frame)
            if self.mask & 0x08:
                events = snap["scroll_events"]
                for index, (line, scroll_x, scroll_y, base_nt) in enumerate(events):
                    end_line = events[index + 1][0] if index + 1 < len(events) else 240
                    if end_line > line:
                        self._draw_background(scroll_x, scroll_y, base_nt, line, end_line)
            self.render_sprites()
        finally:
            self.oam, self.vram, self.palette, self.control, self.mask = live
        # Rendering temporarily walks historical CHR banks; restore the bank
        # active at the end of the frame for the next frame's initial state.
        if self._chr_events and self._chr_rom is not self._chr_events[-1][1]:
            self._select_chr(self._chr_events[-1][1])

    def _draw_background(self, scroll_x: int, scroll_y: int, base_nt: int, y_start: int, y_end: int) -> None:
        """Draw background scanlines [y_start, y_end) using one scroll/nametable state."""
        pattern_base = 0x1000 if self.control & 0x10 else 0
        first_x, fine_x = divmod(scroll_x, 8)
        first_y, fine_y = divmod(scroll_y, 8)
        base_nt_x, base_nt_y = base_nt & 1, (base_nt >> 1) & 1
        palette = self.palette
        # translate tables map decoded colour 0-3 straight to NES palette entries;
        # colour 0 is always the universal backdrop.
        tables = []
        for palette_id in range(4):
            table = bytearray(256)
            table[0] = palette[0]
            for colour in (1, 2, 3): table[colour] = palette[palette_id * 4 + colour]
            tables.append(bytes(table))
        read_vram, tile_rows, frame = self._read_vram, self._tile_rows, self.frame
        crop_start = 8 + fine_x
        events = self._render_chr_events
        # Common case: one CHR bank for the whole frame -> choose it once.
        single = len(events) == 1 and events[0][0] <= 0
        if single and events[0][1] is not self._chr_rom:
            self._select_chr(events[0][1])
        translated: dict[int, dict] = {}   # per CHR bank: (address, palette_id) -> 8 coloured rows
        for screen_tile_y in range(-1, 31):
            top = screen_tile_y * 8 - fine_y
            if top + 8 <= y_start or top >= y_end:
                continue
            tile_y = first_y + screen_tile_y
            nt_y = (base_nt_y + tile_y // 30) & 1
            row_in_table = tile_y % 30
            attr_row = 0x3C0 + (row_in_table // 4) * 8
            attr_shift_y = ((row_in_table % 4) // 2) * 2
            cells = []
            for screen_tile_x in range(-1, 33):
                tile_x = first_x + screen_tile_x
                table_base = 0x2000 + (nt_y * 2 + ((base_nt_x + tile_x // 32) & 1)) * 0x400
                column = tile_x % 32
                tile = read_vram(table_base + row_in_table * 32 + column)
                attribute = read_vram(table_base + attr_row + column // 4)
                palette_id = (attribute >> ((attr_shift_y + ((column % 4) // 2)) * 2)) & 3
                cells.append((pattern_base + tile * 16, palette_id))
            row_rows = None   # decoded+coloured rows for this tile row's CHR bank
            for row in range(8):
                sy = top + row
                if sy < y_start or sy >= y_end:
                    continue
                if not single:
                    for event_line, event_chr in reversed(events):
                        if event_line <= sy:
                            if event_chr is not self._chr_rom:
                                self._select_chr(event_chr)
                                row_rows = None
                            break
                if row_rows is None:
                    cache = translated.setdefault(id(self._chr_rom), {})
                    row_rows = []
                    for address, palette_id in cells:
                        key = (address << 2) | palette_id
                        rows = cache.get(key)
                        if rows is None:
                            table = tables[palette_id]
                            rows = cache[key] = tuple([r.translate(table) for r in tile_rows(address)])
                        row_rows.append(rows)
                line = b"".join([rows[row] for rows in row_rows])
                frame[sy * 256:(sy + 1) * 256] = line[crop_start:crop_start + 256]

    def _base_nametable(self) -> int:
        """Nametable selected for the top-left of the screen (t register bits 10-11)."""
        return (self.temp_address >> 10) & 3

    def _nametable_index(self, address: int) -> int:
        relative = (address - 0x2000) & 0x0FFF; table = relative // 0x400; offset = relative & 0x3FF
        physical = table & 1 if self.mirroring == "vertical" else (0 if table in (0, 1) else 1)
        return physical * 0x400 + offset

    def render_sprites(self) -> None:
        oam, frame, palette = self.oam, self.frame, self.palette
        tall = bool(self.control & 0x20)
        height = 16 if tall else 8
        pattern_8x8 = 0x1000 if self.control & 0x08 else 0
        universal = palette[0]
        events = self._render_chr_events
        single = len(events) == 1 and events[0][0] <= 0
        if single and events[0][1] is not self._chr_rom:
            self._select_chr(events[0][1])
        for sprite in range(63, -1, -1):
            base = sprite * 4
            y = oam[base]
            if y >= 0xEF:
                continue
            tile, attributes, x = oam[base + 1], oam[base + 2], oam[base + 3]
            flip_h, flip_v, behind = attributes & 0x40, attributes & 0x80, attributes & 0x20
            sprite_palette = 0x10 + (attributes & 3) * 4
            for row in range(height):
                screen_y = y + 1 + row
                if screen_y >= 240:
                    break
                if not single:
                    for event_line, event_chr in reversed(events):
                        if event_line <= screen_y:
                            if event_chr is not self._chr_rom:
                                self._select_chr(event_chr)
                            break
                source_row = (height - 1 - row) if flip_v else row
                if tall:
                    address = ((tile & 1) * 0x1000) + (tile & 0xFE) * 16 + (source_row // 8) * 16
                    pixels = self._tile_rows(address)[source_row & 7]
                else:
                    pixels = self._tile_rows(pattern_8x8 + tile * 16)[source_row]
                if not any(pixels):
                    continue
                if flip_h:
                    pixels = pixels[::-1]
                line = screen_y * 256
                for col in range(8):
                    colour = pixels[col]
                    if colour == 0:
                        continue
                    screen_x = x + col
                    if screen_x >= 256:
                        break
                    if behind and frame[line + screen_x] != universal:
                        continue
                    frame[line + screen_x] = palette[sprite_palette + colour]

    def _bg_colour_index(self, x: int, y: int) -> int:
        """2-bit background pixel value at screen (x, y) using current scroll."""
        pattern_base = 0x1000 if self.control & 0x10 else 0
        wx = x + self.scroll_x
        wy = y + self.scroll_y
        base_nt = self._base_nametable()
        nt_x = ((base_nt & 1) + wx // 256) & 1
        nt_y = (((base_nt >> 1) & 1) + wy // 240) & 1
        wx %= 256
        wy %= 240
        nt_addr = 0x2000 + (nt_y * 2 + nt_x) * 0x400
        tile = self._read_vram(nt_addr + (wy // 8) * 32 + wx // 8)
        row, bit = wy & 7, 7 - (wx & 7)
        low = self.chr_rom[(pattern_base + tile * 16 + row) % len(self.chr_rom)]
        high = self.chr_rom[(pattern_base + tile * 16 + row + 8) % len(self.chr_rom)]
        return (((high >> bit) & 1) << 1) | ((low >> bit) & 1)

    def _find_sprite_zero_hit(self) -> tuple[int, int] | None:
        """Return (scanline, dot) of the first sprite-0/background overlap."""
        if self.mask & 0x18 != 0x18:
            return None
        y, tile, attributes, x = self.oam[0:4]
        height = 16 if self.control & 0x20 else 8
        flip_h, flip_v = bool(attributes & 0x40), bool(attributes & 0x80)
        for row in range(height):
            screen_y = y + 1 + row
            if screen_y >= 240:
                break
            source_row = (height - 1 - row) if flip_v else row
            if height == 16:
                index = (tile & 1) * 0x100 + (tile & 0xFE) + (source_row // 8)
                source_row &= 7
            else:
                index = (0x100 if self.control & 0x08 else 0) + tile
            low = self.chr_rom[(index * 16 + source_row) % len(self.chr_rom)]
            high = self.chr_rom[(index * 16 + source_row + 8) % len(self.chr_rom)]
            for col in range(8):
                bit = col if flip_h else 7 - col
                if not (((high >> bit) & 1) | ((low >> bit) & 1)):
                    continue
                screen_x = x + col
                if screen_x >= 255:
                    continue
                if screen_x < 8 and self.mask & 0x06 != 0x06:
                    continue
                if self._bg_colour_index(screen_x, screen_y):
                    return (screen_y, screen_x + 1)
        return None

    def save_ppm(self, path: str | Path) -> None:
        with Path(path).open("wb") as output:
            output.write(b"P6\n256 240\n255\n")
            output.write(self.rgb_bytes())

    def rgb_bytes(self) -> bytes:
        """Frame as packed 24-bit RGB (256x240x3), via byte translate tables."""
        rgb = bytearray(256 * 240 * 3)
        rgb[0::3] = self.frame.translate(_RGB_R)
        rgb[1::3] = self.frame.translate(_RGB_G)
        rgb[2::3] = self.frame.translate(_RGB_B)
        return bytes(rgb)

    def png_bytes(self, level: int = 1) -> bytes:
        rgb = self.rgb_bytes()
        raw = b"".join([b"\0" + rgb[y * 768:(y + 1) * 768] for y in range(240)])
        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 240, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, level)) + chunk(b"IEND", b""))

    def save_png(self, path: str | Path) -> None:
        Path(path).write_bytes(self.png_bytes())