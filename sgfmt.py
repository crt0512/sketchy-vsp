#!/usr/bin/env python3
"""
sgfmt.py - curses TUI to low-level reformat SAS/SCSI disks to 512-byte sectors.

  - Lists every SCSI disk with its logical block size and T10-PI (DIF) state.
  - Lets you pick the odd ones (520/528-byte sectors, protection enabled, unreadable / "medium format corrupted") and runs
    sg_format --format --size=512 --fmtpinfo=0 on them, detached from this script.
  - Shows a progress bar, rate and ETA per drive by asking the drives directly.
  - Restart-safe: the format runs inside the drive firmware, so if you quit or tmux dies, just start the script again. 
    It detects formats that are already running (including ones started by hand) and jumps straight back to the progress view.

Needs: root, sg3-utils, util-linux (lsblk), Python 3.7+.

Run:   python3 sgfmt.py

Keys:  See in TUI, im not explaining this shit here
"""
import curses
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

TARGET_BLOCK = 512
POLL_SECS = 10 # how often running formats are polled
STATE_FILE = "/var/tmp/sgfmt-state.json"
LOG_DIR = "/var/tmp" # sg_format output: /var/tmp/sgfmt-sgN.log
REQUIRED = ["sg_readcap", "sg_format", "sg_requests", "sg_turs", "lsblk"]
HOLDER_FS = {"zfs_member", "LVM2_member", "linux_raid_member", "crypto_LUKS", "swap"}
NORMAL_BLOCKS = (512, 4096)
ACTIVE = ("starting", "formatting")
TRACKED = ("starting", "formatting", "done", "error")
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


# helpers
def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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


def fmt_dur(sec):
    if sec is None:
        return "--"
    sec = int(max(0, sec))
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    if d:
        return f"{d}d{h:02d}h{m:02d}m"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


def fmt_size(b):
    if not b:
        return "?"
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if b >= div:
            return f"{b / div:.2f} {unit}"
    return f"{b} B"


def clock(ts):
    return time.strftime("%a %H:%M", time.localtime(ts))


def parse_progress(text):
    m = PCT_RE.search(text)
    return float(m.group(1)) if m else None


def get_progress(dev):
    """Percent done of a running format, or None."""
    for cmd in (["sg_requests", "-p", dev], ["sg_turs", "-p", "-n", "1", dev]):
        _, out = run(cmd, timeout=20)
        pct = parse_progress(out)
        if pct is not None:
            return pct
    return None


def parse_readcap(out):
    """-> dict(block_len, prot, ptype, size) or None."""
    m = re.search(r"Logical block length=(\d+)", out)
    if not m:
        return None
    info = {"block_len": int(m.group(1)), "prot": 0, "ptype": None, "size": None}
    m = re.search(r"prot_en=(\d)", out)
    if m:
        info["prot"] = int(m.group(1))
    m = re.search(r"p_type=(\d)", out)
    if m:
        info["ptype"] = int(m.group(1))
    m = re.search(r"Device size:\s*(\d+)\s*bytes", out)
    if m:
        info["size"] = int(m.group(1))
    return info


def log_path(sg):
    return os.path.join(LOG_DIR, f"sgfmt-{sg}.log")


