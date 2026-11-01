# NES Emulator

A small, dependency-free NES emulator written in Python 3. It includes a
table-driven 6502 CPU, NES memory bus, PPU timing and software rendering,
controller input, and a Tkinter window frontend.

## Current capabilities

- iNES ROM loading and validation
- Mapper 0 (NROM) cartridges
- Mapper 66 (GxROM) PRG and CHR bank switching
- 6502 reset, interrupts, stack operations, branches, addressing modes, and
  official instructions
- Compatibility support for commonly encountered unofficial 6502 opcodes
- CPU RAM mirroring and NES PPU register mapping
- PPU nametable, attribute-table, palette, sprite, scrolling, and sprite-zero
  timing support
- OAM DMA through `$4014`
- Controller polling through `$4016` and `$4017`
- Interactive Tkinter display with keyboard controls
- PNG and PPM frame output

Basic APU audio (two pulse channels and a triangle-style channel) with PCM playback on Windows. Cartridge mappers other than 0 and 66,
trainers, battery-backed save RAM, and advanced PPU edge cases are not fully
supported.

## Run the emulator

Open a ROM in the interactive window:

```text
python nes_emulator.py path\to\game.nes --window
```

The window is resizable and keeps the NES 4:3 aspect ratio.

## Inspect or step a ROM

Print ROM information and execute a fixed number of CPU instructions:

```text
python nes_emulator.py path\to\game.nes --steps 1000
```

## Render frames

Execute frames and save the rendered output:

```text
python nes_emulator.py path\to\game.nes --frames 1 --render frame.png
```

Use a `.ppm` extension for PPM output. Add `--dump-vram` for PPU diagnostics.

## Controls

| NES button | Key |
| --- | --- |
| A | `Z` |
| B | `X` |
| Select | `A` |
| Start | `S` |
| Up / Down / Left / Right | Arrow keys |

## Project layout

- `nes_emulator.py` — command-line entry point, CPU core, and window frontend
- `nes/rom.py` — iNES ROM loading and validation
- `nes/bus.py` — CPU address bus, RAM, controllers, DMA, and mapper logic
- `nes/ppu.py` — PPU registers, timing, scrolling, sprites, and rendering
- `tests/` — unit tests for CPU, PPU, controllers, mappers, rendering, and
  frame execution

## Tests

```text
python -m unittest discover -s tests -v
```

The project requires Python 3 and Tkinter for the interactive window.
