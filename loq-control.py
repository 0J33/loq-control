#!/usr/bin/env python3
import sys
import subprocess
import glob
import threading
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QFrame, QGridLayout, QSlider,
                             QScrollArea, QMessageBox, QSizePolicy, QProgressBar)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QEvent
from PyQt5.QtGui import QFont


class NoScrollSlider(QSlider):
    """Slider that ignores scroll wheel events."""
    def wheelEvent(self, event):
        event.ignore()


# --- Theme ---
BG = '#0a0a0a'
CARD = '#141414'
BORDER = '#222222'
TEXT = '#ffffff'
TEXT_DIM = '#888888'
TEXT_MUTED = '#555555'
BTN_DEFAULT = '#1a1a1a'
BTN_HOVER = '#252525'
BTN_ACTIVE = '#ffffff'
BTN_ACTIVE_TEXT = '#000000'
TOGGLE_ON = '#e0e0e0'
TOGGLE_OFF = '#1a1a1a'
FONT = 'JetBrains Mono'
IDEAPAD = '/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00'

CARD_STYLE = f"""
    QFrame {{
        background-color: {CARD};
        border: 1px solid {BORDER};
        border-radius: 10px;
    }}
"""

SLIDER_STYLE = f"""
    QSlider {{ background: transparent; border: none; min-height: 26px; }}
    QSlider::groove:horizontal {{
        background: {BORDER}; height: 6px; border-radius: 3px;
    }}
    QSlider::handle:horizontal {{
        background: {TEXT}; width: 16px; height: 16px;
        margin: -5px 0; border-radius: 8px;
    }}
    QSlider::sub-page:horizontal {{
        background: {TEXT_DIM}; border-radius: 3px;
    }}
"""

BAR_STYLE = f"""
    QProgressBar {{
        background: {BORDER}; border: none; border-radius: 4px;
        max-height: 10px; min-height: 10px;
    }}
    QProgressBar::chunk {{
        background: {TEXT_DIM}; border-radius: 4px;
    }}
"""

MSG_STYLE = f"""
    QMessageBox {{ background-color: {CARD}; }}
    QMessageBox QLabel {{ color: {TEXT}; font-family: '{FONT}'; }}
    QMessageBox QPushButton {{
        background-color: {BTN_DEFAULT}; color: {TEXT};
        border: 1px solid {BORDER}; border-radius: 6px;
        padding: 6px 16px; font-family: '{FONT}'; min-width: 60px;
    }}
    QMessageBox QPushButton:hover {{ background-color: {BTN_HOVER}; }}
"""

SCROLL_STYLE = f"""
    QScrollArea {{ border: none; background: {BG}; }}
    QScrollBar:vertical {{
        background: {BG}; width: 6px; border: none;
    }}
    QScrollBar::handle:vertical {{
        background: {BORDER}; border-radius: 3px; min-height: 30px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0; background: none;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: none;
    }}
"""


# --- Fast sysfs / proc reads ---

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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.stdout.strip()
    except Exception:
        return ''


def read_fan_rpm():
    """Read fan RPM from legion debug interface (WMI3 method)."""
    try:
        result = subprocess.run(
            ['sudo', 'cat', '/sys/kernel/debug/legion/fancurve'],
            capture_output=True, text=True, timeout=5)
        cpu_rpm, gpu_rpm = 0, 0
        for line in result.stdout.split('\n'):
            s = line.strip()
            if s.startswith('1 fanspeed WMI3:'):
                cpu_rpm = int(s.split(':')[1].strip())
            elif s.startswith('2 fanspeed WMI3:'):
                gpu_rpm = int(s.split(':')[1].strip())
        return cpu_rpm, gpu_rpm
    except Exception:
        return 0, 0


def nvidia_query(fields):
    """Query multiple GPU fields in one nvidia-smi call."""
    if isinstance(fields, str):
        fields = [fields]
    fstr = ','.join(fields)
    result = run_output(['nvidia-smi', f'--query-gpu={fstr}',
                         '--format=csv,noheader,nounits'])
    if not result:
        return [''] * len(fields)
    return [v.strip() for v in result.split(',')]


