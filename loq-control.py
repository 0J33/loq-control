#!/usr/bin/env python3
"""LOQ Control — Desktop control center for Lenovo LOQ laptops on Linux."""

import sys
import os
import json
import subprocess
import glob
import threading
import time
import pwd
from collections import deque
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QFrame, QGridLayout, QSlider, QScrollArea, QMessageBox, QSizePolicy,
    QProgressBar, QSystemTrayIcon, QMenu, QAction, QShortcut,
    QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit,
    QAbstractItemView)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (QFont, QIcon, QPainter, QColor, QPen,
                         QKeySequence, QPixmap, QLinearGradient)


class NoScrollSlider(QSlider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._snap_step = 0
        self._snap_fn = None
        self._snapping = False

    def wheelEvent(self, event):
        event.ignore()

    def mousePressEvent(self, event):
        """Click-to-jump: a left click anywhere on the track sets the
        slider's value to that position directly, instead of Qt's default
        page-step nudge. This makes "click the '100' tick" actually go
        to 100, which matches user expectation."""
        if event.button() == Qt.LeftButton:
            from PyQt5.QtWidgets import QStyleOptionSlider, QStyle
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(
                QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            if not handle.contains(event.pos()):
                if self.orientation() == Qt.Horizontal:
                    pos = event.pos().x() - handle.width() // 2
                    span = self.width() - handle.width()
                else:
                    pos = event.pos().y() - handle.height() // 2
                    span = self.height() - handle.height()
                val = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(),
                    pos, span, opt.upsideDown)
                self.setValue(val)
                self.sliderReleased.emit()
                event.accept()
                return
        super().mousePressEvent(event)

    def setSnap(self, step):
        self._snap_step = step
        self._snap_fn = None

    def setSnapFn(self, fn):
        self._snap_fn = fn
        self._snap_step = 0

    def _snap_value(self, v):
        if self._snap_fn:
            return self._snap_fn(v)
        if self._snap_step > 1:
            lo, hi = self.minimum(), self.maximum()
            step = self._snap_step
            # Magnetic endpoints: any value within one full step of
            # either endpoint snaps to that endpoint. This makes the
            # max/min reachable without pixel-precise dragging — values
            # like 96-99 on a 5-100 slider all land on 100. Without
            # this, standard "nearest grid" rounding gives 95 for v=97
            # because 95 is closer (diff 2) than 100 (diff 3).
            if v > hi - step:
                return hi
            if v < lo + step:
                return lo
            grid = round(v / step) * step
            return max(lo, min(hi, grid))
        return None

    # Snap at setValue / setSliderPosition rather than via sliderChange.
    # Qt's setValue emits valueChanged with the ORIGINAL parameter after
    # sliderChange returns, so snapping inside sliderChange caused the
    # label listener to see the post-snap value first and then the raw
    # pre-snap value — which is why dragging showed non-multiple-of-5
    # numbers while the handle visually snapped. Intercepting here makes
    # the snap happen before Qt records the value, so every signal Qt
    # emits already carries the snapped number.

    def setValue(self, value):
        snapped = self._snap_value(value)
        if snapped is not None:
            value = snapped
        super().setValue(value)

    def setSliderPosition(self, value):
        snapped = self._snap_value(value)
        if snapped is not None:
            value = snapped
        super().setSliderPosition(value)


# ── Config ────────────────────────────────────────────────────────────

CONFIG_DIR = os.path.expanduser('~/.config/loq-control')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
HISTORY_FILE = os.path.join(CONFIG_DIR, 'battery_history.csv')
AUTOSTART_DIR = os.path.expanduser('~/.config/autostart')
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, 'loq-control.desktop')
SCRIPT_PATH = os.path.abspath(__file__)
ICON_PATH = os.path.join(os.path.dirname(SCRIPT_PATH), 'loq.png')
ICON_DIR = os.path.expanduser('~/.local/share/icons/hicolor')


def _build_icon():
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        path = os.path.join(ICON_DIR, f'{size}x{size}', 'apps',
                            'loq-control.png')
        if os.path.isfile(path):
            icon.addPixmap(QPixmap(path))
    if icon.isNull():
        icon = _build_icon()
    return icon

DEFAULT_CONFIG = {
    'theme': 'dark',
    'oc_power_limit': 0,
    'oc_gpu_clock': 0,
    'oc_mem_clock_idx': 0,
    'auto_apply_oc': False,
}


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


# ── Theme ─────────────────────────────────────────────────────────────

# ojee-ui tokens (design-system/ojee-ui.css in pitvisor): cyan on
# near-black, cream ink scale, zero radius, single accent.
# The palette is generated from ojee-ui.css — see theme.py. It used to be
# typed in here, and had silently drifted to a WARM near-black (#0e0d0a) while
# the design system moved to the cool #08080e its own comment calls "NOT warm".
# Nothing checked, so nobody noticed.
from theme import load_theme  # noqa: E402

_cfg = load_config()
T = load_theme()
FONT = 'Geist Mono'
FONT_DISPLAY = 'Major Mono Display'  # unicase pixel-ish display face
FONT_HUD = 'Departure Mono'          # tiny instrument labels (web --hud)


def mkfont(size, bold=False, ls=0.0, family=None):
    """Design-system font: Qt stylesheets ignore letter-spacing, so
    tracked uppercase labels (web .label/.meta) need it on the QFont."""
    f = QFont(family or FONT, size, QFont.Bold if bold else QFont.Normal)
    if ls:
        f.setLetterSpacing(QFont.AbsoluteSpacing, ls)
    return f
IDEAPAD = '/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00'
RAPL = '/sys/class/powercap/intel-rapl:0'
MEM_LEVELS = [0, 9001, 11001, 12001]
MEM_LABELS = ['Auto', '9 GHz', '11 GHz', '12 GHz']

# GPU clock lock: 0 = Auto, otherwise snap to 5 MHz steps starting at 180.
# Crossover point between Auto and locked is at 90 MHz on the slider.
GPU_CLOCK_MIN = 180
GPU_CLOCK_MAX = 3090
GPU_CLOCK_STEP = 5


def _gpu_clock_snap(v):
    if v < GPU_CLOCK_MIN // 2:
        return 0
    if v < GPU_CLOCK_MIN:
        return GPU_CLOCK_MIN
    rem = (v - GPU_CLOCK_MIN) % GPU_CLOCK_STEP
    if rem == 0:
        return min(GPU_CLOCK_MAX, v)
    up = GPU_CLOCK_STEP - rem
    snapped = v - rem if rem <= up else v + up
    return max(GPU_CLOCK_MIN, min(GPU_CLOCK_MAX, snapped))

CARD_STYLE = f"""
    QFrame {{
        background-color: {T['CARD']};
        border: 1px solid {T['BORDER']};
        border-radius: 0px;
    }}
"""
SLIDER_STYLE = f"""
    QSlider {{ background: transparent; border: none; min-height: 26px; }}
    QSlider::groove:horizontal {{
        background: {T['BORDER']}; height: 6px; border-radius: 0px;
    }}
    QSlider::handle:horizontal {{
        background: {T['ACCENT']}; width: 16px; height: 16px;
        margin: -5px 0; border-radius: 0px;
    }}
    QSlider::sub-page:horizontal {{
        background: {T['ACCENT_DIM']}; border-radius: 0px;
    }}
"""
# Slightly taller / accent variant used by overclock sliders so the ticks
# Qt draws under TicksBelow have room and the track reads more clearly.
OC_SLIDER_STYLE = f"""
    QSlider {{ background: transparent; border: none; min-height: 38px;
               padding: 0; }}
    QSlider::groove:horizontal {{
        background: {T['BORDER']}; height: 6px; border-radius: 0px;
    }}
    QSlider::handle:horizontal {{
        background: {T['ACCENT']}; width: 16px; height: 16px;
        margin: -5px 0; border-radius: 0px;
        border: 2px solid {T['BG']};
    }}
    QSlider::sub-page:horizontal {{
        background: {T['ACCENT_DIM']}; border-radius: 0px;
    }}
    QSlider::tick-mark:horizontal {{
        background: {T['BORDER']};
    }}
"""
BAR_STYLE = f"""
    QProgressBar {{
        background: {T['BORDER']}; border: none; border-radius: 0px;
        max-height: 10px; min-height: 10px;
    }}
    QProgressBar::chunk {{
        background: {T['ACCENT']}; border-radius: 0px;
    }}
"""
MSG_STYLE = f"""
    QMessageBox {{ background-color: {T['CARD']}; }}
    QMessageBox QLabel {{ color: {T['TEXT']}; font-family: '{FONT}'; }}
    QMessageBox QPushButton {{
        background-color: {T['BTN_DEF']}; color: {T['TEXT']};
        border: 1px solid {T['BORDER']}; border-radius: 0px;
        padding: 6px 16px; font-family: '{FONT}'; min-width: 60px;
    }}
    QMessageBox QPushButton:hover {{
        background-color: {T['BTN_HOVER']}; color: {T['ACCENT']};
        border-color: {T['ACCENT']};
    }}
    QToolTip {{
        background-color: #000000; color: {T['ACCENT']};
        border: 1px solid {T['ACCENT']}; border-radius: 0px;
        font-family: '{FONT}'; font-size: 8pt; padding: 4px 8px;
    }}
"""
SCROLL_STYLE = f"""
    QScrollArea {{ border: none; background: {T['BG']}; }}
    QScrollBar:vertical {{
        background: {T['BG']}; width: 6px; border: none;
    }}
    QScrollBar::handle:vertical {{
        background: {T['ACCENT']}; border-radius: 0px; min-height: 30px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0; background: none;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: none;
    }}
"""


# ── Helpers ───────────────────────────────────────────────────────────

