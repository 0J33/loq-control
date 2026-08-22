"""Privileged writes — the only place this project changes hardware state.

Every write is here, in one file, each one named and bounded. That is
deliberate: the agent is reachable by any peer on the tailnet holding the
token, and "what can this thing actually do to my laptop" should be answerable
by reading one short module rather than grepping a 3000-line UI.

Two classes of control, and the distinction is not cosmetic:

  SAFE      profile, conservation mode, keyboard backlight, FN lock, mic,
            touchpad. Instant, reversible, and the worst case is a fan gets
            loud. Exposed unconditionally.

  ADVANCED  CPU TDP, GPU core/memory clocks, GPU TGP, GPU mode. These can
            make a machine unstable or unbootable-into-X. Gated behind
            LOQ_ALLOW_ADVANCED and, on the web side, an auto-revert: the value
            is applied, and unless the client confirms within a timeout it goes
            back. A phone that loses signal mid-drag must not be able to leave
            a machine mis-clocked with nobody at the keyboard.

Privilege: these need root, and the agent does NOT run as root. Every
privileged write goes through one root-owned helper script
(`loq-privhelper`) that takes a fixed verb and validates every argument
before it reaches a device. `sudoers.d/ojee-loq` grants NOPASSWD on that
one file and nothing else.

This indirection is the whole security model, so it is worth being explicit
about why the obvious alternatives are wrong:

  sudo tee <path>     grants overwrite of ANY root-owned file — /etc/shadow
                      and /etc/sudoers included.
  sudo sh -c <string>  is a root shell with extra steps.
  sudo nvidia-smi      is fine in isolation but does not cover sysfs, so it
                      ends up alongside one of the two above.

Any of those would turn a bearer token that any tailnet peer might hold into
full root on the machine. See install-agent.sh and loq-privhelper.
"""

from __future__ import annotations

import os
import subprocess

import hardware as hw

IDEAPAD = '/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00'
KBD_BACKLIGHT = '/sys/class/leds/platform::kbd_backlight/brightness'
PLATFORM_PROFILE = '/sys/firmware/acpi/platform_profile'

PROFILES = ('quiet', 'balanced', 'balanced-performance', 'performance')

# Advanced controls are off unless explicitly enabled. Default-deny, because
# the failure mode is a machine you have to walk over to and reboot.
ALLOW_ADVANCED = os.environ.get('LOQ_ALLOW_ADVANCED', '0') in ('1', 'true', 'yes')


class ControlError(RuntimeError):
    def __init__(self, message, *, status=400, code='control_failed'):
        super().__init__(message)
        self.status = status
        self.code = code


def _require_advanced(what: str) -> None:
    if not ALLOW_ADVANCED:
        raise ControlError(
            f'{what} is an advanced control and is disabled. Set '
            f'LOQ_ALLOW_ADVANCED=1 on the agent to permit it.',
            status=403, code='advanced_disabled')


HELPER = os.environ.get('LOQ_HELPER', '/usr/local/lib/ojee-loq/loq-privhelper')