def get_gpu_power():
    output = run_output(['nvidia-smi', '-q', '-d', 'POWER'])
    info = {'current': 0, 'default': 45, 'min': 5, 'max': 100}
    found = set()
    in_module = False
    for line in output.split('\n'):
        s = line.strip()
        if 'Module Power' in s or 'GPU Memory Power' in s:
            in_module = True
            continue
        if 'GPU Power Readings' in s:
            in_module = False
            continue
        if in_module or ':' not in s:
            continue
        for pattern, key in [('Current Power Limit', 'current'),
                             ('Default Power Limit', 'default'),
                             ('Min Power Limit', 'min'),
                             ('Max Power Limit', 'max')]:
            if s.startswith(pattern) and key not in found:
                try:
                    info[key] = float(
                        s.split(':')[1].strip().replace(' W', ''))
                    found.add(key)
                except (ValueError, IndexError):
                    pass
    return info


def find_cpu_zone():
    for path in sorted(glob.glob('/sys/class/thermal/thermal_zone*')):
        if read_sys(f'{path}/type') in ('x86_pkg_temp', 'TCPU', 'cpu-thermal'):
            return path
    return '/sys/class/thermal/thermal_zone0'


def find_battery():
    for name in ['BAT0', 'BAT1']:
        path = f'/sys/class/power_supply/{name}'
        if read_sys(f'{path}/type') == 'Battery':
            return path
    return None


def find_legion_hwmon():
    for d in glob.glob('/sys/class/hwmon/hwmon*'):
        if read_sys(f'{d}/name') == 'legion_hwmon':
            return d
    return None


def read_cpu_stat():
    try:
        with open('/proc/stat') as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        return sum(vals), vals[3] + vals[4]  # total, idle+iowait
    except Exception:
        return 0, 0


