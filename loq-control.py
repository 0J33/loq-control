#!/usr/bin/env python3
"""LOQ Control — Desktop control center for Lenovo LOQ laptops on Linux."""

import sys
import os
import re
import json
import subprocess
import glob
import threading
import time
from collections import deque
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QFrame, QGridLayout, QSlider, QScrollArea, QMessageBox, QSizePolicy,
    QProgressBar, QSystemTrayIcon, QMenu, QAction, QShortcut)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QIcon, QPainter, QColor, QPen, QKeySequence


class NoScrollSlider(QSlider):
    def wheelEvent(self, event):
        event.ignore()


# ── Config ────────────────────────────────────────────────────────────

CONFIG_DIR = os.path.expanduser('~/.config/loq-control')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
HISTORY_FILE = os.path.join(CONFIG_DIR, 'battery_history.csv')
AUTOSTART_DIR = os.path.expanduser('~/.config/autostart')
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, 'loq-control.desktop')
SCRIPT_PATH = os.path.abspath(__file__)

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

THEMES = {
    'dark': dict(
        BG='#0a0a0a', CARD='#141414', BORDER='#222222',
        TEXT='#ffffff', TEXT_DIM='#888888', TEXT_MUTED='#555555',
        BTN_DEF='#1a1a1a', BTN_HOVER='#252525',
        BTN_ACT='#ffffff', BTN_ACT_T='#000000', BTN_ACT_H='#dddddd',
        TOG_ON='#e0e0e0', TOG_OFF='#1a1a1a', TOG_ON_H='#cccccc',
        GR_GRID='#333333', GR_TEXT='#555555',
        CORE_LO='#222222', CORE_MED='#555555',
        CORE_HI='#888888', CORE_MAX='#ffffff',
    ),
    'light': dict(
        BG='#f0f0f0', CARD='#ffffff', BORDER='#d0d0d0',
        TEXT='#1a1a1a', TEXT_DIM='#666666', TEXT_MUTED='#999999',
        BTN_DEF='#e8e8e8', BTN_HOVER='#d8d8d8',
        BTN_ACT='#1a1a1a', BTN_ACT_T='#ffffff', BTN_ACT_H='#333333',
        TOG_ON='#333333', TOG_OFF='#e0e0e0', TOG_ON_H='#444444',
        GR_GRID='#cccccc', GR_TEXT='#999999',
        CORE_LO='#e0e0e0', CORE_MED='#aaaaaa',
        CORE_HI='#666666', CORE_MAX='#1a1a1a',
    ),
}

_cfg = load_config()
T = THEMES.get(_cfg.get('theme', 'dark'), THEMES['dark'])
FONT = 'JetBrains Mono'
IDEAPAD = '/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00'
RAPL = '/sys/class/powercap/intel-rapl:0'
MEM_LEVELS = [0, 9001, 11001, 12001]
MEM_LABELS = ['Auto', '9 GHz', '11 GHz', '12 GHz']

