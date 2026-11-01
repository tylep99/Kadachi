import unittest

from nes_emulator import Bus


class ControllerTests(unittest.TestCase):
    def test_controller_latch_and_serial_order(self):
        bus = Bus()
        # Start is the fourth NES controller bit: A, B, Select, Start.
        bus.set_controller(0, 0x08)
        bus.write(0x4016, 1)
        self.assertEqual(bus.read(0x4016), 0)
        bus.write(0x4016, 0)
        self.assertEqual([bus.read(0x4016) for _ in range(3)], [0, 0, 1])

    def test_strobe_reports_a_until_released(self):
        bus = Bus()
        bus.set_controller(0, 0x01)
        bus.write(0x4016, 1)
        self.assertEqual([bus.read(0x4016) for _ in range(4)], [1, 1, 1, 1])


if __name__ == "__main__":
    unittest.main()