class LOQControl(QWidget):
    _update_done = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('LOQ Control')
        self.setFixedWidth(760)
        self.setMinimumHeight(640)
        self.setStyleSheet(f'background-color: {BG};')
        self.cpu_zone = find_cpu_zone()
        self.bat_path = find_battery()
        self.legion_hw = find_legion_hwmon()
        self.cpu_freq_paths = sorted(
            glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq'))
        self.gpu_power_default = 45
        self.mem_clock_levels = [0, 9001, 11001, 12001]  # 0 = auto
        self.mem_clock_labels = ['Auto', '9 GHz', '11 GHz', '12 GHz']
        self._prev_stat = read_cpu_stat()
        self._update_done.connect(self._on_update_done)
        self.initUI()

    def initUI(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(SCROLL_STYLE)

        container = QWidget()
        container.setStyleSheet(f'background: {BG};')
        root = QVBoxLayout(container)
        root.setSpacing(12)
        root.setContentsMargins(24, 24, 24, 24)

        title = QLabel('LOQ CONTROL')
        title.setFont(QFont(FONT, 18, QFont.Bold))
        title.setStyleSheet(
            f'color: {TEXT}; background: transparent; letter-spacing: 3px;')
        root.addWidget(title)

        sub = QLabel('LENOVO LOQ // SYSTEM CONTROLS')
        sub.setFont(QFont(FONT, 9))
        sub.setStyleSheet(
            f'color: {TEXT_MUTED}; background: transparent; '
            f'margin-bottom: 4px; letter-spacing: 2px;')
        root.addWidget(sub)

        grid = QGridLayout()
        grid.setSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        grid.addWidget(self._build_sensors_card(), 0, 0, 1, 2)
        grid.addWidget(self._build_battery_card(), 1, 0, 1, 2)
        grid.addWidget(self._build_profile_card(), 2, 0)
        grid.addWidget(self._build_gpu_mode_card(), 2, 1)
        grid.addWidget(self._build_overclock_card(), 3, 0, 1, 2)
        grid.addWidget(self._build_kbd_card(), 4, 0)
        grid.addWidget(self._build_toggles_card(), 4, 1)
        grid.addWidget(self._build_specs_card(), 5, 0, 1, 2)
        grid.addWidget(self._build_updates_card(), 6, 0, 1, 2)

        root.addLayout(grid)
        root.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)

        self.refresh_all()

        self.sensor_timer = QTimer()
        self.sensor_timer.timeout.connect(self.refresh_sensors)
        self.sensor_timer.start(3000)

    # ── widget helpers ──────────────────────────────────────────────

    def _btn(self, text, font_size=10):
        btn = QPushButton(text)
        btn.setMinimumHeight(36)
        btn.setFont(QFont(FONT, font_size, QFont.Bold))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {BTN_DEFAULT}; color: {TEXT_DIM};
                border: 1px solid {BORDER}; border-radius: 8px;
                padding: 7px 6px;
            }}
            QPushButton:hover {{
                background-color: {BTN_HOVER}; color: {TEXT};
            }}
        """)
        return btn

    def _header(self, text):
        lbl = QLabel(text)
        lbl.setFont(QFont(FONT, 11, QFont.Bold))
        lbl.setStyleSheet(
            f'color: {TEXT}; border: none; background: transparent;')
        return lbl

    def _sub_header(self, text):
        lbl = QLabel(text)
        lbl.setFont(QFont(FONT, 10, QFont.Bold))
        lbl.setStyleSheet(
            f'color: {TEXT}; border: none; background: transparent;')
        return lbl

    def _info(self, text=''):
        lbl = QLabel(text)
        lbl.setFont(QFont(FONT, 9))
        lbl.setStyleSheet(
            f'color: {TEXT_MUTED}; border: none; background: transparent;')
        return lbl

    def _set_btn_active(self, btn, active):
        if active:
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {BTN_ACTIVE}; color: {BTN_ACTIVE_TEXT};
                    border: 1px solid {BTN_ACTIVE}; border-radius: 8px;
                    padding: 7px 6px;
                }}
                QPushButton:hover {{
                    background-color: #dddddd; color: {BTN_ACTIVE_TEXT};
                }}
            """)
        else:
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {BTN_DEFAULT}; color: {TEXT_DIM};
                    border: 1px solid {BORDER}; border-radius: 8px;
                    padding: 7px 6px;
                }}
                QPushButton:hover {{
                    background-color: {BTN_HOVER}; color: {TEXT};
                }}
            """)

    def _switch(self, width=70):
        btn = QPushButton('OFF')
        btn.setFixedSize(width, 30)
        btn.setFont(QFont(FONT, 9, QFont.Bold))
        btn.setCursor(Qt.PointingHandCursor)
        return btn

    def _style_switch(self, btn, active):
        btn.setText('ON' if active else 'OFF')
        if active:
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {TOGGLE_ON}; color: #000;
                    border: 1px solid {TOGGLE_ON}; border-radius: 6px;
                }}
                QPushButton:hover {{ background-color: #cccccc; color: #000; }}
            """)
        else:
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {TOGGLE_OFF}; color: {TEXT_MUTED};
                    border: 1px solid {BORDER}; border-radius: 6px;
                }}
                QPushButton:hover {{
                    background-color: {BTN_HOVER}; color: {TEXT_DIM};
                }}
            """)

    def _sensor_bar(self, name, max_val):
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(name)
        lbl.setFont(QFont(FONT, 9))
        lbl.setStyleSheet(
            f'color: {TEXT_DIM}; border: none; background: transparent;')
        lbl.setFixedWidth(110)
        row.addWidget(lbl)
        bar = QProgressBar()
        bar.setRange(0, max_val)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setStyleSheet(BAR_STYLE)
        bar.setFixedHeight(10)
        row.addWidget(bar)
        val = QLabel('--')
        val.setFont(QFont(FONT, 9, QFont.Bold))
        val.setStyleSheet(
            f'color: {TEXT}; border: none; background: transparent;')
        val.setFixedWidth(75)
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(val)
        return row, bar, val

    def _wide_bar(self, name, max_val):
        row = QHBoxLayout()
        row.setSpacing(12)
        lbl = QLabel(name)
        lbl.setFont(QFont(FONT, 9))
        lbl.setStyleSheet(
            f'color: {TEXT_DIM}; border: none; background: transparent;')
        lbl.setFixedWidth(150)
        row.addWidget(lbl)
        bar = QProgressBar()
        bar.setRange(0, max_val)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setStyleSheet(BAR_STYLE)
        bar.setFixedHeight(10)
        row.addWidget(bar)
        val = QLabel('--')
        val.setFont(QFont(FONT, 10, QFont.Bold))
        val.setStyleSheet(
            f'color: {TEXT}; border: none; background: transparent;')
        val.setFixedWidth(60)
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(val)
        return row, bar, val

    def _slider_row(self, name, lo, hi, default, suffix=''):
        row = QHBoxLayout()
        row.setSpacing(10)
        lbl = QLabel(name)
        lbl.setFont(QFont(FONT, 9))
        lbl.setStyleSheet(
            f'color: {TEXT_DIM}; border: none; background: transparent;')
        lbl.setFixedWidth(150)
        row.addWidget(lbl)
        slider = NoScrollSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(default)
        slider.setStyleSheet(SLIDER_STYLE)
        row.addWidget(slider)
        val = QLabel(f'{default}{suffix}')
        val.setFont(QFont(FONT, 10, QFont.Bold))
        val.setStyleSheet(
            f'color: {TEXT}; border: none; background: transparent;')
        val.setFixedWidth(90)
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(val)
        return row, slider, val

    def _toggle_row(self, name):
        row = QHBoxLayout()
        row.setSpacing(12)
        lbl = QLabel(name)
        lbl.setFont(QFont(FONT, 9))
        lbl.setStyleSheet(
            f'color: {TEXT_DIM}; border: none; background: transparent;')
        row.addWidget(lbl)
        row.addStretch()
        btn = self._switch()
        row.addWidget(btn)
        return row, btn

    # ── card builders ───────────────────────────────────────────────

    def _card(self):
        card = QFrame()
        card.setStyleSheet(CARD_STYLE)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        vbox = QVBoxLayout(card)
        vbox.setContentsMargins(16, 14, 16, 14)
        vbox.setSpacing(10)
        return card, vbox

    def _build_sensors_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('Sensors'))

        hbox = QHBoxLayout()
        hbox.setSpacing(16)

        # CPU column
        cpu = QVBoxLayout()
        cpu.setSpacing(6)
        cpu.addWidget(self._sub_header('CPU'))

        r, self.cpu_util_bar, self.cpu_util_val = self._sensor_bar(
            'Utilization', 100)
        cpu.addLayout(r)
        r, self.cpu_clk_bar, self.cpu_clk_val = self._sensor_bar(
            'Core Clock', 5500)
        cpu.addLayout(r)
        r, self.cpu_tmp_bar, self.cpu_tmp_val = self._sensor_bar(
            'Temperature', 105)
        cpu.addLayout(r)
        r, self.cpu_fan_bar, self.cpu_fan_val = self._sensor_bar(
            'Fan', 5000)
        cpu.addLayout(r)
        cpu.addStretch()

        # separator
        sep = QFrame()
        sep.setFixedWidth(1)
        sep.setStyleSheet(f'background: {BORDER}; border: none;')

        # GPU column
        gpu = QVBoxLayout()
        gpu.setSpacing(6)
        gpu.addWidget(self._sub_header('GPU'))

        r, self.gpu_util_bar, self.gpu_util_val = self._sensor_bar(
            'Utilization', 100)
        gpu.addLayout(r)
        r, self.gpu_clk_bar, self.gpu_clk_val = self._sensor_bar(
            'Core Clock', 3090)
        gpu.addLayout(r)
        r, self.gpu_mem_bar, self.gpu_mem_val = self._sensor_bar(
            'Memory Clock', 12001)
        gpu.addLayout(r)
        r, self.gpu_tmp_bar, self.gpu_tmp_val = self._sensor_bar(
            'Temperature', 105)
        gpu.addLayout(r)
        r, self.gpu_fan_bar, self.gpu_fan_val = self._sensor_bar(
            'Fan', 5000)
        gpu.addLayout(r)

        hbox.addLayout(cpu)
        hbox.addWidget(sep)
        hbox.addLayout(gpu)
        vbox.addLayout(hbox)
        return card

    def _build_battery_card(self):
        card, vbox = self._card()

        hdr = QHBoxLayout()
        hdr.addWidget(self._header('Battery'))
        hdr.addStretch()
        self.batt_status_lbl = QLabel('--')
        self.batt_status_lbl.setFont(QFont(FONT, 10, QFont.Bold))
        self.batt_status_lbl.setStyleSheet(
            f'color: {TEXT_DIM}; border: none; background: transparent;')
        hdr.addWidget(self.batt_status_lbl)
        vbox.addLayout(hdr)

        r, self.charge_bar, self.charge_val = self._wide_bar(
            'Charge Level', 100)
        vbox.addLayout(r)
        r, self.health_bar, self.health_val = self._wide_bar(
            'Battery Health', 110)
        vbox.addLayout(r)

        cyc = QHBoxLayout()
        cyc.setSpacing(12)
        cl = QLabel('Cycle Count')
        cl.setFont(QFont(FONT, 9))
        cl.setStyleSheet(
            f'color: {TEXT_DIM}; border: none; background: transparent;')
        cl.setFixedWidth(150)
        cyc.addWidget(cl)
        cyc.addStretch()
        self.cycles_val = QLabel('--')
        self.cycles_val.setFont(QFont(FONT, 10, QFont.Bold))
        self.cycles_val.setStyleSheet(
            f'color: {TEXT}; border: none; background: transparent;')
        self.cycles_val.setFixedWidth(60)
        self.cycles_val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        cyc.addWidget(self.cycles_val)
        vbox.addLayout(cyc)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f'background: {BORDER}; border: none;')
        vbox.addWidget(sep)

        cr = QHBoxLayout()
        cr.setSpacing(12)
        ct = QVBoxLayout()
        ct.setSpacing(2)
        t = QLabel('Conservation Mode')
        t.setFont(QFont(FONT, 10, QFont.Bold))
        t.setStyleSheet(
            f'color: {TEXT}; border: none; background: transparent;')
        d = QLabel('Keeps battery between 75-80% to extend lifespan')
        d.setFont(QFont(FONT, 8))
        d.setStyleSheet(
            f'color: {TEXT_MUTED}; border: none; background: transparent;')
        ct.addWidget(t)
        ct.addWidget(d)
        cr.addLayout(ct)
        cr.addStretch()
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
        g = QGridLayout()
        g.setSpacing(6)
        self.profile_btns = {}
        for i, (key, label) in enumerate([
                ('quiet', 'Quiet'), ('balanced', 'Balanced'),
                ('balanced-performance', 'Balanced Performance'),
                ('performance', 'Performance')]):
            btn = self._btn(label, font_size=9)
            btn.clicked.connect(lambda _, m=key: self.set_profile(m))
            g.addWidget(btn, i // 2, i % 2)
            self.profile_btns[key] = btn
        vbox.addLayout(g)
        vbox.addStretch()
        return card

    def _build_gpu_mode_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('GPU Mode'))
        self.gpu_mode_status = self._info()
        vbox.addWidget(self.gpu_mode_status)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.gpu_mode_btns = {}
        for key, label in [('on-demand', 'Hybrid'), ('intel', 'Intel'),
                           ('nvidia', 'NVIDIA')]:
            btn = self._btn(label)
            btn.clicked.connect(lambda _, m=key: self.set_gpu_mode(m))
            row.addWidget(btn)
            self.gpu_mode_btns[key] = btn
        vbox.addLayout(row)
        note = QLabel('Requires reboot to take effect')
        note.setFont(QFont(FONT, 8))
        note.setStyleSheet(
            f'color: {TEXT_MUTED}; border: none; background: transparent;')
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
            f'color: {TEXT_MUTED}; border: none; background: transparent;')
        hdr.addWidget(note)
        vbox.addLayout(hdr)

        # guidance
        tip = QLabel(
            'Tip: Power 80-100W for gaming. Lock GPU clock above 2000 MHz '
            'to prevent downclocking. Memory 11-12 GHz for max bandwidth. '
            'Keep temps below 82\u00b0C. '
            'Run: sudo systemctl enable nvidia-powerd')
        tip.setFont(QFont(FONT, 8))
        tip.setWordWrap(True)
        tip.setStyleSheet(
            f'color: {TEXT_MUTED}; border: none; background: transparent; '
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

        r2, self.gc_slider, self.gc_val = self._slider_row(
            'GPU Clock Min', 0, 3090, 0, '')
        self.gc_val.setText('Auto')
        self.gc_slider.valueChanged.connect(self._on_gc_change)
        self.gc_slider.sliderReleased.connect(self._apply_gpu_clock)
        vbox.addLayout(r2)

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

        reset = self._btn('Reset All Defaults')
        reset.clicked.connect(self._reset_overclock)
        vbox.addWidget(reset)
        return card

    def _build_kbd_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('Keyboard Backlight'))
        self.kbd_status = self._info()
        vbox.addWidget(self.kbd_status)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.kbd_btns = {}
        for name, val in [('Off', 0), ('Low', 1), ('High', 2)]:
            btn = self._btn(name)
            btn.clicked.connect(lambda _, v=val: self.set_kbd(v))
            row.addWidget(btn)
            self.kbd_btns[val] = btn
        vbox.addLayout(row)
        vbox.addStretch()
        return card

    def _build_toggles_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('Quick Settings'))
        r1, self.mic_sw = self._toggle_row('Microphone')
        self.mic_sw.clicked.connect(self.toggle_mic)
        vbox.addLayout(r1)
        r2, self.fn_sw = self._toggle_row('FN Lock')
        self.fn_sw.clicked.connect(self.toggle_fn_lock)
        vbox.addLayout(r2)
        r3, self.tp_sw = self._toggle_row('Touchpad')
        self.tp_sw.clicked.connect(self.toggle_touchpad)
        vbox.addLayout(r3)
        vbox.addStretch()
        return card

    def _build_specs_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('Specifications'))

        def _spec(label_text, value_text):
            row = QHBoxLayout()
            row.setSpacing(12)
            lbl = QLabel(label_text)
            lbl.setFont(QFont(FONT, 9))
            lbl.setStyleSheet(
                f'color: {TEXT_DIM}; border: none; background: transparent;')
            lbl.setFixedWidth(150)
            row.addWidget(lbl)
            val = QLabel(value_text)
            val.setFont(QFont(FONT, 9, QFont.Bold))
            val.setStyleSheet(
                f'color: {TEXT}; border: none; background: transparent;')
            val.setWordWrap(True)
            row.addWidget(val)
            return row

        # CPU
        cpu_model = ''
        try:
            with open('/proc/cpuinfo') as f:
                for line in f:
                    if line.startswith('model name'):
                        cpu_model = line.split(':')[1].strip()
                        break
        except Exception:
            pass
        cores = run_output(['nproc'])
        vbox.addLayout(_spec('CPU', f'{cpu_model} ({cores} threads)'))

        # GPU
        gpu_name = nvidia_query('name')[0]
        gpu_vram = nvidia_query('memory.total')[0]
        if gpu_name:
            gpu_text = gpu_name
            if gpu_vram:
                gpu_text += f' ({gpu_vram} MiB)'
            vbox.addLayout(_spec('GPU', gpu_text))

        # RAM
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        kb = int(line.split()[1])
                        gb = round(kb / 1024 / 1024)
                        vbox.addLayout(_spec('RAM', f'{gb} GB'))
                        break
        except Exception:
            pass

        # Disks
        try:
            result = subprocess.run(
                ['lsblk', '-d', '-o', 'NAME,SIZE,MODEL', '-n'],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.strip().split('\n'):
                parts = line.split(None, 2)
                if len(parts) >= 3 and parts[0].startswith('nvme'):
                    vbox.addLayout(
                        _spec(f'/dev/{parts[0]}',
                              f'{parts[2].strip()} ({parts[1]})'))
        except Exception:
            pass

        # Disk usage
        try:
            result = subprocess.run(
                ['df', '-h', '--output=target,size,used,avail,pcent'],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.strip().split('\n')[1:]:
                parts = line.split()
                if len(parts) == 5 and parts[0] in ('/', '/media/ojee/NVME'):
                    name = 'Root (/)' if parts[0] == '/' else parts[0].split('/')[-1]
                    vbox.addLayout(
                        _spec(name,
                              f'{parts[2]} / {parts[1]} ({parts[4]} used)'))
        except Exception:
            pass

        # Displays
        sep1 = QFrame()
        sep1.setFixedHeight(1)
        sep1.setStyleSheet(f'background: {BORDER}; border: none;')
        vbox.addWidget(sep1)

        try:
            for drm in sorted(glob.glob('/sys/class/drm/card*-*')):
                if read_sys(f'{drm}/status') == 'connected':
                    name = drm.split('/')[-1].split('-', 1)[1]
                    mode = read_sys(f'{drm}/modes').split('\n')[0]
                    vbox.addLayout(_spec('Display', f'{name}  {mode}'))
        except Exception:
            pass

        # Network
        sep2 = QFrame()
        sep2.setFixedHeight(1)
        sep2.setStyleSheet(f'background: {BORDER}; border: none;')
        vbox.addWidget(sep2)

        wifi_ssid = ''
        try:
            result = subprocess.run(
                ['iw', 'dev'], capture_output=True, text=True, timeout=5)
            for line in result.stdout.split('\n'):
                if 'ssid' in line.lower():
                    wifi_ssid = line.split('ssid')[1].strip()
                    break
        except Exception:
            pass
        if wifi_ssid:
            vbox.addLayout(_spec('Wi-Fi', wifi_ssid))

        try:
            result = subprocess.run(
                ['ip', '-br', 'link', 'show'],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.strip().split('\n'):
                parts = line.split()
                if len(parts) >= 2 and parts[0] not in (
                        'lo', 'tailscale0') and not parts[0].startswith('wl'):
                    state = parts[1]
                    vbox.addLayout(_spec(parts[0], state))
        except Exception:
            pass

        bt_name = ''
        try:
            result = subprocess.run(
                ['bluetoothctl', 'show'],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.split('\n'):
                if 'Powered:' in line:
                    bt_name = 'On' if 'yes' in line else 'Off'
                    break
        except Exception:
            pass
        if bt_name:
            vbox.addLayout(_spec('Bluetooth', bt_name))

        return card

    def _build_updates_card(self):
        card, vbox = self._card()
        vbox.addWidget(self._header('System Info'))

        def _vr(label_text):
            row = QHBoxLayout()
            row.setSpacing(12)
            lbl = QLabel(label_text)
            lbl.setFont(QFont(FONT, 9))
            lbl.setStyleSheet(
                f'color: {TEXT_DIM}; border: none; background: transparent;')
            row.addWidget(lbl)
            row.addStretch()
            val = QLabel('--')
            val.setFont(QFont(FONT, 9, QFont.Bold))
            val.setStyleSheet(
                f'color: {TEXT}; border: none; background: transparent;')
            row.addWidget(val)
            return row, val

        r1, self.nv_ver = _vr('NVIDIA Driver')
        vbox.addLayout(r1)
        r2, self.kern_ver = _vr('Kernel')
        vbox.addLayout(r2)
        r3, self.bios_ver = _vr('BIOS')
        vbox.addLayout(r3)

        bot = QHBoxLayout()
        bot.setSpacing(12)
        self.check_btn = self._btn('Check Updates')
        self.check_btn.clicked.connect(self.check_updates)
        bot.addWidget(self.check_btn)
        self.update_status = QLabel('')
        self.update_status.setFont(QFont(FONT, 9))
        self.update_status.setStyleSheet(
            f'color: {TEXT_MUTED}; border: none; background: transparent;')
        bot.addWidget(self.update_status)
        bot.addStretch()
        vbox.addLayout(bot)
        return card

    # ── refresh ─────────────────────────────────────────────────────

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
        # CPU utilization from /proc/stat delta
        cur = read_cpu_stat()
        prev = self._prev_stat
        self._prev_stat = cur
        dt = cur[0] - prev[0]
        di = cur[1] - prev[1]
        pct = round((1 - di / dt) * 100) if dt > 0 else 0
        pct = max(0, min(100, pct))
        self.cpu_util_bar.setValue(pct)
        self.cpu_util_val.setText(f'{pct}%')

        # CPU core clock (max across cores)
        try:
            freq = max(
                (int(read_sys(f) or '0') for f in self.cpu_freq_paths),
                default=0) // 1000
            self.cpu_clk_bar.setValue(freq)
            if freq >= 1000:
                self.cpu_clk_val.setText(f'{freq / 1000:.1f} GHz')
            else:
                self.cpu_clk_val.setText(f'{freq} MHz')
        except (ValueError, TypeError):
            self.cpu_clk_val.setText('--')

        # CPU temp
        try:
            raw = read_sys(f'{self.cpu_zone}/temp')
            t = int(raw) // 1000 if raw else 0
            self.cpu_tmp_bar.setValue(t)
            self.cpu_tmp_val.setText(f'{t}\u00b0C')
        except (ValueError, TypeError):
            self.cpu_tmp_val.setText('--')

        # Fan RPM from legion debug WMI3
        cpu_rpm, gpu_rpm = read_fan_rpm()
        self.cpu_fan_bar.setValue(cpu_rpm)
        self.cpu_fan_val.setText(f'{cpu_rpm} RPM')

        # GPU - batch query
        vals = nvidia_query([
            'utilization.gpu', 'clocks.current.graphics',
            'clocks.current.memory', 'temperature.gpu'])

        for i, (bar, lbl, suffix) in enumerate([
            (self.gpu_util_bar, self.gpu_util_val, '%'),
            (self.gpu_clk_bar, self.gpu_clk_val, ' MHz'),
            (self.gpu_mem_bar, self.gpu_mem_val, ' MHz'),
            (self.gpu_tmp_bar, self.gpu_tmp_val, '\u00b0C'),
        ]):
            try:
                v = int(vals[i])
                bar.setValue(v)
                lbl.setText(f'{v}{suffix}')
            except (ValueError, IndexError):
                lbl.setText('--')

        # GPU fan from same WMI3 read
        self.gpu_fan_bar.setValue(gpu_rpm)
        self.gpu_fan_val.setText(f'{gpu_rpm} RPM')

    def refresh_profile(self):
        p = read_sys('/sys/firmware/acpi/platform_profile')
        self.profile_status.setText(p.upper().replace('-', ' '))
        for key, btn in self.profile_btns.items():
            self._set_btn_active(btn, key == p)

    def refresh_gpu_mode(self):
        mode = run_output(['prime-select', 'query'])
        names = {'on-demand': 'HYBRID', 'intel': 'INTEL ONLY',
                 'nvidia': 'NVIDIA ONLY'}
        self.gpu_mode_status.setText(names.get(mode, mode.upper()))
        for key, btn in self.gpu_mode_btns.items():
            self._set_btn_active(btn, key == mode)

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
        for val, btn in self.kbd_btns.items():
            self._set_btn_active(btn, val == b)

    def refresh_toggles(self):
        mic_muted = 'yes' in run_output(
            ['pactl', 'get-source-mute', '@DEFAULT_SOURCE@'])
        self._style_switch(self.mic_sw, not mic_muted)
        self._style_switch(self.fn_sw,
                           read_sys(f'{IDEAPAD}/fn_lock') == '1')
        tp = run_output(['gsettings', 'get',
                         'org.gnome.desktop.peripherals.touchpad',
                         'send-events'])
        self._style_switch(self.tp_sw, tp == "'enabled'")

    def refresh_battery(self):
        bp = self.bat_path
        if not bp:
            for w in [self.charge_val, self.health_val, self.cycles_val]:
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
        status = read_sys(f'{bp}/status')
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

    # ── actions ─────────────────────────────────────────────────────

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
            f'Switch to {names[mode]}?\n\n'
            'A reboot is required for this change.',
            QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            run_cmd(f'prime-select {mode}')
            self.refresh_gpu_mode()
            QMessageBox.information(
                self, 'GPU Mode',
                f'Set to {names[mode]}.\n'
                'Please reboot for changes to take effect.')

    def _apply_power_limit(self):
        run_cmd(f'nvidia-smi -pl {self.pl_slider.value()}')
        self.refresh_overclock()

    def _on_gc_change(self, val):
        self.gc_val.setText('Auto' if val < 180 else f'{val} MHz')

    def _apply_gpu_clock(self):
        val = self.gc_slider.value()
        if val < 180:
            run_cmd('nvidia-smi -rgc')
        else:
            run_cmd(f'nvidia-smi -lgc {val},3090')

    def _on_mc_change(self, idx):
        self.mc_val.setText(self.mem_clock_labels[idx])

    def _apply_mem_clock(self):
        idx = self.mc_slider.value()
        freq = self.mem_clock_levels[idx]
        if freq == 0:
            run_cmd('nvidia-smi -rmc')
        else:
            run_cmd(f'nvidia-smi -lmc {freq},{freq}')

    def _reset_overclock(self):
        run_cmd(f'nvidia-smi -pl {self.gpu_power_default}')
        run_cmd('nvidia-smi -rgc')
        run_cmd('nvidia-smi -rmc')
        self.mc_slider.blockSignals(True)
        self.mc_slider.setValue(0)
        self.mc_slider.blockSignals(False)
        self.mc_val.setText('Auto')
        self.gc_slider.blockSignals(True)
        self.gc_slider.setValue(0)
        self.gc_slider.blockSignals(False)
        self.gc_val.setText('Auto')
        self.refresh_overclock()

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
        text = (f'{n} update{"s" if n != 1 else ""} available'
                if n else 'System is up to date')
        self._update_done.emit(text)

    def _on_update_done(self, text):
        self.update_status.setText(text)
        self.check_btn.setEnabled(True)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyleSheet(MSG_STYLE)
    win = LOQControl()
    win.show()
    sys.exit(app.exec_())