def log_tail(sg):
    try:
        with open(log_path(sg), errors="replace") as f:
            lines = [l.strip() for l in f if l.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


# persistent state
class State:
    """Remembers when we started each format so ETAs survive a restart."""

    def __init__(self):
        self.lock = threading.Lock()
        try:
            with open(STATE_FILE) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def _save(self):
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, STATE_FILE)

    def get(self, key):
        with self.lock:
            return self.data.get(key)

    def set(self, key, val):
        with self.lock:
            self.data[key] = val
            self._save()

    def drop(self, key):
        with self.lock:
            if self.data.pop(key, None) is not None:
                self._save()


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
        self.ptype = None
        self.size = None
        self.rc_err = ""
        self.in_use = ""
        self.status = "unknown" # ok / needs format / starting / formatting / done / error
        self.progress = None
        self.samples = deque(maxlen=5000)
        self.start_time = None
        self.start_pct = 0.0
        self.start_known = False # True if its known when the format really began
        self.launched = None
        self.proc = None
        self.misses = 0
        self.error = ""
        self.finished = None
        self.selected = False

    @property
    def selectable(self):
        return self.status == "needs format" and not self.in_use

    def readcap(self):
        rc, out = run(["sg_readcap", "-l", self.dev])
        info = parse_readcap(out) if rc == 0 else None
        if info:
            self.block_len = info["block_len"]
            self.prot = info["prot"]
            self.ptype = info["ptype"]
            self.size = info["size"] or self.size
            self.rc_err = ""
            return True
        self.block_len = None
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        self.rc_err = lines[-1] if lines else f"sg_readcap rc={rc}"
        return False

    def check_in_use(self):
        self.in_use = ""
        if not self.block:
            return
        _, out = run(["lsblk", "-nro", "NAME,FSTYPE,MOUNTPOINT", "/dev/" + self.block])
        for line in out.splitlines():
            parts = (line.split(" ") + ["", "", ""])[:3]
            name, fstype, mnt = parts
            if mnt:
                self.in_use = f"{name} mounted on {mnt}"
                return
            if fstype in HOLDER_FS:
                self.in_use = f"{name} is {fstype}"
                return

    def classify(self):
        if self.progress is not None:
            self.status = "formatting"
        elif self.block_len in NORMAL_BLOCKS and not self.prot:
            self.status = "ok"
        else:
            self.status = "needs format"

    @property
    def pi_label(self):
        if not self.prot:
            return "-"
        return f"T{self.ptype + 1}" if self.ptype is not None else "on"

    def rate_eta(self):
        """-> (%/hour, seconds remaining) or (None, None)."""
        if self.status != "formatting" or self.progress is None:
            return None, None
        now, pct = time.time(), self.progress
        rate = None
        if self.start_time and now - self.start_time >= 120 and pct > self.start_pct:
            rate = (pct - self.start_pct) / (now - self.start_time)
        elif len(self.samples) >= 2:
            (t0, p0), (t1, p1) = self.samples[0], self.samples[-1]
            if t1 - t0 >= 30 and p1 > p0:
                rate = (p1 - p0) / (t1 - t0)
        if not rate:
            return None, None
        return rate * 3600, (100.0 - pct) / rate


