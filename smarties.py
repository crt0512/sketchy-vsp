#!/usr/bin/env python3
"""
smarties.py: TUI that snapshots and compares SMART data of the sketchy Hitachi VSP drives.

sgfmt.py and sdmagic.py torture the drives for hours and reset parts of the SMART data,
to get a better understanding of how the drive is doing before and after whats basically
a full disk write check this tool was made.

  - Finds drives whose model string follows the VSP scheme (DKS2E-H4R0SS, ...)
  - Runs smartctl -a on each one and shows the bits that matter: power-on hours, grown
    defects, uncorrected read/write/verify errors, non medium errors, health status
  - The first time it sees a drive it saves a baseline snapshot (never overwritten).
    Press s whenever you want another one (e.g. after formatting / unlocking).

Everything lives next to this script in smarties-data/ :
  smarties-data/<SERIAL>/<YYYY-MM-DD_HH-MM-SS>.txt      raw smartctl -a output, oldest = baseline
  smarties-data/summary.txt                             one table row per saved snapshot (greppable)

Needs root and smartmontools (smartctl).

Run:   python3 smarties.py
"""
import curses
import locale
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

VSP_RE = re.compile(r"^DK[A-Z][25][A-Z]-")
HERE = os.path.dirname(os.path.realpath(__file__))
DATA_DIR = os.path.join(HERE, "smarties-data")
SUMMARY_FILE = os.path.join(DATA_DIR, "summary.txt")
TS_FMT = "%Y-%m-%d_%H-%M-%S"
SMARTCTL_TIMEOUT = 90
WORKERS = 8 # smartctl calls run in parallel, it's slow-ish per drive
REQUIRED = ["smartctl"]

SUMMARY_HDR = (f"{'DEV':<5} {'MODEL':<13} {'SERIAL':<20} {'HOURS':>6} {'GROWN':>5} "
               f"{'RD_UE':>5} {'WR_UE':>5} {'VF_UE':>5} {'NONMED':>6} HEALTH")

# counters compared against the baseline: key, label, severity when the number goes UP
COUNTERS = (("grown", "grown", "bad"), ("rd_ue", "rd_ue", "bad"), ("wr_ue", "wr_ue", "bad"),
            ("vf_ue", "vf_ue", "bad"), ("nonmed", "nonmed", "warn"))
SEV = {"ok": 0, "warn": 1, "bad": 2}

# rows of the detail screen: label, record key
DETAIL_ROWS = (("Health", "health"), ("Power-on hours", "hours"), ("Grown defects", "grown"),
               ("Read uncorrected", "rd_ue"), ("Write uncorrected", "wr_ue"),
               ("Verify uncorrected", "vf_ue"), ("Non-medium errors", "nonmed"),
               ("Read errors corrected", "rd_corr"), ("Write errors corrected", "wr_corr"),
               ("Verify errors corrected", "vf_corr"), ("GB read", "rd_gb"), ("GB written", "wr_gb"),
               ("Temperature (C)", "temp"), ("Start-stop cycles", "start_stop"),
               ("Load-unload cycles", "load_unload"), ("Sector size", "block_len"),
               ("Protection", "prot"), ("Firmware", "rev"))
BAD_UP = {"grown", "rd_ue", "wr_ue", "vf_ue"}
WARN_UP = {"nonmed"}


# helpers
def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except OSError as e:
        return -2, str(e)


