# hwcheck

**Hardware health and second-hand checker for Linux** (Pop!_OS / Ubuntu / Debian).

`hwcheck` reads and tests your CPU, GPU, RAM and disks and gives each part a health score. It is built for one main job: **checking used hardware before you pay for it**, and comparing a PC **before and after** a hardware change.

It runs as a modern terminal UI (TUI) or as a plain text report.

---

## What can (and cannot) be measured

| Part | What hwcheck does |
|---|---|
| **Storage (SSD / HDD / NVMe)** | Reads SMART data: power-on time, power cycles, data written, wear (life used), error counters, temperature. This is the only part of a PC that really "remembers" how much it was used. |
| **CPU** | Has no usage counter, so it tests the *condition*: temperature, hardware errors (MCE), and a 60 s all-core stress test with result verification (clock speed, throttling). |
| **RAM** | Has no usage counter either. Reads the module info (size, type, speed, maker) and can run `memtester` to look for errors. |
| **GPU** | No usage counter. Detects the GPU and shows live stats where available (full details for NVIDIA via `nvidia-smi`). |
| **USB sticks** | No SMART data, so no health score. The Buyer check tab explains how to test real capacity with `f3`. |

---

## Requirements

- A Debian-based Linux (Pop!_OS, Ubuntu, Debian, ...) with `apt`
- Python 3 (the setup script installs `python3-venv` and `pip` if missing)
- `sudo` access (hardware data needs root)

`setup.sh` installs everything else for you.

---

## Installation

```bash
git clone https://github.com/myweb768/custome_linux_tool.git
cd custome_linux_tool/hwcheck
bash setup.sh
```

> Run `bash setup.sh` as a **normal user**, not with `sudo`. The script asks for your password when it needs it.

### What `setup.sh` does

1. Installs the system packages with apt: `smartmontools`, `dmidecode`, `stress-ng`, `memtester`, `sysbench`, `fio`, `pciutils`, `lm-sensors`, `python3-venv`
2. Copies `hwcheck.py` to `~/.hwcheck/`
3. Creates a Python virtual environment in `~/.hwcheck/venv` and installs `psutil`, `rich`, `textual`
4. Loads the temperature sensor kernel modules (`coretemp` or `k10temp`, and `nct6775` if present) and makes them load at every boot
5. Creates the `hwcheck` command in `/usr/local/bin/`

When it finishes, just type `hwcheck`.

---

## Usage

```bash
hwcheck                # open the TUI (default)
hwcheck report         # plain text report
hwcheck snapshot       # save a baseline (before changing hardware)
hwcheck compare        # compare now vs the saved baseline
hwcheck stress         # 60 s CPU stress / stability test
```

The command asks for your sudo password automatically.

### Options

| Option | Works with | Description |
|---|---|---|
| `--bench` | report, snapshot, compare | Run benchmarks: CPU (single + multi thread), RAM read/write, disk sequential read/write and random 4K read |
| `--memtest MB` | report, snapshot, compare | Run `memtester` on `MB` megabytes of RAM (one pass, can take several minutes) |
| `--stress` | report, snapshot, compare | Also run the 60 s CPU stress test |

Examples:

```bash
hwcheck report --bench
hwcheck report --memtest 1024
hwcheck report --bench --memtest 1024 --stress
```

### TUI keys

| Key | Action |
|---|---|
| `1` - `8` | Jump to a tab: Overview, Live, CPU, GPU, RAM, Storage, Buyer check, Compare |
| `r` | Rescan hardware |
| `b` | Run benchmarks |
| `t` | Run the CPU stress test |
| `m` | Run the RAM test (memtester) |
| `s` | Save a baseline |
| `q` | Quit |

---

## Typical workflows

### 1. Check a used PC or part before buying

```bash
hwcheck                      # look at Overview, Storage and Buyer check tabs
```

Then press `t` (CPU stress) and `m` (RAM test) and watch temperature and clocks. Read the **Buyer check** tab: it lists what to inspect for every part.

### 2. Compare before and after a hardware change

Take a full baseline **before** changing anything:

```bash
hwcheck snapshot --bench --memtest 1024 --stress
```

Change the hardware, then run:

```bash
hwcheck compare --bench --memtest 1024 --stress
```

> Each command runs on its own, so include `--bench`, `--memtest` and `--stress` in **both** commands. Otherwise there is nothing to compare for those tests.

---

## Reading the report

- **Health score (0-100)**: Excellent / Good / Fair / Poor
- **Tier (Low / Medium / High)**: how powerful the part is
- **Usage**: how heavily a part has been used (storage only; others show "Not readable")
- **Condition**: GOOD / CAUTION / RISKY / UNKNOWN based on the tests that were run (CPU and RAM stay UNKNOWN until you run the stress test / memtester)
- **Errors `r/p/u/m/crc`** in the storage table: reallocated / pending / uncorrectable / media / interface CRC errors

Notes:

- **CRC errors** on a SATA disk usually mean a bad cable or port, not a dying drive.
- Some SSDs report "data written" in units that SMART does not specify. If the plain reading is clearly impossible for a worn drive, hwcheck assumes 32 MiB units, so the **written** value of some drives is an **estimate**.
- "UNKNOWN" for CPU, GPU and RAM is normal until you run the tests: those parts keep no usage history.

---

## Where files are saved

Everything lives in `~/.hwcheck/`:

| File | Content |
|---|---|
| `hwcheck.py` | the program |
| `venv/` | Python virtual environment |
| `baseline.json` | the saved baseline (used by `compare`) |
| `snapshot-<date>.json` | a dated copy of each snapshot |
| `latest.json` | the result of the last plain-command run |

Copy `baseline.json` to a USB stick if you want a backup.

---

## Updating

Replace `hwcheck.py` in the repo with the new version, then run `bash setup.sh` again. It overwrites the program and keeps your baseline files.

Or, to update only the script:

```bash
cp hwcheck.py ~/.hwcheck/hwcheck.py
```

## Uninstall

```bash
rm -rf ~/.hwcheck
sudo rm -f /usr/local/bin/hwcheck /etc/modules-load.d/hwcheck-sensors.conf
```

The apt packages are normal system tools and are left installed.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Please run with sudo` | Use the `hwcheck` command (it adds sudo), or `sudo ~/.hwcheck/venv/bin/python ~/.hwcheck/hwcheck.py` |
| `smartctl / dmidecode is not installed` | Run `bash setup.sh` again |
| `The TUI needs the 'textual' package` | `~/.hwcheck/venv/bin/pip install textual` |
| `Temperature could not be read` | `sudo sensors-detect --auto`, then `sudo modprobe coretemp` (Intel) or `sudo modprobe k10temp` (AMD) |
| `stress-ng is not installed` | `sudo apt install stress-ng` |
| `memtester is not installed` | `sudo apt install memtester` |
| GPU shows `N/A` everywhere | Normal for integrated graphics; full live stats need an NVIDIA GPU with `nvidia-smi` |
| USB drive shows "Not readable" | Normal: most USB sticks do not report SMART data |

## Safety notes

- The stress test and benchmarks put the PC under heavy load. Make sure cooling works and save your work first.
- The disk benchmark writes a temporary 1 GB file (`fio.tmp`) in your home `.hwcheck` folder and deletes it afterwards.
- `hwcheck` only **reads** SMART data. It never writes to your disks, apart from that temporary benchmark file.

## License

Add your license here (for example MIT).
