#!/usr/bin/env python3
"""
sdmagic.py - a silly TUI to unlock drives yoinksed from Hitachi VSP systems.

Drives from Hitachi VSP systems run firmware that only accepts WRITE AND VERIFY,
they reject normal SCSI WRITEs (Will throw "Hardware Error", ASC 0x81) even
after being reformatted to 512/4096 byte sectors. Setting the least significant bit
of byte 0x14 in the undocumented mode page 0x38 lifts that lock.

  - Finds drives whose model string follows the VSP scheme (e.g. DKS2E-H4R0SS)
  - Shows sector size, lock bit (current + saved value) and status
  - Unlocks selected drives with: sdparm --page=0x38 --set=0x14:0:1=1 --save
    (sdparm is used because sg_wr_mode --contents is broken in sg3-utils 1.48)
  - Verifies with a non destructive write test: reads the first and last sector
    then writes the same data back with O_DIRECT.

Drives must be reformatted first (512 or 4096 bytes, protection off)
Use sgfmt.py, makes the whole thing ez. 

Needs root, sg3-utils, sdparm, util-linux.

Run:   python3 sdmagic.py
"""
import curses
import locale
import mmap
import os
import re
import shutil
import subprocess
import sys
import time

VSP_RE = re.compile(r"^DK[A-Z][25][A-Z]-[HJK][0-9R]{3}SS$")
LOCK_PAGE = 0x38
LOCK_BYTE = 0x14
GOOD_BLOCKS = (512, 4096)
HOLDER_FS = {"zfs_member", "LVM2_member", "linux_raid_member", "crypto_LUKS", "swap"}
REQUIRED = ["sg_modes", "sg_readcap", "sdparm", "lsblk"]


# ---------------------------------------------------------------- helpers
def run(cmd, timeout=30, binary=False):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, text=not binary)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, b"" if binary else "", "timeout"
    except OSError as e:
        return -2, b"" if binary else "", str(e)


