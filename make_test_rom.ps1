$rom = [byte[]]::new(16 + 0x4000 + 0x2000)

$header = [byte[]](0x4E,0x45,0x53,0x1A,1,1,0,0,0,0,0,0,0,0,0,0)
[Array]::Copy($header, 0, $rom, 0, $header.Length)

# LDA #$05, ADC #$03, STA $0002, JMP $8000
$program = [byte[]](0xA9,0x05,0x69,0x03,0x8D,0x02,0x00,0x4C,0x00,0x80)
[Array]::Copy($program, 0, $rom, 16, $program.Length)

# Reset vector points to $8000
$rom[16 + 0x3FFC] = 0x00
$rom[16 + 0x3FFD] = 0x80

[IO.File]::WriteAllBytes((Join-Path $PSScriptRoot "test.nes"), $rom)
Write-Host "Created $PSScriptRoot\test.nes"