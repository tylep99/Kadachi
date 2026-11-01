import unittest
from nes_emulator import Bus


class PPUTests(unittest.TestCase):
    def test_vram_address_and_data_registers(self):
        bus = Bus()
        bus.write(0x2006, 0x20)
        bus.write(0x2006, 0x00)
        bus.write(0x2007, 0x37)
        self.assertEqual(bus.ppu_device.vram[0], 0x37)

    def test_oam_registers_increment(self):
        bus = Bus()
        bus.write(0x2003, 0x10)
        bus.write(0x2004, 0xAA)
        bus.write(0x2004, 0xBB)
        self.assertEqual(bus.ppu_device.oam[0x10:0x12], bytes([0xAA, 0xBB]))

    def test_frame_buffer_pattern(self):
        bus = Bus(); bus.ppu_device.render_test_pattern()
        self.assertEqual(len(bus.ppu_device.frame), 256 * 240)
        self.assertNotEqual(bus.ppu_device.frame[0], bus.ppu_device.frame[16])
