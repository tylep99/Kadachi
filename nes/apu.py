"""Small deterministic NES APU model producing signed 16-bit PCM.

Implements pulse x2 (envelope, sweep, length), triangle (linear + length),
noise (envelope, length) and the frame counter. DMC is not implemented.

Performance: the APU is advanced lazily. The CPU only calls sync() once per
run_cycles() and the APU also syncs itself before register reads/writes, so
samples are generated in batches whose channel parameters are constant.
"""
from __future__ import annotations

import array

CPU_HZ = 1_789_773
SAMPLE_RATE = 22_050

LENGTHS = (10, 254, 20, 2, 40, 4, 80, 6, 160, 8, 60, 10, 14, 12, 26, 14,
           12, 16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30)
DUTY = ((0, 1, 0, 0, 0, 0, 0, 0),
        (0, 1, 1, 0, 0, 0, 0, 0),
        (0, 1, 1, 1, 1, 0, 0, 0),
        (1, 0, 0, 1, 1, 1, 1, 1))
TRIANGLE = tuple(list(range(15, -1, -1)) + list(range(16)))
NOISE_PERIODS = (4, 8, 16, 32, 64, 96, 128, 160, 202, 254, 380, 508, 762, 1016, 2034, 4068)

_PULSE_GAIN = 0.00752 * 40000
_TRI_GAIN = 0.00851 * 40000
_NOISE_GAIN = 0.00494 * 40000
_TRI_SCALED = tuple(v * _TRI_GAIN for v in TRIANGLE)

