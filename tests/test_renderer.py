import tempfile
import unittest
from pathlib import Path
from nes_emulator import Bus, Rom


class RendererTests(unittest.TestCase):
    def test_renders_chr_tile_to_ppm(self):
        chr_rom = bytearray(0x2000)
        chr_rom[0:8] = bytes([0xFF] * 8)  # tile 0: colour 1 across every pixel
        rom = Rom(bytes(0x4000), bytes(chr_rom), 0, "horizontal")
        bus = Bus(rom)
        bus.ppu_device.palette[1] = 0x21
        bus.ppu_device.vram[0] = 0
        bus.ppu_device.render_background()
        self.assertEqual(bus.ppu_device.frame[0], 0x21)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "frame.ppm"
            bus.ppu_device.save_ppm(output)
            self.assertTrue(output.read_bytes().startswith(b"P6\n256 240\n255\n"))
