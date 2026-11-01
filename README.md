# NES Emulator Starter

This is a small, dependency-free NES emulator foundation written in Python 3.

The project is split into focused modules under `nes/`:

- `rom.py` loads and validates iNES cartridges
- `ppu.py` handles PPU registers, timing, and software rendering
- `bus.py` handles CPU memory mapping and cartridge banks
- `nes_emulator.py` is the single entry point and keeps the original public imports working
It currently includes:

- iNES/NES 2.0 header parsing and mapper-0 (NROM) PRG loading
- 6502 CPU registers, flags, stack, reset, and interrupt handling
- NES CPU memory mirroring and controller stubs
- a command-line ROM inspection and CPU stepping tool

It intentionally does not yet render graphics or play audio. The next major
milestone is a PPU implementation, followed by controller input and a frontend.

## Run

```text
python nes_emulator.py path\to\game.nes --steps 1000
```

Render the first nametable as a 256x240 PPM image:

```text
python nes_emulator.py path\to\game.nes --render frame.ppm
```

The renderer decodes CHR-ROM tiles, nametable tile IDs, attribute-table
palettes, and palette RAM. Open the resulting `frame.ppm` with Paint or any
image viewer that supports PPM.

Run the tests with:

```text
python -m unittest discover -s tests -v
```

Mapper 0 and mapper 66 (GxROM) are supported. Mapper 66 is used by some
commercial cartridges and switches PRG and CHR banks through writes in the
cartridge address range.
