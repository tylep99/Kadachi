import unittest
from nes_emulator import Bus, Rom


class MirroringTests(unittest.TestCase):
    def test_vertical_mirroring(self):
        bus = Bus(Rom(bytes(0x4000), bytes(0x2000), 0, "vertical"))
        bus.ppu_device._write_vram(0x2000, 0x12); self.assertEqual(bus.ppu_device._read_vram(0x2800), 0x12)
        bus.ppu_device._write_vram(0x2400, 0x34); self.assertEqual(bus.ppu_device._read_vram(0x2C00), 0x34)

    def test_horizontal_mirroring(self):
        bus = Bus(Rom(bytes(0x4000), bytes(0x2000), 0, "horizontal"))
        bus.ppu_device._write_vram(0x2000, 0x12); self.assertEqual(bus.ppu_device._read_vram(0x2400), 0x12)
        bus.ppu_device._write_vram(0x2800, 0x34); self.assertEqual(bus.ppu_device._read_vram(0x2C00), 0x34)