# (cpu cycle, also clocks half-frame units). Quarter-frame units clock every step.
FRAME_STEPS = (
    ((3729, False), (7457, True), (11186, False), (14915, True)),   # 4-step
    ((3729, False), (7457, True), (11186, False), (18641, True)),   # 5-step
)


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

        # Lazy-sync support: now_fn returns the CPU's current cycle count.
        self.now_fn = None
        self._synced = 0

        # Length counters: pulse1, pulse2, triangle, noise.
        self.length = [0, 0, 0, 0]
        # Envelopes (indexed by channel; only 0, 1 and 3 are used).
        self.env_start = [False] * 4
        self.env_div = [0] * 4
        self.decay = [0] * 4
        # Pulse timers and sweep units (pulse1, pulse2).
        self.timer = [0, 0]
        self.sweep_div = [0, 0]
        self.sweep_reload = [False, False]
        # Triangle.
        self.linear = 0
        self.linear_reload = False
        self.tri_out = 0
        # Frame counter.
        self.five_step = False
        self.frame_cycles = 0
        self.frame_index = 0
        # DC-blocking high-pass state.
        self._hp_x = 0.0
        self._hp_y = 0.0

    # ------------------------------------------------------------------
    # Catch-up
    # ------------------------------------------------------------------
    def sync(self, now: int) -> None:
        """Advance the APU to CPU cycle count `now`."""
        delta = now - self._synced
        if delta > 0:
            self._synced = now
            self._advance(delta)

    def _sync_now(self) -> None:
        if self.now_fn is not None:
            self.sync(self.now_fn())

    def clock(self, cpu_cycles: int) -> None:
        """Advance by a cycle count directly. Don't mix with sync()/now_fn."""
        self._advance(cpu_cycles)

    # ------------------------------------------------------------------
    # Registers
    # ------------------------------------------------------------------
    def read(self, address: int) -> int:
        if address == 0x4015:
            self._sync_now()
            return sum((1 << i) for i in range(4) if self.length[i] > 0)
        return self.registers[address - 0x4000] if 0x4000 <= address < 0x4018 else 0

    def write(self, address: int, value: int) -> None:
        value &= 0xFF
        if not 0x4000 <= address <= 0x4017:
            return
        self._sync_now()  # settle everything before this register changes
        self.registers[address - 0x4000] = value
        if address == 0x4015:
            self.enabled = [bool(value & (1 << i)) for i in range(5)]
            for ch in range(4):
                if not self.enabled[ch]:
                    self.length[ch] = 0
        elif address in (0x4001, 0x4005):
            self.sweep_reload[(address - 0x4000) // 4] = True
        elif address in (0x4002, 0x4006):
            ch = (address - 0x4000) // 4
            self.timer[ch] = (self.timer[ch] & 0x700) | value
        elif address in (0x4003, 0x4007):
            ch = (address - 0x4000) // 4
            self.timer[ch] = (self.timer[ch] & 0xFF) | ((value & 7) << 8)
            if self.enabled[ch]:
                self.length[ch] = LENGTHS[value >> 3]
            self.env_start[ch] = True
            self.phase[ch] = 0.0
        elif address == 0x400B:
            if self.enabled[2]:
                self.length[2] = LENGTHS[value >> 3]
            self.linear_reload = True
        elif address == 0x400F:
            if self.enabled[3]:
                self.length[3] = LENGTHS[value >> 3]
            self.env_start[3] = True
        elif address == 0x4017:
            self.five_step = bool(value & 0x80)
            self.frame_cycles = 0
            self.frame_index = 0
            if self.five_step:
                self._quarter()
                self._half()

    # ------------------------------------------------------------------
    # Frame counter and its units
    # ------------------------------------------------------------------
    def _advance(self, cycles: int) -> None:
        """Run `cycles` CPU cycles, splitting at frame-counter steps."""
        while cycles > 0:
            steps = FRAME_STEPS[self.five_step]
            at, half = steps[self.frame_index]
            until = at - self.frame_cycles
            if until > cycles:
                self.frame_cycles += cycles
                self._generate(cycles)
                return
            if until > 0:
                self._generate(until)
                cycles -= until
            self.frame_cycles = at
            self._quarter()
            if half:
                self._half()
            self.frame_index += 1
            if self.frame_index == len(steps):
                self.frame_cycles -= steps[-1][0]
                self.frame_index = 0

    def _quarter(self) -> None:
        regs = self.registers
        for ch in (0, 1, 3):
            reg = regs[ch * 4]
            period = reg & 15
            if self.env_start[ch]:
                self.env_start[ch] = False
                self.decay[ch] = 15
                self.env_div[ch] = period
            elif self.env_div[ch] == 0:
                self.env_div[ch] = period
                if self.decay[ch] > 0:
                    self.decay[ch] -= 1
                elif reg & 0x20:
                    self.decay[ch] = 15
            else:
                self.env_div[ch] -= 1
        if self.linear_reload:
            self.linear = regs[8] & 0x7F
        elif self.linear:
            self.linear -= 1
        if not regs[8] & 0x80:
            self.linear_reload = False

    def _half(self) -> None:
        regs = self.registers
        for ch, halt_mask in ((0, 0x20), (1, 0x20), (2, 0x80), (3, 0x20)):
            if self.length[ch] and not regs[ch * 4] & halt_mask:
                self.length[ch] -= 1
        for ch in (0, 1):
            reg = regs[ch * 4 + 1]
            target = self._sweep_target(ch)
            if (self.sweep_div[ch] == 0 and reg & 0x80 and reg & 7
                    and self.timer[ch] >= 8 and 0 <= target <= 0x7FF):
                self.timer[ch] = target
            if self.sweep_div[ch] == 0 or self.sweep_reload[ch]:
                self.sweep_div[ch] = (reg >> 4) & 7
                self.sweep_reload[ch] = False
            else:
                self.sweep_div[ch] -= 1

    def _sweep_target(self, ch: int) -> int:
        reg = self.registers[ch * 4 + 1]
        timer = self.timer[ch]
        change = timer >> (reg & 7)
        if reg & 8:
            # Pulse 1 uses one's complement, pulse 2 two's complement.
            change = -change - (1 if ch == 0 else 0)
        return timer + change

    def _volume(self, ch: int) -> int:
        reg = self.registers[ch * 4]
        return reg & 15 if reg & 0x10 else self.decay[ch]

    # ------------------------------------------------------------------
    # Sample generation (batched; all channel state is constant per batch)
    # ------------------------------------------------------------------
    def _generate(self, cycles: int) -> None:
        self.sample_clock += cycles * self.sample_rate / CPU_HZ
        n = int(self.sample_clock)
        if n:
            self.sample_clock -= n
            self._render(n)

    def take_samples(self) -> bytes:
        self._sync_now()
        values = array.array("h", self._samples)
        self._samples.clear()
        return values.tobytes()

    def _render(self, n: int) -> None:
        regs, rate, length = self.registers, self.sample_rate, self.length
        mix = None  # allocated only if some channel is audible

        # Pulse channels.
        for ch in (0, 1):
            if not length[ch]:
                continue
            timer = self.timer[ch]
            if timer < 8 or self._sweep_target(ch) > 0x7FF:
                continue
            vol = self._volume(ch)
            if not vol:
                continue
            if mix is None:
                mix = [0.0] * n
            inc = CPU_HZ / (16 * (timer + 1)) / rate
            duty = DUTY[regs[ch * 4] >> 6]
            amp = vol * _PULSE_GAIN
            phase = self.phase[ch]
            for i in range(n):
                phase += inc
                if phase >= 1.0:
                    phase -= 1.0
                if duty[int(phase * 8)]:
                    mix[i] += amp
            self.phase[ch] = phase

        # Triangle: when halted or ultrasonic, hold the last output level.
        const = self.tri_out * _TRI_GAIN
        if length[2] and self.linear:
            timer = regs[10] | ((regs[11] & 7) << 8)
            if timer >= 2:
                if mix is None:
                    mix = [0.0] * n
                inc = CPU_HZ / (32 * (timer + 1)) / rate
                tri = _TRI_SCALED
                phase = self.phase[2]
                for i in range(n):
                    phase += inc
                    if phase >= 1.0:
                        phase -= 1.0
                    mix[i] += tri[int(phase * 32)]
                self.phase[2] = phase
                self.tri_out = TRIANGLE[int(phase * 32)]
                const = 0.0

        # Noise.
        if length[3]:
            vol = self._volume(3)
            if vol:
                if mix is None:
                    mix = [0.0] * n
                inc = CPU_HZ / NOISE_PERIODS[regs[14] & 15] / rate
                tap = 6 if regs[14] & 0x80 else 1
                amp = vol * _NOISE_GAIN
                acc, lfsr = self.noise_phase, self.noise
                for i in range(n):
                    acc += inc
                    s = int(acc)
                    if s:
                        acc -= s
                        for _ in range(s):
                            lfsr = (lfsr >> 1) | (((lfsr ^ (lfsr >> tap)) & 1) << 14)
                    if not lfsr & 1:
                        mix[i] += amp
                self.noise_phase, self.noise = acc, lfsr

        # Fully silent fast path.
        if mix is None and const == 0.0 and self._hp_x == 0.0 and abs(self._hp_y) < 1.0:
            self._hp_y = 0.0
            self._samples.extend([0] * n)
            return

        if mix is None:
            mix = [0.0] * n
        out = self._samples
        px, py = self._hp_x, self._hp_y
        for i in range(n):
            x = mix[i] + const
            py = x - px + 0.995 * py   # DC-blocking high-pass
            px = x
            v = int(py)
            out.append(32767 if v > 32767 else -32767 if v < -32767 else v)
        self._hp_x, self._hp_y = px, py