def read_sys(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def stamp(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def short_path(p):
    rel = os.path.relpath(p, HERE)
    return p if rel.startswith("..") else rel


def fmt_val(v):
    if v is None or v == "":
        return "?"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


# smartctl parsing
STR_FIELDS = {
    "vendor": r"^Vendor:\s+(.+?)\s*$",
    "model": r"^Product:\s+(.+?)\s*$",
    "rev": r"^Revision:\s+(.+?)\s*$",
    "serial": r"^Serial number:\s+(\S+)",
    "capacity": r"^User Capacity:.*\[(.+?)\]",
    "health": r"^SMART Health Status:\s*(.+?)\s*$",
}
INT_FIELDS = {
    "hours": r"hours:minutes\s+(\d+):\d+",
    "grown": r"grown defect list:\s*(\d+)",
    "nonmed": r"Non-medium error count:\s*(\d+)",
    "temp": r"Current Drive Temperature:\s*(\d+)",
    "start_stop": r"Accumulated start-stop cycles:\s*(\d+)",
    "load_unload": r"Accumulated load-unload cycles:\s*(\d+)",
    "block_len": r"Logical block size:\s*(\d+)",
}
ERR_ROWS = {"rd": "read", "wr": "write", "vf": "verify"}


def num(s, kind=int):
    try:
        return kind(s)
    except (TypeError, ValueError):
        return None


def parse_smart(text):
    """Pick the interesting bits out of smartctl -a output. Numbers that aren't there are None."""
    r = {}
    for k, pat in STR_FIELDS.items():
        m = re.search(pat, text, re.M)
        r[k] = m.group(1) if m else ""
    for k, pat in INT_FIELDS.items():
        m = re.search(pat, text, re.M)
        r[k] = int(m.group(1)) if m else None
    m = re.search(r"Formatted with type (\d) protection", text)
    r["prot"] = f"type {m.group(1)}" if m else ("off" if r["block_len"] else "")
    for k, label in ERR_ROWS.items():
        # error counter log columns:
        # ECC fast | ECC delayed | rereads | total corrected | algorithm invocations | GB | uncorrected
        m = re.search(rf"^{label}:\s+(.*)$", text, re.M)
        cols = m.group(1).split() if m else []
        if len(cols) < 7:
            cols = [None] * 7
        r[k + "_corr"] = num(cols[3])
        r[k + "_gb"] = num(cols[5], float)
        r[k + "_ue"] = num(cols[6])
    return r


def complete(r):
    """Good enough to be a baseline? (a drive mid-format answers with serial only)"""
    return bool(r and r["serial"] and r["health"] and r["hours"] is not None)


# snapshot storage
def safe_name(serial):
    return re.sub(r"[^A-Za-z0-9._-]", "_", serial) or "unknown"


def serial_dir(serial):
    return os.path.join(DATA_DIR, safe_name(serial))


def snapshots(serial):
    """Sorted [(epoch, path)] of the saved snapshots of one drive. First one = baseline."""
    out = []
    d = serial_dir(serial)
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in sorted(names):
        try:
            ts = time.mktime(time.strptime(n, TS_FMT + ".txt"))
        except ValueError:
            continue
        out.append((ts, os.path.join(d, n)))
    return out


def load_snapshot(snap):
    ts, path = snap
    try:
        with open(path, errors="replace") as f:
            r = parse_smart(f.read())
    except OSError:
        r = parse_smart("")
    r["when"] = ts
    return r


def save_snapshot(serial, text, when=None):
    """Write raw smartctl output as a new timestamped snapshot. Never overwrites. -> path"""
    when = time.time() if when is None else when
    d = serial_dir(serial)
    os.makedirs(d, exist_ok=True)
    while True:
        path = os.path.join(d, time.strftime(TS_FMT, time.localtime(when)) + ".txt")
        if not os.path.exists(path):
            break
        when += 1
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    os.utime(path, (when, when))
    return path


def summary_row(dev, r):
    v = lambda k: "?" if r.get(k) is None else str(r[k])
    return (f"{dev:<5} {r['model']:<13} {r['serial']:<20} {v('hours'):>6} {v('grown'):>5} "
            f"{v('rd_ue'):>5} {v('wr_ue'):>5} {v('vf_ue'):>5} {v('nonmed'):>6} {r['health']}")


def append_summary(rows, when=None):
    """rows = [(dev, record)] saved together -> one '# batch' block in summary.txt"""
    when = time.time() if when is None else when
    os.makedirs(DATA_DIR, exist_ok=True)
    fresh = not os.path.exists(SUMMARY_FILE) or os.path.getsize(SUMMARY_FILE) == 0
    with open(SUMMARY_FILE, "a") as f:
        if fresh:
            f.write(SUMMARY_HDR + "\n")
        f.write(f"# batch {stamp(when)}\n")
        for dev, r in rows:
            f.write(summary_row(dev, r) + "\n")


def compare(base, now):
    """Counters now vs baseline -> (level ok/warn/bad, text)."""
    level, parts = "ok", []
    bh, nh = base.get("health") or "?", now.get("health") or "?"
    if bh != nh:
        parts.append(f"health {bh} -> {nh}")
        level = "bad" if nh != "OK" else "warn"
    for key, label, sev in COUNTERS:
        b, n = base.get(key), now.get(key)
        if b is None or n is None or b == n:
            continue
        if key == "grown" and n < b:
            label = "shrunk" # a format drops the old grown list (sg_format CMPLST=1)
        parts.append(f"{label} {n - b:+d}")
        s = sev if n > b else "warn" # other counters going DOWN = wrong baseline / swapped drive
        level = s if SEV[s] > SEV[level] else level
    return level, ", ".join(parts) or "no change"


def since(base, now):
    s = f"since {stamp(base['when'])}"
    if base.get("hours") is not None and now.get("hours") is not None:
        s += f", +{now['hours'] - base['hours']}h"
    return s


def change(key, b, n):
    """One cell of the CHANGE column -> (text, level or None)."""
    if b is None or n is None:
        return ("-" if b == n else "?"), None
    if isinstance(b, (int, float)) and isinstance(n, (int, float)):
        diff = n - b
        if diff == 0:
            return "-", None
        txt = f"{diff:+.1f}" if isinstance(diff, float) else f"{diff:+d}"
        if key in BAD_UP:
            return txt, "bad" if diff > 0 else "warn"
        if key in WARN_UP:
            return txt, "warn"
        return txt, "dim"
    if b == n:
        return "-", None
    if key == "health":
        return "changed", "bad" if n != "OK" else "warn"
    return "changed", "warn"


# drives
def find_drives():
    """-> ([(sg, model)] of VSP drives, number of other SCSI disks)"""
    vsp, others = [], 0
    try:
        names = os.listdir("/sys/class/scsi_generic")
    except OSError:
        names = []
    for sg in sorted(names, key=lambda s: int(re.sub(r"\D", "", s) or 0)):
        base = f"/sys/class/scsi_generic/{sg}/device"
        if read_sys(base + "/type") != "0":
            continue
        model = read_sys(base + "/model")
        if VSP_RE.match(model):
            vsp.append((sg, model))
        else:
            others += 1
    return vsp, others


class Disk:
    def __init__(self, sg, model):
        self.sg, self.model = sg, model
        try:
            self.block = sorted(os.listdir(f"/sys/class/scsi_generic/{sg}/device/block"))[0]
        except (OSError, IndexError):
            self.block = ""
        self.dev = "/dev/" + (self.block or sg)
        self.text, self.rec, self.err = "", None, ""
        self.snaps, self.base, self.latest = [], None, None
        self.save_err = ""
        self.fresh = False          # baseline got saved by this run
        self.selected = False

    @property
    def name(self):
        return self.block or self.sg

    @property
    def serial(self):
        return self.rec["serial"] if self.rec else ""

    def read_smart(self):
        rc, out = run(["smartctl", "-a", self.dev], timeout=SMARTCTL_TIMEOUT)
        self.text, self.rec = out, parse_smart(out)
        if self.rec["serial"]:
            self.err = ""
        else:
            self.rec = None
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            bad = [l for l in lines if re.search(r"fail|error|unable|not support|no such", l, re.I)]
            self.err = (bad[:1] or lines[-1:] or [f"smartctl exited {rc}"])[0]
        return self

    def load_snapshots(self):
        self.snaps = snapshots(self.serial) if self.serial else []
        self.base = load_snapshot(self.snaps[0]) if self.snaps else None
        self.latest = load_snapshot(self.snaps[-1]) if len(self.snaps) > 1 else None

    def status(self):
        """-> (level, text) for the 'vs BASELINE' column"""
        if self.rec is None:
            return "bad", f"smartctl: {self.err}"
        if self.fresh:
            return "ok", "baseline saved just now"
        if self.base is None:
            if self.save_err:
                return "warn", f"baseline not saved: {self.save_err}"
            if not complete(self.rec):
                return "warn", "SMART data incomplete (formatting?), no baseline saved"
            return "warn", "no baseline"
        return compare(self.base, self.rec)


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
        self.disks, self.others, self.absent = [], 0, 0
        self.cursor, self.top = 0, 0
        self.screen = "list"
        self.msg, self.msg_until, self.msg_attr = "", 0, 0
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
        self.C = {"ok": G | curses.A_BOLD, "warn": Y | curses.A_BOLD,
                  "bad": R | curses.A_BOLD, "dim": C}

    def flash(self, msg, attr=None, secs=5):
        self.msg, self.msg_until = msg, time.time() + secs
        self.msg_attr = self.C["warn"] if attr is None else attr

    def splash(self, text):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        put(self.scr, h // 2, max(0, (w - len(text)) // 2), text, curses.A_BOLD)
        self.scr.refresh()

    def confirm(self, text):
        h, w = self.scr.getmaxyx()
        put(self.scr, h - 2, 0, " " * (w - 1))
        put(self.scr, h - 2, 1, text + " [y/N] ", self.C["warn"])
        self.scr.refresh()
        self.scr.timeout(-1)
        k = self.scr.getch()
        self.scr.timeout(500)
        return k in (ord("y"), ord("Y"))

    # data
    def read_all(self, disks, what):
        self.splash(f"Reading SMART from {len(disks)} drive(s)... ({what})")
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(Disk.read_smart, disks))

    def scan(self):
        keep = {d.sg: d for d in self.disks}
        specs, self.others = find_drives()
        found = []
        for sg, model in specs:
            d = keep.get(sg)
            if d is None or d.model != model:
                d = Disk(sg, model)
            d.fresh = False
            found.append(d)
        self.read_all(found, "scan")
        now, rows = time.time(), []
        for d in found:
            d.load_snapshots()
            if d.rec and not d.snaps and complete(d.rec): # first sighting -> baseline
                try:
                    save_snapshot(d.serial, d.text, now)
                    d.load_snapshots()
                    d.fresh, d.save_err = True, ""
                    rows.append((d.name, d.rec))
                except OSError as e:
                    d.save_err = str(e)
        if rows:
            try:
                append_summary(rows, now)
            except OSError as e:
                self.flash(f"summary.txt: {e}", self.C["bad"], 10)
            else:
                self.flash(f"Saved baseline for {len(rows)} new drive(s).", self.C["ok"], 8)
        present = {safe_name(d.serial) for d in found if d.serial}
        try:
            self.absent = sum(1 for n in os.listdir(DATA_DIR)
                              if os.path.isdir(os.path.join(DATA_DIR, n)) and n not in present)
        except OSError:
            self.absent = 0
        self.disks = found
        self.cursor = min(self.cursor, max(0, len(found) - 1))

    def snapshot(self, sel):
        if not sel:
            self.flash("No drives to snapshot.")
            return
        if not self.confirm(f"Save a SMART snapshot of {len(sel)} drive(s) now?"):
            self.flash("Cancelled.")
            return
        self.read_all(sel, "snapshot")
        now, rows, failed = time.time(), [], []
        for d in sel:
            d.selected = False
            if d.rec is None:
                failed.append(d.name)
                continue
            if not complete(d.rec):
                failed.append(f"{d.name} (incomplete SMART data)")
                continue
            try:
                save_snapshot(d.serial, d.text, now)
                d.save_err = ""
                rows.append((d.name, d.rec))
            except OSError as e:
                d.save_err = str(e)
                failed.append(d.name)
            d.load_snapshots()
            d.fresh = len(d.snaps) == 1
        if rows:
            try:
                append_summary(rows, now)
            except OSError as e:
                failed.append(f"summary.txt ({e})")
        msg = f"Saved {len(rows)} snapshot(s) -> {short_path(SUMMARY_FILE)}"
        if failed:
            msg += "   failed: " + ", ".join(failed)
        self.flash(msg, self.C["ok"] if not failed else self.C["warn"], 10)

    # screens
    def draw_list(self):
        s, C = self.scr, self.C
        s.erase()
        h, w = s.getmaxyx()
        put(s, 0, 0, " smarties: keepin track of your sketchy Hitachi VSP drives SMARTIE data :^) ".ljust(w),
            curses.A_REVERSE)
        put(s, 1, 1, "Counters are live from smartctl. Last column = change since the oldest "
                     "saved snapshot (the baseline).", C["dim"])
        put(s, 2, 1, f"Data: {DATA_DIR}", C["dim"])
        hdr = (f"{'':4} {'DEV':<6}{'MODEL':<14}{'SERIAL':<21}{'HOURS':>6} {'GROWN':>5} {'RD_UE':>5} "
               f"{'WR_UE':>5} {'VF_UE':>5} {'NONMED':>6}  {'HEALTH':<7} vs BASELINE")
        put(s, 3, 0, hdr, curses.A_BOLD)
        first, nrows = 4, max(1, h - 8)
        n = len(self.disks)
        if not n:
            put(s, 5, 2, "Crap! No drives with a VSP model string (DKxxx-...) found. r = rescan", C["warn"])
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + nrows:
            self.top = self.cursor - nrows + 1
        end = min(n, self.top + nrows)
        if n > nrows:
            put(s, 3, max(len(hdr) + 2, w - 16), f"[{self.top + 1}-{end} of {n}]", C["dim"])
        for i in range(self.top, end):
            d, y = self.disks[i], first + i - self.top
            r = d.rec
            v = lambda k: "?" if r is None or r.get(k) is None else str(r[k])
            mark = "[x]" if d.selected else "[ ]"
            model = (r["model"] if r and r["model"] else d.model)[:13]
            serial = (r["serial"] if r else "?")[:20]
            line = (f" {mark} {d.name:<6}{model:<14}{serial:<21}{v('hours'):>6} {v('grown'):>5} "
                    f"{v('rd_ue'):>5} {v('wr_ue'):>5} {v('vf_ue'):>5} {v('nonmed'):>6}  ")
            attr = curses.A_REVERSE if i == self.cursor else 0
            put(s, y, 0, line, attr)
            x = len(line)
            health = (r["health"] or "?") if r else "?"
            hattr = C["ok"] if health == "OK" else (C["bad"] if r else 0)
            put(s, y, x, f"{health[:7]:<7} ", hattr | attr)
            x += 8
            level, txt = d.status()
            put(s, y, x, txt, C[level] | attr)
            if d.base and r and not d.fresh:
                x += len(txt) + 2
                put(s, y, x, since(d.base, r), C["dim"] | attr)
        # summary
        st = [(d.status()[0], d) for d in self.disks]
        same = sum(1 for lv, d in st if lv == "ok" and not d.fresh)
        fresh = sum(1 for d in self.disks if d.fresh)
        changed = sum(1 for lv, d in st if lv == "warn")
        worse = sum(1 for lv, d in st if lv == "bad" and d.rec)
        unread = sum(1 for d in self.disks if d.rec is None)
        summary = (f"{n} VSP drive(s): {same} unchanged, {changed} changed, {worse} worse, "
                   f"{fresh} new baseline(s), {unread} unreadable   ({self.others} other disk(s) "
                   f"ignored, {self.absent} absent drive(s) have snapshots)")
        put(s, h - 3, 1, summary, C["dim"])
        if time.time() < self.msg_until:
            put(s, h - 2, 1, self.msg, self.msg_attr)
        else:
            nsel = sum(d.selected for d in self.disks)
            if nsel:
                put(s, h - 2, 1, f"{nsel} selected", C["warn"])
        keys = ("up/down move, SPACE select, a all, ENTER details, s snapshot selected/all, "
                "r rescan, q quit")
        put(s, h - 1, 0, (" " + keys).ljust(w), curses.A_REVERSE)

    def draw_detail(self):
        s, C = self.scr, self.C
        s.erase()
        h, w = s.getmaxyx()
        d = self.disks[self.cursor]
        r = d.rec
        put(s, 0, 0, f" smarties: {d.name}  {r['model'] if r else d.model}  {d.serial or '?'}  "
                     f"({self.cursor + 1}/{len(self.disks)}) ".ljust(w), curses.A_REVERSE)
        if r:
            put(s, 1, 1, f"{r['vendor']} {r['model']} fw {r['rev']}   {r['capacity'] or '?'}, "
                         f"{fmt_val(r['block_len'])} B sectors, protection {r['prot'] or '?'}",
                C["dim"])
            if r["health"] and r["health"] != "OK":
                put(s, 2, 1, f"Health: {r['health']}", C["bad"])
        else:
            put(s, 1, 1, f"smartctl failed: {d.err}", C["bad"])
        cols = []
        if d.base:
            cols.append(("BASELINE", stamp(d.base["when"]), d.base))
        if d.latest:
            cols.append(("LAST SNAPSHOT", stamp(d.latest["when"]), d.latest))
        if r:
            cols.append(("NOW (live)", stamp(time.time()), r))
        lw, cw = 26, 22
        diff = bool(d.base and r)
        put(s, 3, 1, " " * lw + "".join(f"{c[0]:<{cw}}" for c in cols) + ("CHANGE" if diff else ""),
            curses.A_BOLD)
        put(s, 4, 1, " " * lw + "".join(f"{c[1]:<{cw}}" for c in cols), C["dim"])
        y = 5
        for label, key in DETAIL_ROWS:
            if y >= h - 5:
                put(s, y, 1, "... make pp window grow", C["dim"])
                y += 1
                break
            put(s, y, 1, label)
            x = 1 + lw
            for _, _, rec in cols:
                val = fmt_val(rec.get(key))
                attr = 0
                if key == "health" and val != "?":
                    attr = C["ok"] if val == "OK" else C["bad"]
                put(s, y, x, val[:cw - 1], attr)
                x += cw
            if diff:
                txt, lv = change(key, d.base.get(key), r.get(key))
                put(s, y, x, txt, C[lv] if lv else 0)
            y += 1
        y += 1
        if d.snaps:
            names = [stamp(ts) + (" (baseline)" if i == 0 else "") for i, (ts, _) in enumerate(d.snaps)]
            put(s, y, 1, f"Snapshots ({len(d.snaps)}): " + ", ".join(names))
            put(s, y + 1, 1, f"raw smartctl output: {serial_dir(d.serial)}/", C["dim"])
        else:
            put(s, y, 1, "No snapshots saved for this drive.", C["warn"])
        if time.time() < self.msg_until:
            put(s, h - 2, 1, self.msg, self.msg_attr)
        keys = "up/down other drive, s snapshot this drive, b/ESC back to list, q quit"
        put(s, h - 1, 0, (" " + keys).ljust(w), curses.A_REVERSE)

    # ---- keys
    def handle(self, k):
        if k in (ord("q"), ord("Q")):
            return False
        if k == curses.KEY_RESIZE:
            return True
        n = len(self.disks)
        if self.screen == "detail":
            if k in (ord("b"), ord("B"), 27, curses.KEY_LEFT, ord("h")):
                self.screen = "list"
            elif k in (curses.KEY_UP, ord("k"), ord("p")):
                self.cursor = max(0, self.cursor - 1)
            elif k in (curses.KEY_DOWN, ord("j"), ord("n")):
                self.cursor = min(n - 1, self.cursor + 1)
            elif k in (ord("s"), ord("S")):
                self.snapshot([self.disks[self.cursor]])
            return True
        if k in (ord("r"), ord("R")):
            self.scan()
            return True
        if not n:
            return True
        d = self.disks[self.cursor]
        if k in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(n - 1, self.cursor + 1)
        elif k == ord(" "):
            d.selected = not d.selected
        elif k in (ord("a"), ord("A")):
            want = not all(x.selected for x in self.disks)
            for x in self.disks:
                x.selected = want
        elif k in (10, 13, curses.KEY_ENTER, ord("d"), ord("D"), curses.KEY_RIGHT, ord("l")):
            self.screen = "detail"
        elif k in (ord("s"), ord("S")):
            self.snapshot([x for x in self.disks if x.selected] or list(self.disks))
        return True

    def loop(self):
        self.scan()
        while True:
            if self.screen == "detail" and not self.disks:
                self.screen = "list"
            (self.draw_list if self.screen == "list" else self.draw_detail)()
            self.scr.refresh()
            k = self.scr.getch()
            if k != -1 and not self.handle(k):
                break


def main():
    if os.geteuid() != 0:
        sys.exit("Run as root, smartctl wants it.")
    missing = [t for t in REQUIRED if not shutil.which(t)]
    if missing:
        sys.exit(f"Missing tools: {', '.join(missing)}  ->  apt install smartmontools")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError as e:
        sys.exit(f"Cannot create {DATA_DIR}: {e}")
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: App(scr).loop())


if __name__ == "__main__":
    main()
