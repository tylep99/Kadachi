import unittest
from nes_emulator import Bus, CPU, Rom


class FrameExecutionTests(unittest.TestCase):
    def test_cpu_can_populate_ppu_vram_before_render(self):
        # LDA #$20; STA $2006; LDA #$00; STA $2006; LDA #$01; STA $2007; JMP $8000
        program = bytes([0xA9, 0x20, 0x8D, 0x06, 0x20, 0xA9, 0x00, 0x8D, 0x06, 0x20,
                         0xA9, 0x01, 0x8D, 0x07, 0x20, 0x4C, 0x00, 0x80])
        prg = bytearray(0x4000); prg[:len(program)] = program
        prg[0x3FFC:0x4000] = bytes([0x00, 0x80, 0x00, 0x80])
        cpu = CPU(Bus(Rom(bytes(prg), bytes(0x2000), 0, "horizontal"))); cpu.reset()
        cpu.run_cycles(60)
        self.assertEqual(cpu.bus.ppu_device.vram[0], 1)