def read_sys(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ''


def run_cmd(cmd):
    subprocess.run(['sudo', 'sh', '-c', cmd],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def set_gpu_tgp(watts):
    # nvidia-smi -pl is locked on mobile Blackwell; route through Lenovo
    # WMAE method which writes EC GTGP and notifies NVIDIA NPCF.
    w = max(45, min(100, int(watts)))
    run_cmd(
        "modprobe acpi_call 2>/dev/null; "
        "echo '\\_SB.GZFD.WMAE 0 0x12 "
        f"{{0x00, 0x00, 0x02, 0x02, 0x{w:02X}, 0x00, 0x00, 0x00}}' "
        "> /proc/acpi/call")


def run_output(cmd):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ''


def read_fan_rpm():
    try:
        r = subprocess.run(
            ['sudo', 'cat', '/sys/kernel/debug/legion/fancurve'],
            capture_output=True, text=True, timeout=5)
        cpu, gpu = 0, 0
        for line in r.stdout.split('\n'):
            s = line.strip()
            if s.startswith('1 fanspeed WMI3:'):
                cpu = int(s.split(':')[1])
            elif s.startswith('2 fanspeed WMI3:'):
                gpu = int(s.split(':')[1])
        return cpu, gpu
    except Exception:
        return 0, 0


def nvidia_query(fields):
    if isinstance(fields, str):
        fields = [fields]
    r = run_output(['nvidia-smi', f'--query-gpu={",".join(fields)}',
                    '--format=csv,noheader,nounits'])
    return [v.strip() for v in r.split(',')] if r else [''] * len(fields)


def get_gpu_power():
    output = run_output(['nvidia-smi', '-q', '-d', 'POWER'])
    info = {'current': 0, 'default': 45, 'min': 5, 'max': 100}
    found, in_mod = set(), False
    for line in output.split('\n'):
        s = line.strip()
        if 'Module Power' in s or 'GPU Memory Power' in s:
            in_mod = True; continue
        if 'GPU Power Readings' in s:
            in_mod = False; continue
        if in_mod or ':' not in s:
            continue
        for pat, key in [('Current Power Limit', 'current'),
                         ('Default Power Limit', 'default'),
                         ('Min Power Limit', 'min'),
                         ('Max Power Limit', 'max')]:
            if s.startswith(pat) and key not in found:
                try:
                    info[key] = float(s.split(':')[1].replace('W', '').strip())
                    found.add(key)
                except (ValueError, IndexError):
                    pass
    return info




def find_cpu_zone():
    for p in sorted(glob.glob('/sys/class/thermal/thermal_zone*')):
        if read_sys(f'{p}/type') in ('x86_pkg_temp', 'TCPU', 'cpu-thermal'):
            return p
    return '/sys/class/thermal/thermal_zone0'


def find_battery():
    for n in ['BAT0', 'BAT1']:
        p = f'/sys/class/power_supply/{n}'
        if read_sys(f'{p}/type') == 'Battery':
            return p
    return None


def fix_rapl_perms():
    """RAPL energy_uj is root-only on modern kernels (PLATYPUS
    mitigation). Make the package energy counters readable so the
    monitor can sample CPU watts without spawning sudo each tick."""
    paths = glob.glob('/sys/class/powercap/intel-rapl:[0-9]*/energy_uj')
    if not paths:
        return
    try:
        subprocess.run(
            ['sudo', '-n', 'chmod', 'a+r', *paths],
            check=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3)
    except Exception:
        pass


def read_cpu_package_energy_uj():
    """Cumulative CPU package energy counter in microjoules (RAPL).

    Take deltas between samples to derive average power over the
    interval. Returns None if unavailable.
    """
    raw = read_sys(f'{RAPL}/energy_uj')
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def read_cpu_max_energy_uj():
    raw = read_sys(f'{RAPL}/max_energy_range_uj')
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def read_gpu_power_draw():
    """Instantaneous GPU power draw in W via nvidia-smi power.draw."""
    out = run_output([
        'nvidia-smi', '--query-gpu=power.draw',
        '--format=csv,noheader,nounits'])
    try:
        return float((out or '').strip())
    except (ValueError, AttributeError):
        return None


def cpu_model_short():
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.startswith('model name'):
                    full = line.split(':', 1)[1].strip()
                    s = (full.replace('(R)', '').replace('(TM)', '')
                         .replace('Intel ', '').replace('  ', ' ').strip())
                    return s
    except Exception:
        pass
    return ''


def gpu_model_short():
    name = nvidia_query('name')[0] or ''
    vram = nvidia_query('memory.total')[0] or ''
    n = (name.replace('NVIDIA ', '').replace('GeForce ', '')
         .replace(' GPU', '').replace(' Laptop', ' Laptop'))
    if vram:
        try:
            gb = round(int(vram) / 1024)
            return f'{n.strip()} · {gb} GB'
        except ValueError:
            pass
    return n.strip()


def fmt_bytes(n):
    if n >= 1099511627776:
        return f'{n / 1099511627776:.1f} TB'
    if n >= 1073741824:
        return f'{n / 1073741824:.1f} GB'
    if n >= 1048576:
        return f'{n / 1048576:.0f} MB'
    if n >= 1024:
        return f'{n / 1024:.0f} KB'
    return f'{n} B'


def list_drives():
    """Whole-disk block devices we care about, with model and size."""
    drives = []
    try:
        paths = (glob.glob('/sys/block/nvme*n*')
                 + glob.glob('/sys/block/sd*')
                 + glob.glob('/sys/block/mmcblk*'))
        for path in sorted(paths):
            dev = os.path.basename(path)
            if dev.startswith('nvme') and 'p' in dev.split('n', 1)[-1]:
                continue
            size_sec = int(read_sys(f'{path}/size') or '0')
            size = size_sec * 512
            if size <= 0:
                continue
            model = (read_sys(f'{path}/device/model')
                     or read_sys(f'{path}/device/name') or '').strip()
            drives.append({'dev': dev, 'model': model, 'size': size})
    except Exception:
        pass
    return drives


def drive_temp(dev):
    """NVMe / SATA drive temperature in °C, or None if unavailable."""
    try:
        for hwmon in glob.glob(f'/sys/block/{dev}/device/hwmon/hwmon*'):
            for tf in sorted(glob.glob(f'{hwmon}/temp*_input')):
                raw = read_sys(tf)
                if raw:
                    return int(raw) // 1000
        for hwmon in glob.glob('/sys/class/nvme/*/hwmon*'):
            ctrl = os.path.basename(os.path.dirname(hwmon))
            if dev.startswith(ctrl):
                raw = read_sys(f'{hwmon}/temp1_input')
                if raw:
                    return int(raw) // 1000
    except Exception:
        pass
    return None


def _unescape_mount_path(s):
    """/proc/mounts encodes spaces (and a few other chars) as backslash-
    octal sequences (e.g. \\040 for ' '). os.statvfs needs the real path."""
    out, i = [], 0
    while i < len(s):
        if s[i] == '\\' and i + 3 < len(s) and s[i+1:i+4].isdigit():
            try:
                out.append(chr(int(s[i+1:i+4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(s[i]); i += 1
    return ''.join(out)


def drive_usage(dev):
    """Best-effort (used, total) bytes for the largest partition on this drive."""
    try:
        with open('/proc/mounts') as f:
            mounts = []
            for line in f:
                p = line.split()
                if len(p) >= 2 and p[0].startswith(f'/dev/{dev}'):
                    mounts.append(_unescape_mount_path(p[1]))
        best_total = 0; best_used = 0
        for mp in mounts:
            try:
                s = os.statvfs(mp)
                total = s.f_blocks * s.f_frsize
                used = (s.f_blocks - s.f_bavail) * s.f_frsize
                if total > best_total:
                    best_total = total; best_used = used
            except OSError:
                pass
        return best_used, best_total
    except Exception:
        return 0, 0


def list_active_nics():
    """Active non-loopback interfaces with type, SSID/IP info."""
    nics = []
    try:
        for path in sorted(glob.glob('/sys/class/net/*')):
            iface = os.path.basename(path)
            if iface == 'lo':
                continue
            operstate = (read_sys(f'{path}/operstate') or 'unknown').strip()
            carrier = (read_sys(f'{path}/carrier') or '0').strip()
            if operstate != 'up' and carrier != '1':
                continue
            if os.path.isdir(f'{path}/wireless'):
                kind = 'wifi'
            elif iface.startswith(('en', 'eth')):
                kind = 'eth'
            elif iface.startswith(('tailscale', 'wg', 'tun', 'tap')):
                kind = 'vpn'
            else:
                kind = 'other'
            ssid = None
            if kind == 'wifi':
                try:
                    r = subprocess.run(['iwgetid', '-r', iface],
                                       capture_output=True, text=True,
                                       timeout=2)
                    ssid = r.stdout.strip() or None
                except Exception:
                    pass
            ip = None
            try:
                r = subprocess.run(
                    ['ip', '-4', '-br', 'addr', 'show', iface],
                    capture_output=True, text=True, timeout=2)
                parts = r.stdout.strip().split()
                if len(parts) >= 3 and '/' in parts[2]:
                    ip = parts[2].split('/')[0]
            except Exception:
                pass
            nics.append({'iface': iface, 'kind': kind,
                         'ssid': ssid, 'ip': ip})
    except Exception:
        pass
    return nics


def read_cpu_stat():
    try:
        with open('/proc/stat') as f:
            parts = f.readline().split()
        v = [int(x) for x in parts[1:]]
        return sum(v), v[3] + v[4]
    except Exception:
        return 0, 0


def read_per_core_stats():
    cores = []
    try:
        with open('/proc/stat') as f:
            for line in f:
                if line.startswith('cpu') and not line.startswith('cpu '):
                    v = [int(x) for x in line.split()[1:]]
                    cores.append((sum(v), v[3] + v[4]))
    except Exception:
        pass
    return cores


def read_net_stats():
    stats = {}
    try:
        with open('/proc/net/dev') as f:
            for line in f:
                if ':' not in line:
                    continue
                iface, data = line.split(':', 1)
                iface = iface.strip()
                if iface == 'lo':
                    continue
                p = data.split()
                stats[iface] = (int(p[0]), int(p[8]))
    except Exception:
        pass
    return stats


def read_disk_stats():
    stats = {}
    try:
        with open('/proc/diskstats') as f:
            for line in f:
                p = line.split()
                if len(p) < 14:
                    continue
                name = p[2]
                # whole-disk devices only, no partitions
                if name.startswith('nvme'):
                    tail = name.split('n', 1)[-1]
                    if 'p' in tail:
                        continue
                elif name.startswith('sd'):
                    if any(c.isdigit() for c in name[2:]):
                        continue
                elif name.startswith('mmcblk'):
                    if 'p' in name:
                        continue
                else:
                    continue
                stats[name] = (int(p[5]), int(p[9]))
    except Exception:
        pass
    return stats


def fmt_rate(bps):
    if bps >= 1048576:
        return f'{bps / 1048576:.1f} MB/s'
    if bps >= 1024:
        return f'{bps / 1024:.1f} KB/s'
    return f'{bps} B/s'


def get_rapl_info():
    try:
        cur = int(read_sys(f'{RAPL}/constraint_0_power_limit_uw') or '0')
        tdp = int(read_sys(f'{RAPL}/constraint_0_max_power_uw') or '0')
        if cur > 0 and tdp > 0 and read_sys(f'{RAPL}/name') == 'package-0':
            cur_w = cur // 1000000
            tdp_w = tdp // 1000000
            # max_power_uw is TDP; actual limit can be higher (firmware)
            slider_max = max(cur_w, tdp_w * 3, 100)
            slider_max = min(slider_max, 200)
            return min(cur_w, slider_max), slider_max
    except Exception:
        pass
    return None


def log_battery_history(bp):
    if not bp:
        return
    try:
        full = int(read_sys(f'{bp}/energy_full') or
                   read_sys(f'{bp}/charge_full') or '0')
        design = int(read_sys(f'{bp}/energy_full_design') or
                     read_sys(f'{bp}/charge_full_design') or '1')
        health = round(full / design * 100)
        cycles = read_sys(f'{bp}/cycle_count') or '0'
        today = time.strftime('%Y-%m-%d')
        os.makedirs(CONFIG_DIR, exist_ok=True)
        last = ''
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE) as f:
                for line in f:
                    if line.strip():
                        last = line.split(',')[0]
        if last != today:
            with open(HISTORY_FILE, 'a') as f:
                f.write(f'{today},{health},{cycles}\n')
    except Exception:
        pass


def battery_history_summary():
    try:
        if not os.path.exists(HISTORY_FILE):
            return None
        with open(HISTORY_FILE) as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return None
        first, last = lines[0].split(','), lines[-1].split(',')
        return dict(since=first[0], first_h=first[1],
                    cur_h=last[1], n=len(lines))
    except Exception:
        return None


# ── Custom Widgets ────────────────────────────────────────────────────

class GridBackdrop(QWidget):
    """web .grid-bg — the 18px dot-grid surface everything sits on."""

    _tile = None

    @classmethod
    def tile(cls):
        if cls._tile is None:
            t = QPixmap(18, 18)
            t.fill(QColor(T['BG']))
            p = QPainter(t)
            dot = QColor(T['TEXT'])
            dot.setAlpha(15)  # ≈6% — radial-gradient dot opacity in css
            p.fillRect(0, 0, 1, 1, dot)
            p.end()
            cls._tile = t
        return cls._tile

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawTiledPixmap(self.rect(), self.tile())


class TempGraph(QWidget):
    def __init__(self, max_pts=60):
        super().__init__()
        self.max_pts = max_pts
        self.cpu = deque(maxlen=max_pts)
        self.gpu = deque(maxlen=max_pts)
        self.setFixedHeight(140)
        self.setMouseTracking(True)
        self._hover_x = None
        self.setStyleSheet('background: transparent; border: none;')

    def add(self, ct, gt):
        self.cpu.append(ct)
        self.gpu.append(gt)
        self.update()

    def mouseMoveEvent(self, ev):
        self._hover_x = ev.pos().x()
        self.update()

    def leaveEvent(self, ev):
        self._hover_x = None
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        ml, mb = 35, 20
        gw, gh = w - ml - 8, h - mb - 6
        mx = 105
        # horizontal grid + Y labels
        for t in (25, 50, 75, 100):
            y = int(6 + gh * (1 - t / mx))
            grid = QColor(T['GR_GRID']); grid.setAlpha(110)
            p.setPen(QPen(grid, 1, Qt.DotLine))
            p.drawLine(ml, y, w - 5, y)
            p.setPen(QPen(QColor(T['GR_TEXT']), 1))
            p.setFont(QFont(FONT_HUD, 7))
            p.drawText(0, y + 4, f'{t}\u00b0')
        # vertical time gridlines
        grid = QColor(T['GR_GRID']); grid.setAlpha(60)
        p.setPen(QPen(grid, 1, Qt.DotLine))
        for f in (0.25, 0.5, 0.75):
            x = int(ml + gw * f)
            p.drawLine(x, 6, x, 6 + gh)

        def points(data):
            return [(ml + int(i * gw / (self.max_pts - 1)),
                     int(6 + gh * (1 - min(v, mx) / mx)))
                    for i, v in enumerate(data)]

        cpu_pts = points(self.cpu) if len(self.cpu) >= 2 else []
        gpu_pts = points(self.gpu) if len(self.gpu) >= 2 else []

        def draw(pts, color):
            if not pts:
                return
            p.setPen(QPen(QColor(color), 2))
            for i in range(len(pts) - 1):
                p.drawLine(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1])

        draw(cpu_pts, '#00ffff')
        draw(gpu_pts, '#ffd700')
        p.setFont(QFont(FONT_HUD, 7))
        p.setPen(QPen(QColor('#00ffff'), 1))
        p.drawText(ml + 5, h - 4, 'CPU')
        p.setPen(QPen(QColor('#ffd700'), 1))
        p.drawText(ml + 40, h - 4, 'GPU')

        # hover crosshair
        if self._hover_x is None or self._hover_x < ml or self._hover_x > ml + gw:
            return
        if not cpu_pts and not gpu_pts:
            return
        # pick best index from whichever line has data
        ref = cpu_pts or gpu_pts
        best_i, best_dx = 0, abs(ref[0][0] - self._hover_x)
        for i, (px, _) in enumerate(ref):
            dx = abs(px - self._hover_x)
            if dx < best_dx:
                best_i, best_dx = i, dx
        hx = ref[best_i][0]
        cross = QColor(T['TEXT']); cross.setAlpha(120)
        p.setPen(QPen(cross, 1))
        p.drawLine(hx, 6, hx, 6 + gh)
        # dots + tooltip text
        cv = self.cpu[best_i] if best_i < len(self.cpu) else None
        gv = self.gpu[best_i] if best_i < len(self.gpu) else None
        if cv is not None and cpu_pts:
            p.setBrush(QColor('#00ffff')); p.setPen(QPen(QColor(T['BG']), 1.2))
            cy = cpu_pts[best_i][1]
            p.drawEllipse(hx - 3, cy - 3, 6, 6)
        if gv is not None and gpu_pts:
            p.setBrush(QColor('#ffd700')); p.setPen(QPen(QColor(T['BG']), 1.2))
            gy = gpu_pts[best_i][1]
            p.drawEllipse(hx - 3, gy - 3, 6, 6)
        parts = []
        if cv is not None: parts.append(f'CPU {int(cv)}\u00b0')
        if gv is not None: parts.append(f'GPU {int(gv)}\u00b0')
        if not parts:
            return
        text = '  '.join(parts)
        p.setFont(QFont(FONT, 8, QFont.Bold))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text) + 10
        th = fm.height() + 4
        lx = hx + 8
        if lx + tw > w - 4: lx = hx - tw - 8
        ly = 6
        bg = QColor(T['CARD']); bg.setAlpha(235)
        p.setPen(QPen(QColor(T['BORDER']), 1)); p.setBrush(bg)
        p.drawRoundedRect(lx, ly, tw, th, 0, 0)
        p.setPen(QPen(QColor(T['TEXT'])))
        p.drawText(lx + 5, ly + fm.ascent() + 2, text)


class SliderTicks(QWidget):
    """Tick marks + labels aligned with a QSlider's actual track geometry.

    Sits in the layout directly below a slider. Uses
    QStyle.subControlRect(SC_SliderHandle / SC_SliderGroove) to find the
    handle's left/right travel limits, so labels land exactly under the
    handle when the slider is at that value.
    """

    def __init__(self, slider, ticks):
        super().__init__()
        self.slider = slider
        self.ticks = ticks  # list of (value, label)
        self.setFixedHeight(22)
        # Match the slider's expanding horizontal policy so this widget
        # has the same width as the slider above it — otherwise the tick
        # positions (computed from the slider's geometry) won't match
        # this widget's geometry and clicks resolve to the wrong value.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet('background: transparent; border: none;')

    def mousePressEvent(self, event):
        """Click a tick to snap the slider exactly to that tick's value."""
        if event.button() != Qt.LeftButton or not self.ticks:
            return super().mousePressEvent(event)
        x_min, x_max = self._track_extents()
        lo, hi = self.slider.minimum(), self.slider.maximum()
        rng = hi - lo if hi > lo else 1
        click_x = event.pos().x()
        best_v, best_dx = self.ticks[0][0], 10**9
        for v, _ in self.ticks:
            vv = max(lo, min(hi, v))
            frac = (vv - lo) / rng
            tx = x_min + int(frac * (x_max - x_min))
            dx = abs(tx - click_x)
            if dx < best_dx:
                best_v, best_dx = vv, dx
        # Only treat as a tick-click if the click was reasonably close
        # to one of the labels (don't hijack random clicks).
        if best_dx <= 24:
            self.slider.setValue(best_v)
            self.slider.sliderReleased.emit()
            event.accept()
        else:
            super().mousePressEvent(event)

    def _track_extents(self):
        """Return (x_at_min, x_at_max) in this widget's coordinates."""
        from PyQt5.QtWidgets import QStyleOptionSlider, QStyle
        opt = QStyleOptionSlider()
        self.slider.initStyleOption(opt)
        style = self.slider.style()
        # Range where the handle's center can sit:
        # slider.minimum() corresponds to handle centered at left end of
        # available track, slider.maximum() at right end.
        handle = style.subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self.slider)
        groove = style.subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self.slider)
        hw = handle.width()
        avail = max(1, groove.width() - hw)
        # the slider's x within its own widget; assume our widget has the
        # same width and starts at x=0 too (true inside a QVBoxLayout)
        x_min = groove.x() + hw // 2
        x_max = x_min + avail
        return x_min, x_max

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        if w < 4 or not self.ticks:
            return
        x_min, x_max = self._track_extents()
        lo, hi = self.slider.minimum(), self.slider.maximum()
        rng = hi - lo if hi > lo else 1
        p.setFont(QFont(FONT_HUD, 7))
        fm = p.fontMetrics()
        for value, label in self.ticks:
            v = max(lo, min(hi, value))
            frac = (v - lo) / rng
            x = x_min + int(frac * (x_max - x_min))
            # tick line
            tick = QColor(T['GR_TEXT']); tick.setAlpha(180)
            p.setPen(QPen(tick, 1))
            p.drawLine(x, 0, x, 4)
            # label centered on tick
            p.setPen(QPen(QColor(T['TEXT_MUTED'])))
            tw = fm.horizontalAdvance(label)
            lx = max(0, min(w - tw, x - tw // 2))
            p.drawText(lx, fm.ascent() + 6, label)


class Sparkline(QWidget):
    """Compact line graph for a single metric. Auto-scales to data peak.

    Features:
      - Horizontal grid lines (25/50/75% of fixed_max, or quartiles otherwise)
      - Faint vertical time gridlines
      - Hover crosshair + value tooltip when mouse is over the graph
    """

    def __init__(self, max_pts=60, color='#00ffff', height=46,
                 fixed_max=None, fmt=None, color2=None,
                 label1='', label2=''):
        super().__init__()
        self.max_pts = max_pts
        self.color = color
        self.color2 = color2          # if set, a second series is drawn
        self.label1 = label1
        self.label2 = label2
        self.fixed_max = fixed_max
        # fmt(v) -> str. Defaults to integer percent-like formatting.
        self.fmt = fmt or (lambda v: f'{v:.1f}' if v < 10 else f'{int(v)}')
        self.data = deque(maxlen=max_pts)
        self.data2 = deque(maxlen=max_pts)
        self._hover_x = None
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setStyleSheet('background: transparent; border: none;')

    def add(self, v):
        self.data.append(max(0.0, float(v)))
        self.update()

    def add_pair(self, v1, v2):
        self.data.append(max(0.0, float(v1)))
        self.data2.append(max(0.0, float(v2)))
        self.update()

    def reset(self):
        self.data.clear()
        self.data2.clear()
        self.update()

    def mouseMoveEvent(self, ev):
        self._hover_x = ev.pos().x()
        self.update()

    def leaveEvent(self, ev):
        self._hover_x = None
        self.update()

    def paintEvent(self, event):
        from PyQt5.QtGui import QPainterPath
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 4 or h <= 4:
            return
        # ── grid lines ───────────────────────────────────────────────
        grid = QColor(T['GR_GRID']); grid.setAlpha(90)
        p.setPen(QPen(grid, 1, Qt.DotLine))
        for frac in (0.25, 0.5, 0.75):
            y = int((h - 2) * frac) + 1
            p.drawLine(0, y, w, y)
        for frac in (0.25, 0.5, 0.75):
            x = int((w - 1) * frac)
            p.drawLine(x, 0, x, h)
        # solid baseline
        p.setPen(QPen(QColor(T['GR_GRID']), 1))
        p.drawLine(0, h - 1, w, h - 1)

        n = len(self.data)
        if n < 2:
            return
        has2 = self.color2 is not None and len(self.data2) >= 2
        peak1 = max(self.data) if self.data else 0
        peak2 = max(self.data2) if (has2 and self.data2) else 0
        mx = self.fixed_max if self.fixed_max else max(peak1, peak2, 1.0)
        if mx <= 0:
            mx = 1.0

        def build_pts(series):
            pts = []
            for i, v in enumerate(series):
                x = int(i * (w - 1) / (self.max_pts - 1))
                y = int((h - 2) * (1 - min(v, mx) / mx)) + 1
                pts.append((x, y))
            return pts

        def draw_series(series, color, alpha_fill=60):
            if len(series) < 2:
                return None
            pts = build_pts(series)
            fill = QColor(color); fill.setAlpha(alpha_fill)
            end = QColor(color);  end.setAlpha(0)
            grad = QLinearGradient(0, 0, 0, h)
            grad.setColorAt(0.0, fill)
            grad.setColorAt(1.0, end)
            path = QPainterPath()
            path.moveTo(pts[0][0], h)
            for x, y in pts:
                path.lineTo(x, y)
            path.lineTo(pts[-1][0], h)
            path.closeSubpath()
            p.fillPath(path, grad)
            p.setPen(QPen(QColor(color), 1.6))
            for i in range(len(pts) - 1):
                p.drawLine(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1])
            return pts

        # When two series share a chart, dim fills so they don't fight
        fill_a = 60 if not has2 else 35
        pts = draw_series(self.data, self.color, alpha_fill=fill_a)
        pts2 = draw_series(self.data2, self.color2, alpha_fill=fill_a) if has2 else None

        # ── hover crosshair + value ─────────────────────────────────
        if self._hover_x is None or not (0 <= self._hover_x < w):
            return
        hx_in = self._hover_x
        best_i, best_dx = 0, abs(pts[0][0] - hx_in)
        for i, (px, _) in enumerate(pts):
            dx = abs(px - hx_in)
            if dx < best_dx:
                best_i, best_dx = i, dx
        hx, hy = pts[best_i]
        cross = QColor(T['TEXT']); cross.setAlpha(120)
        p.setPen(QPen(cross, 1))
        p.drawLine(hx, 0, hx, h)
        # dot on series 1
        p.setBrush(QColor(self.color))
        p.setPen(QPen(QColor(T['BG']), 1.2))
        p.drawEllipse(hx - 3, hy - 3, 6, 6)
        # dot on series 2
        if pts2 and best_i < len(pts2):
            hx2, hy2 = pts2[best_i]
            p.setBrush(QColor(self.color2))
            p.setPen(QPen(QColor(T['BG']), 1.2))
            p.drawEllipse(hx2 - 3, hy2 - 3, 6, 6)
        if has2:
            v1 = self.data[best_i]
            v2 = self.data2[best_i] if best_i < len(self.data2) else 0
            t1 = f'{self.label1}{self.fmt(v1)}' if self.label1 else self.fmt(v1)
            t2 = f'{self.label2}{self.fmt(v2)}' if self.label2 else self.fmt(v2)
            text = f'{t1} · {t2}'
        else:
            text = self.fmt(self.data[best_i])
        p.setFont(QFont(FONT, 8, QFont.Bold))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text) + 8
        th = fm.height() + 2
        lx = hx + 6
        if lx + tw > w:
            lx = hx - tw - 6
        ly = max(0, min(hy, hy2 if pts2 else hy) - th - 2)
        bg = QColor(T['CARD']); bg.setAlpha(230)
        p.setPen(QPen(QColor(T['BORDER']), 1))
        p.setBrush(bg)
        p.drawRoundedRect(lx, ly, tw, th, 0, 0)
        p.setPen(QPen(QColor(T['TEXT'])))
        p.drawText(lx + 4, ly + fm.ascent() + 1, text)


class CpuCoreGrid(QWidget):
    """Per-core utilization as the design system's .coregrid heatmap:
    square cells, fill graded by load (idle/med/hi/max), core index
    centred in each cell. Hover a cell for "CORE N · xx%"."""

    GAP = 2
    MIN_CELL = 18
    MAX_CELL = 34

    def __init__(self, count):
        super().__init__()
        self.count = count
        self.values = [0] * count
        self.setMouseTracking(True)
        self._hover_i = None
        self.setStyleSheet('background: transparent; border: none;')
        self.setMinimumHeight(self.MIN_CELL)

    def set_values(self, vals):
        self.values = vals[:self.count] + [0] * (self.count - len(vals))
        self.update()

    def _layout(self):
        w = max(1, self.width())
        n = max(1, self.count)
        gap = self.GAP
        cell = (w - gap * (n - 1)) // n
        if cell >= self.MIN_CELL:
            cols = n
            cell = min(self.MAX_CELL, cell)
        else:
            cell = self.MIN_CELL
            cols = max(1, (w + gap) // (cell + gap))
        rows = (n + cols - 1) // cols
        return cell, gap, cols, rows

    def _sync_height(self):
        cell, gap, cols, rows = self._layout()
        h = rows * cell + (rows - 1) * gap
        if self.height() != h:
            self.setFixedHeight(h)

    def resizeEvent(self, ev):
        self._sync_height()
        super().resizeEvent(ev)

    def _cell_rect(self, i):
        cell, gap, cols, _ = self._layout()
        r, c = divmod(i, cols)
        return c * (cell + gap), r * (cell + gap), cell, cell

    def _index_at(self, pos):
        cell, gap, cols, _ = self._layout()
        step = cell + gap
        c, r = pos.x() // step, pos.y() // step
        if c >= cols or pos.x() - c * step > cell or pos.y() - r * step > cell:
            return None
        i = int(r * cols + c)
        return i if 0 <= i < self.count else None

    def mouseMoveEvent(self, ev):
        self._hover_i = self._index_at(ev.pos())
        self.update()

    def leaveEvent(self, ev):
        self._hover_i = None
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        cell, gap, cols, rows = self._layout()
        p.setFont(QFont(FONT_HUD, max(6, min(8, cell // 3))))
        fm = p.fontMetrics()
        for i, v in enumerate(self.values):
            v = max(0, min(100, v))
            x, y, cw, ch = self._cell_rect(i)
            # .coregrid thresholds: idle / med / hi / max
            if v > 88:
                bg, fg = T['CORE_MAX'], '#000000'
            elif v > 60:
                bg, fg = T['CORE_HI'], '#000000'
            elif v > 25:
                bg, fg = T['CORE_MED'], '#000000'
            else:
                bg, fg = T['CORE_LO'], T['GR_TEXT']
            p.fillRect(x, y, cw, ch, QColor(bg))
            if v > 88:
                # stand-in for the web box-shadow glow on .max cells
                glow = QColor(T['CORE_MAX'])
                glow.setAlpha(110)
                p.setPen(QPen(glow, 1))
                p.setBrush(Qt.NoBrush)
                p.drawRect(x - 1, y - 1, cw + 1, ch + 1)
            text = str(i)
            p.setPen(QPen(QColor(fg)))
            p.drawText(x + (cw - fm.horizontalAdvance(text)) // 2,
                       y + (ch + fm.ascent()) // 2 - 1, text)
        # hover: outline + tooltip
        if self._hover_i is None or self._hover_i >= len(self.values):
            return
        i = self._hover_i
        x, y, cw, ch = self._cell_rect(i)
        hi = QColor(T['TEXT']); hi.setAlpha(160)
        p.setPen(QPen(hi, 1)); p.setBrush(Qt.NoBrush)
        p.drawRect(x, y, cw - 1, ch - 1)
        text = f'CORE {i} · {self.values[i]}%'
        p.setFont(QFont(FONT, 8, QFont.Bold))
        fm2 = p.fontMetrics()
        tw = fm2.horizontalAdvance(text) + 10
        th = fm2.height() + 4
        lx = x + cw + 6
        if lx + tw > self.width():
            lx = max(0, x - tw - 6)
        ly = min(max(0, y), max(0, self.height() - th))
        bg2 = QColor(T['CARD']); bg2.setAlpha(235)
        p.setPen(QPen(QColor(T['BORDER']), 1)); p.setBrush(bg2)
        p.drawRect(lx, ly, tw, th)
        p.setPen(QPen(QColor(T['TEXT'])))
        p.drawText(lx + 5, ly + fm2.ascent() + 2, text)


# ── Process Manager Window ────────────────────────────────────────────

class ProcessManagerWindow(GridBackdrop):
    """Full system process manager with sortable table, search, and kill."""

    _COLS = ['PID', 'Name', 'User', 'CPU %', 'MEM %', 'Memory', 'Status',
             'Command']
    _KEYS = ['pid', 'name', 'user', 'cpu_pct', 'mem_pct', 'mem_kb', 'state',
             'cmdline']
    _STATES = {'R': 'Running', 'S': 'Sleeping', 'D': 'Disk Wait',
               'Z': 'Zombie', 'T': 'Stopped', 't': 'Tracing',
               'X': 'Dead', 'I': 'Idle'}

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle('Process Manager / LOQ Control')
        self.setWindowIcon(_build_icon())
        self.setMinimumSize(960, 640)
        # bg comes from GridBackdrop.paintEvent (dot grid)
        self._prev_times = {}
        self._n_cpus = os.cpu_count() or 1
        self._prev_total = self._read_cpu_total()
        self._sort_col = 3
        self._sort_order = Qt.DescendingOrder
        self._uid_cache = {}
        self._all_procs = []
        self._build_ui()
        self._refresh()
        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh)
        self._timer.start(2000)

    @staticmethod
    def _read_cpu_total():
        try:
            with open('/proc/stat') as f:
                parts = f.readline().split()
            return sum(int(x) for x in parts[1:])
        except Exception:
            return 0

    def _resolve_uid(self, uid):
        if uid not in self._uid_cache:
            try:
                self._uid_cache[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                self._uid_cache[uid] = str(uid)
        return self._uid_cache[uid]

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        title = QLabel('process manager')
        title.setFont(QFont(FONT_DISPLAY, 14))
        title.setStyleSheet(
            f'color: {T["TEXT"]}; letter-spacing: 2px;')
        lay.addWidget(title)

        tb = QHBoxLayout()
        tb.setSpacing(10)
        self._search = QLineEdit()
        self._search.setPlaceholderText('Search processes...')
        self._search.setFont(QFont(FONT, 10))
        self._search.setStyleSheet(f"""
            QLineEdit {{
                background: {T['CARD']}; color: {T['TEXT']};
                border: 1px solid {T['BORDER']}; border-radius: 0px;
                padding: 8px 12px;
            }}
            QLineEdit:focus {{
                border-color: {T['ACCENT']}; background: {T['BTN_HOVER']};
            }}
        """)
        self._search.textChanged.connect(self._apply_filter)
        tb.addWidget(self._search)

        kill_btn = QPushButton('END PROCESS')
        kill_btn.setFont(mkfont(9, bold=True, ls=1.5))
        kill_btn.setCursor(Qt.PointingHandCursor)
        kill_btn.setMinimumHeight(36)
        # ghost-danger (web .btn--danger): red ink, faint red border;
        # hover inverts to red fill / black text
        kill_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: #ff0000;
                border: 1px solid #620d0a; border-radius: 0px;
                padding: 8px 16px;
            }}
            QPushButton:hover {{
                background: #ff0000; color: #000000;
                border-color: #ff0000;
            }}
        """)
        kill_btn.clicked.connect(self._kill_selected)
        tb.addWidget(kill_btn)

        fkill_btn = QPushButton('FORCE KILL')
        fkill_btn.setFont(mkfont(9, bold=True, ls=1.5))
        fkill_btn.setCursor(Qt.PointingHandCursor)
        fkill_btn.setMinimumHeight(36)
        # filled-danger: the destructive primary, red fill / black text
        fkill_btn.setStyleSheet(f"""
            QPushButton {{
                background: #ff0000; color: #000000;
                border: 1px solid #ff0000; border-radius: 0px;
                padding: 8px 16px;
            }}
            QPushButton:hover {{ background: #ff5050; border-color: #ff5050; }}
        """)
        fkill_btn.clicked.connect(lambda: self._kill_selected(force=True))
        tb.addWidget(fkill_btn)

        lay.addLayout(tb)

        self._count_lbl = QLabel('0 processes')
        self._count_lbl.setFont(QFont(FONT, 9))
        self._count_lbl.setStyleSheet(f'color: {T["TEXT_MUTED"]};')
        lay.addWidget(self._count_lbl)

        self._table = QTableWidget()
        self._table.setColumnCount(len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(False)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().sectionClicked.connect(
            self._on_header_click)
        self._table.setColumnWidth(0, 70)
        self._table.setColumnWidth(1, 150)
        self._table.setColumnWidth(2, 80)
        self._table.setColumnWidth(3, 70)
        self._table.setColumnWidth(4, 70)
        self._table.setColumnWidth(5, 90)
        self._table.setColumnWidth(6, 80)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {T['CARD']};
                alternate-background-color: {T['BG']};
                border: 1px solid {T['BORDER']};
                color: {T['TEXT']};
                font-family: '{FONT}'; font-size: 9pt;
            }}
            QTableWidget::item {{ padding: 2px 8px; border: none; }}
            QTableWidget::item:selected {{
                background-color: {T['ACCENT']}; color: {T['BTN_ACT_T']};
            }}
            QHeaderView::section {{
                background-color: {T['CARD']}; color: {T['TEXT_DIM']};
                border: none; border-bottom: 2px solid {T['BORDER']};
                padding: 6px 8px; font-family: '{FONT}';
                font-size: 9pt; font-weight: bold;
            }}
            QHeaderView::section:hover {{ color: {T['TEXT']}; }}
            QScrollBar:vertical {{
                background: {T['CARD']}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {T['ACCENT']}; border-radius: 0px;
                min-height: 30px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)
        lay.addWidget(self._table)

        QShortcut(QKeySequence('Ctrl+F'), self).activated.connect(
            self._search.setFocus)
        QShortcut(QKeySequence('Escape'), self).activated.connect(self.close)
        QShortcut(QKeySequence('Delete'), self).activated.connect(
            self._kill_selected)

    def _get_processes(self):
        total_mem = 1
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        total_mem = int(line.split()[1]); break
        except Exception:
            pass

        total_cpu = self._read_cpu_total()
        total_delta = max(total_cpu - self._prev_total, 1)
        self._prev_total = total_cpu
        new_times = {}
        procs = []

        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f'/proc/{pid}/stat') as f:
                    stat = f.read()
                name = stat[stat.index('(') + 1:stat.rindex(')')]
                fields = stat[stat.rindex(')') + 2:].split()
                cpu_time = int(fields[11]) + int(fields[12])
                new_times[pid] = cpu_time
                prev = self._prev_times.get(pid, cpu_time)
                cpu_pct = max(0.0, (cpu_time - prev)
                              / total_delta * 100)

                rss_kb, uid = 0, 0
                with open(f'/proc/{pid}/status') as f:
                    for sline in f:
                        if sline.startswith('VmRSS:'):
                            rss_kb = int(sline.split()[1])
                        elif sline.startswith('Uid:'):
                            uid = int(sline.split()[1])

                try:
                    with open(f'/proc/{pid}/cmdline') as f:
                        cmdline = f.read().replace('\0', ' ').strip()
                except Exception:
                    cmdline = ''

                procs.append({
                    'pid': pid, 'name': name,
                    'user': self._resolve_uid(uid),
                    'cpu_pct': round(cpu_pct, 1),
                    'mem_pct': round(rss_kb / total_mem * 100, 1),
                    'mem_kb': rss_kb,
                    'state': self._STATES.get(fields[0], fields[0]),
                    'cmdline': cmdline or name,
                })
            except (FileNotFoundError, ProcessLookupError,
                    PermissionError, ValueError, IndexError):
                continue

        self._prev_times = new_times
        return procs

    def _refresh(self):
        self._all_procs = self._get_processes()
        self._update_table()

    def _update_table(self):
        procs = list(self._all_procs)
        key = self._KEYS[self._sort_col]
        rev = self._sort_order == Qt.DescendingOrder
        procs.sort(key=lambda p: (p[key] if not isinstance(p[key], str)
                                  else p[key].lower()), reverse=rev)

        filt = self._search.text().lower()
        if filt:
            procs = [p for p in procs
                     if filt in p['name'].lower()
                     or filt in str(p['pid'])
                     or filt in p['cmdline'].lower()
                     or filt in p['user'].lower()]

        sel_pid = None
        row = self._table.currentRow()
        if row >= 0:
            item = self._table.item(row, 0)
            if item:
                sel_pid = item.data(Qt.UserRole)
        vpos = self._table.verticalScrollBar().value()

        self._table.setRowCount(len(procs))
        new_row = -1
        for i, p in enumerate(procs):
            if p['pid'] == sel_pid:
                new_row = i
            mem_txt = (f'{p["mem_kb"] / 1048576:.1f} GB'
                       if p['mem_kb'] >= 1048576
                       else f'{p["mem_kb"] / 1024:.0f} MB'
                       if p['mem_kb'] >= 1024
                       else f'{p["mem_kb"]} KB')
            vals = [str(p['pid']), p['name'], p['user'],
                    f'{p["cpu_pct"]:.1f}', f'{p["mem_pct"]:.1f}',
                    mem_txt, p['state'], p['cmdline']]
            data = [p['pid'], None, None, p['cpu_pct'], p['mem_pct'],
                    p['mem_kb'], None, None]
            for c, (txt, d) in enumerate(zip(vals, data)):
                it = QTableWidgetItem(txt)
                if d is not None:
                    it.setData(Qt.UserRole, d)
                it.setForeground(QColor(T['TEXT']))
                self._table.setItem(i, c, it)

        if new_row >= 0:
            self._table.selectRow(new_row)
        self._table.verticalScrollBar().setValue(vpos)

        arrows = {Qt.AscendingOrder: ' \u25b2', Qt.DescendingOrder: ' \u25bc'}
        for c, col_name in enumerate(self._COLS):
            lbl = col_name + (arrows[self._sort_order]
                              if c == self._sort_col else '')
            self._table.horizontalHeaderItem(c).setText(lbl)

        total = len(self._prev_times)
        shown = len(procs)
        self._count_lbl.setText(
            f'{shown} / {total} processes (filtered)' if filt
            else f'{total} processes')

    def _on_header_click(self, col):
        if col == self._sort_col:
            self._sort_order = (Qt.AscendingOrder
                                if self._sort_order == Qt.DescendingOrder
                                else Qt.DescendingOrder)
        else:
            self._sort_col = col
            self._sort_order = (Qt.DescendingOrder if col in (0, 3, 4, 5)
                                else Qt.AscendingOrder)
        self._update_table()

    def _apply_filter(self):
        self._update_table()

    def _kill_selected(self, force=False):
        row = self._table.currentRow()
        if row < 0:
            return
        pid_item = self._table.item(row, 0)
        name_item = self._table.item(row, 1)
        if not pid_item:
            return
        pid = pid_item.data(Qt.UserRole) or int(pid_item.text())
        name = name_item.text() if name_item else str(pid)
        verb = 'Force kill' if force else 'End'
        msg = QMessageBox(self)
        msg.setStyleSheet(MSG_STYLE)
        msg.setWindowTitle(f'{verb} Process')
        msg.setText(f'{verb} {name} (PID {pid})?')
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        if msg.exec_() != QMessageBox.Yes:
            return
        sig = 9 if force else 15
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            subprocess.run(['sudo', 'kill', f'-{sig}', str(pid)],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        self._refresh()

    def closeEvent(self, event):
        self._timer.stop()
        event.accept()


# ── Main Window ───────────────────────────────────────────────────────

class LOQControl(QWidget):
    _update_done = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('LOQ Control')
        self.setWindowIcon(_build_icon())
        self.setMinimumSize(820, 640)
        self.resize(1100, 880)
        self.setStyleSheet(f'background-color: {T["BG"]};')
        self.cfg = load_config()
        self.cpu_zone = find_cpu_zone()
        self.bat_path = find_battery()
        self.cpu_freq_paths = sorted(
            glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq'))
        self.n_cores = len(self.cpu_freq_paths) or 1
        self.gpu_power_default = 45
        self._pl_user_set = False
        self._prev_throttle = int(read_sys(
            '/sys/devices/system/cpu/cpu0/thermal_throttle/'
            'package_throttle_count') or '0')
        self._prev_stat = read_cpu_stat()
        self._prev_cores = read_per_core_stats()
        self._prev_net = read_net_stats()
        self._prev_disk = read_disk_stats()
        self._prev_time = time.monotonic()
        # CPU package energy tracking for system power (W) calc.
        # The RAPL energy_uj file is root-only since the PLATYPUS
        # mitigations — chmod it once at startup so refreshes can read
        # it as the user.
        fix_rapl_perms()
        self._cpu_energy_prev = (read_cpu_package_energy_uj(),
                                 time.monotonic())
        self._last_cpu_w = 0.0
        # Last sample-tick timestamps for long-span battery sparklines
        self._battery_spark_last = 0.0
        self.rapl = get_rapl_info()
        self._update_done.connect(self._on_update_done)
        self._proc_manager = None
        log_battery_history(self.bat_path)
        self.initUI()
        self._setup_tray()
        self._setup_shortcuts()
        self._auto_apply_oc()
        # Restore slider positions from config
        pl = self.cfg.get('oc_power_limit', 0)
        gc = self.cfg.get('oc_gpu_clock', 0)
        mc = self.cfg.get('oc_mem_clock_idx', 0)
        # Restore the slider positions silently (blockSignals avoids
        # re-applying the OC), then re-evaluate each AUTO chip against
        # the freshly-loaded value — otherwise the chip stays lit from
        # construction time when the slider sat at its auto default.
        def _sync_auto(btn, slider):
            if btn is not None and hasattr(btn, '_update_active'):
                btn._update_active(slider.value())

        if pl > 0:
            self.pl_slider.blockSignals(True)
            self.pl_slider.setValue(pl)
            self.pl_slider.blockSignals(False)
            self.pl_val.setText(f'{pl} W')
        _sync_auto(getattr(self, 'pl_auto_btn', None), self.pl_slider)
        if gc >= GPU_CLOCK_MIN:
            self.gc_slider.blockSignals(True)
            self.gc_slider.setValue(gc)
            self.gc_slider.blockSignals(False)
            self.gc_val.setText(f'{gc} MHz')
        _sync_auto(getattr(self, 'gc_auto_btn', None), self.gc_slider)
        if mc > 0:
            self.mc_slider.blockSignals(True)
            self.mc_slider.setValue(mc)
            self.mc_slider.blockSignals(False)
            self.mc_val.setText(MEM_LABELS[mc])
        _sync_auto(getattr(self, 'mc_auto_btn', None), self.mc_slider)

    def initUI(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(SCROLL_STYLE)
        container = GridBackdrop()
        root = QVBoxLayout(container)
        root.setSpacing(12)
        root.setContentsMargins(24, 24, 24, 24)

        # web .nav-logo: lowercase major-mono brand with accent dot
        title = QLabel(
            f'loq<span style="color: {T["ACCENT"]};">.</span>control')
        title.setFont(QFont(FONT_DISPLAY, 18))
        title.setStyleSheet(
            f'color: {T["TEXT"]}; background: transparent; letter-spacing: 3px;')
        root.addWidget(title)
        sub = QLabel('LENOVO LOQ // SYSTEM CONTROLS')
        sub.setFont(mkfont(9, ls=2.0, family=FONT_HUD))
        sub.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; background: transparent; '
            f'margin-bottom: 4px; letter-spacing: 2px;')
        root.addWidget(sub)

        g = QGridLayout()
        g.setSpacing(12)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(1, 1)
        row = 0
        g.addWidget(self._build_sensors_card(), row, 0, 1, 2); row += 1
        g.addWidget(self._build_activity_card(), row, 0, 1, 2); row += 1
        g.addWidget(self._build_temp_graph_card(), row, 0, 1, 2); row += 1
        g.addWidget(self._build_battery_card(), row, 0, 1, 2); row += 1
        g.addWidget(self._build_profile_card(), row, 0)
        g.addWidget(self._build_gpu_mode_card(), row, 1); row += 1
        g.addWidget(self._build_overclock_card(), row, 0, 1, 2); row += 1
        g.addWidget(self._build_kbd_card(), row, 0)
        g.addWidget(self._build_toggles_card(), row, 1); row += 1
        g.addWidget(self._build_proc_manager_card(), row, 0, 1, 2); row += 1
        g.addWidget(self._build_updates_card(), row, 0, 1, 2)

        root.addLayout(g)
        root.addStretch()

        # web footer: hairline on top, meta left, accent label link right
        fsep = QFrame()
        fsep.setFixedHeight(1)
        fsep.setStyleSheet(f'background: {T["BORDER"]}; border: none;')
        root.addSpacing(12)
        root.addWidget(fsep)
        frow = QHBoxLayout()
        frow.setContentsMargins(0, 10, 0, 0)
        made = QLabel('MADE BY OJEE')
        made.setFont(mkfont(8, ls=1.5, family=FONT_HUD))
        made.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; background: transparent;')
        link = QLabel(
            f'<a href="https://ojee.net" style="color: {T["ACCENT"]}; '
            f'text-decoration: none;">OJEE.NET</a>')
        link.setFont(mkfont(8, bold=True, ls=2.0))
        link.setOpenExternalLinks(True)
        link.setStyleSheet('background: transparent;')
        frow.addWidget(made)
        frow.addStretch()
        frow.addWidget(link)
        root.addLayout(frow)
        disclaimer = QLabel(
            '\u201cLOQ\u201d and the LOQ logo are trademarks of Lenovo. '
            'Not affiliated with or endorsed by Lenovo.')
        disclaimer.setFont(QFont(FONT, 7))
        disclaimer.setAlignment(Qt.AlignCenter)
        disclaimer.setWordWrap(True)
        disclaimer.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; background: transparent; '
            f'padding: 0 20px 4px 20px;')
        root.addWidget(disclaimer)
        scroll.setWidget(container)
        outer.addWidget(scroll)

        self.refresh_all()
        self.sensor_timer = QTimer()
        self.sensor_timer.timeout.connect(self.refresh_sensors)
        self.sensor_timer.timeout.connect(self.refresh_battery)
        # Performance profile can change out from under us via Fn+Q or an
        # AC plug/unplug — a cheap sysfs read is enough to keep the UI
        # in sync without listening on D-Bus.
        self.sensor_timer.timeout.connect(self.refresh_profile)
        self.sensor_timer.start(3000)

    # ── widget helpers ────────────────────────────────────────────────

    def _btn(self, text, fs=10):
        # ghost button (web .btn--ghost): uppercase tracked label,
        # hover = accent ink + accent border + cyan-tint fill
        b = QPushButton(text.upper())
        b.setMinimumHeight(36)
        b.setFont(mkfont(fs, bold=True, ls=1.2))
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(f"""
            QPushButton {{
                background-color: {T['BTN_DEF']}; color: {T['TEXT_DIM']};
                border: 1px solid {T['BORDER']}; border-radius: 0px;
                padding: 7px 6px;
            }}
            QPushButton:hover {{
                background-color: {T['BTN_HOVER']}; color: {T['ACCENT']};
                border-color: {T['ACCENT']};
            }}
        """)
        return b

    def _header(self, text):
        # web .h2: uppercase 900-weight
        l = QLabel(text.upper())
        l.setFont(mkfont(11, bold=True, ls=1.2))
        l.setStyleSheet(
            f'color: {T["TEXT"]}; border: none; background: transparent;')
        return l

    def _sub_header(self, text):
        l = QLabel(text.upper())
        l.setFont(mkfont(10, bold=True, ls=1.0))
        l.setStyleSheet(
            f'color: {T["TEXT"]}; border: none; background: transparent;')
        return l

    def _info(self, text=''):
        l = QLabel(text)
        l.setFont(QFont(FONT, 9))
        l.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; background: transparent;')
        return l

    def _set_btn_active(self, b, active):
        # Re-applying a stylesheet forces Qt to re-parse and re-polish the
        # widget, which can stall the UI thread mid-scroll. Skip the work
        # when nothing actually changed (refreshes call this every tick).
        if getattr(b, '_active_state', None) is active:
            return
        b._active_state = active
        if active:
            b.setStyleSheet(f"""
                QPushButton {{
                    background-color: {T['BTN_ACT']}; color: {T['BTN_ACT_T']};
                    border: 1px solid {T['BTN_ACT']}; border-radius: 0px;
                    padding: 7px 6px;
                }}
                QPushButton:hover {{
                    background-color: {T['BTN_ACT_H']}; color: {T['BTN_ACT_T']};
                }}
            """)
        else:
            b.setStyleSheet(f"""
                QPushButton {{
                    background-color: {T['BTN_DEF']}; color: {T['TEXT_DIM']};
                    border: 1px solid {T['BORDER']}; border-radius: 0px;
                    padding: 7px 6px;
                }}
                QPushButton:hover {{
                    background-color: {T['BTN_HOVER']}; color: {T['ACCENT']};
                    border-color: {T['ACCENT']};
                }}
            """)

    def _switch(self, w=70):
        b = QPushButton('OFF')
        b.setFixedSize(w, 30)
        b.setFont(mkfont(9, bold=True, ls=1.5))
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _style_switch(self, b, on):
        b.setText('ON' if on else 'OFF')
        # Cache the last applied state — refresh handlers call this every
        # tick; re-applying the stylesheet stalls the UI thread.
        if getattr(b, '_switch_state', None) is on:
            return
        b._switch_state = on
        if on:
            b.setStyleSheet(f"""
                QPushButton {{
                    background-color: {T['TOG_ON']}; color: {T['BTN_ACT_T']};
                    border: 1px solid {T['TOG_ON']}; border-radius: 0px;
                }}
                QPushButton:hover {{
                    background-color: {T['TOG_ON_H']};
                    color: {T['BTN_ACT_T']};
                }}
            """)
        else:
            b.setStyleSheet(f"""
                QPushButton {{
                    background-color: {T['TOG_OFF']}; color: {T['TEXT_MUTED']};
                    border: 1px solid {T['BORDER']}; border-radius: 0px;
                }}
                QPushButton:hover {{
                    background-color: {T['BTN_HOVER']}; color: {T['TEXT_DIM']};
                }}
            """)

    def _wide_bar(self, name, mx):
        row = QHBoxLayout(); row.setSpacing(12)
        l = QLabel(name); l.setFont(QFont(FONT, 9))
        l.setStyleSheet(
            f'color: {T["TEXT_DIM"]}; border: none; background: transparent;')
        l.setFixedWidth(150); row.addWidget(l)
        bar = QProgressBar(); bar.setRange(0, mx); bar.setValue(0)
        bar.setTextVisible(False); bar.setStyleSheet(BAR_STYLE)
        bar.setFixedHeight(10); row.addWidget(bar)
        v = QLabel('--'); v.setFont(QFont(FONT, 10, QFont.Bold))
        v.setStyleSheet(
            f'color: {T["TEXT"]}; border: none; background: transparent;')
        v.setFixedWidth(60); v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(v)
        return row, bar, v

    def _slider_row(self, name, lo, hi, default, suffix=''):
        row = QHBoxLayout(); row.setSpacing(10)
        l = QLabel(name); l.setFont(QFont(FONT, 9))
        l.setStyleSheet(
            f'color: {T["TEXT_DIM"]}; border: none; background: transparent;')
        l.setFixedWidth(150); row.addWidget(l)
        s = NoScrollSlider(Qt.Horizontal)
        s.setRange(lo, hi); s.setValue(default); s.setStyleSheet(SLIDER_STYLE)
        row.addWidget(s)
        v = QLabel(f'{default}{suffix}')
        v.setFont(QFont(FONT, 10, QFont.Bold))
        v.setStyleSheet(
            f'color: {T["TEXT"]}; border: none; background: transparent;')
        v.setFixedWidth(90); v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(v)
        return row, s, v

    def _slider_markers(self, markers, lw=150):
        row = QHBoxLayout()
        row.setContentsMargins(lw + 18, 0, 98, 0)
        row.setSpacing(0)
        prev = 0
        for i, (frac, text) in enumerate(markers):
            gap = frac - prev
            if gap > 0:
                row.addStretch(max(int(gap * 100), 1))
            lbl = QLabel(text)
            lbl.setFont(QFont(FONT, 7))
            lbl.setStyleSheet(
                f'color: {T["TEXT_MUTED"]}; border: none; '
                f'background: transparent;')
            if frac <= 0:
                lbl.setAlignment(Qt.AlignLeft)
            elif frac >= 1.0:
                lbl.setAlignment(Qt.AlignRight)
            else:
                lbl.setAlignment(Qt.AlignCenter)
            row.addWidget(lbl)
            prev = frac
        if markers and markers[-1][0] < 1.0:
            row.addStretch(max(int((1.0 - markers[-1][0]) * 100), 1))
        return row

    def _toggle_row(self, name):
        # web .row: uppercase 900-weight tracked label
        row = QHBoxLayout(); row.setSpacing(12)
        l = QLabel(name.upper()); l.setFont(mkfont(9, bold=True, ls=1.0))
        l.setStyleSheet(
            f'color: {T["TEXT_DIM"]}; border: none; background: transparent;')
        row.addWidget(l); row.addStretch()
        b = self._switch(); row.addWidget(b)
        return row, b

    def _val_row(self, name):
        row = QHBoxLayout(); row.setSpacing(12)
        l = QLabel(name.upper()); l.setFont(mkfont(9, bold=True, ls=1.0))
        l.setStyleSheet(
            f'color: {T["TEXT_DIM"]}; border: none; background: transparent;')
        l.setFixedWidth(150); row.addWidget(l); row.addStretch()
        v = QLabel('--'); v.setFont(QFont(FONT, 9, QFont.Bold))
        v.setStyleSheet(
            f'color: {T["TEXT"]}; border: none; background: transparent;')
        row.addWidget(v)
        return row, v

    # ── card builders ─────────────────────────────────────────────────

    def _card(self):
        c = QFrame(); c.setStyleSheet(CARD_STYLE)
        c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        v = QVBoxLayout(c)
        v.setContentsMargins(16, 14, 16, 14); v.setSpacing(10)
        return c, v

    def _build_sensors_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('Sensors'))

        self._sensor_tiles = {}

        # CPU + GPU subheaders side-by-side
        sub_row = QHBoxLayout(); sub_row.setSpacing(10)
        cpu_sub_w = QWidget()
        cpu_sub_w.setLayout(self._device_subheader(
            'CPU', cpu_model_short() + f' · {self.n_cores} threads'))
        cpu_sub_w.setStyleSheet('background: transparent;')
        gpu_sub_w = QWidget()
        gpu_sub_w.setLayout(self._device_subheader(
            'GPU', gpu_model_short()))
        gpu_sub_w.setStyleSheet('background: transparent;')
        sub_row.addWidget(cpu_sub_w, 1)
        sub_row.addWidget(gpu_sub_w, 1)
        vbox.addLayout(sub_row)

        # Single grid: CPU left col, GPU right col, similar graphs paired
        grid = QGridLayout(); grid.setSpacing(10)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)

        self._sensor_tiles['cpu_util'] = self._make_metric_tile(
            'CPU UTIL', '#00ffff', primary='--%', secondary='peak --',
            fixed_max=100, fmt=lambda v: f'{int(v)}%')
        self._sensor_tiles['uptime'] = self._make_metric_tile(
            'UPTIME', '#cc99ff', primary='--', secondary='since boot')
        # Uptime is monotonic — sparkline would be a flat slope, hide it
        self._sensor_tiles['uptime']['spark'].setVisible(False)
        self._sensor_tiles['cpu_clk'] = self._make_metric_tile(
            'CPU CLOCK', '#88ccff', primary='-- MHz', secondary='',
            fmt=lambda v: f'{int(v)} MHz')
        self._sensor_tiles['cpu_tmp'] = self._make_metric_tile(
            'CPU TEMP', '#ff0000', primary='--°C', secondary='peak --',
            fixed_max=105, fmt=lambda v: f'{int(v)}°C')
        self._sensor_tiles['cpu_fan'] = self._make_metric_tile(
            'CPU FAN', '#44bbaa', primary='-- RPM', secondary='',
            fmt=lambda v: f'{int(v)} RPM')
        self._sensor_tiles['gpu_util'] = self._make_metric_tile(
            'GPU UTIL', '#88aa44', primary='--%', secondary='peak --',
            fixed_max=100, fmt=lambda v: f'{int(v)}%')
        self._sensor_tiles['gpu_mem'] = self._make_metric_tile(
            'GPU MEM CLK', '#cc99ff', primary='-- MHz', secondary='',
            fmt=lambda v: f'{int(v)} MHz')
        self._sensor_tiles['gpu_clk'] = self._make_metric_tile(
            'GPU CLOCK', '#dd9944', primary='-- MHz', secondary='',
            fmt=lambda v: f'{int(v)} MHz')
        self._sensor_tiles['gpu_vram'] = self._make_metric_tile(
            'VRAM USAGE', '#cc99ff', show_bar=True, bar_max=100,
            primary='-- / -- GB', secondary='--',
            fmt=lambda v: f'{v:.1f} GB')
        self._sensor_tiles['loadavg'] = self._make_metric_tile(
            'LOAD AVG', '#88ccff',
            primary='-- · -- · --', secondary='1m · 5m · 15m',
            fmt=lambda v: f'{v:.2f}')
        self._sensor_tiles['gpu_tmp'] = self._make_metric_tile(
            'GPU TEMP', '#ff0000', primary='--°C', secondary='peak --',
            fixed_max=105, fmt=lambda v: f'{int(v)}°C')
        self._sensor_tiles['gpu_fan'] = self._make_metric_tile(
            'GPU FAN', '#44bbaa', primary='-- RPM', secondary='',
            fmt=lambda v: f'{int(v)} RPM')

        # Row 0: UTIL    / UTIL
        # Row 1: POWER   / MEM CLK
        # Row 2: CLOCK   / CLOCK
        # Row 3: TEMP    / TEMP
        # Row 4: FAN     / FAN
        grid.addWidget(self._sensor_tiles['cpu_util']['frame'], 0, 0)
        grid.addWidget(self._sensor_tiles['gpu_util']['frame'], 0, 1)
        grid.addWidget(self._sensor_tiles['uptime']['frame'], 1, 0)
        grid.addWidget(self._sensor_tiles['gpu_mem']['frame'], 1, 1)
        grid.addWidget(self._sensor_tiles['cpu_clk']['frame'], 2, 0)
        grid.addWidget(self._sensor_tiles['gpu_clk']['frame'], 2, 1)
        grid.addWidget(self._sensor_tiles['loadavg']['frame'], 3, 0)
        grid.addWidget(self._sensor_tiles['gpu_vram']['frame'], 3, 1)
        grid.addWidget(self._sensor_tiles['cpu_tmp']['frame'], 4, 0)
        grid.addWidget(self._sensor_tiles['gpu_tmp']['frame'], 4, 1)
        grid.addWidget(self._sensor_tiles['cpu_fan']['frame'], 5, 0)
        grid.addWidget(self._sensor_tiles['gpu_fan']['frame'], 5, 1)
        vbox.addLayout(grid)

        # Per-core bar chart
        cl = QLabel('PER-CORE UTILIZATION')
        cl.setFont(mkfont(8, ls=1.5, family=FONT_HUD))
        cl.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; background: transparent; '
            f'letter-spacing: 1.5px; margin-top: 4px;')
        vbox.addWidget(cl)
        self.core_grid = CpuCoreGrid(self.n_cores)
        vbox.addWidget(self.core_grid)
        return card

    def _build_temp_graph_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('Temperature History'))
        self.temp_graph = TempGraph(60)
        vbox.addWidget(self.temp_graph)
        return card

    def _build_battery_card(self):
        card, vbox = self._card()
        hdr = QHBoxLayout()
        hdr.addWidget(self._header('Battery'))
        hdr.addStretch()
        self.batt_status_lbl = QLabel('--')
        self.batt_status_lbl.setFont(QFont(FONT, 8, QFont.Bold))
        self.batt_status_lbl.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; '
            f'background: transparent; letter-spacing: 1.5px; '
            f'padding: 2px 8px; border-radius: 0px;')
        hdr.addWidget(self.batt_status_lbl)
        vbox.addLayout(hdr)

        # Battery model / design capacity subheader
        bp = self.bat_path
        det = '—'
        if bp:
            model = (read_sys(f'{bp}/model_name') or '').strip()
            mfr = (read_sys(f'{bp}/manufacturer') or '').strip()
            try:
                design_uwh = int(read_sys(f'{bp}/energy_full_design') or '0')
                if design_uwh > 0:
                    design_wh = design_uwh / 1e6
                    parts = [model] if model else []
                    if mfr and mfr not in (model or ''):
                        parts.append(mfr)
                    parts.append(f'{design_wh:.0f} Wh design')
                    det = ' · '.join(parts)
            except (ValueError, TypeError):
                pass
        self._battery_subheader = self._device_subheader('CELL', det)
        vbox.addLayout(self._battery_subheader)

        # Tile grid
        grid = QGridLayout(); grid.setSpacing(10)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
        self._batt_tiles = {}
        self._batt_tiles['charge'] = self._make_metric_tile(
            'CHARGE', '#88aa44', show_bar=True, bar_max=100,
            primary='--%', secondary='--',
            fixed_max=100, fmt=lambda v: f'{int(v)}%')
        # Battery's own power_now reading — what the battery is sourcing
        # or sinking. Will be tiny on AC (just trickle maintenance),
        # meaningful when discharging.
        self._batt_tiles['batt_draw'] = self._make_metric_tile(
            'BATTERY DRAW', '#ffd700',
            primary='-- W', secondary='peak --',
            fmt=lambda v: f'{v:.2f} W')
        # CPU package (RAPL) + GPU (nvidia-smi power.draw) — the bulk
        # of what's actually being consumed by the system, regardless
        # of whether AC is plugged in.
        self._batt_tiles['system_power'] = self._make_metric_tile(
            'SYSTEM POWER', '#ffaacc',
            primary='-- W', secondary='CPU -- · GPU --',
            fmt=lambda v: f'{v:.1f} W')
        self._batt_tiles['health'] = self._make_metric_tile(
            'HEALTH', '#44bbaa', show_bar=True, bar_max=100,
            primary='--%', secondary='--',
            fixed_max=100, fmt=lambda v: f'{int(v)}%')
        # Seed health sparkline with historical readings so it has a curve.
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE) as f:
                    pts = []
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        parts = line.split(',')
                        if len(parts) >= 2:
                            try:
                                pts.append(float(parts[1]))
                            except ValueError:
                                pass
                    for v in pts[-60:]:
                        self._batt_tiles['health']['spark'].add(v)
        except Exception:
            pass
        # CHARGE | HEALTH / BATTERY DRAW | SYSTEM POWER
        grid.addWidget(self._batt_tiles['charge']['frame'], 0, 0)
        grid.addWidget(self._batt_tiles['health']['frame'], 0, 1)
        grid.addWidget(self._batt_tiles['batt_draw']['frame'], 1, 0)
        grid.addWidget(self._batt_tiles['system_power']['frame'], 1, 1)
        vbox.addLayout(grid)

        # History caption
        self.batt_hist_lbl = self._info('')
        self.batt_hist_lbl.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; '
            f'background: transparent; padding-top: 4px;')
        vbox.addWidget(self.batt_hist_lbl)
        hist = battery_history_summary()
        if hist:
            self.batt_hist_lbl.setText(
                f'// tracking since {hist["since"]} · {hist["n"]} entries · '
                f'started at {hist["first_h"]}% health')

        sep = QFrame(); sep.setFixedHeight(1)
        sep.setStyleSheet(f'background: {T["BORDER"]}; border: none;')
        vbox.addWidget(sep)

        cr = QHBoxLayout(); cr.setSpacing(12)
        ct = QVBoxLayout(); ct.setSpacing(2)
        t = QLabel('CONSERVATION MODE')
        t.setFont(mkfont(10, bold=True, ls=1.0))
        t.setStyleSheet(
            f'color: {T["TEXT"]}; border: none; background: transparent;')
        d = QLabel('Keeps battery between 75-80% to extend lifespan')
        d.setFont(QFont(FONT, 8))
        d.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; background: transparent;')
        ct.addWidget(t); ct.addWidget(d)
        cr.addLayout(ct); cr.addStretch()
        self.cons_btn = self._switch(70)
        self.cons_btn.clicked.connect(self.toggle_conservation)
        cr.addWidget(self.cons_btn)
        vbox.addLayout(cr)
        return card

    def _build_profile_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('Performance'))
        self.profile_status = self._info()
        vbox.addWidget(self.profile_status)
        g = QGridLayout(); g.setSpacing(6)
        self.profile_btns = {}
        for i, (key, label) in enumerate([
                ('quiet', 'Quiet'), ('balanced', 'Balanced'),
                ('balanced-performance', 'Balanced Performance'),
                ('performance', 'Performance')]):
            b = self._btn(label, fs=9)
            b.clicked.connect(lambda _, m=key: self.set_profile(m))
            g.addWidget(b, i // 2, i % 2)
            self.profile_btns[key] = b
        vbox.addLayout(g)

        if self.rapl:
            sep = QFrame(); sep.setFixedHeight(1)
            sep.setStyleSheet(f'background: {T["BORDER"]}; border: none;')
            vbox.addWidget(sep)
            tdp_max = self.rapl[1]
            tdp_default = self.rapl[0]
            # Reasonable mid-range tick
            mid = (tdp_max // 10) * 5  # round down to a multiple of 5
            tdp_panel, self.tdp_slider, self.tdp_val, self.tdp_auto_btn = (
                self._oc_panel(
                    'CPU POWER LIMIT', 5, tdp_max, tdp_default,
                    value_fmt=lambda v: f'{int(v)} W',
                    snap=5, tick_interval=5, major_tick_every=25,
                    ticks=[(5, '5'), (25, '25'),
                           (mid, str(mid)),
                           (tdp_max, str(tdp_max))],
                    hint=f'5 W steps · current default {tdp_default} W · '
                         f'AUTO = factory default',
                    auto_value=tdp_default,
                    on_auto=self._cpu_tdp_auto))
            self.tdp_slider.valueChanged.connect(
                lambda v: self.tdp_val.setText(f'{v} W'))
            self.tdp_slider.sliderReleased.connect(self._apply_tdp)
            vbox.addWidget(tdp_panel)

        vbox.addStretch()
        return card

    def _build_gpu_mode_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('GPU Mode'))
        self.gpu_mode_status = self._info()
        vbox.addWidget(self.gpu_mode_status)
        row = QHBoxLayout(); row.setSpacing(6)
        self.gpu_mode_btns = {}
        for key, label in [('on-demand', 'Hybrid'), ('intel', 'Intel'),
                           ('nvidia', 'NVIDIA')]:
            b = self._btn(label)
            b.clicked.connect(lambda _, m=key: self.set_gpu_mode(m))
            row.addWidget(b); self.gpu_mode_btns[key] = b
        vbox.addLayout(row)
        note = QLabel('Requires reboot to take effect')
        note.setFont(QFont(FONT, 8))
        note.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; background: transparent;')
        vbox.addWidget(note)
        vbox.addStretch()
        return card

    def _build_overclock_card(self):
        card, vbox = self._card()
        hdr = QHBoxLayout()
        hdr.addWidget(self._header('GPU Overclock'))
        hdr.addStretch()
        note = QLabel('// resets on reboot')
        note.setFont(mkfont(8, ls=1.0, family=FONT_HUD))
        note.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; '
            f'background: transparent; letter-spacing: 1px;')
        hdr.addWidget(note)
        vbox.addLayout(hdr)
        self.oc_status = self._info()
        vbox.addWidget(self.oc_status)

        # \u2500\u2500 Power Limit \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        pl_panel, self.pl_slider, self.pl_val, self.pl_auto_btn = (
            self._oc_panel(
                'POWER LIMIT', 45, 100, 45,
                value_fmt=lambda v: f'{int(v)} W',
                snap=5, tick_interval=5, major_tick_every=15,
                ticks=[(45, '45'), (60, '60'), (75, '75'),
                       (90, '90'), (100, '100')],
                hint='5 W steps \u00b7 45 W base \u00b7 100 W vBIOS max \u00b7 '
                     'AUTO = factory default',
                auto_value=45,  # placeholder \u2014 updated by refresh_overclock
                on_auto=self._gpu_pl_auto))
        self.pl_slider.valueChanged.connect(
            lambda v: self.pl_val.setText(f'{v} W'))
        self.pl_slider.sliderReleased.connect(self._apply_power_limit)
        vbox.addWidget(pl_panel)

        # \u2500\u2500 GPU Clock Min \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        gc_panel, self.gc_slider, self.gc_val, self.gc_auto_btn = self._oc_panel(
            'GPU CLOCK MIN', 0, 3090, 0,
            value_fmt=lambda v: ('Auto' if v < GPU_CLOCK_MIN
                                 else f'{int(v)} MHz'),
            snap_fn=_gpu_clock_snap, tick_interval=200,
            major_tick_every=200,
            ticks=[(0, 'Auto'),
                   (1000, '1000'), (1500, '1500'),
                   (2000, '2000'), (2500, '2500'), (3090, '3090')],
            hint='5 MHz steps \u00b7 lock 2000\u20132500 to stop idle downclocks',
            auto_value=0, on_auto=self._apply_gpu_clock)
        self.gc_slider.valueChanged.connect(self._on_gc_change)
        self.gc_slider.sliderReleased.connect(self._apply_gpu_clock)
        vbox.addWidget(gc_panel)

        # \u2500\u2500 Memory Clock \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        mc_panel, self.mc_slider, self.mc_val, self.mc_auto_btn = self._oc_panel(
            'MEMORY CLOCK', 0, 3, 0,
            value_fmt=lambda v: MEM_LABELS[int(v)],
            snap=1, tick_interval=1, major_tick_every=1,
            ticks=[(0, 'Auto'),
                   (1, '9 GHz'), (2, '11 GHz'), (3, '12 GHz')],
            hint='Discrete steps \u00b7 11\u201312 GHz for max bandwidth',
            auto_value=0, on_auto=self._apply_mem_clock)
        self.mc_slider.valueChanged.connect(self._on_mc_change)
        self.mc_slider.sliderReleased.connect(self._apply_mem_clock)
        vbox.addWidget(mc_panel)

        # \u2500\u2500 Actions row \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        actions = QHBoxLayout(); actions.setSpacing(10)
        reset = self._btn('Reset to Defaults')
        reset.clicked.connect(self._reset_overclock)
        actions.addWidget(reset)
        vbox.addLayout(actions)

        sep = QFrame(); sep.setFixedHeight(1)
        sep.setStyleSheet(f'background: {T["BORDER"]}; border: none;')
        vbox.addWidget(sep)

        ar, self.auto_oc_sw = self._toggle_row('Auto-apply on startup')
        self.auto_oc_sw.clicked.connect(self._toggle_auto_oc)
        self._style_switch(self.auto_oc_sw, self.cfg.get('auto_apply_oc', False))
        vbox.addLayout(ar)
        vbox.addWidget(self._info(
            'Saves current overclock and applies it when the app starts'))
        return card

    def _oc_panel(self, title, lo, hi, default, value_fmt,
                  snap=None, snap_fn=None, tick_interval=None,
                  major_tick_every=None, ticks=None, hint=None,
                  auto_value=None, on_auto=None):
        """Build a card-style overclock control panel.

        ticks: list of (value, label) drawn under the slider, aligned to
        the slider's actual track geometry via SliderTicks.
        auto_value / on_auto: enable an "Auto" chip in the header that
        sets the slider to auto_value and (optionally) calls on_auto().
        """
        panel = QFrame()
        panel.setStyleSheet(
            f'QFrame {{ background: {T["BG"]}; '
            f'border: 1px solid {T["BORDER"]}; border-radius: 0px; }}')
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 12, 14, 12); lay.setSpacing(8)

        hdr = QHBoxLayout(); hdr.setSpacing(8)
        name = QLabel(title)
        name.setFont(QFont(FONT, 8, QFont.Bold))
        name.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; '
            f'background: transparent; letter-spacing: 1.5px;')
        hdr.addWidget(name)
        hdr.addStretch()
        auto_btn = None
        if auto_value is not None:
            auto_btn = QPushButton('AUTO')
            auto_btn.setFont(QFont(FONT, 7, QFont.Bold))
            auto_btn.setCursor(Qt.PointingHandCursor)
            auto_btn.setFixedHeight(20)
            # Pre-baked stylesheets for the two states. Swapped directly
            # via setStyleSheet — Qt re-applies stylesheets reliably this
            # way; the dynamic-property approach (setProperty + polish)
            # is finicky across themes / Qt versions.
            auto_btn._style_off = (
                f'QPushButton {{ background: transparent; '
                f'color: {T["TEXT_MUTED"]}; '
                f'border: 1px solid {T["BORDER"]}; border-radius: 0px; '
                f'padding: 0 8px; letter-spacing: 1.5px; }} '
                f'QPushButton:hover {{ color: {T["TEXT"]}; '
                f'border-color: {T["TEXT_DIM"]}; }}')
            auto_btn._style_on = (
                f'QPushButton {{ background: {T["BTN_ACT"]}; '
                f'color: {T["BTN_ACT_T"]}; '
                f'border: 1px solid {T["BTN_ACT"]}; border-radius: 0px; '
                f'padding: 0 8px; letter-spacing: 1.5px; }} '
                f'QPushButton:hover {{ background: {T["BTN_ACT_H"]}; }}')
            auto_btn.setStyleSheet(auto_btn._style_off)
            hdr.addWidget(auto_btn)
        val = QLabel(value_fmt(default))
        val.setFont(QFont(FONT, 14, QFont.Bold))
        val.setStyleSheet(
            f'color: {T["TEXT"]}; border: none; background: transparent;')
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        hdr.addWidget(val)
        lay.addLayout(hdr)

        s = NoScrollSlider(Qt.Horizontal)
        s.setRange(lo, hi); s.setValue(default)
        s.setStyleSheet(OC_SLIDER_STYLE)
        if snap is not None:
            s.setSnap(snap)
        if snap_fn is not None:
            s.setSnapFn(snap_fn)
        if tick_interval is not None and tick_interval >= 1:
            s.setTickPosition(QSlider.TicksBelow)
            s.setTickInterval(tick_interval)
            if major_tick_every:
                s.setSingleStep(major_tick_every)
                s.setPageStep(major_tick_every)
        lay.addWidget(s)

        if ticks:
            lay.addWidget(SliderTicks(s, ticks))

        if hint:
            h = QLabel(hint)
            h.setFont(QFont(FONT, 8))
            h.setStyleSheet(
                f'color: {T["TEXT_MUTED"]}; border: none; '
                f'background: transparent;')
            h.setWordWrap(True)
            lay.addWidget(h)

        if auto_btn is not None:
            auto_btn._auto_value = auto_value

            def _update_active(v=None):
                if v is None:
                    v = s.value()
                is_auto = v == auto_btn._auto_value
                auto_btn.setStyleSheet(
                    auto_btn._style_on if is_auto else auto_btn._style_off)
            auto_btn._update_active = _update_active

            def _do_auto():
                s.blockSignals(True)
                s.setValue(auto_btn._auto_value)
                s.blockSignals(False)
                val.setText(value_fmt(s.value()))
                _update_active()
                if on_auto:
                    on_auto()

            s.valueChanged.connect(_update_active)
            s.sliderReleased.connect(_update_active)
            auto_btn.clicked.connect(_do_auto)
            _update_active()

        return panel, s, val, auto_btn

    def _build_proc_manager_card(self):
        card, vbox = self._card()
        hdr = QHBoxLayout()
        hdr.addWidget(self._header('Process Manager'))
        hdr.addStretch()
        btn = self._btn('Open', fs=9)
        btn.setFixedSize(80, 30)
        btn.clicked.connect(self._open_proc_manager)
        hdr.addWidget(btn)
        vbox.addLayout(hdr)
        vbox.addWidget(self._info(
            'System process manager with sorting, search, and process control'))
        return card

    def _open_proc_manager(self):
        if self._proc_manager is None or not self._proc_manager.isVisible():
            self._proc_manager = ProcessManagerWindow(self)
        self._proc_manager.show()
        self._proc_manager.activateWindow()

    def _build_kbd_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('Keyboard Backlight'))
        self.kbd_status = self._info()
        vbox.addWidget(self.kbd_status)
        row = QHBoxLayout(); row.setSpacing(8)
        self.kbd_btns = {}
        for name, val in [('Off', 0), ('Low', 1), ('High', 2)]:
            b = self._btn(name)
            b.clicked.connect(lambda _, v=val: self.set_kbd(v))
            row.addWidget(b); self.kbd_btns[val] = b
        vbox.addLayout(row)
        vbox.addStretch()
        return card

    def _build_toggles_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('Quick Settings'))
        for name, attr, handler in [
                ('Microphone', 'mic_sw', 'toggle_mic'),
                ('FN Lock', 'fn_sw', 'toggle_fn_lock'),
                ('Touchpad', 'tp_sw', 'toggle_touchpad'),
                ('Autostart', 'autostart_sw', 'toggle_autostart')]:
            r, sw = self._toggle_row(name)
            sw.clicked.connect(getattr(self, handler))
            setattr(self, attr, sw)
            vbox.addLayout(r)

        sep = QFrame(); sep.setFixedHeight(1)
        sep.setStyleSheet(f'background: {T["BORDER"]}; border: none;')
        vbox.addWidget(sep)

        tr = QHBoxLayout(); tr.setSpacing(8)
        tl = QLabel('Theme')
        tl.setFont(QFont(FONT, 9))
        tl.setStyleSheet(
            f'color: {T["TEXT_DIM"]}; border: none; background: transparent;')
        tr.addWidget(tl); tr.addStretch()
        self.theme_btn = self._btn(
            'Light' if self.cfg.get('theme') == 'dark' else 'Dark', fs=9)
        self.theme_btn.setFixedSize(70, 30)
        self.theme_btn.clicked.connect(self.toggle_theme)
        tr.addWidget(self.theme_btn)
        vbox.addLayout(tr)
        vbox.addStretch()
        return card

    def _build_activity_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('Activity Monitor'))

        self._activity_tiles = {}
        self._activity_order = []  # explicit order list
        self.activity_grid = QGridLayout()
        self.activity_grid.setSpacing(10)
        self.activity_grid.setColumnStretch(0, 1)
        self.activity_grid.setColumnStretch(1, 1)

        # Memory subheader
        try:
            with open('/proc/meminfo') as f:
                total_kb = 0
                for line in f:
                    if line.startswith('MemTotal:'):
                        total_kb = int(line.split()[1]); break
            ram_total_str = f'{round(total_kb / 1048576)} GB'
        except Exception:
            ram_total_str = ''
        vbox.addLayout(self._device_subheader('MEMORY', ram_total_str))

        self._activity_tiles['ram'] = self._make_metric_tile(
            'RAM', '#88ccff', show_bar=True, bar_max=100,
            primary='-- / -- GB', secondary='--', fixed_max=100,
            fmt=lambda v: f'{int(v)}%')
        self._activity_order.append('ram')
        # Swap is added lazily by refresh if SwapTotal > 0

        self._mem_grid = QGridLayout(); self._mem_grid.setSpacing(10)
        self._mem_grid.setColumnStretch(0, 1); self._mem_grid.setColumnStretch(1, 1)
        vbox.addLayout(self._mem_grid)

        # Storage subheader — populated adaptively per drive
        self._storage_header = self._device_subheader('STORAGE', '')
        vbox.addLayout(self._storage_header)
        self._storage_grid = QGridLayout(); self._storage_grid.setSpacing(10)
        self._storage_grid.setColumnStretch(0, 1); self._storage_grid.setColumnStretch(1, 1)
        vbox.addLayout(self._storage_grid)
        self._drive_tiles = {}  # dev -> tile dict
        self._prev_drive_stats = {}  # dev -> (r, w)

        # Network subheader — populated adaptively per interface
        self._network_header = self._device_subheader('NETWORK', '')
        vbox.addLayout(self._network_header)
        self._network_grid = QGridLayout(); self._network_grid.setSpacing(10)
        self._network_grid.setColumnStretch(0, 1); self._network_grid.setColumnStretch(1, 1)
        vbox.addLayout(self._network_grid)
        self._nic_tiles = {}  # iface -> tile dict
        self._prev_nic_stats = {}  # iface -> (rx, tx)

        self._layout_memory_tiles()
        return card

    def _layout_memory_tiles(self):
        self._fill_paired_grid(
            self._mem_grid,
            [self._activity_tiles[k]['frame']
             for k in self._activity_order
             if k in self._activity_tiles])

    def _fill_paired_grid(self, grid, frames):
        """Pack frames into a 2-col grid; last odd one spans both columns
        so we never leave a visible empty slot beside it."""
        while grid.count():
            it = grid.takeAt(0)
            w = it.widget()
            if w is not None:
                grid.removeWidget(w)
        n = len(frames)
        i = 0
        for idx, frame in enumerate(frames):
            last = (idx == n - 1)
            row, col = i // 2, i % 2
            if last and col == 0:
                grid.addWidget(frame, row, 0, 1, 2)
                i += 2
            else:
                grid.addWidget(frame, row, col)
                i += 1

    def _ensure_drive_tile(self, dev, model, size):
        if dev in self._drive_tiles:
            return self._drive_tiles[dev]
        size_str = fmt_bytes(size)
        label = f'/dev/{dev}'.upper()
        primary = '#88ccff'  # read
        secondary_line = QColor(primary).lighter(140).name()  # write
        tile = self._make_metric_tile(
            label, primary,
            primary='R -- · W --',
            secondary=(model[:24] + '…') if len(model) > 25 else (model or size_str),
            fmt=lambda v: fmt_rate(int(v)),
            color2=secondary_line, label1='R ', label2='W ')
        # Capacity bar shows used %; rates feed sparkline
        tile['cap_bar'] = QProgressBar()
        tile['cap_bar'].setRange(0, 100); tile['cap_bar'].setTextVisible(False)
        tile['cap_bar'].setFixedHeight(4)
        tile['cap_bar'].setStyleSheet(
            f'QProgressBar {{ background: {T["BORDER"]}; border: none; '
            f'border-radius: 0px; }} '
            f'QProgressBar::chunk {{ background: #88ccff; '
            f'border-radius: 0px; }}')
        tile['cap_label'] = QLabel('-- / -- · --°C')
        tile['cap_label'].setFont(QFont(FONT, 8))
        tile['cap_label'].setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; '
            f'background: transparent;')
        lay = tile['frame'].layout()
        lay.insertWidget(lay.count() - 1, tile['cap_bar'])
        lay.insertWidget(lay.count() - 1, tile['cap_label'])
        tile['size'] = size
        tile['model'] = model
        self._drive_tiles[dev] = tile
        return tile

    def _ensure_nic_tile(self, iface, kind, ssid, ip):
        if iface in self._nic_tiles:
            tile = self._nic_tiles[iface]
            # update info line if changed
            tile['info'].setText(self._nic_info_text(kind, ssid, ip))
            return tile
        color = {'wifi': '#88aa44', 'eth': '#88ccff',
                 'vpn': '#cc99ff'}.get(kind, '#8a8478')
        color_up = QColor(color).lighter(140).name()
        label = iface.upper()
        tile = self._make_metric_tile(
            label, color, primary='↓ -- · ↑ --',
            secondary=kind.upper(),
            fmt=lambda v: fmt_rate(int(v)),
            color2=color_up, label1='↓ ', label2='↑ ')
        info = QLabel(self._nic_info_text(kind, ssid, ip))
        info.setFont(QFont(FONT, 8))
        info.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; '
            f'background: transparent;')
        lay = tile['frame'].layout()
        lay.insertWidget(lay.count() - 1, info)
        tile['info'] = info
        tile['kind'] = kind
        self._nic_tiles[iface] = tile
        return tile

    def _nic_info_text(self, kind, ssid, ip):
        bits = []
        if kind == 'wifi' and ssid:
            bits.append(ssid)
        if ip:
            bits.append(ip)
        return ' · '.join(bits) if bits else 'no address'

    def _relayout_drive_tiles(self):
        keys = sorted(self._drive_tiles.keys())
        self._fill_paired_grid(
            self._storage_grid,
            [self._drive_tiles[k]['frame'] for k in keys])

    def _relayout_nic_tiles(self):
        # ordering: physical first (wifi, eth), then vpn, then other
        order_key = {'wifi': 0, 'eth': 1, 'vpn': 2, 'other': 3}
        keys = sorted(self._nic_tiles.keys(),
                      key=lambda k: (order_key.get(self._nic_tiles[k].get('kind', 'other'), 9), k))
        self._fill_paired_grid(
            self._network_grid,
            [self._nic_tiles[k]['frame'] for k in keys])

    def _device_subheader(self, name, detail):
        row = QHBoxLayout(); row.setSpacing(8)
        nm = QLabel(name)
        nm.setFont(QFont(FONT, 9, QFont.Bold))
        nm.setStyleSheet(
            f'color: {T["TEXT"]}; border: none; background: transparent; '
            f'letter-spacing: 2px;')
        row.addWidget(nm)
        dt = QLabel(detail)
        dt.setFont(QFont(FONT, 8))
        dt.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; '
            f'background: transparent;')
        row.addWidget(dt)
        row.addStretch()
        row.detail_label = dt
        return row

    def _set_subheader_detail(self, row, text):
        lbl = getattr(row, 'detail_label', None)
        if lbl is not None:
            lbl.setText(text)

    def _make_metric_tile(self, label, color, show_bar=False, bar_max=100,
                          primary='--', secondary='', fixed_max=None,
                          fmt=None, color2=None, label1='', label2=''):
        frame = QFrame()
        # surfaces lighten as they stack (web: .panel tint over --bg)
        frame.setStyleSheet(
            f'QFrame {{ background: {T["BTN_DEF"]}; '
            f'border: 1px solid {T["BORDER"]}; border-radius: 0px; }}')
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 10, 12, 10); lay.setSpacing(6)
        top = QHBoxLayout(); top.setSpacing(8)
        lbl = QLabel(label)
        lbl.setFont(mkfont(8, ls=1.5, family=FONT_HUD))
        lbl.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; background: transparent; '
            f'letter-spacing: 1.5px;')
        top.addWidget(lbl)
        top.addStretch()
        sec = QLabel(secondary)
        sec.setFont(QFont(FONT, 8))
        sec.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; background: transparent;')
        top.addWidget(sec)
        lay.addLayout(top)
        pri = QLabel(primary)
        pri.setFont(QFont(FONT, 12, QFont.Bold))
        pri.setStyleSheet(
            f'color: {T["TEXT"]}; border: none; background: transparent;')
        lay.addWidget(pri)
        bar = None
        if show_bar:
            bar = QProgressBar()
            bar.setRange(0, bar_max)
            bar.setTextVisible(False)
            bar.setFixedHeight(4)
            bar.setStyleSheet(
                f'QProgressBar {{ background: {T["BORDER"]}; border: none; '
                f'border-radius: 0px; }} '
                f'QProgressBar::chunk {{ background: {color}; '
                f'border-radius: 0px; }}')
            lay.addWidget(bar)
        spark = Sparkline(color=color, height=46, fixed_max=fixed_max,
                          fmt=fmt, color2=color2,
                          label1=label1, label2=label2)
        lay.addWidget(spark)
        return {'frame': frame, 'label': lbl, 'primary': pri,
                'secondary': sec, 'bar': bar, 'spark': spark,
                'peak': 0.0}

    def _build_updates_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('System Info'))
        for name, attr in [('NVIDIA Driver', 'nv_ver'),
                           ('Kernel', 'kern_ver'), ('BIOS', 'bios_ver')]:
            r, v = self._val_row(name)
            setattr(self, attr, v)
            vbox.addLayout(r)
        bot = QHBoxLayout(); bot.setSpacing(12)
        self.check_btn = self._btn('Check Updates')
        self.check_btn.clicked.connect(self.check_updates)
        bot.addWidget(self.check_btn)
        self.update_status = QLabel('')
        self.update_status.setFont(QFont(FONT, 9))
        self.update_status.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; background: transparent;')
        bot.addWidget(self.update_status); bot.addStretch()
        vbox.addLayout(bot)
        return card

    # ── refresh ───────────────────────────────────────────────────────

    def refresh_all(self):
        self.refresh_profile()
        self.refresh_gpu_mode()
        self.refresh_overclock()
        self.refresh_kbd()
        self.refresh_toggles()
        self.refresh_sensors()
        self.refresh_battery()
        self.refresh_sysinfo()

    def refresh_sensors(self):
        # CPU utilization
        cur = read_cpu_stat()
        prev = self._prev_stat; self._prev_stat = cur
        dt = cur[0] - prev[0]; di = cur[1] - prev[1]
        pct = max(0, min(100, round((1 - di / dt) * 100))) if dt > 0 else 0
        self._set_sensor('cpu_util', pct, f'{pct}%', 'peak {p}%')

        # Per-core
        cur_cores = read_per_core_stats()
        prev_cores = self._prev_cores; self._prev_cores = cur_cores
        core_pcts = []
        for i in range(min(len(cur_cores), len(prev_cores))):
            cdt = cur_cores[i][0] - prev_cores[i][0]
            cdi = cur_cores[i][1] - prev_cores[i][1]
            cp = (max(0, min(100, round((1 - cdi / cdt) * 100)))
                  if cdt > 0 else 0)
            core_pcts.append(cp)
        self.core_grid.set_values(core_pcts)

        # CPU clock
        try:
            freq = max(
                (int(read_sys(f) or '0') for f in self.cpu_freq_paths),
                default=0) // 1000
            txt = (f'{freq / 1000:.1f} GHz' if freq >= 1000
                   else f'{freq} MHz')
            self._set_sensor('cpu_clk', freq, txt)
        except (ValueError, TypeError):
            pass

        # CPU temp
        cpu_temp = 0
        try:
            raw = read_sys(f'{self.cpu_zone}/temp')
            cpu_temp = int(raw) // 1000 if raw else 0
            self._set_sensor('cpu_tmp', cpu_temp,
                             f'{cpu_temp}\u00b0C', 'peak {p}\u00b0C')
        except (ValueError, TypeError):
            pass

        # CPU package power (RAPL energy delta \u2192 watts).
        # Shared with the battery card so it doesn't have to recompute.
        cpu_w = 0.0
        e_now = read_cpu_package_energy_uj()
        e_prev, t_prev = self._cpu_energy_prev
        now_mono = time.monotonic()
        if e_now is not None and e_prev is not None:
            dt_e = now_mono - t_prev
            if dt_e > 0.1:
                delta_uj = e_now - e_prev
                if delta_uj < 0:
                    mx = read_cpu_max_energy_uj()
                    if mx:
                        delta_uj += mx
                if delta_uj >= 0:
                    cpu_w = (delta_uj / 1e6) / dt_e
        self._cpu_energy_prev = (e_now, now_mono)
        self._last_cpu_w = cpu_w

        # Uptime
        up_tile = self._sensor_tiles.get('uptime')
        if up_tile is not None:
            try:
                with open('/proc/uptime') as f:
                    sec = float(f.read().split()[0])
                days = int(sec // 86400)
                hours = int((sec % 86400) // 3600)
                mins = int((sec % 3600) // 60)
                if days > 0:
                    up_tile['primary'].setText(f'{days}d {hours}h {mins}m')
                elif hours > 0:
                    up_tile['primary'].setText(f'{hours}h {mins}m')
                else:
                    up_tile['primary'].setText(f'{mins}m')
                boot_t = time.time() - sec
                up_tile['secondary'].setText(
                    f'since {time.strftime("%b %d %H:%M", time.localtime(boot_t))}')
            except Exception:
                pass

        # Fans
        cpu_rpm, gpu_rpm = read_fan_rpm()
        self._set_sensor('cpu_fan', cpu_rpm, f'{cpu_rpm} RPM')
        self._set_sensor('gpu_fan', gpu_rpm, f'{gpu_rpm} RPM')

        # GPU group
        gpu_temp = 0
        vals = nvidia_query([
            'utilization.gpu', 'clocks.current.graphics',
            'clocks.current.memory', 'temperature.gpu',
            'memory.used', 'memory.total'])
        for i, (k, suf, peak_fmt) in enumerate([
                ('gpu_util', '%', 'peak {p}%'),
                ('gpu_clk', ' MHz', None),
                ('gpu_mem', ' MHz', None),
                ('gpu_tmp', '\u00b0C', 'peak {p}\u00b0C')]):
            try:
                v = int(vals[i])
                self._set_sensor(k, v, f'{v}{suf}', peak_fmt)
                if k == 'gpu_tmp':
                    gpu_temp = v
            except (ValueError, IndexError):
                pass

        # VRAM usage (nvidia-smi reports MiB). Spark holds GB so hover
        # shows the absolute number, and we pin fixed_max to total VRAM
        # so the curve doesn't look saturated when only ~1 GB is used.
        try:
            used_mib = int(vals[4]); total_mib = int(vals[5])
            if total_mib > 0:
                used_gb = used_mib / 1024
                total_gb = total_mib / 1024
                pct = round(used_mib / total_mib * 100)
                vram_t = self._sensor_tiles.get('gpu_vram')
                if vram_t is not None:
                    vram_t['spark'].fixed_max = total_gb
                    if vram_t['bar'] is not None:
                        vram_t['bar'].setValue(pct)
                    vram_t['primary'].setText(
                        f'{used_gb:.1f} / {total_gb:.1f} GB')
                    vram_t['secondary'].setText(f'{pct}%')
                    vram_t['spark'].add(used_gb)
        except (ValueError, IndexError, ZeroDivisionError):
            pass

        # Load average (1m / 5m / 15m)
        try:
            with open('/proc/loadavg') as f:
                la = f.read().split()
            l1, l5, l15 = float(la[0]), float(la[1]), float(la[2])
            la_t = self._sensor_tiles.get('loadavg')
            if la_t is not None:
                la_t['primary'].setText(f'{l1:.2f} \u00b7 {l5:.2f} \u00b7 {l15:.2f}')
                la_t['spark'].add(l1)
        except Exception:
            pass

        self.temp_graph.add(cpu_temp, gpu_temp)

        # Throttle — only flag if actively increasing
        cpu_throttling = False
        gpu_throttling = []
        try:
            tc = int(read_sys(
                '/sys/devices/system/cpu/cpu0/thermal_throttle/'
                'package_throttle_count') or '0')
            if tc > self._prev_throttle:
                cpu_throttling = True
            self._prev_throttle = tc
        except Exception:
            pass
        try:
            for reason in ['hw_thermal_slowdown', 'sw_thermal_slowdown',
                           'hw_slowdown']:
                v = nvidia_query(f'clocks_throttle_reasons.{reason}')[0]
                if v == 'Active':
                    gpu_throttling.append(reason.replace('_', ' ').title())
        except Exception:
            pass

        # Inline throttle indicator + temp-based label coloring on temp tiles
        self._update_temp_tile('cpu_tmp', cpu_temp,
                               'CPU Thermal' if cpu_throttling else '')
        self._update_temp_tile('gpu_tmp', gpu_temp,
                               ', '.join(gpu_throttling))

        self.refresh_activity()

    def _update_temp_tile(self, key, temp, throttle_text):
        tile = self._sensor_tiles.get(key)
        if not tile:
            return
        # Threshold-based color: < 75 normal, 75-85 warning, > 85 danger
        if temp >= 85:
            label_color = '#ff0000'
        elif temp >= 75:
            label_color = '#ffd700'
        else:
            label_color = T['TEXT_MUTED']
        # Cache the last applied color/secondary-style so we only re-set
        # the stylesheet when it actually changes — restyling forces Qt
        # to re-parse and re-polish, which stalls scroll on each tick.
        if tile.get('_last_label_color') != label_color:
            tile['label'].setStyleSheet(
                f'color: {label_color}; border: none; background: transparent; '
                f'letter-spacing: 1.5px;')
            tile['_last_label_color'] = label_color
        if throttle_text:
            tile['secondary'].setText(f'THROTTLED · {throttle_text}')
            if tile.get('_last_sec_state') != 'throttle':
                tile['secondary'].setStyleSheet(
                    'color: #ff0000; border: none; background: transparent;')
                tile['_last_sec_state'] = 'throttle'
        else:
            peak = int(tile['peak']) if tile['peak'] else 0
            tile['secondary'].setText(
                f'peak {peak}°C' if peak else 'peak --')
            if tile.get('_last_sec_state') != 'peak':
                tile['secondary'].setStyleSheet(
                    f'color: {T["TEXT_MUTED"]}; border: none; background: transparent;')
                tile['_last_sec_state'] = 'peak'

    def _set_sensor(self, key, value, text, peak_fmt=None):
        tile = self._sensor_tiles.get(key)
        if not tile:
            return
        tile['primary'].setText(text)
        tile['spark'].add(value)
        tile['peak'] = max(tile['peak'], value)
        if peak_fmt:
            tile['secondary'].setText(peak_fmt.format(p=int(tile['peak'])))

    def refresh_activity(self):
        now = time.monotonic()
        elapsed = max(now - self._prev_time, 0.1)
        self._prev_time = now

        # ── Memory ──────────────────────────────────────────────────
        mem = self._read_meminfo()
        ram_total = mem.get('MemTotal', 1)
        ram_avail = mem.get('MemAvailable', 0)
        ram_used = ram_total - ram_avail
        ram_pct = round(ram_used / ram_total * 100) if ram_total else 0
        cached = mem.get('Cached', 0) + mem.get('Buffers', 0)
        ram_tile = self._activity_tiles['ram']
        if ram_tile['bar'] is not None:
            ram_tile['bar'].setValue(ram_pct)
        ram_tile['primary'].setText(
            f'{ram_used / 1048576:.1f} / {ram_total / 1048576:.0f} GB')
        ram_tile['secondary'].setText(
            f'{ram_pct}% · cache {cached / 1048576:.1f} GB')
        ram_tile['spark'].add(ram_pct)

        swap_total = mem.get('SwapTotal', 0)
        swap_free = mem.get('SwapFree', 0)
        if swap_total > 0:
            if 'swap' not in self._activity_tiles:
                self._activity_tiles['swap'] = self._make_metric_tile(
                    'SWAP', '#cc99ff', show_bar=True, bar_max=100,
                    primary='-- / -- GB', secondary='--',
                    fixed_max=100, fmt=lambda v: f'{int(v)}%')
                self._activity_order.append('swap')
                self._layout_memory_tiles()
            swap_used = swap_total - swap_free
            swap_pct = (round(swap_used / swap_total * 100)
                        if swap_total else 0)
            swap_tile = self._activity_tiles['swap']
            if swap_tile['bar'] is not None:
                swap_tile['bar'].setValue(swap_pct)
            swap_tile['primary'].setText(
                f'{swap_used / 1048576:.1f} / '
                f'{swap_total / 1048576:.0f} GB')
            swap_tile['secondary'].setText(f'{swap_pct}%')
            swap_tile['spark'].add(swap_pct)
        elif 'swap' in self._activity_tiles:
            self._activity_tiles['swap']['frame'].setParent(None)
            del self._activity_tiles['swap']
            self._activity_order.remove('swap')
            self._layout_memory_tiles()

        # ── Storage (per-drive) ─────────────────────────────────────
        drives = list_drives()
        current_devs = {d['dev'] for d in drives}
        relayout = False
        for dev in list(self._drive_tiles.keys()):
            if dev not in current_devs:
                self._drive_tiles[dev]['frame'].setParent(None)
                del self._drive_tiles[dev]
                self._prev_drive_stats.pop(dev, None)
                relayout = True
        cur_disk = read_disk_stats()
        for d in drives:
            dev = d['dev']
            if dev not in self._drive_tiles:
                self._ensure_drive_tile(dev, d['model'], d['size'])
                relayout = True
            tile = self._drive_tiles[dev]
            if dev in cur_disk and dev in self._prev_drive_stats:
                pr, pw = self._prev_drive_stats[dev]
                cr, cw = cur_disk[dev]
                r = int(max(0, cr - pr) * 512 / elapsed)
                w = int(max(0, cw - pw) * 512 / elapsed)
                tile['primary'].setText(
                    f'R {fmt_rate(r)} · W {fmt_rate(w)}')
                tile['peak'] = max(tile['peak'], r, w)
                tile['spark'].add_pair(r, w)
            if dev in cur_disk:
                self._prev_drive_stats[dev] = cur_disk[dev]
            used, total = drive_usage(dev)
            tmp = drive_temp(dev)
            cap_str = ('— / —' if total == 0
                       else f'{fmt_bytes(used)} / {fmt_bytes(total)}')
            tmp_str = '' if tmp is None else f' · {tmp}°C'
            tile['cap_label'].setText(cap_str + tmp_str)
            if total > 0:
                tile['cap_bar'].setValue(round(used / total * 100))
        if relayout:
            self._relayout_drive_tiles()
        if drives:
            total_size = sum(d['size'] for d in drives)
            n = len(drives)
            self._set_subheader_detail(
                self._storage_header,
                f'{n} drive{"s" if n != 1 else ""} · {fmt_bytes(total_size)}')
        else:
            self._set_subheader_detail(self._storage_header, 'none')

        # ── Network (per-NIC) ───────────────────────────────────────
        nics = list_active_nics()
        current_ifaces = {n['iface'] for n in nics}
        relayout = False
        for iface in list(self._nic_tiles.keys()):
            if iface not in current_ifaces:
                self._nic_tiles[iface]['frame'].setParent(None)
                del self._nic_tiles[iface]
                self._prev_nic_stats.pop(iface, None)
                relayout = True
        cur_net = read_net_stats()
        for n in nics:
            iface = n['iface']
            existed = iface in self._nic_tiles
            self._ensure_nic_tile(iface, n['kind'], n['ssid'], n['ip'])
            if not existed:
                relayout = True
            tile = self._nic_tiles[iface]
            if iface in cur_net and iface in self._prev_nic_stats:
                prx, ptx = self._prev_nic_stats[iface]
                crx, ctx = cur_net[iface]
                rx = int(max(0, crx - prx) / elapsed)
                tx = int(max(0, ctx - ptx) / elapsed)
                tile['primary'].setText(
                    f'↓ {fmt_rate(rx)} · ↑ {fmt_rate(tx)}')
                tile['spark'].add_pair(rx, tx)
            if iface in cur_net:
                self._prev_nic_stats[iface] = cur_net[iface]
        if relayout:
            self._relayout_nic_tiles()
        if nics:
            self._set_subheader_detail(
                self._network_header, f'{len(nics)} active')
        else:
            self._set_subheader_detail(self._network_header, 'offline')

    def _read_meminfo(self):
        info = {}
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    p = line.split()
                    if not p:
                        continue
                    key = p[0].rstrip(':')
                    if key in ('MemTotal', 'MemAvailable', 'Cached',
                               'Buffers', 'SwapTotal', 'SwapFree'):
                        info[key] = int(p[1])
        except Exception:
            pass
        return info

    def refresh_profile(self):
        p = read_sys('/sys/firmware/acpi/platform_profile')
        self.profile_status.setText(p.upper().replace('-', ' '))
        for k, b in self.profile_btns.items():
            self._set_btn_active(b, k == p)

    def refresh_gpu_mode(self):
        mode = run_output(['prime-select', 'query'])
        names = {'on-demand': 'HYBRID', 'intel': 'INTEL ONLY',
                 'nvidia': 'NVIDIA ONLY'}
        self.gpu_mode_status.setText(names.get(mode, mode.upper()))
        for k, b in self.gpu_mode_btns.items():
            self._set_btn_active(b, k == mode)

    def refresh_overclock(self):
        info = get_gpu_power()
        cur = info['current']
        if cur == 0:
            self.oc_status.setText('GPU not active')
            self.pl_slider.setEnabled(False)
            self.gc_slider.setEnabled(False)
            self.mc_slider.setEnabled(False)
            return
        self.pl_slider.setEnabled(True)
        self.gc_slider.setEnabled(True)
        self.mc_slider.setEnabled(True)
        self.gpu_power_default = int(info['default'])
        self.pl_slider.setRange(round(info['min']), round(info['max']))
        ci = round(cur)
        if not self._pl_user_set:
            self.pl_slider.blockSignals(True)
            self.pl_slider.setValue(ci)
            self.pl_slider.blockSignals(False)
            self.pl_val.setText(f'{ci} W')
        self._pl_user_set = False
        # Keep the AUTO chip's reset target in sync with the live default.
        # Compare against the slider's actual current value — NOT the
        # nvidia-smi readback, which can lag right after we apply a new
        # power limit and would otherwise spuriously light up the chip.
        if getattr(self, 'pl_auto_btn', None) is not None:
            self.pl_auto_btn._auto_value = round(info['default'])
            self.pl_auto_btn._update_active(self.pl_slider.value())
        self.oc_status.setText(
            f'// {ci}W of {int(info["max"])}W max '
            f'· default {int(info["default"])}W')

    def refresh_kbd(self):
        b = int(read_sys(
            '/sys/class/leds/platform::kbd_backlight/brightness') or '0')
        self.kbd_status.setText({0: 'OFF', 1: 'LOW', 2: 'HIGH'}.get(b, '?'))
        for v, btn in self.kbd_btns.items():
            self._set_btn_active(btn, v == b)

    def refresh_toggles(self):
        mic = 'yes' in run_output(
            ['pactl', 'get-source-mute', '@DEFAULT_SOURCE@'])
        self._style_switch(self.mic_sw, not mic)
        self._style_switch(self.fn_sw,
                           read_sys(f'{IDEAPAD}/fn_lock') == '1')
        tp = run_output(['gsettings', 'get',
                         'org.gnome.desktop.peripherals.touchpad',
                         'send-events'])
        self._style_switch(self.tp_sw, tp == "'enabled'")
        self._style_switch(self.autostart_sw,
                           os.path.exists(AUTOSTART_FILE))

    def refresh_battery(self):
        bp = self.bat_path
        charge_t = self._batt_tiles['charge']
        bdraw_t = self._batt_tiles['batt_draw']
        syspow_t = self._batt_tiles['system_power']
        health_t = self._batt_tiles['health']

        # Labels update every refresh tick (same cadence as the CPU/GPU
        # tiles); sparkline samples are kept at a long interval so the
        # 60-point series spans hours rather than minutes. The very first
        # refresh always samples so each graph starts with a point.
        BATTERY_SPARK_INTERVAL = 180  # 3 min/sample → ~3 hours of history
        now_mono = time.monotonic()
        do_sample = (self._battery_spark_last == 0.0
                     or now_mono - self._battery_spark_last
                     >= BATTERY_SPARK_INTERVAL)

        # ── System power (CPU + GPU) — works AC or battery ─────────
        # CPU watts are computed in refresh_sensors and cached for reuse.
        cpu_w = getattr(self, '_last_cpu_w', 0.0)
        gpu_w = read_gpu_power_draw() or 0.0
        sys_w = cpu_w + gpu_w
        syspow_t['primary'].setText(f'{sys_w:.1f} W')
        syspow_t['secondary'].setText(
            f'CPU {cpu_w:.1f} · GPU {gpu_w:.1f}')
        syspow_t['peak'] = max(syspow_t['peak'], sys_w)
        if do_sample:
            syspow_t['spark'].add(sys_w)

        if not bp:
            for t in (charge_t, bdraw_t, health_t):
                t['primary'].setText('--')
                t['secondary'].setText('—')
            if charge_t['bar']: charge_t['bar'].setValue(0)
            if health_t['bar']: health_t['bar'].setValue(0)
            self.batt_status_lbl.setText('—')
            self.batt_status_lbl.setStyleSheet(
                f'color: {T["TEXT_MUTED"]}; border: none; '
                f'background: transparent; letter-spacing: 1.5px; '
                f'padding: 2px 8px; border-radius: 0px;')
            self._style_switch(self.cons_btn, False)
            if do_sample:
                self._battery_spark_last = now_mono
            return

        # ── Status badge ────────────────────────────────────────────
        status = read_sys(f'{bp}/status') or ''
        names = {'Charging': 'CHARGING', 'Discharging': 'ON BATTERY',
                 'Full': 'FULL', 'Not charging': 'IDLE'}
        status_color = {
            'Charging': '#88aa44', 'Discharging': '#ffd700',
            'Full': '#44bbaa', 'Not charging': '#cc99ff',
        }.get(status, T['TEXT_MUTED'])
        self.batt_status_lbl.setText(names.get(status, status.upper() or '—'))
        if getattr(self, '_last_status_color', None) != status_color:
            self.batt_status_lbl.setStyleSheet(
                f'color: {status_color}; border: 1px solid {status_color}; '
                f'background: transparent; letter-spacing: 1.5px; '
                f'padding: 2px 8px; border-radius: 0px;')
            self._last_status_color = status_color

        # ── Charge ──────────────────────────────────────────────────
        try:
            pct = int(read_sys(f'{bp}/capacity') or '0')
        except ValueError:
            pct = 0
        if charge_t['bar'] is not None:
            charge_t['bar'].setValue(min(pct, 100))
        if do_sample:
            charge_t['spark'].add(pct)

        # ── Battery draw + time-to-empty/full ──────────────────────
        pw = read_sys(f'{bp}/power_now')
        try:
            batt_w = int(pw) / 1e6 if pw else 0
        except ValueError:
            batt_w = 0
        en = read_sys(f'{bp}/energy_now')
        eta = ''
        if status == 'Discharging' and en and batt_w > 0:
            try:
                hrs = int(en) / 1e6 / batt_w
                eta = f'ETA {int(hrs)}h {int((hrs % 1) * 60):02d}m'
            except (ValueError, ZeroDivisionError):
                pass
        elif status == 'Charging' and en and batt_w > 0:
            ef = read_sys(f'{bp}/energy_full')
            try:
                rem = (int(ef) - int(en)) / 1e6
                hrs = rem / batt_w
                eta = f'{int(hrs)}h {int((hrs % 1) * 60):02d}m to full'
            except (ValueError, TypeError, ZeroDivisionError):
                pass
        elif status == 'Full':
            eta = 'plugged in'
        charge_t['primary'].setText(f'{pct}%')
        charge_t['secondary'].setText(eta or '—')

        bdraw_t['peak'] = max(bdraw_t['peak'], batt_w)
        bdraw_t['primary'].setText(
            f'{batt_w:.2f} W' if batt_w > 0 else '—')
        bdraw_t['secondary'].setText(
            f'peak {bdraw_t["peak"]:.2f} W' if bdraw_t['peak'] > 0 else '—')
        if do_sample:
            bdraw_t['spark'].add(batt_w)

        # ── Health (+ cycles in secondary) ─────────────────────────
        try:
            full = int(read_sys(f'{bp}/energy_full') or
                       read_sys(f'{bp}/charge_full') or '0')
            design = int(read_sys(f'{bp}/energy_full_design') or
                         read_sys(f'{bp}/charge_full_design') or '1')
            h_pct = round(full / design * 100)
            if health_t['bar'] is not None:
                health_t['bar'].setValue(min(h_pct, 100))
            health_t['primary'].setText(f'{h_pct}%')
            if do_sample:
                health_t['spark'].add(h_pct)
            wh_now = full / 1e6
            wh_design = design / 1e6
            cyc_raw = read_sys(f'{bp}/cycle_count')
            try:
                cyc = int(cyc_raw or '0')
            except ValueError:
                cyc = 0
            secondary_bits = [f'{wh_now:.1f} / {wh_design:.1f} Wh']
            if cyc > 0:
                secondary_bits.append(f'{cyc} cycles')
            health_t['secondary'].setText(' · '.join(secondary_bits))
        except (ValueError, ZeroDivisionError):
            health_t['primary'].setText('—')
            health_t['secondary'].setText('—')

        if do_sample:
            self._battery_spark_last = now_mono

        # ── Conservation toggle ─────────────────────────────────────
        cons = read_sys(f'{IDEAPAD}/conservation_mode') == '1'
        self._style_switch(self.cons_btn, cons)

    def refresh_sysinfo(self):
        v = nvidia_query('driver_version')
        self.nv_ver.setText(v[0] if v[0] else 'N/A')
        self.kern_ver.setText(run_output(['uname', '-r']) or 'N/A')
        self.bios_ver.setText(
            read_sys('/sys/class/dmi/id/bios_version') or 'N/A')

    # ── actions ───────────────────────────────────────────────────────

    def set_profile(self, mode):
        run_cmd(f'echo {mode} > /sys/firmware/acpi/platform_profile')
        self.refresh_profile()

    def set_gpu_mode(self, mode):
        current = run_output(['prime-select', 'query'])
        if mode == current:
            return
        names = {'on-demand': 'Hybrid', 'intel': 'Intel Only',
                 'nvidia': 'NVIDIA Only'}
        reply = QMessageBox.question(
            self, 'GPU Mode',
            f'Switch to {names[mode]}?\n\nA reboot is required.',
            QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            run_cmd(f'prime-select {mode}')
            self.refresh_gpu_mode()
            QMessageBox.information(
                self, 'GPU Mode',
                f'Set to {names[mode]}.\nPlease reboot.')

    def _apply_power_limit(self):
        set_gpu_tgp(self.pl_slider.value())
        self._save_oc_if_auto()
        self._pl_user_set = True
        self.refresh_overclock()

    def _gpu_pl_auto(self):
        target = self.gpu_power_default
        set_gpu_tgp(target)
        # Sync slider + chip directly to the target so we don't race
        # nvidia-smi's apply latency (refresh_overclock would otherwise
        # read a stale PL and briefly desync the UI).
        self.pl_slider.blockSignals(True)
        self.pl_slider.setValue(target)
        self.pl_slider.blockSignals(False)
        self.pl_val.setText(f'{target} W')
        if getattr(self, 'pl_auto_btn', None) is not None:
            self.pl_auto_btn._update_active(target)
        self._pl_user_set = True
        self._save_oc_if_auto()

    def _on_gc_change(self, v):
        self.gc_val.setText('Auto' if v < GPU_CLOCK_MIN else f'{v} MHz')

    # mc label uses the same path as on-init formatter (MEM_LABELS).

    def _apply_gpu_clock(self):
        v = self.gc_slider.value()
        run_cmd('nvidia-smi -rgc' if v < GPU_CLOCK_MIN
                else f'nvidia-smi -lgc {v},{GPU_CLOCK_MAX}')
        self._save_oc_if_auto()

    def _on_mc_change(self, idx):
        self.mc_val.setText(MEM_LABELS[idx])

    def _apply_mem_clock(self):
        freq = MEM_LEVELS[self.mc_slider.value()]
        run_cmd('nvidia-smi -rmc' if freq == 0
                else f'nvidia-smi -lmc {freq},{freq}')
        self._save_oc_if_auto()

    def _reset_overclock(self):
        set_gpu_tgp(self.gpu_power_default)
        run_cmd('nvidia-smi -rgc')
        run_cmd('nvidia-smi -rmc')
        for s, v, txt in [(self.mc_slider, self.mc_val, 'Auto'),
                          (self.gc_slider, self.gc_val, 'Auto')]:
            s.blockSignals(True); s.setValue(0); s.blockSignals(False)
            v.setText(txt)
        self._save_oc_if_auto()
        self.refresh_overclock()

    def _save_oc_if_auto(self):
        self.cfg['oc_power_limit'] = self.pl_slider.value()
        self.cfg['oc_gpu_clock'] = self.gc_slider.value()
        self.cfg['oc_mem_clock_idx'] = self.mc_slider.value()
        save_config(self.cfg)

    def _toggle_auto_oc(self):
        on = not self.cfg.get('auto_apply_oc', False)
        self.cfg['auto_apply_oc'] = on
        if on:
            self.cfg['oc_power_limit'] = self.pl_slider.value()
            self.cfg['oc_gpu_clock'] = self.gc_slider.value()
            self.cfg['oc_mem_clock_idx'] = self.mc_slider.value()
        save_config(self.cfg)
        self._style_switch(self.auto_oc_sw, on)

    def _auto_apply_oc(self):
        pl = self.cfg.get('oc_power_limit', 0)
        gc = self.cfg.get('oc_gpu_clock', 0)
        mc_idx = self.cfg.get('oc_mem_clock_idx', 0)
        if pl > 0:
            set_gpu_tgp(pl)
        if gc >= GPU_CLOCK_MIN:
            run_cmd(f'nvidia-smi -lgc {gc},{GPU_CLOCK_MAX}')
        if mc_idx > 0:
            freq = MEM_LEVELS[mc_idx]
            run_cmd(f'nvidia-smi -lmc {freq},{freq}')

    def _apply_tdp(self):
        v = self.tdp_slider.value()
        run_cmd(
            f'echo {v * 1000000} > '
            f'{RAPL}/constraint_0_power_limit_uw')

    def _cpu_tdp_auto(self):
        v = self.rapl[0] if self.rapl else self.tdp_slider.value()
        run_cmd(
            f'echo {v * 1000000} > '
            f'{RAPL}/constraint_0_power_limit_uw')

    def set_kbd(self, val):
        run_cmd(
            f'echo {val} > /sys/class/leds/platform::kbd_backlight/brightness')
        run_cmd(
            f'echo {val} > /sys/class/leds/platform::kbd_backlight_1/brightness')
        self.refresh_kbd()

    def toggle_mic(self):
        subprocess.run(
            ['pactl', 'set-source-mute', '@DEFAULT_SOURCE@', 'toggle'])
        self.refresh_toggles()

    def toggle_fn_lock(self):
        cur = read_sys(f'{IDEAPAD}/fn_lock')
        run_cmd(f'echo {"0" if cur == "1" else "1"} > {IDEAPAD}/fn_lock')
        self.refresh_toggles()

    def toggle_touchpad(self):
        cur = run_output(['gsettings', 'get',
                          'org.gnome.desktop.peripherals.touchpad',
                          'send-events'])
        new = 'disabled' if cur == "'enabled'" else 'enabled'
        subprocess.run(['gsettings', 'set',
                        'org.gnome.desktop.peripherals.touchpad',
                        'send-events', new])
        self.refresh_toggles()

    def toggle_conservation(self):
        cur = read_sys(f'{IDEAPAD}/conservation_mode')
        run_cmd(
            f'echo {"0" if cur == "1" else "1"} > '
            f'{IDEAPAD}/conservation_mode')
        self.refresh_battery()

    def toggle_autostart(self):
        if os.path.exists(AUTOSTART_FILE):
            try:
                os.remove(AUTOSTART_FILE)
            except Exception:
                pass
        else:
            os.makedirs(AUTOSTART_DIR, exist_ok=True)
            with open(AUTOSTART_FILE, 'w') as f:
                f.write(
                    f'[Desktop Entry]\n'
                    f'Name=LOQ Control Center\n'
                    f'Exec={SCRIPT_PATH} --minimized\n'
                    f'Icon=loq-control\n'
                    f'Terminal=false\n'
                    f'Type=Application\n'
                    f'X-GNOME-Autostart-enabled=true\n')
        self.refresh_toggles()

    def toggle_theme(self):
        new = 'light' if self.cfg.get('theme') == 'dark' else 'dark'
        self.cfg['theme'] = new
        save_config(self.cfg)
        QMessageBox.information(
            self, 'Theme',
            f'Theme set to {new}. Restart the app to apply.')

    def check_updates(self):
        self.update_status.setText('Checking...')
        self.check_btn.setEnabled(False)
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        subprocess.run(['sudo', 'apt', 'update'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=120)
        result = run_output(['apt', 'list', '--upgradable'])
        lines = [l for l in result.split('\n')
                 if l and not l.startswith('Listing')]
        n = len(lines)
        self._update_done.emit(
            f'{n} update{"s" if n != 1 else ""} available'
            if n else 'System is up to date')

    def _on_update_done(self, text):
        self.update_status.setText(text)
        self.check_btn.setEnabled(True)

    # ── tray icon ─────────────────────────────────────────────────────

    def _setup_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(_build_icon(), self)
        menu = QMenu()
        show = menu.addAction('Show / Hide')
        show.triggered.connect(self._toggle_visibility)
        menu.addSeparator()
        for key, label in [('quiet', 'Quiet'), ('balanced', 'Balanced'),
                           ('balanced-performance', 'Balanced-Performance'),
                           ('performance', 'Performance')]:
            a = menu.addAction(label)
            a.triggered.connect(lambda _, m=key: self.set_profile(m))
        menu.addSeparator()
        quit_a = menu.addAction('Quit')
        quit_a.triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _toggle_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self._toggle_visibility()

    def _quit(self):
        if self.tray:
            self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event):
        if self.tray and self.tray.isVisible():
            self.hide()
            event.ignore()
        else:
            event.accept()

    # ── keyboard shortcuts ────────────────────────────────────────────

    def _setup_shortcuts(self):
        for i, key in enumerate(
                ['quiet', 'balanced', 'balanced-performance', 'performance']):
            QShortcut(QKeySequence(f'Ctrl+{i+1}'), self).activated.connect(
                lambda m=key: self.set_profile(m))
        QShortcut(QKeySequence('Ctrl+Q'), self).activated.connect(self._quit)
        QShortcut(QKeySequence('Ctrl+P'), self).activated.connect(
            self._open_proc_manager)


# ── Entry Point ───────────────────────────────────────────────────────

def _kill_existing():
    """Kill any other running instance of this app — and nothing else.

    This used to be `pgrep -f loq-control.py`, which matches ANY process whose
    command line contains that string: the editor you have the file open in, a
    grep for it, a shell that merely mentions it. Launching the app would
    silently SIGTERM them. (It killed the terminal it was launched from during
    this very rewrite.)

    So: read /proc directly and require the process to actually be a Python
    interpreter running THIS script, resolved to an absolute path.
    """
    me = os.getpid()
    script = os.path.realpath(__file__)

    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as fh:
                argv = [a.decode('utf-8', 'replace')
                        for a in fh.read().split(b'\x00') if a]
        except OSError:
            continue                      # process vanished, or not ours
        if not argv:
            continue

        # argv[0] must be a python interpreter, and some later argument must
        # resolve to this exact file. A shell whose command line merely
        # mentions the name satisfies neither.
        if 'python' not in os.path.basename(argv[0]):
            continue
        if not any(os.path.realpath(a) == script for a in argv[1:] if a and not a.startswith('-')):
            continue

        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _set_app_identity(app):
    """Make the dock/taskbar show the right icon and name.

    Under Wayland, GNOME matches a window to its .desktop file by the surface's
    `app_id`, which Qt takes from QGuiApplication::desktopFileName(). The
    StartupWMClass hint in the .desktop file is an X11-ONLY mechanism and is
    ignored entirely — which is why this app lost its dock icon when the
    session moved to Wayland, and why the fix is here rather than in the
    .desktop file.

    The name must match the installed .desktop basename exactly:
    loq-control.desktop.
    """
    app.setApplicationName('LOQ Control')
    app.setApplicationDisplayName('LOQ Control')
    app.setOrganizationName('ojee')
    app.setDesktopFileName('loq-control')
    app.setWindowIcon(_build_icon())


if __name__ == '__main__':
    _kill_existing()

    # Qt5 on Wayland falls back to QtWayland's own client-side decoration
    # plugin ("bradient"), which paints a pale titlebar and border that ignore
    # the app stylesheet — the white frame that made this stop looking like a
    # Linux app. Running under XWayland hands decoration back to the
    # compositor, so the window gets real Zorin/Adwaita chrome again.
    #
    # Override with OJEE_QPA=wayland to test the native path.
    if os.environ.get('OJEE_QPA'):
        os.environ['QT_QPA_PLATFORM'] = os.environ['OJEE_QPA']
    elif os.environ.get('XDG_SESSION_TYPE') == 'wayland' and not os.environ.get('QT_QPA_PLATFORM'):
        os.environ['QT_QPA_PLATFORM'] = 'xcb'

    app = QApplication(sys.argv)
    _set_app_identity(app)
    app.setStyleSheet(MSG_STYLE)
    win = LOQControl()
    if '--minimized' not in sys.argv:
        win.show()
    sys.exit(app.exec_())
