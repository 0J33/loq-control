# LOQ Control

A desktop control center for Lenovo LOQ laptops on Linux. Built with PyQt5.

Inspired by [LenovoLegionToolkit](https://github.com/BartoszCichecki/LenovoLegionToolkit) — bringing similar functionality to Linux natively.

![LOQ Control](screenshots/1.png)

## Features

### Monitoring
- **Sensors** — Real-time CPU & GPU utilization, clock speed, memory clock, temperature, fan RPM with progress bars
- **Per-Core CPU Heatmap** — Visual grid showing utilization of every CPU core
- **Temperature History** — Rolling 3-minute line graph of CPU and GPU temperatures
- **Thermal Throttle Detection** — Real-time alert when CPU or GPU is being throttled
- **Activity Monitor** — RAM usage, disk read/write speed, network upload/download speed
- **GPU Processes** — List of processes currently using the NVIDIA GPU with memory usage

### Power & Performance
- **Performance Profiles** — Quiet, Balanced, Balanced Performance, Performance
- **CPU Power Limit (TDP)** — Adjustable via Intel RAPL with slider markers for TDP reference
- **GPU Overclock** — GPU clock lock, memory clock control with recommended position markers
- **GPU Mode** — Switch between Hybrid, Intel Only, and NVIDIA Only (requires reboot)

### Battery
- **Battery Status** — Charge level, health percentage, cycle count, charge/discharge rate, estimated time remaining
- **Conservation Mode** — Toggle 75-80% charge limit to extend battery lifespan
- **Battery History** — Daily logging of battery health and cycle count to CSV

### Quick Settings
- **Keyboard Backlight** — Off / Low / High
- **Microphone Toggle** — Mute/unmute default input
- **FN Lock Toggle** — Swap function key behavior
- **Touchpad Toggle** — Enable/disable touchpad

### System
- **Specifications** — CPU, GPU, RAM, all disks with usage, connected displays with resolution, Wi-Fi, Ethernet, Bluetooth
- **System Info** — NVIDIA driver, kernel, BIOS versions with update checker
- **Dark / Light Theme** — Toggle between themes (saved to config)
- **System Tray Icon** — Minimize to tray with quick profile switching from the context menu
- **Autostart** — Optional start on login (minimized to tray)
- **Export Specs** — Copy full system specifications to clipboard
- **Keyboard Shortcuts** — Ctrl+1-4 for profiles, Ctrl+Q to quit, Ctrl+E to export specs
- **Config Persistence** — All settings saved to `~/.config/loq-control/config.json`

## Tested On

- **Laptop:** Lenovo LOQ 16IRX9 (83JE)
- **CPU:** Intel Core i7-14700HX
- **GPU:** NVIDIA GeForce RTX 5060 Laptop GPU
- **OS:** Zorin OS 17.3 (Ubuntu 24.04 based)
- **Kernel:** 6.8.0+
- **Driver:** NVIDIA 590.48.01
- **Session:** Wayland (GNOME)

## Requirements

- Python 3
- PyQt5
- NVIDIA drivers with `nvidia-smi`
- [`legion_laptop`](https://github.com/johnfanv2/LenovoLegionLinux) kernel module (for performance profiles and fan RPM)
- `ideapad_acpi` kernel module (for conservation mode and FN lock)

### Install dependencies

```bash
sudo apt install python3-pyqt5
```

### Install legion_laptop (if not already present)

```bash
git clone https://github.com/johnfanv2/LenovoLegionLinux.git
cd LenovoLegionLinux/kernel_module
sudo make dkms
```

This installs the module via DKMS so it automatically rebuilds on kernel updates.

## Usage

```bash
chmod +x loq-control.py
./loq-control.py
```

Launch minimized to tray:

```bash
./loq-control.py --minimized
```

Some features require root access (performance profiles, conservation mode, GPU overclock, fan RPM reading). The app uses `sudo` for these operations — you may want to configure passwordless sudo for the relevant sysfs paths.

### Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| Ctrl+1 | Quiet profile |
| Ctrl+2 | Balanced profile |
| Ctrl+3 | Balanced Performance profile |
| Ctrl+4 | Performance profile |
| Ctrl+E | Export specs to clipboard |
| Ctrl+Q | Quit |

## How It Works

| Feature | Backend |
|---|---|
| Performance profiles | `/sys/firmware/acpi/platform_profile` via `legion_laptop` |
| Conservation mode | `/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00/conservation_mode` |
| FN Lock | `/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00/fn_lock` |
| Keyboard backlight | `/sys/class/leds/platform::kbd_backlight/brightness` |
| CPU sensors | `/proc/stat`, `/sys/devices/system/cpu/*/cpufreq/`, `/sys/class/thermal/` |
| CPU TDP | `/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw` |
| GPU sensors | `nvidia-smi` CSV query |
| Fan RPM | `legion_laptop` WMI3 debug interface (`/sys/kernel/debug/legion/fancurve`) |
| GPU overclock | `nvidia-smi -lgc` (clock lock), `nvidia-smi -lmc` (memory clock) |
| GPU mode | `prime-select` |
| RAM usage | `/proc/meminfo` |
| Disk I/O | `/proc/diskstats` |
| Network speed | `/proc/net/dev` |
| Microphone | `pactl` |
| Touchpad | `gsettings` |

## Notes

- GPU overclock settings are saved to config and re-applied on app startup, but do not survive reboots without the app running
- GPU mode changes require a reboot
- The `balanced-performance` profile is a hidden mode exposed by the kernel's `platform_profile` driver that Lenovo Vantage on Windows does not surface
- Fan RPM uses the WMI3 access method via the `legion_laptop` debug interface, as the default EC method returns 0 on some LOQ models
- Battery is detected at BAT1 (not BAT0) on LOQ models

## Disclaimer

"LOQ" and the LOQ logo are trademarks of Lenovo Group Limited. This project is not affiliated with, endorsed by, or sponsored by Lenovo. The LOQ logo is used for identification purposes only.

## License

MIT

## Author

**ojee** — [ojee.net](https://ojee.net)
