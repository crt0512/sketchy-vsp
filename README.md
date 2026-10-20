# Sketchy VSP

Scripts to reformat and unlock hard disks from a Hitachi VSP storage array found dumped in the woods with all its disks still inside.

<img src="assets/README-1.jpg" alt="Sketchy VSP" width="50%">

## Why?

Disks pulled from a Hitachi VSP systems are not usable in your everyday server usually :(

This is because of two reasons :

- **Odd sector sizes:** They are formatted with 520/528 byte sectors and often have T10 protection information (DIF) enabled, so they show up as unreadable or corrupted.
- **Write lock:** Hitachi wants to feel extra fucking special and make life for us all hell hence why these drives firmware only accepts `WRITE AND VERIFY`, rejecting the normal SCSI writes with `Hardware Error` (ASC `0x81`), even after a reformat to 512 byte sectors.

## What's in here

| Script                   | What it does                                                                                                                                                 |
|--------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [sgfmt.py](sgfmt.py)     | TUI that lists SCSI disks with their block size and protection state, then low level formats the selected ones to 512 byte sectors with protection off       |
| [sdmagic.py](sdmagic.py) | TUI that finds VSP drives, shows if their locked, then clears selected ones write lock by setting bit 0 of byte `0x14` in mode page `0x38` (using `sdparm`). |

## Requirements

- Linux, root access, Python 3.7+
- `sg3-utils` and `util-linux` (`lsblk`) for both scripts
- `sdparm` for `sdmagic.py`

## Usage

Run them in order:

1. `sudo python3 sgfmt.py` reformats the drives to 512 byte sectors
2. `sudo python3 sdmagic.py` tries tp remove the VSP write restriction and tests if it worked.

Notes:

- **Formatting erases everything** (on the selected disks) make sure you dont format your boot drive or something on accident lol
- Formats run inside the drive firmware and can take from hours up to a day in some cases
  - They survive quitting the script or a dead tmux session
  - Start `sgfmt.py` again and it picks up running formats, including ones you started by hand
- Formats are logged to `/var/tmp/sgfmt-sgN.log`, and the script keeps its state in `/var/tmp/sgfmt-state.json`
- Drives must be reformatted (512 or 4096 bytes, protection off) before `sdmagic.py` will unlock them