def read_sys(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def fmt_size(b):
    if not b:
        return "?"
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if b >= div:
            return f"{b / div:.2f} {unit}"
    return f"{b} B"


def parse_mode_sense10(data, page):
    """Return the mode page bytes from a raw MODE SENSE(10) response, or None."""
    if len(data) < 8:
        return None
    total = min(len(data), ((data[0] << 8) | data[1]) + 2)
    off = 8 + ((data[6] << 8) | data[7])
    while off + 2 <= total:
        code = data[off] & 0x3F
        spf = data[off] & 0x40
        if spf:
            if off + 4 > total:
                return None
            plen = ((data[off + 2] << 8) | data[off + 3]) + 4
        else:
            plen = data[off + 1] + 2
        if off + plen > total:
            return None
        if code == page:
            return data[off:off + plen]
        off += plen
    return None


def read_lock(dev, control=0):
    """Lock bit from mode page 0x38: 1/0, or None if the page doesn't exist. control 0 = current values, 2 = saved values."""
    rc, out, _ = run(["sg_modes", f"--page=0x{LOCK_PAGE:x}", f"--control={control}",
                      "--raw", dev], binary=True)
    if rc != 0 or not out:
        return None
    pg = parse_mode_sense10(out, LOCK_PAGE)
    if pg is None or len(pg) <= LOCK_BYTE:
        return None
    return pg[LOCK_BYTE] & 0x01


def parse_readcap(text):
    m = re.search(r"Logical block length=(\d+)", text)
    if not m:
        return None
    info = {"block_len": int(m.group(1)), "prot": 0, "blocks": None}
    m = re.search(r"prot_en=(\d)", text)
    if m:
        info["prot"] = int(m.group(1))
    m = re.search(r"Number of logical blocks=(\d+)", text)
    if m:
        info["blocks"] = int(m.group(1))
    return info


def write_test(blockdev, block_len, blocks):
    """Read the first and last block, write the same bytes back (O_DIRECT). Returns (ok, message)."""
    path = "/dev/" + blockdev
    try:  # make the kernel pick up the current capacity (avoids 0B devices)
        with open(f"/sys/block/{blockdev}/device/rescan", "w") as f:
            f.write("1")
        time.sleep(0.5)
    except OSError:
        pass
    buf = mmap.mmap(-1, max(block_len, mmap.PAGESIZE))   # page aligned for O_DIRECT
    mv = memoryview(buf)[:block_len]
    fd = None
    try:
        fd = os.open(path, os.O_RDWR | os.O_DIRECT | os.O_SYNC)
        for label, lba in (("first", 0), ("last", blocks - 1)):
            off = lba * block_len
            try:
                n = os.preadv(fd, [mv], off)
                if n != block_len:
                    return False, f"short read on {label} sector ({n} bytes)"
            except OSError as e:
                return False, f"read of {label} sector failed: {e.strerror}"
            try:
                n = os.pwritev(fd, [mv], off)
                if n != block_len:
                    return False, f"short write on {label} sector ({n} bytes)"
            except OSError as e:
                return False, f"write to {label} sector failed: {e.strerror}"
        return True, "first + last sector rewritten OK"
    except OSError as e:
        return False, f"cannot open {path}: {e.strerror}"
    finally:
        if fd is not None:
            os.close(fd)
        mv.release()
        buf.close()


# disks
class Disk:
    def __init__(self, sg):
        self.sg = sg
        self.dev = "/dev/" + sg
        base = f"/sys/class/scsi_generic/{sg}/device"
        self.vendor = read_sys(base + "/vendor")
        self.model = read_sys(base + "/model")
        try:
            self.block = sorted(os.listdir(base + "/block"))[0]
        except (OSError, IndexError):
            self.block = ""
        self.block_len = None
        self.prot = 0
        self.blocks = None
        self.cur = None
        self.saved = None
        self.in_use = ""
        self.status = "unknown"
        self.note = ""
        self.test = None # None -> (ok, msg)
        self.selected = False

    @property
    def size(self):
        return self.block_len * self.blocks if self.block_len and self.blocks else None

    @property
    def actionable(self):
        return self.status in ("locked", "unlocked", "no lock page")

    def refresh(self):
        rc, out, err = run(["sg_readcap", "-l", self.dev])
        info = parse_readcap(out) if rc == 0 else None
        if info:
            self.block_len, self.prot, self.blocks = info["block_len"], info["prot"], info["blocks"]
        else:
            self.block_len = None
        self.cur = read_lock(self.dev, 0)
        self.saved = read_lock(self.dev, 2)
        self.check_in_use()
        if self.block_len not in GOOD_BLOCKS or self.prot:
            self.status = "format first"
        elif self.cur is None:
            self.status = "no lock page"
        elif self.cur == 1:
            self.status = "unlocked"
        else:
            self.status = "locked"
        if not self.actionable:
            self.selected = False

    def check_in_use(self):
        self.in_use = ""
        if not self.block:
            return
        _, out, _ = run(["lsblk", "-nro", "NAME,FSTYPE,MOUNTPOINT", "/dev/" + self.block])
        for line in out.splitlines():
            name, fstype, mnt = (line.split(" ") + ["", "", ""])[:3]
            if mnt:
                self.in_use = f"{name} mounted on {mnt}"
                return
            if fstype in HOLDER_FS:
                self.in_use = f"{name} is {fstype}"
                return

    def unlock(self):
        rc, out, err = run(["sdparm", f"--page=0x{LOCK_PAGE:x}",
                            f"--set=0x{LOCK_BYTE:x}:0:1=1", "--save", self.dev])
        if rc != 0:
            lines = [l.strip() for l in (err + out).splitlines() if l.strip()]
            return False, lines[-1] if lines else f"sdparm exited {rc}"
        self.cur = read_lock(self.dev, 0)
        self.saved = read_lock(self.dev, 2)
        if self.cur != 1:
            return False, "lock bit did not change"
        self.status = "unlocked"
        if self.saved == 1:
            return True, "unlocked"
        if self.saved is None:
            return True, "unlocked (saved value unreadable - confirm with a power cycle)"
        return True, "unlocked (saved copy still 0 - retry, then power-cycle to confirm)"

    def run_test(self):
        if self.in_use:
            self.test = (False, f"skipped - in use ({self.in_use})")
        elif not self.block or not self.blocks:
            self.test = (False, "no block device / capacity")
        else:
            self.test = write_test(self.block, self.block_len, self.blocks)
        return self.test


# TUI
def put(win, y, x, s, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    s = s[: max(0, w - x - (1 if y == h - 1 else 0))]
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass


class App:
    def __init__(self, scr):
        self.scr = scr
        self.disks = []
        self.others = 0
        self.cursor = 0
        self.msg, self.msg_until, self.msg_attr = "", 0, 0
        self.msg_faint = ""
        curses.curs_set(0)
        scr.timeout(500)
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW,
                               curses.COLOR_RED, curses.COLOR_CYAN), start=1):
            curses.init_pair(i, c, bg)
        G, Y, R, C = (curses.color_pair(i) for i in range(1, 5))
        self.C = {"okay": G | curses.A_BOLD, "ok": G, "warn": Y | curses.A_BOLD,
                  "fucking failed": R | curses.A_BOLD, "dim": C}
        self.status_attr = {"unlocked": G | curses.A_BOLD, "no lock page": G,
                            "locked": R | curses.A_BOLD, "format first": Y | curses.A_BOLD,
                            "bussy": Y | curses.A_BOLD}

    def flash(self, msg, attr=None, secs=5):
        self.msg, self.msg_until = msg, time.time() + secs
        self.msg_faint = ""
        self.msg_attr = self.C["warn"] if attr is None else attr

    def splash(self, text):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        put(self.scr, h // 2, max(0, (w - len(text)) // 2), text, curses.A_BOLD)
        self.scr.refresh()

    def scan(self):
        keep = {d.sg: d for d in self.disks}
        found, others = [], 0
        try:
            names = os.listdir("/sys/class/scsi_generic")
        except OSError:
            names = []
        for sg in sorted(names, key=lambda s: int(re.sub(r"\D", "", s) or 0)):
            if read_sys(f"/sys/class/scsi_generic/{sg}/device/type") != "0":
                continue
            model = read_sys(f"/sys/class/scsi_generic/{sg}/device/model")
            if not VSP_RE.match(model):
                others += 1
                continue
            d = keep.get(sg)
            if d is None or d.model != model:
                d = Disk(sg)
            d.refresh()
            found.append(d)
        self.disks, self.others = found, others
        self.cursor = min(self.cursor, max(0, len(found) - 1))

    def draw(self):
        s, C = self.scr, self.C
        s.erase()
        h, w = s.getmaxyx()
        put(s, 0, 0, " sdmagic: Unlocks da Hitachi VSP drives!!! ".ljust(w), curses.A_REVERSE)
        put(s, 1, 1, "Lock bit = mode page 0x38, byte 0x14, bit 0.  CUR = active now, "
                     "SAVED = after power cycle.", C["dim"])
        hdr = (f"{'':4} {'SG':<6}{'DEV':<6}{'MODEL':<14}{'SIZE':>10}  {'SECTOR':>6}  "
               f"{'CUR':>3} {'SAVED':>5}  STATUS")
        put(s, 3, 0, hdr, curses.A_BOLD)
        if not self.disks:
            put(s, 5, 2, "Crap! No drives with a VSP model string (DKxxx-xxxxSS) found.", C["warn"])
        for i, d in enumerate(self.disks):
            y = 4 + i
            if y >= h - 4:
                put(s, y, 2, f"… {len(self.disks) - i} more, make pp window grow", C["dim"])
                break
            mark = "[x]" if d.selected else ("[ ]" if d.actionable else "   ")
            sector = str(d.block_len) if d.block_len else "?"
            bit = lambda v: "-" if v is None else str(v)
            line = (f" {mark} {d.sg:<6}{d.block or '-':<6}{d.model:<14}{fmt_size(d.size):>10}  "
                    f"{sector:>6}  {bit(d.cur):>3} {bit(d.saved):>5}  ")
            attr = curses.A_REVERSE if i == self.cursor else 0
            put(s, y, 0, line, attr)
            x = len(line)
            st = d.status
            if st == "format first":
                st += " (run sgfmt.py duh)"
            put(s, y, x, st, self.status_attr.get(d.status, 0) | attr)
            x += len(st) + 2
            if d.note:
                put(s, y, x, d.note, C["dim"] | attr)
                x += len(d.note) + 2
            if d.test is not None:
                ok, tmsg = d.test
                put(s, y, x, (":) write test: " if ok else ":( write test: ") + tmsg,
                    (C["okay"] if ok else C["fucking failed"]) | attr)
            elif d.in_use:
                put(s, y, x, f"in use: {d.in_use}", C["warn"] | attr)
        # summary
        n = len(self.disks)
        cnt = lambda st: sum(d.status == st for d in self.disks)
        passed = sum(1 for d in self.disks if d.test and d.test[0])
        summary = (f"{n} VSP drive(s): {cnt('unlocked')} unlocked, {cnt('locked')} locked, "
                   f"{cnt('format first')} plz format, {passed} passed write test"
                   f"   ({self.others} other disk(s) ignored)")
        put(s, h - 3, 1, summary, C["dim"])
        if time.time() < self.msg_until:
            put(s, h - 2, 1, self.msg, self.msg_attr)
            if self.msg_faint:
                put(s, h - 2, 1 + len(self.msg), self.msg_faint, self.C["dim"])
        else:
            nsel = sum(d.selected for d in self.disks)
            if nsel:
                put(s, h - 2, 1, f"{nsel} selected", C["warn"])
        keys = ("up/down = move duh, SPACE = select a disk, a = select all, ENTER = rape+test, "
                "t test only  r rescan  q quit")
        put(s, h - 1, 0, (" " + keys).ljust(w), curses.A_REVERSE)
        s.refresh()

    def confirm(self, text):
        h, w = self.scr.getmaxyx()
        put(self.scr, h - 2, 0, " " * (w - 1))
        put(self.scr, h - 2, 1, text + " [y/N] ", self.C["warn"])
        self.scr.refresh()
        self.scr.timeout(-1)
        k = self.scr.getch()
        self.scr.timeout(500)
        return k in (ord("y"), ord("Y"))

    def process(self, sel, do_unlock):
        done_ok = failed = 0
        for d in sel:
            d.note, d.test = "", None
            if do_unlock and d.status == "locked":
                d.status = "bussy"
                d.note = "unlocking..."
                self.draw()
                ok, msg = d.unlock()
                if not ok:
                    d.status, d.note = "locked", "unlock failed: " + msg
                    failed += 1
                    self.draw()
                    continue
                d.note = msg if "(" in msg else ""
            prev = d.status
            d.status, d.note = "bussy", (d.note + "  testing...").strip()
            self.draw()
            ok, _ = d.run_test()
            d.status = prev
            d.note = d.note.replace("testing...", "").strip()
            d.selected = False
            done_ok += ok
            failed += not ok
            self.draw()
        attr = self.C["okay"] if not failed else self.C["fucking failed"]
        self.flash(f"Done: {done_ok} drive(s) passed, {failed} failed.", attr, secs=15)
        if done_ok and do_unlock:
            self.msg_faint = "  (powercycle drive to verify)"

    def handle(self, k):
        if k in (ord("q"), ord("Q")):
            return False
        if not self.disks:
            if k in (ord("r"), ord("R")):
                self.splash("Rescanning...")
                self.scan()
            return True
        d = self.disks[self.cursor]
        if k in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(len(self.disks) - 1, self.cursor + 1)
        elif k == ord(" "):
            if d.actionable:
                d.selected = not d.selected
            else:
                self.flash(f"{d.sg}: {d.status} - reformat to 512/4096 first.")
        elif k in (ord("a"), ord("A")):
            cand = [x for x in self.disks if x.actionable]
            want = not all(x.selected for x in cand)
            for x in cand:
                x.selected = want
        elif k in (ord("r"), ord("R")):
            self.splash("Rescanning...")
            self.scan()
        elif k in (10, 13, curses.KEY_ENTER, ord("t"), ord("T")):
            do_unlock = k not in (ord("t"), ord("T"))
            sel = [x for x in self.disks if x.selected and x.actionable] or \
                  ([d] if d.actionable else [])
            if not sel:
                self.flash("Nothing to do. Select drives with SPACE or aaaaaaa.")
                return True
            n_lock = sum(x.status == "locked" for x in sel) if do_unlock else 0
            what = (f"Unlock {n_lock} and write-test {len(sel)} drive(s)?" if n_lock
                    else f"Write-test {len(sel)} drive(s)? (rewrites first/last sector with its own data)")
            if self.confirm(what):
                self.process(sel, do_unlock)
            else:
                self.flash("Cancelled.")
        return True

    def loop(self):
        self.splash("Scanning drives...")
        self.scan()
        while True:
            self.draw()
            k = self.scr.getch()
            if k != -1 and not self.handle(k):
                break


def main():
    if os.geteuid() != 0:
        sys.exit("Run as root you doofus!")
    missing = [t for t in REQUIRED if not shutil.which(t)]
    if missing:
        sys.exit(f"Missing tools: {', '.join(missing)}  ->  apt install sg3-utils sdparm util-linux")
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(lambda scr: App(scr).loop())


if __name__ == "__main__":
    main()