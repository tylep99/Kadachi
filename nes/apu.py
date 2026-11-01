"""Small deterministic NES APU model producing signed 16-bit PCM."""
from __future__ import annotations

import array

CPU_HZ = 1_789_773
SAMPLE_RATE = 22_050


class APU:
    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.registers = bytearray(0x18)
        self.enabled = [False] * 5
        self.phase = [0.0] * 3
        self.noise_phase = 0.0
        self.noise = 1
        self.sample_clock = 0.0
        self._samples: list[int] = []

    def read(self, address: int) -> int:
        if address == 0x4015:
            return sum((1 << i) for i, on in enumerate(self.enabled) if on)
        return self.registers[address - 0x4000] if 0x4000 <= address < 0x4018 else 0

    def write(self, address: int, value: int) -> None:
        value &= 0xff
        if address == 0x4015:
            self.enabled = [bool(value & (1 << i)) for i in range(5)]
            self.registers[address - 0x4000] = value
        elif 0x4000 <= address <= 0x4017:
            self.registers[address - 0x4000] = value
            if address in (0x4003, 0x4007, 0x400B, 0x400F):
                channel = (address - 0x4000) // 4
                if channel < len(self.phase):
                    self.phase[channel] = 0.0

    def clock(self, cpu_cycles: int) -> None:
        self.sample_clock += cpu_cycles * self.sample_rate / CPU_HZ
        count = int(self.sample_clock)
        self.sample_clock -= count
        for _ in range(count):
            self._samples.append(self._mix_sample())

    def take_samples(self) -> bytes:
        values = array.array("h", self._samples)
        self._samples.clear()
        return values.tobytes()

    def _mix_sample(self) -> int:
        out = 0.0
        for channel in (0, 1):
            base = channel * 4
            timer = self.registers[base + 2] | ((self.registers[base + 3] & 7) << 8)
            if self.enabled[channel] and timer:
                freq = CPU_HZ / (16 * (timer + 1))
                self.phase[channel] = (self.phase[channel] + freq / self.sample_rate) % 1.0
                duty = (self.registers[base] >> 6) & 3
                out += (1.0 if self.phase[channel] < (0.125, 0.25, 0.5, 0.75)[duty] else -1.0) * ((self.registers[base] & 15) / 15)
        base = 8
        timer = self.registers[base + 2] | ((self.registers[base + 3] & 7) << 8)
        if self.enabled[2] and timer:
            freq = CPU_HZ / (32 * (timer + 1))
            self.phase[2] = (self.phase[2] + freq / self.sample_rate) % 1.0
            step = int(self.phase[2] * 32)
            triangle = (step if step < 16 else 31 - step) / 15.0
            out += (triangle * 2 - 1) * 0.75
        base = 12
        period_table = (4, 8, 16, 32, 64, 96, 128, 160, 202, 254, 380, 508, 762, 1016, 2034, 4068)
        timer = period_table[self.registers[base + 2] & 15]
        if self.enabled[3] and timer:
            freq = CPU_HZ / timer
            phase = (self.noise_phase + freq / self.sample_rate) % 1.0
            if phase < self.noise_phase:
                tap = 6 if self.registers[base + 2] & 0x80 else 1
                self.noise = (self.noise >> 1) | (((self.noise ^ (self.noise >> tap)) & 1) << 14)
            self.noise_phase = phase
            out += (1.0 if self.noise & 1 else -1.0) * ((self.registers[base] & 15) / 15) * 0.5
        return max(-32767, min(32767, int(out * 5000)))
