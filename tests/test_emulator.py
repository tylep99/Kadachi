import unittest
from nes_emulator import Bus, CPU, Rom


class EmulatorTests(unittest.TestCase):
    def test_ram_mirroring(self):
        bus = Bus(); bus.write(0x0002, 0xAB)
        self.assertEqual(bus.read(0x0802), 0xAB)
        self.assertEqual(bus.read(0x1802), 0xAB)

    def test_nrom_prg_mirroring(self):
        rom = Rom(bytes([0x42]) * 0x4000, bytes(0x2000), 0, "horizontal")
        bus = Bus(rom)
        self.assertEqual(bus.read(0x8000), 0x42)
        self.assertEqual(bus.read(0xC000), 0x42)

    def test_cpu_reset_and_basic_program(self):
        rom_data = bytearray(0x4000)
        rom_data[:8] = bytes([0xA9, 0x05, 0x69, 0x03, 0x8D, 0x02, 0x00, 0xEA])
        rom_data[0x3FFC:0x4000] = bytes([0x00, 0x80, 0x00, 0x80])
        cpu = CPU(Bus(Rom(bytes(rom_data), bytes(0x2000), 0, "horizontal"))); cpu.reset()
        for _ in range(3): cpu.step()
        self.assertEqual(cpu.a, 8); self.assertEqual(cpu.bus.read(2), 8)


if __name__ == "__main__": unittest.main()
