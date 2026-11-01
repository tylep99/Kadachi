import unittest
from nes_emulator import Bus, Rom


class Mapper66Tests(unittest.TestCase):
    def test_prg_and_chr_banks_switch(self):
        prg = b''.join(bytes([bank]) * 0x8000 for bank in range(4))
        chr_rom = b''.join(bytes([bank + 10]) * 0x2000 for bank in range(4))
        bus = Bus(Rom(prg, chr_rom, 66, "horizontal"))
        self.assertEqual(bus.read(0x8000), 0)
        bus.write(0x8000, 0x21)
        self.assertEqual(bus.read(0x8000), 2)
        self.assertEqual(bus.ppu_device.chr_rom[0], 11)