def _priv(*args) -> str:
    """Run one validated verb through the single privileged entry point.

    NOT `sudo tee <path>` and NOT `sudo sh -c <string>`: the first grants
    overwrite of any root-owned file, the second is a root shell outright.
    Either would turn the agent's bearer token into full root on this machine,
    which is the exact thing the sudoers file exists to prevent. The helper
    takes a fixed verb and validates every argument before it touches a device.
    """
    r = subprocess.run(['sudo', '-n', HELPER, *[str(a) for a in args]],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip() or 'permission denied'
        if 'a password is required' in detail or 'no tty' in detail:
            raise ControlError(
                f'the agent may not run {HELPER} without a password. '
                f'Install sudoers.d/ojee-loq — see install-agent.sh.',
                status=500, code='permission_denied')
        raise ControlError(detail, status=500, code='control_failed')
    return r.stdout.strip()


# ── safe controls ──────────────────────────────────────────────────────

def set_profile(name: str) -> dict:
    if name not in PROFILES:
        raise ControlError(f'unknown profile {name!r}; have {", ".join(PROFILES)}')
    _priv('profile', name)
    return {'profile': hw.read_sys(PLATFORM_PROFILE)}


def set_conservation(on: bool) -> dict:
    """75-80% charge cap. The single most useful control here for battery life."""
    _priv('conservation', 1 if on else 0)
    return {'conservation': hw.read_sys(f'{IDEAPAD}/conservation_mode') == '1'}


def set_fn_lock(on: bool) -> dict:
    _priv('fnlock', 1 if on else 0)
    return {'fnLock': hw.read_sys(f'{IDEAPAD}/fn_lock') == '1'}


def set_kbd_backlight(level: int) -> dict:
    """0 off, 1 low, 2 high. Writable by the user — no sudo needed."""
    level = max(0, min(2, int(level)))
    try:
        with open(KBD_BACKLIGHT, 'w') as fh:
            fh.write(str(level))
    except OSError:
        _priv('kbdlight', level)
    return {'kbdBacklight': int(hw.read_sys(KBD_BACKLIGHT) or 0)}


def set_mic_muted(muted: bool) -> dict:
    subprocess.run(['pactl', 'set-source-mute', '@DEFAULT_SOURCE@', '1' if muted else '0'],
                   capture_output=True, timeout=10)
    out = subprocess.run(['pactl', 'get-source-mute', '@DEFAULT_SOURCE@'],
                         capture_output=True, text=True, timeout=10).stdout
    return {'micMuted': 'yes' in out.lower()}


def set_touchpad(enabled: bool) -> dict:
    subprocess.run(['gsettings', 'set', 'org.gnome.desktop.peripherals.touchpad',
                    'send-events', 'enabled' if enabled else 'disabled'],
                   capture_output=True, timeout=10)
    return {'touchpad': enabled}


# ── advanced controls ──────────────────────────────────────────────────

def set_cpu_tdp(watts: int) -> dict:
    """Intel RAPL long-term power limit."""
    _require_advanced('CPU TDP')
    lo, hi = hw.get_rapl_info()
    watts = max(10, min(int(hi or 200), int(watts)))
    _priv('tdp', watts)
    return {'cpuTdp': watts}


def set_gpu_clock(mhz: int | None) -> dict:
    """Lock the GPU core clock, or None to release it."""
    _require_advanced('GPU core clock')
    if mhz is None:
        _priv('gpuclock', 'reset')
        return {'gpuClock': None}
    mhz = max(hw.GPU_CLOCK_MIN, min(hw.GPU_CLOCK_MAX, int(mhz)))
    _priv('gpuclock', mhz, hw.GPU_CLOCK_MAX)
    return {'gpuClock': mhz}


def set_gpu_mem_clock(mhz: int | None) -> dict:
    _require_advanced('GPU memory clock')
    if mhz is None:
        _priv('gpumemclock', 'reset')
        return {'gpuMemClock': None}
    _priv('gpumemclock', int(mhz), int(mhz))
    return {'gpuMemClock': int(mhz)}


def set_gpu_tgp(watts: int) -> dict:
    """GPU total graphics power, via the Lenovo WMAE ACPI method.

    nvidia-smi -pl is locked on mobile Blackwell, so this routes through the EC.
    """
    _require_advanced('GPU TGP')
    watts = max(45, min(100, int(watts)))
    _priv('gputgp', watts)
    return {'gpuTgp': watts}


def set_gpu_mode(mode: str) -> dict:
    """hybrid | intel | nvidia. Requires a REBOOT to take effect."""
    _require_advanced('GPU mode')
    if mode not in ('hybrid', 'intel', 'nvidia'):
        raise ControlError(f'unknown gpu mode {mode!r}')
    _priv('gpumode', mode)
    return {
        'gpuMode': mode,
        # Said plainly, because a control that appears to do nothing is worse
        # than one that refuses.
        'note': 'takes effect after a reboot',
    }


# ── dispatch ───────────────────────────────────────────────────────────

SAFE = {
    'profile':      lambda v: set_profile(str(v)),
    'conservation': lambda v: set_conservation(bool(v)),
    'fnLock':       lambda v: set_fn_lock(bool(v)),
    'kbdBacklight': lambda v: set_kbd_backlight(int(v)),
    'micMuted':     lambda v: set_mic_muted(bool(v)),
    'touchpad':     lambda v: set_touchpad(bool(v)),
}

ADVANCED = {
    'cpuTdp':       lambda v: set_cpu_tdp(int(v)),
    'gpuClock':     lambda v: set_gpu_clock(None if v in (None, '', 'auto') else int(v)),
    'gpuMemClock':  lambda v: set_gpu_mem_clock(None if v in (None, '', 'auto') else int(v)),
    'gpuTgp':       lambda v: set_gpu_tgp(int(v)),
    'gpuMode':      lambda v: set_gpu_mode(str(v)),
}

ALL = {**SAFE, **ADVANCED}


def apply(key: str, value) -> dict:
    fn = ALL.get(key)
    if fn is None:
        raise ControlError(f'unknown control {key!r}', status=404, code='unknown_control')
    return fn(value)


def capabilities() -> dict:
    """What this machine can actually do, so the UI renders only real controls.

    Probed rather than assumed: a LOQ without the legion_laptop module has no
    platform_profile, and rendering a profile switcher that silently fails is
    worse than not rendering one.
    """
    return {
        'profile':      os.path.exists(PLATFORM_PROFILE),
        'conservation': os.path.exists(f'{IDEAPAD}/conservation_mode'),
        'fnLock':       os.path.exists(f'{IDEAPAD}/fn_lock'),
        'kbdBacklight': os.path.exists(KBD_BACKLIGHT),
        'micMuted':     bool(subprocess.run(['which', 'pactl'], capture_output=True).returncode == 0),
        'touchpad':     bool(subprocess.run(['which', 'gsettings'], capture_output=True).returncode == 0),
        'cpuTdp':       os.path.exists(f'{hw.RAPL}/constraint_0_power_limit_uw'),
        'gpuClock':     bool(hw.nvidia_query(['name'])),
        'gpuMemClock':  bool(hw.nvidia_query(['name'])),
        'gpuTgp':       os.path.exists('/proc/acpi/call') or os.path.exists('/sys/module/acpi_call'),
        'gpuMode':      subprocess.run(['which', 'prime-select'], capture_output=True).returncode == 0,
        'advancedEnabled': ALLOW_ADVANCED,
    }