# monitor
class Monitor(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.wake = threading.Event()

    def run(self):
        while True:
            for d in list(self.app.disks.values()):
                if d.status in ACTIVE:
                    try:
                        self.poll(d)
                    except Exception as e: # never let the poller die
                        pass
                        d.error = f"poll error: {e}"
            self.app.last_poll = time.time()
            self.wake.wait(POLL_SECS)
            self.wake.clear()

    def poll(self, d):
        now = time.time()
        pct = get_progress(d.dev)
        if pct is not None:
            d.misses = 0
            d.progress = pct
            d.status = "formatting"
            d.samples.append((now, pct))
            return

        # No progress reported.
        if d.status == "starting":
            proc_alive = d.proc is not None and d.proc.poll() is None
            if proc_alive or (d.launched and now - d.launched < 90):
                return  # give sg_format time to get going
            if d.proc is not None and d.proc.returncode not in (0, None):
                self.fail(d, log_tail(d.sg) or f"sg_format exited {d.proc.returncode}")
                return

        d.misses += 1
        if d.misses < 3:
            return  # tolerate a transient miss
        if d.readcap() and d.block_len == TARGET_BLOCK and not d.prot:
            d.status = "done"
            d.progress = 100.0
            d.finished = now
            self.app.state.drop(d.sg)
        elif "format" in d.rc_err.lower() and "progress" in d.rc_err.lower():
            d.misses = 0  # still formatting, just no percentage this time
        else:
            self.fail(d, log_tail(d.sg) or d.rc_err or "format did not complete")

    def fail(self, d, msg):
        d.status = "error"
        d.error = msg
        self.app.state.drop(d.sg)


# --------------------------------------------------------------------- UI
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
        self.disks = {}
        self.state = State()
        self.cursor = 0
        self.screen = "select"
        self.msg, self.msg_until = "", 0
        self.last_poll = None
        _, helptxt = run(["sg_format", "--help"])
        self.opt_early = "--early" in helptxt
        self.opt_quick = "--quick" in helptxt
        self.monitor = Monitor(self)

        curses.curs_set(0)
        scr.timeout(500)
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_GREEN, bg)
        curses.init_pair(2, curses.COLOR_YELLOW, bg)
        curses.init_pair(3, curses.COLOR_RED, bg)
        curses.init_pair(4, curses.COLOR_CYAN, bg)
        self.C = {
            "ok": curses.color_pair(1), "done": curses.color_pair(1) | curses.A_BOLD,
            "formatting": curses.color_pair(2) | curses.A_BOLD,
            "starting": curses.color_pair(2),
            "needs format": curses.color_pair(3) | curses.A_BOLD,
            "error": curses.color_pair(3) | curses.A_BOLD,
            "dim": curses.color_pair(4), "unknown": 0,
        }

    # ---- data
    def flash(self, msg, secs=4):
        self.msg, self.msg_until = msg, time.time() + secs

    def splash(self, text):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        put(self.scr, h // 2, max(0, (w - len(text)) // 2), text, curses.A_BOLD)
        self.scr.refresh()

    def rescan(self):
        found = {}
        try:
            names = os.listdir("/sys/class/scsi_generic")
        except OSError:
            names = []
        for sg in sorted(names, key=lambda s: int(re.sub(r"\D", "", s) or 0)):
            if read_sys(f"/sys/class/scsi_generic/{sg}/device/type") != "0":
                continue  # not a disk (controller, enclosure, ...)
            d = self.disks.get(sg) or Disk(sg)
            found[sg] = d
            if d.status in ACTIVE or d.status == "done":
                continue
            d.progress = get_progress(d.dev)
            if d.progress is None:
                d.readcap()
            d.check_in_use()
            d.classify()
            if d.status != "needs format":
                d.selected = False
        self.disks = found
        self.reconcile()
        self.cursor = min(self.cursor, max(0, len(self.disks) - 1))

    def reconcile(self):
        """Match drives against the state file (formats started earlier)."""
        now = time.time()
        for sg, d in self.disks.items():
            ent = self.state.get(sg)
            if ent and ent.get("model") != d.model:
                self.state.drop(sg)
                ent = None
            if d.status == "formatting":
                if d.start_time is None:
                    if ent:
                        d.start_time = ent["started"]
                        d.start_pct = ent.get("start_pct", 0.0)
                        d.start_known = True
                    else:  # started outside this script: measure from now
                        d.start_time, d.start_pct = now, d.progress or 0.0
                        self.state.set(sg, {"started": now, "start_pct": d.start_pct,
                                            "model": d.model, "observed": True})
                if not d.samples:
                    d.samples.append((now, d.progress or 0.0))
            elif ent and d.status != "starting":
                if d.status == "ok":
                    d.status = "done"  # finished while we were away
                else:
                    d.error = "previous format did not complete - format it again"
                self.state.drop(sg)

    def start_format(self, d):
        cmd = ["sg_format", "--format", f"--size={TARGET_BLOCK}", "--fmtpinfo=0"]
        if self.opt_quick:
            cmd.append("--quick")   # skip sg_format's own 15 s abort window
        if self.opt_early:
            cmd.append("--early")   # return as soon as the drive accepted it
        cmd.append(d.dev)
        with open(log_path(d.sg), "w") as logf:
            logf.write("$ " + " ".join(cmd) + "\n")
            logf.flush()
            d.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf,
                                      stderr=subprocess.STDOUT, start_new_session=True)
        now = time.time()
        d.status, d.launched = "starting", now
        d.start_time, d.start_pct, d.start_known = now, 0.0, True
        d.progress, d.misses, d.error, d.selected = 0.0, 0, "", False
        d.samples.clear()
        self.state.set(d.sg, {"started": now, "start_pct": 0.0, "model": d.model})

    # screens
    def draw_select(self):
        s, C = self.scr, self.C
        s.erase()
        h, w = s.getmaxyx()
        put(s, 0, 0, " sgfmt - drive sector formats ".ljust(w), curses.A_REVERSE)
        put(s, 1, 1, f"Drives that aren't {TARGET_BLOCK}/4096-byte without protection "
                     f"can be reformatted to {TARGET_BLOCK} bytes.", C["dim"])
        hdr = (f"{'':4} {'SG':<6}{'DEV':<6}{'VENDOR':<9}{'MODEL':<18}"
               f"{'SIZE':>10}  {'SECTOR':>6}  {'PI':<4} STATUS")
        put(s, 3, 0, hdr, curses.A_BOLD)
        rows = list(self.disks.values())
        if not rows:
            put(s, 5, 2, "No SCSI disks found.")
        for i, d in enumerate(rows):
            y = 4 + i
            if y >= h - 3:
                break
            mark = "[x]" if d.selected else ("[ ]" if d.selectable else "   ")
            sector = str(d.block_len) if d.block_len else "?"
            line = (f" {mark} {d.sg:<6}{d.block or '-':<6}{d.vendor[:8]:<9}{d.model[:17]:<18}"
                    f"{fmt_size(d.size):>10}  {sector:>6}  {d.pi_label:<4} ")
            if d.status == "formatting":
                st = f"formatting {d.progress or 0:.1f}%"
            elif d.status == "needs format" and d.in_use:
                st = f"in use ({d.in_use}) - locked"
            elif d.status == "needs format" and d.block_len is None:
                st = f"needs format - {d.error or d.rc_err or 'unreadable'}"
            elif d.status == "needs format" and d.error:
                st = f"needs format - {d.error}"
            elif d.status == "error":
                st = f"error - {d.error}"
            else:
                st = d.status
            attr = curses.A_REVERSE if i == self.cursor else 0
            put(s, y, 0, line.ljust(len(line)), attr)
            put(s, y, len(line), st, C.get(d.status, 0) | attr)
        nsel = sum(d.selected for d in rows)
        if time.time() < self.msg_until:
            put(s, h - 2, 1, self.msg, C["formatting"])
        elif nsel:
            put(s, h - 2, 1, f"{nsel} selected", C["needs format"])
        has_tracked = any(d.status in TRACKED for d in rows)
        keys = ("↑/↓ move  SPACE select  a select all  ENTER format selected  "
                "r rescan  " + ("p progress  " if has_tracked else "") + "q quit")
        put(s, h - 1, 0, (" " + keys).ljust(w), curses.A_REVERSE)

    def draw_progress(self):
        s, C = self.scr, self.C
        s.erase()
        h, w = s.getmaxyx()
        now = time.time()
        rows = [d for d in self.disks.values() if d.status in TRACKED]
        active = [d for d in rows if d.status in ACTIVE]
        ago = f"{int(now - self.last_poll)}s ago" if self.last_poll else "pending"
        title = f" sgfmt - formatting {len(active)} drive(s)   last poll {ago} "
        put(s, 0, 0, title.ljust(w), curses.A_REVERSE)

        etas = [d.rate_eta()[1] for d in active]
        if active and all(e is not None for e in etas):
            put(s, 1, 1, f"All done in ≈ {fmt_dur(max(etas))}  (≈ {clock(now + max(etas))})",
                curses.A_BOLD)
        elif active:
            put(s, 1, 1, "Measuring rate... ETA appears after a couple of minutes.", C["dim"])
        elif rows:
            put(s, 1, 1, "No formats running.", C["done"])

        y = 3
        bw = max(10, min(w - 12, 80))
        for d in rows:
            if y + 3 >= h - 1:
                put(s, y, 1, f"… {len(rows)} drives, enlarge the window to see all", C["dim"])
                break
            put(s, y, 1, f"{d.sg:<5} /dev/{d.block or '?':<5} {d.vendor} {d.model}  "
                         f"{fmt_size(d.size)}", curses.A_BOLD)
            pct = max(0.0, min(100.0, d.progress or 0.0))
            fill = int(bw * pct / 100)
            attr = C.get(d.status, 0)
            put(s, y + 1, 1, "[" + "█" * fill + "░" * (bw - fill) + "]", attr)
            put(s, y + 1, bw + 3, f"{pct:6.2f}%", attr | curses.A_BOLD)
            if d.status == "formatting":
                rate, rem = d.rate_eta()
                el = now - d.start_time if d.start_time else None
                el_lbl = "elapsed" if d.start_known else "watched"
                info = f"{el_lbl} {fmt_dur(el)}"
                if rate:
                    info += f"   rate {rate:.2f}%/h   ETA {fmt_dur(rem)}   done ≈ {clock(now + rem)}"
                else:
                    info += "   measuring rate..."
            elif d.status == "starting":
                info = f"starting sg_format… ({fmt_dur(now - d.launched)})" if d.launched else "starting…"
            elif d.status == "done":
                info = f"finished{' ' + clock(d.finished) if d.finished else ''} - now {d.block_len or TARGET_BLOCK}-byte sectors, PI off"
            else:
                info = f"ERROR: {d.error}   (log: {log_path(d.sg)})"
            put(s, y + 2, 3, info, attr if d.status == "error" else 0)
            y += 4
        if not rows:
            put(s, 3, 2, "Nothing is formatting. Press b to pick drives.")
        if time.time() < self.msg_until:
            put(s, h - 2, 1, self.msg, C["formatting"])
        keys = "b drive list, r poll now, q quit (formats keep running inside the drives)"
        put(s, h - 1, 0, (" " + keys).ljust(w), curses.A_REVERSE)

    def confirm(self, sel):
        s, C = self.scr, self.C
        s.erase()
        h, w = s.getmaxyx()
        put(s, 0, 0, " CONFIRM LOW-LEVEL FORMAT ".ljust(w), curses.A_REVERSE | C["error"])
        put(s, 2, 1, "These drives will be LOW-LEVEL FORMATTED. ALL DATA ON THEM IS DESTROYED.",
            C["error"])
        y = 4
        for d in sel:
            put(s, y, 3, f"{d.sg:<5} /dev/{d.block or '?':<5} {d.vendor} {d.model}  "
                         f"{fmt_size(d.size)}  sector {d.block_len or '?'}  PI {d.pi_label}")
            y += 1
        y += 1
        put(s, y, 1, f"Runs: sg_format --format --size={TARGET_BLOCK} --fmtpinfo=0 "
                     f"{'--quick ' if self.opt_quick else ''}{'--early ' if self.opt_early else ''}/dev/sgX",
            C["dim"])
        put(s, y + 1, 1, "Takes hours. Don't power off or reboot until they're done.", C["dim"])
        prompt = "Type YES and press Enter to start (anything else cancels): "
        put(s, y + 3, 1, prompt, curses.A_BOLD)
        s.refresh()
        curses.echo()
        curses.curs_set(1)
        s.timeout(-1)
        try:
            txt = s.getstr(y + 3, 1 + len(prompt), 10).decode(errors="ignore").strip()
        except curses.error:
            txt = ""
        curses.noecho()
        curses.curs_set(0)
        s.timeout(500)
        return txt == "YES"

    # ---- main loop
    def handle(self, k):
        rows = list(self.disks.values())
        if k in (ord("q"), ord("Q")):
            return False
        if k == curses.KEY_RESIZE:
            return True
        if self.screen == "progress":
            if k in (ord("b"), ord("B")):
                self.screen = "select"
            elif k in (ord("r"), ord("R")):
                self.monitor.wake.set()
                self.flash("Polling drives…", 2)
            return True
        # select screen
        if k in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(len(rows) - 1, self.cursor + 1)
        elif k == ord(" ") and rows:
            d = rows[self.cursor]
            if d.selectable:
                d.selected = not d.selected
            else:
                self.flash(f"{d.sg} can't be selected ({d.in_use or d.status}).")
        elif k in (ord("a"), ord("A")):
            cand = [d for d in rows if d.selectable]
            want = not all(d.selected for d in cand)
            for d in cand:
                d.selected = want
        elif k in (ord("r"), ord("R")):
            self.splash("Rescanning drives…")
            self.rescan()
        elif k in (ord("p"), ord("P")):
            if any(d.status in TRACKED for d in rows):
                self.screen = "progress"
        elif k in (10, 13, curses.KEY_ENTER):
            sel = [d for d in rows if d.selected and d.selectable]
            if not sel:
                self.flash("Nothing selected - use SPACE or a.")
            elif self.confirm(sel):
                started = 0
                for d in sel:
                    try:
                        self.start_format(d)
                        started += 1
                    except OSError as e:
                        d.status, d.error = "error", f"could not start sg_format: {e}"
                self.monitor.wake.set()
                self.screen = "progress"
                self.flash(f"Started {started} format(s). Polling every {POLL_SECS}s.")
            else:
                self.flash("Cancelled.")
        return True

    def loop(self):
        self.splash("Scanning drives…")
        self.rescan()
        self.monitor.start()
        if any(d.status in ACTIVE for d in self.disks.values()):
            self.screen = "progress"
            self.flash("Found formats already running - resumed monitoring.")
        while True:
            (self.draw_select if self.screen == "select" else self.draw_progress)()
            self.scr.refresh()
            k = self.scr.getch()
            if k != -1 and not self.handle(k):
                break
        return [d for d in self.disks.values() if d.status in ACTIVE]


def main():
    if os.geteuid() != 0:
        sys.exit("Run as root.")
    missing = [t for t in REQUIRED if not shutil.which(t)]
    if missing:
        sys.exit(f"Missing tools: {', '.join(missing)}  ->  apt install sg3-utils util-linux")
    locale.setlocale(locale.LC_ALL, "")
    active = curses.wrapper(lambda scr: App(scr).loop())
    if active:
        print(f"{len(active)} format(s) still running inside the drives: "
              + ", ".join(d.sg for d in active))
        print("Don't power off. Run this script again any time to resume monitoring.")


if __name__ == "__main__":
    main()