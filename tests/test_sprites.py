import unittest
from nes_emulator import Bus, Rom


class SpriteTests(unittest.TestCase):
    def test_sprite_is_rendered_from_oam(self):
        chr_rom = bytearray(0x2000)
        chr_rom[0:8] = bytes([0x80] * 8)  # leftmost pixel of tile 0
        bus = Bus(Rom(bytes(0x4000), bytes(chr_rom), 0, "horizontal"))
        bus.ppu_device.palette[0] = 0
        bus.ppu_device.palette[0x11] = 0x2A
        bus.ppu_device.oam[0:4] = bytes([9, 0, 0, 20])
        bus.ppu_device.render_sprites()
        self.assertEqual(bus.ppu_device.frame[10 * 256 + 20], 0x2A)