CARD_STYLE = f"""
    QFrame {{
        background-color: {T['CARD']};
        border: 1px solid {T['BORDER']};
        border-radius: 10px;
    }}
"""
SLIDER_STYLE = f"""
    QSlider {{ background: transparent; border: none; min-height: 26px; }}
    QSlider::groove:horizontal {{
        background: {T['BORDER']}; height: 6px; border-radius: 3px;
    }}
    QSlider::handle:horizontal {{
        background: {T['TEXT']}; width: 16px; height: 16px;
        margin: -5px 0; border-radius: 8px;
    }}
    QSlider::sub-page:horizontal {{
        background: {T['TEXT_DIM']}; border-radius: 3px;
    }}
"""
BAR_STYLE = f"""
    QProgressBar {{
        background: {T['BORDER']}; border: none; border-radius: 4px;
        max-height: 10px; min-height: 10px;
    }}
    QProgressBar::chunk {{
        background: {T['TEXT_DIM']}; border-radius: 4px;
    }}
"""
MSG_STYLE = f"""
    QMessageBox {{ background-color: {T['CARD']}; }}
    QMessageBox QLabel {{ color: {T['TEXT']}; font-family: '{FONT}'; }}
    QMessageBox QPushButton {{
        background-color: {T['BTN_DEF']}; color: {T['TEXT']};
        border: 1px solid {T['BORDER']}; border-radius: 6px;
        padding: 6px 16px; font-family: '{FONT}'; min-width: 60px;
    }}
    QMessageBox QPushButton:hover {{ background-color: {T['BTN_HOVER']}; }}
"""
SCROLL_STYLE = f"""
    QScrollArea {{ border: none; background: {T['BG']}; }}
    QScrollBar:vertical {{
        background: {T['BG']}; width: 6px; border: none;
    }}
    QScrollBar::handle:vertical {{
        background: {T['BORDER']}; border-radius: 3px; min-height: 30px;
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


def get_gpu_processes():
    output = run_output(['nvidia-smi'])
    procs = []
    for m in re.finditer(
            r'\|\s+\d+\s+N/A\s+N/A\s+(\d+)\s+(\w+)\s+(.*?)\s+(\d+)MiB\s*\|',
            output):
        pid, ptype, name, mem = m.groups()
        procs.append(dict(pid=pid, type=ptype,
                          name=os.path.basename(name.strip()), mem=mem))
    return procs


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
                name = p[2]
                if name.startswith('nvme') and 'p' not in name:
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
        mx = int(read_sys(f'{RAPL}/constraint_0_max_power_uw') or '0')
        if cur > 0 and mx > 0 and read_sys(f'{RAPL}/name') == 'package-0':
            return cur // 1000000, mx // 1000000
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

class TempGraph(QWidget):
    def __init__(self, max_pts=60):
        super().__init__()
        self.max_pts = max_pts
        self.cpu = deque(maxlen=max_pts)
        self.gpu = deque(maxlen=max_pts)
        self.setFixedHeight(120)
        self.setStyleSheet('background: transparent; border: none;')

    def add(self, ct, gt):
        self.cpu.append(ct)
        self.gpu.append(gt)
        self.update()

    def paintEvent(self, event):
        if len(self.cpu) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        ml, mb = 35, 18
        gw, gh = w - ml - 5, h - mb - 5
        mx = 105
        p.setPen(QPen(QColor(T['GR_GRID']), 1))
        for t in (25, 50, 75, 100):
            y = int(5 + gh * (1 - t / mx))
            p.drawLine(ml, y, w - 5, y)
            p.setPen(QPen(QColor(T['GR_TEXT']), 1))
            p.setFont(QFont(FONT, 7))
            p.drawText(0, y + 4, f'{t}\u00b0')
            p.setPen(QPen(QColor(T['GR_GRID']), 1))

        def draw(data, color):
            if len(data) < 2:
                return
            p.setPen(QPen(QColor(color), 2))
            pts = [(ml + int(i * gw / (self.max_pts - 1)),
                    int(5 + gh * (1 - min(v, mx) / mx)))
                   for i, v in enumerate(data)]
            for i in range(len(pts) - 1):
                p.drawLine(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1])

        draw(self.cpu, '#4488ff')
        draw(self.gpu, '#ff4444')
        p.setFont(QFont(FONT, 7, QFont.Bold))
        p.setPen(QPen(QColor('#4488ff'), 1))
        p.drawText(ml + 5, h - 3, 'CPU')
        p.setPen(QPen(QColor('#ff4444'), 1))
        p.drawText(ml + 40, h - 3, 'GPU')
        p.end()


class CpuCoreGrid(QWidget):
    def __init__(self, count):
        super().__init__()
        self.count = count
        self.values = [0] * count
        self.cols = min(count, 14)
        rows = (count + self.cols - 1) // self.cols
        self.setFixedHeight(rows * 12 + (rows - 1) * 2)
        self.setStyleSheet('background: transparent; border: none;')

    def set_values(self, vals):
        self.values = vals[:self.count]
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        w = self.width()
        cw = max(4, (w - (self.cols - 1) * 2) // self.cols)
        for i, v in enumerate(self.values):
            r, c = i // self.cols, i % self.cols
            x, y = c * (cw + 2), r * 14
            v = max(0, min(100, v))
            color = (T['CORE_LO'] if v < 30 else T['CORE_MED'] if v < 60
                     else T['CORE_HI'] if v < 85 else T['CORE_MAX'])
            p.fillRect(x, y, cw, 10, QColor(color))
        p.end()


# ── Main Window ───────────────────────────────────────────────────────

class LOQControl(QWidget):
    _update_done = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('LOQ Control')
        self.setFixedWidth(760)
        self.setMinimumHeight(640)
        self.setStyleSheet(f'background-color: {T["BG"]};')
        self.cfg = load_config()
        self.cpu_zone = find_cpu_zone()
        self.bat_path = find_battery()
        self.cpu_freq_paths = sorted(
            glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq'))
        self.n_cores = len(self.cpu_freq_paths) or 1
        self.gpu_power_default = 45
        self._prev_throttle = int(read_sys(
            '/sys/devices/system/cpu/cpu0/thermal_throttle/'
            'package_throttle_count') or '0')
        self._prev_stat = read_cpu_stat()
        self._prev_cores = read_per_core_stats()
        self._prev_net = read_net_stats()
        self._prev_disk = read_disk_stats()
        self._prev_time = time.monotonic()
        self.rapl = get_rapl_info()
        self._update_done.connect(self._on_update_done)
        log_battery_history(self.bat_path)
        self.initUI()
        self._setup_tray()
        self._setup_shortcuts()
        if self.cfg.get('auto_apply_oc'):
            self._auto_apply_oc()

    def initUI(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(SCROLL_STYLE)
        container = QWidget()
        container.setStyleSheet(f'background: {T["BG"]};')
        root = QVBoxLayout(container)
        root.setSpacing(12)
        root.setContentsMargins(24, 24, 24, 24)

        title = QLabel('LOQ CONTROL')
        title.setFont(QFont(FONT, 18, QFont.Bold))
        title.setStyleSheet(
            f'color: {T["TEXT"]}; background: transparent; letter-spacing: 3px;')
        root.addWidget(title)
        sub = QLabel('LENOVO LOQ // SYSTEM CONTROLS')
        sub.setFont(QFont(FONT, 9))
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
        g.addWidget(self._build_gpu_procs_card(), row, 0, 1, 2); row += 1
        g.addWidget(self._build_temp_graph_card(), row, 0, 1, 2); row += 1
        g.addWidget(self._build_battery_card(), row, 0, 1, 2); row += 1
        g.addWidget(self._build_profile_card(), row, 0)
        g.addWidget(self._build_gpu_mode_card(), row, 1); row += 1
        g.addWidget(self._build_overclock_card(), row, 0, 1, 2); row += 1
        g.addWidget(self._build_kbd_card(), row, 0)
        g.addWidget(self._build_toggles_card(), row, 1); row += 1
        g.addWidget(self._build_specs_card(), row, 0, 1, 2); row += 1
        g.addWidget(self._build_updates_card(), row, 0, 1, 2)

        root.addLayout(g)
        root.addStretch()

        footer = QLabel(
            f'Made by ojee  \u2022  '
            f'<a href="https://ojee.net" style="color: {T["TEXT_DIM"]};">'
            f'ojee.net</a>')
        footer.setFont(QFont(FONT, 8))
        footer.setAlignment(Qt.AlignCenter)
        footer.setOpenExternalLinks(True)
        footer.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; background: transparent; '
            f'padding: 12px 0 4px 0;')
        root.addWidget(footer)
        scroll.setWidget(container)
        outer.addWidget(scroll)

        self.refresh_all()
        self.sensor_timer = QTimer()
        self.sensor_timer.timeout.connect(self.refresh_sensors)
        self.sensor_timer.start(3000)
        self.proc_timer = QTimer()
        self.proc_timer.timeout.connect(self.refresh_gpu_procs)
        self.proc_timer.start(10000)

    # ── widget helpers ────────────────────────────────────────────────

    def _btn(self, text, fs=10):
        b = QPushButton(text)
        b.setMinimumHeight(36)
        b.setFont(QFont(FONT, fs, QFont.Bold))
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(f"""
            QPushButton {{
                background-color: {T['BTN_DEF']}; color: {T['TEXT_DIM']};
                border: 1px solid {T['BORDER']}; border-radius: 8px;
                padding: 7px 6px;
            }}
            QPushButton:hover {{
                background-color: {T['BTN_HOVER']}; color: {T['TEXT']};
            }}
        """)
        return b

    def _header(self, text):
        l = QLabel(text)
        l.setFont(QFont(FONT, 11, QFont.Bold))
        l.setStyleSheet(
            f'color: {T["TEXT"]}; border: none; background: transparent;')
        return l

    def _sub_header(self, text):
        l = QLabel(text)
        l.setFont(QFont(FONT, 10, QFont.Bold))
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
        if active:
            b.setStyleSheet(f"""
                QPushButton {{
                    background-color: {T['BTN_ACT']}; color: {T['BTN_ACT_T']};
                    border: 1px solid {T['BTN_ACT']}; border-radius: 8px;
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
                    border: 1px solid {T['BORDER']}; border-radius: 8px;
                    padding: 7px 6px;
                }}
                QPushButton:hover {{
                    background-color: {T['BTN_HOVER']}; color: {T['TEXT']};
                }}
            """)

    def _switch(self, w=70):
        b = QPushButton('OFF')
        b.setFixedSize(w, 30)
        b.setFont(QFont(FONT, 9, QFont.Bold))
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _style_switch(self, b, on):
        b.setText('ON' if on else 'OFF')
        if on:
            b.setStyleSheet(f"""
                QPushButton {{
                    background-color: {T['TOG_ON']}; color: {T['BTN_ACT_T']};
                    border: 1px solid {T['TOG_ON']}; border-radius: 6px;
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
                    border: 1px solid {T['BORDER']}; border-radius: 6px;
                }}
                QPushButton:hover {{
                    background-color: {T['BTN_HOVER']}; color: {T['TEXT_DIM']};
                }}
            """)

    def _sensor_bar(self, name, mx):
        row = QHBoxLayout(); row.setSpacing(8)
        l = QLabel(name); l.setFont(QFont(FONT, 9))
        l.setStyleSheet(
            f'color: {T["TEXT_DIM"]}; border: none; background: transparent;')
        l.setFixedWidth(110); row.addWidget(l)
        bar = QProgressBar(); bar.setRange(0, mx); bar.setValue(0)
        bar.setTextVisible(False); bar.setStyleSheet(BAR_STYLE)
        bar.setFixedHeight(10); row.addWidget(bar)
        v = QLabel('--'); v.setFont(QFont(FONT, 9, QFont.Bold))
        v.setStyleSheet(
            f'color: {T["TEXT"]}; border: none; background: transparent;')
        v.setFixedWidth(75); v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(v)
        return row, bar, v

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
        row.setContentsMargins(lw + 10, 0, 90, 0)
        row.setSpacing(0)
        prev = 0
        for i, (frac, text) in enumerate(markers):
            if i > 0:
                row.addStretch(max(int((frac - prev) * 100), 1))
            lbl = QLabel(text)
            lbl.setFont(QFont(FONT, 7))
            lbl.setStyleSheet(
                f'color: {T["TEXT_MUTED"]}; border: none; '
                f'background: transparent;')
            if i == 0:
                lbl.setAlignment(Qt.AlignLeft)
            elif i == len(markers) - 1:
                lbl.setAlignment(Qt.AlignRight)
            else:
                lbl.setAlignment(Qt.AlignCenter)
            row.addWidget(lbl)
            prev = frac
        return row

    def _toggle_row(self, name):
        row = QHBoxLayout(); row.setSpacing(12)
        l = QLabel(name); l.setFont(QFont(FONT, 9))
        l.setStyleSheet(
            f'color: {T["TEXT_DIM"]}; border: none; background: transparent;')
        row.addWidget(l); row.addStretch()
        b = self._switch(); row.addWidget(b)
        return row, b

    def _val_row(self, name):
        row = QHBoxLayout(); row.setSpacing(12)
        l = QLabel(name); l.setFont(QFont(FONT, 9))
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
        hdr = QHBoxLayout()
        hdr.addWidget(self._header('Sensors'))
        hdr.addStretch()
        self.throttle_lbl = QLabel('')
        self.throttle_lbl.setFont(QFont(FONT, 8, QFont.Bold))
        self.throttle_lbl.setStyleSheet(
            'color: #ff4444; border: none; background: transparent;')
        hdr.addWidget(self.throttle_lbl)
        vbox.addLayout(hdr)

        hbox = QHBoxLayout(); hbox.setSpacing(16)

        cpu = QVBoxLayout(); cpu.setSpacing(6)
        cpu.addWidget(self._sub_header('CPU'))
        r, self.cpu_util_bar, self.cpu_util_val = self._sensor_bar(
            'Utilization', 100); cpu.addLayout(r)
        r, self.cpu_clk_bar, self.cpu_clk_val = self._sensor_bar(
            'Core Clock', 5500); cpu.addLayout(r)
        r, self.cpu_tmp_bar, self.cpu_tmp_val = self._sensor_bar(
            'Temperature', 105); cpu.addLayout(r)
        r, self.cpu_fan_bar, self.cpu_fan_val = self._sensor_bar(
            'Fan', 5000); cpu.addLayout(r)
        cpu.addStretch()

        sep = QFrame(); sep.setFixedWidth(1)
        sep.setStyleSheet(f'background: {T["BORDER"]}; border: none;')

        gpu = QVBoxLayout(); gpu.setSpacing(6)
        gpu.addWidget(self._sub_header('GPU'))
        r, self.gpu_util_bar, self.gpu_util_val = self._sensor_bar(
            'Utilization', 100); gpu.addLayout(r)
        r, self.gpu_clk_bar, self.gpu_clk_val = self._sensor_bar(
            'Core Clock', 3090); gpu.addLayout(r)
        r, self.gpu_mem_bar, self.gpu_mem_val = self._sensor_bar(
            'Memory Clock', 12001); gpu.addLayout(r)
        r, self.gpu_tmp_bar, self.gpu_tmp_val = self._sensor_bar(
            'Temperature', 105); gpu.addLayout(r)
        r, self.gpu_fan_bar, self.gpu_fan_val = self._sensor_bar(
            'Fan', 5000); gpu.addLayout(r)

        hbox.addLayout(cpu); hbox.addWidget(sep); hbox.addLayout(gpu)
        vbox.addLayout(hbox)

        cl = QLabel('Per-Core Utilization')
        cl.setFont(QFont(FONT, 8))
        cl.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; background: transparent;')
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
        self.batt_status_lbl.setFont(QFont(FONT, 10, QFont.Bold))
        self.batt_status_lbl.setStyleSheet(
            f'color: {T["TEXT_DIM"]}; border: none; background: transparent;')
        hdr.addWidget(self.batt_status_lbl)
        vbox.addLayout(hdr)

        r, self.charge_bar, self.charge_val = self._wide_bar(
            'Charge Level', 100); vbox.addLayout(r)
        r, self.health_bar, self.health_val = self._wide_bar(
            'Battery Health', 110); vbox.addLayout(r)

        for name, attr in [('Cycle Count', 'cycles_val'),
                           ('Power Draw', 'batt_rate_val'),
                           ('Time Remaining', 'batt_time_val')]:
            rv, v = self._val_row(name)
            setattr(self, attr, v)
            vbox.addLayout(rv)

        self.batt_hist_lbl = self._info('')
        vbox.addWidget(self.batt_hist_lbl)
        hist = battery_history_summary()
        if hist:
            self.batt_hist_lbl.setText(
                f'Tracking since {hist["since"]} '
                f'({hist["n"]} logs, started at {hist["first_h"]}% health)')

        sep = QFrame(); sep.setFixedHeight(1)
        sep.setStyleSheet(f'background: {T["BORDER"]}; border: none;')
        vbox.addWidget(sep)

        cr = QHBoxLayout(); cr.setSpacing(12)
        ct = QVBoxLayout(); ct.setSpacing(2)
        t = QLabel('Conservation Mode')
        t.setFont(QFont(FONT, 10, QFont.Bold))
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
            r, self.tdp_slider, self.tdp_val = self._slider_row(
                'CPU Power Limit', 5, self.rapl[1], self.rapl[0], 'W')
            self.tdp_slider.valueChanged.connect(
                lambda v: self.tdp_val.setText(f'{v}W'))
            self.tdp_slider.sliderReleased.connect(self._apply_tdp)
            vbox.addLayout(r)
            vbox.addLayout(self._slider_markers([
                (0, '5W min'), (0.5, f'{self.rapl[1]//2}W'),
                (1.0, f'{self.rapl[1]}W max')]))

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
        note = QLabel('Does not persist across reboots')
        note.setFont(QFont(FONT, 8))
        note.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; background: transparent;')
        hdr.addWidget(note)
        vbox.addLayout(hdr)

        tip = QLabel(
            'Tip: Power 80-100W for gaming. Lock GPU clock above 2000 MHz '
            'to prevent downclocking. Memory 11-12 GHz for max bandwidth. '
            'Keep temps below 82\u00b0C. '
            'Run: sudo systemctl enable nvidia-powerd')
        tip.setFont(QFont(FONT, 8)); tip.setWordWrap(True)
        tip.setStyleSheet(
            f'color: {T["TEXT_MUTED"]}; border: none; background: transparent; '
            f'padding: 4px 0;')
        vbox.addWidget(tip)
        self.oc_status = self._info()
        vbox.addWidget(self.oc_status)

        r1, self.pl_slider, self.pl_val = self._slider_row(
            'Power Limit', 5, 100, 45, 'W')
        self.pl_slider.valueChanged.connect(
            lambda v: self.pl_val.setText(f'{v}W'))
        self.pl_slider.sliderReleased.connect(self._apply_power_limit)
        vbox.addLayout(r1)
        vbox.addLayout(self._slider_markers([
            (0.42, '45W base'), (0.79, '80W gaming'), (1.0, '100W max')]))

        r2, self.gc_slider, self.gc_val = self._slider_row(
            'GPU Clock Min', 0, 3090, 0, '')
        self.gc_val.setText('Auto')
        self.gc_slider.valueChanged.connect(self._on_gc_change)
        self.gc_slider.sliderReleased.connect(self._apply_gpu_clock)
        vbox.addLayout(r2)
        vbox.addLayout(self._slider_markers([
            (0, 'Auto'), (0.68, '2100 gaming'), (0.78, '2400 aggr.'),
            (1.0, '3090 max')]))

        r3, self.mc_slider, self.mc_val = self._slider_row(
            'Memory Clock', 0, 3, 0, '')
        self.mc_val.setText('Auto')
        self.mc_slider.setTickPosition(QSlider.TicksBelow)
        self.mc_slider.setTickInterval(1)
        self.mc_slider.setSingleStep(1)
        self.mc_slider.setPageStep(1)
        self.mc_slider.valueChanged.connect(self._on_mc_change)
        self.mc_slider.sliderReleased.connect(self._apply_mem_clock)
        vbox.addLayout(r3)
        vbox.addLayout(self._slider_markers([
            (0, 'Auto'), (0.33, '9 GHz safe'), (0.67, '11 GHz gaming'),
            (1.0, '12 GHz max')]))

        reset = self._btn('Reset All Defaults')
        reset.clicked.connect(self._reset_overclock)
        vbox.addWidget(reset)

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

    def _build_gpu_procs_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('GPU Processes'))
        self.gpu_procs_lbl = QLabel('Loading...')
        self.gpu_procs_lbl.setFont(QFont(FONT, 9))
        self.gpu_procs_lbl.setStyleSheet(
            f'color: {T["TEXT_DIM"]}; border: none; background: transparent;')
        self.gpu_procs_lbl.setWordWrap(True)
        vbox.addWidget(self.gpu_procs_lbl)
        return card

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
        r, self.ram_bar, self.ram_val = self._wide_bar('RAM Usage', 100)
        vbox.addLayout(r)
        for name, attr in [('Disk Read', 'disk_r_val'),
                           ('Disk Write', 'disk_w_val'),
                           ('Network Down', 'net_rx_val'),
                           ('Network Up', 'net_tx_val')]:
            rv, v = self._val_row(name)
            setattr(self, attr, v)
            vbox.addLayout(rv)
        return card

    def _build_specs_card(self):
        card, vbox = self._card()
        hdr = QHBoxLayout()
        hdr.addWidget(self._header('Specifications'))
        hdr.addStretch()
        exp = self._btn('Export', fs=8)
        exp.setFixedSize(70, 28)
        exp.clicked.connect(self.export_specs)
        hdr.addWidget(exp)
        vbox.addLayout(hdr)

        self._spec_lines = []

        def _spec(label, value):
            row = QHBoxLayout(); row.setSpacing(12)
            l = QLabel(label); l.setFont(QFont(FONT, 9))
            l.setStyleSheet(
                f'color: {T["TEXT_DIM"]}; border: none; '
                f'background: transparent;')
            l.setFixedWidth(150); row.addWidget(l)
            v = QLabel(value); v.setFont(QFont(FONT, 9, QFont.Bold))
            v.setStyleSheet(
                f'color: {T["TEXT"]}; border: none; background: transparent;')
            v.setWordWrap(True); row.addWidget(v)
            self._spec_lines.append((label, value))
            return row

        cpu_model = ''
        try:
            with open('/proc/cpuinfo') as f:
                for line in f:
                    if line.startswith('model name'):
                        cpu_model = line.split(':')[1].strip(); break
        except Exception:
            pass
        cores = run_output(['nproc'])
        vbox.addLayout(_spec('CPU', f'{cpu_model} ({cores} threads)'))

        gpu_name = nvidia_query('name')[0]
        gpu_vram = nvidia_query('memory.total')[0]
        if gpu_name:
            gt = gpu_name + (f' ({gpu_vram} MiB)' if gpu_vram else '')
            vbox.addLayout(_spec('GPU', gt))

        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        gb = round(int(line.split()[1]) / 1024 / 1024)
                        vbox.addLayout(_spec('RAM', f'{gb} GB')); break
        except Exception:
            pass

        try:
            r = subprocess.run(['lsblk', '-d', '-o', 'NAME,SIZE,MODEL', '-n'],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.strip().split('\n'):
                p = line.split(None, 2)
                if len(p) >= 3 and p[0].startswith('nvme'):
                    vbox.addLayout(
                        _spec(f'/dev/{p[0]}', f'{p[2].strip()} ({p[1]})'))
        except Exception:
            pass

        try:
            r = subprocess.run(
                ['df', '-h', '--output=target,size,used,avail,pcent'],
                capture_output=True, text=True, timeout=5)
            for line in r.stdout.strip().split('\n')[1:]:
                p = line.split()
                if len(p) == 5 and p[0] in ('/', '/media/ojee/NVME'):
                    name = 'Root (/)' if p[0] == '/' else p[0].split('/')[-1]
                    vbox.addLayout(
                        _spec(name, f'{p[2]} / {p[1]} ({p[4]} used)'))
        except Exception:
            pass

        def _sep():
            s = QFrame(); s.setFixedHeight(1)
            s.setStyleSheet(f'background: {T["BORDER"]}; border: none;')
            vbox.addWidget(s)

        _sep()
        try:
            for drm in sorted(glob.glob('/sys/class/drm/card*-*')):
                if read_sys(f'{drm}/status') == 'connected':
                    name = drm.split('/')[-1].split('-', 1)[1]
                    mode = read_sys(f'{drm}/modes').split('\n')[0]
                    vbox.addLayout(_spec('Display', f'{name}  {mode}'))
        except Exception:
            pass

        _sep()
        wifi = ''
        try:
            r = subprocess.run(['iw', 'dev'], capture_output=True,
                               text=True, timeout=5)
            for line in r.stdout.split('\n'):
                if 'ssid' in line.lower():
                    wifi = line.split('ssid')[1].strip(); break
        except Exception:
            pass
        if wifi:
            vbox.addLayout(_spec('Wi-Fi', wifi))
        try:
            r = subprocess.run(['ip', '-br', 'link', 'show'],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.strip().split('\n'):
                p = line.split()
                if len(p) >= 2 and p[0] not in ('lo', 'tailscale0') \
                        and not p[0].startswith('wl'):
                    vbox.addLayout(_spec(p[0], p[1]))
        except Exception:
            pass
        bt = ''
        try:
            r = subprocess.run(['bluetoothctl', 'show'],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.split('\n'):
                if 'Powered:' in line:
                    bt = 'On' if 'yes' in line else 'Off'; break
        except Exception:
            pass
        if bt:
            vbox.addLayout(_spec('Bluetooth', bt))
        return card

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
        self.refresh_gpu_procs()

    def refresh_sensors(self):
        cur = read_cpu_stat()
        prev = self._prev_stat; self._prev_stat = cur
        dt = cur[0] - prev[0]; di = cur[1] - prev[1]
        pct = max(0, min(100, round((1 - di / dt) * 100))) if dt > 0 else 0
        self.cpu_util_bar.setValue(pct); self.cpu_util_val.setText(f'{pct}%')

        cur_cores = read_per_core_stats()
        prev_cores = self._prev_cores; self._prev_cores = cur_cores
        core_pcts = []
        for i in range(min(len(cur_cores), len(prev_cores))):
            cdt = cur_cores[i][0] - prev_cores[i][0]
            cdi = cur_cores[i][1] - prev_cores[i][1]
            cp = max(0, min(100, round((1 - cdi / cdt) * 100))) if cdt > 0 else 0
            core_pcts.append(cp)
        self.core_grid.set_values(core_pcts)

        try:
            freq = max(
                (int(read_sys(f) or '0') for f in self.cpu_freq_paths),
                default=0) // 1000
            self.cpu_clk_bar.setValue(freq)
            self.cpu_clk_val.setText(
                f'{freq / 1000:.1f} GHz' if freq >= 1000 else f'{freq} MHz')
        except (ValueError, TypeError):
            self.cpu_clk_val.setText('--')

        cpu_temp = 0
        try:
            raw = read_sys(f'{self.cpu_zone}/temp')
            cpu_temp = int(raw) // 1000 if raw else 0
            self.cpu_tmp_bar.setValue(cpu_temp)
            self.cpu_tmp_val.setText(f'{cpu_temp}\u00b0C')
        except (ValueError, TypeError):
            self.cpu_tmp_val.setText('--')

        cpu_rpm, gpu_rpm = read_fan_rpm()
        self.cpu_fan_bar.setValue(cpu_rpm)
        self.cpu_fan_val.setText(f'{cpu_rpm} RPM')

        gpu_temp = 0
        vals = nvidia_query([
            'utilization.gpu', 'clocks.current.graphics',
            'clocks.current.memory', 'temperature.gpu'])
        for i, (bar, lbl, suf) in enumerate([
            (self.gpu_util_bar, self.gpu_util_val, '%'),
            (self.gpu_clk_bar, self.gpu_clk_val, ' MHz'),
            (self.gpu_mem_bar, self.gpu_mem_val, ' MHz'),
            (self.gpu_tmp_bar, self.gpu_tmp_val, '\u00b0C')]):
            try:
                v = int(vals[i]); bar.setValue(v); lbl.setText(f'{v}{suf}')
                if i == 3:
                    gpu_temp = v
            except (ValueError, IndexError):
                lbl.setText('--')

        self.gpu_fan_bar.setValue(gpu_rpm)
        self.gpu_fan_val.setText(f'{gpu_rpm} RPM')
        self.temp_graph.add(cpu_temp, gpu_temp)

        # Throttle — only flag if actively increasing
        throttle = []
        try:
            tc = int(read_sys(
                '/sys/devices/system/cpu/cpu0/thermal_throttle/'
                'package_throttle_count') or '0')
            if tc > self._prev_throttle:
                throttle.append('CPU Thermal')
            self._prev_throttle = tc
        except Exception:
            pass
        try:
            for reason in ['hw_thermal_slowdown', 'sw_thermal_slowdown',
                           'hw_slowdown']:
                v = nvidia_query(f'clocks_throttle_reasons.{reason}')[0]
                if v == 'Active':
                    throttle.append(reason.replace('_', ' ').title())
        except Exception:
            pass
        self.throttle_lbl.setText(
            'THROTTLED: ' + ', '.join(throttle) if throttle else '')

        self.refresh_activity()

    def refresh_activity(self):
        now = time.monotonic()
        elapsed = max(now - self._prev_time, 0.1)
        self._prev_time = now

        try:
            with open('/proc/meminfo') as f:
                info = {}
                for line in f:
                    p = line.split()
                    if p[0].rstrip(':') in ('MemTotal', 'MemAvailable'):
                        info[p[0].rstrip(':')] = int(p[1])
            total = info.get('MemTotal', 1)
            avail = info.get('MemAvailable', 0)
            used_pct = round((1 - avail / total) * 100)
            self.ram_bar.setValue(used_pct)
            self.ram_val.setText(
                f'{(total - avail) / 1048576:.1f} / {total / 1048576:.0f} GB')
        except Exception:
            pass

        cur_disk = read_disk_stats()
        tr, tw = 0, 0
        for name in cur_disk:
            if name in self._prev_disk:
                tr += (cur_disk[name][0] - self._prev_disk[name][0]) * 512
                tw += (cur_disk[name][1] - self._prev_disk[name][1]) * 512
        self._prev_disk = cur_disk
        self.disk_r_val.setText(fmt_rate(int(tr / elapsed)))
        self.disk_w_val.setText(fmt_rate(int(tw / elapsed)))

        cur_net = read_net_stats()
        rx, tx = 0, 0
        for iface in cur_net:
            if iface in self._prev_net:
                rx += cur_net[iface][0] - self._prev_net[iface][0]
                tx += cur_net[iface][1] - self._prev_net[iface][1]
        self._prev_net = cur_net
        self.net_rx_val.setText(fmt_rate(int(rx / elapsed)))
        self.net_tx_val.setText(fmt_rate(int(tx / elapsed)))

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
        self.pl_slider.setRange(int(info['min']), int(info['max']))
        ci = int(cur)
        self.pl_slider.blockSignals(True)
        self.pl_slider.setValue(ci)
        self.pl_slider.blockSignals(False)
        self.pl_val.setText(f'{ci}W')
        self.oc_status.setText(
            f'Power {ci}W / {int(info["max"])}W max '
            f'(default {int(info["default"])}W)')

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
        if not bp:
            for w in [self.charge_val, self.health_val, self.cycles_val,
                      self.batt_rate_val, self.batt_time_val]:
                w.setText('--')
            self.batt_status_lbl.setText('--')
            self._style_switch(self.cons_btn, False)
            return
        pct = read_sys(f'{bp}/capacity')
        if pct:
            self.charge_bar.setValue(min(int(pct), 100))
            self.charge_val.setText(f'{pct}%')
        else:
            self.charge_val.setText('--')
        try:
            full = int(read_sys(f'{bp}/energy_full') or
                       read_sys(f'{bp}/charge_full') or '0')
            design = int(read_sys(f'{bp}/energy_full_design') or
                         read_sys(f'{bp}/charge_full_design') or '1')
            h = round(full / design * 100)
            self.health_bar.setValue(min(h, 110))
            self.health_val.setText(f'{h}%')
        except (ValueError, ZeroDivisionError):
            self.health_val.setText('--')
        cyc = read_sys(f'{bp}/cycle_count')
        self.cycles_val.setText(cyc if cyc and cyc != '0' else '--')

        pw = read_sys(f'{bp}/power_now')
        status = read_sys(f'{bp}/status')
        watts = int(pw) / 1e6 if pw else 0
        self.batt_rate_val.setText(f'{watts:.1f}W' if watts > 0 else '--')

        en = read_sys(f'{bp}/energy_now')
        if status == 'Discharging' and en and watts > 0:
            hrs = int(en) / 1e6 / watts
            self.batt_time_val.setText(f'{int(hrs)}h {int((hrs % 1) * 60)}m')
        elif status == 'Charging' and en and watts > 0:
            ef = read_sys(f'{bp}/energy_full')
            if ef:
                rem = (int(ef) - int(en)) / 1e6
                hrs = rem / watts
                self.batt_time_val.setText(
                    f'{int(hrs)}h {int((hrs % 1) * 60)}m to full')
            else:
                self.batt_time_val.setText('--')
        else:
            self.batt_time_val.setText('--')

        names = {'Charging': 'Charging', 'Discharging': 'On Battery',
                 'Full': 'Full', 'Not charging': 'Idle'}
        self.batt_status_lbl.setText(names.get(status, status or '--'))
        cons = read_sys(f'{IDEAPAD}/conservation_mode') == '1'
        self._style_switch(self.cons_btn, cons)

    def refresh_sysinfo(self):
        v = nvidia_query('driver_version')
        self.nv_ver.setText(v[0] if v[0] else 'N/A')
        self.kern_ver.setText(run_output(['uname', '-r']) or 'N/A')
        self.bios_ver.setText(
            read_sys('/sys/class/dmi/id/bios_version') or 'N/A')

    def refresh_gpu_procs(self):
        procs = get_gpu_processes()
        if not procs:
            self.gpu_procs_lbl.setText('No GPU processes')
            return
        lines = []
        for p in procs[:8]:
            lines.append(
                f'{p["pid"]:>7}  {p["type"]:>1}  {p["mem"]:>5} MiB  '
                f'{p["name"]}')
        self.gpu_procs_lbl.setText('\n'.join(lines))

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
        run_cmd(f'nvidia-smi -pl {self.pl_slider.value()}')
        self._save_oc_if_auto()
        self.refresh_overclock()

    def _on_gc_change(self, v):
        self.gc_val.setText('Auto' if v < 180 else f'{v} MHz')

    def _apply_gpu_clock(self):
        v = self.gc_slider.value()
        run_cmd('nvidia-smi -rgc' if v < 180
                else f'nvidia-smi -lgc {v},3090')
        self._save_oc_if_auto()

    def _on_mc_change(self, idx):
        self.mc_val.setText(MEM_LABELS[idx])

    def _apply_mem_clock(self):
        freq = MEM_LEVELS[self.mc_slider.value()]
        run_cmd('nvidia-smi -rmc' if freq == 0
                else f'nvidia-smi -lmc {freq},{freq}')
        self._save_oc_if_auto()

    def _reset_overclock(self):
        run_cmd(f'nvidia-smi -pl {self.gpu_power_default}')
        run_cmd('nvidia-smi -rgc')
        run_cmd('nvidia-smi -rmc')
        for s, v, txt in [(self.mc_slider, self.mc_val, 'Auto'),
                          (self.gc_slider, self.gc_val, 'Auto')]:
            s.blockSignals(True); s.setValue(0); s.blockSignals(False)
            v.setText(txt)
        self._save_oc_if_auto()
        self.refresh_overclock()

    def _save_oc_if_auto(self):
        if not self.cfg.get('auto_apply_oc'):
            return
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
            run_cmd(f'nvidia-smi -pl {pl}')
        if gc >= 180:
            run_cmd(f'nvidia-smi -lgc {gc},3090')
        if mc_idx > 0:
            freq = MEM_LEVELS[mc_idx]
            run_cmd(f'nvidia-smi -lmc {freq},{freq}')

    def _apply_tdp(self):
        v = self.tdp_slider.value()
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
                    f'Icon=preferences-system\n'
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

    def export_specs(self):
        lines = ['LOQ Control — System Specifications', '=' * 40]
        for label, value in self._spec_lines:
            lines.append(f'{label}: {value}')
        lines.append('')
        lines.append(f'NVIDIA Driver: {self.nv_ver.text()}')
        lines.append(f'Kernel: {self.kern_ver.text()}')
        lines.append(f'BIOS: {self.bios_ver.text()}')
        QApplication.clipboard().setText('\n'.join(lines))
        self.update_status.setText('Specs copied to clipboard')

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
        self.tray = QSystemTrayIcon(
            QIcon.fromTheme('preferences-system'), self)
        menu = QMenu()
        show = menu.addAction('Show / Hide')
        show.triggered.connect(self._toggle_visibility)
        menu.addSeparator()
        for key, label in [('quiet', 'Quiet'), ('balanced', 'Balanced'),
                           ('balanced-performance', 'Bal-Performance'),
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
        QShortcut(QKeySequence('Ctrl+E'), self).activated.connect(
            self.export_specs)


# ── Entry Point ───────────────────────────────────────────────────────

def _kill_existing():
    """Kill any other running instances of this app."""
    pid = os.getpid()
    try:
        r = subprocess.run(
            ['pgrep', '-f', 'loq-control.py'],
            capture_output=True, text=True, timeout=5)
        for line in r.stdout.strip().split('\n'):
            if line.strip() and int(line.strip()) != pid:
                os.kill(int(line.strip()), 15)
    except Exception:
        pass


if __name__ == '__main__':
    _kill_existing()
    app = QApplication(sys.argv)
    app.setStyleSheet(MSG_STYLE)
    win = LOQControl()
    if '--minimized' not in sys.argv:
        win.show()
    sys.exit(app.exec_())
