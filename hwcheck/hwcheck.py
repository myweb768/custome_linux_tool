#!/usr/bin/env python3
"""
hwcheck - hardware health & second-hand checker for Linux (Pop!_OS / Ubuntu)

  sudo python3 hwcheck.py              # open the modern TUI (default)
  sudo python3 hwcheck.py report       # plain text report
  sudo python3 hwcheck.py snapshot     # save baseline (before changing hardware)
  sudo python3 hwcheck.py compare      # compare now vs baseline
  sudo python3 hwcheck.py stress       # 60s CPU stress/stability test

Options (plain commands): --bench  --memtest MB  --stress

What can be measured?
  Storage (SSD/HDD/NVMe): keeps a real usage history (SMART) -> power-on time,
      data written, wear, errors. This is the only part that "remembers" its use.
  CPU / GPU / RAM: have NO usage counters. For these the tool tests their CONDITION
      (stress test, temperatures, throttling, errors, memtester) instead.
"""
import argparse
import collections
import datetime
import glob
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import time

import psutil
from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

__version__ = "3.0"
console = Console()

# Tokyo-night inspired palette
C = dict(good="#9ece6a", warn="#e0af68", bad="#f7768e", orange="#ff9e64", accent="#7aa2f7",
         cyan="#7dcfff", purple="#bb9af7", dim="#565f89", muted="#8c94bd", text="#c0caf5")

TIER_NUM = {"Low": 1, "Medium": 2, "High": 3}
NUM_TIER = {1: "Low", 2: "Medium", 3: "High"}
BENCH_NAMES = {
    "cpu_single": "CPU single-thread (events/s)",
    "cpu_multi": "CPU multi-thread (events/s)",
    "ram_read_mib": "RAM read (MiB/s)",
    "ram_write_mib": "RAM write (MiB/s)",
    "ssd_seq_read_mbs": "Disk sequential read (MB/s)",
    "ssd_seq_write_mbs": "Disk sequential write (MB/s)",
    "ssd_rand_read_iops": "Disk random 4K read (IOPS)",
}


# ================================================================ helpers
def sh(cmd, timeout=180):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def base_dir():
    user = os.environ.get("SUDO_USER") or "root"
    d = os.path.join(pwd.getpwnam(user).pw_dir, ".hwcheck")
    os.makedirs(d, exist_ok=True)
    return d


def fix_owner(d):
    user = os.environ.get("SUDO_USER")
    if not user:
        return
    pw = pwd.getpwnam(user)
    for p in [d] + [os.path.join(d, f) for f in os.listdir(d)]:
        try:
            os.chown(p, pw.pw_uid, pw.pw_gid)
        except OSError:
            pass


def clamp(x):
    return max(1, min(100, int(round(x))))


def num(text):
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


def flt(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def health_text(score):
    if score is None:
        return "Not scored"
    return "Excellent" if score >= 90 else "Good" if score >= 75 else "Fair" if score >= 50 else "Poor"


def health_color(score):
    if score is None:
        return C["dim"]
    return C["good"] if score >= 75 else C["warn"] if score >= 50 else C["bad"]


def fmt_score(score):
    return "N/A" if score is None else f"{score}/100"


def fmt_hours(h):
    if h is None:
        return "N/A"
    days = h / 24
    return f"{days:,.0f} days ({days / 365:.1f} yrs)"


def fmt_bytes(b):
    if not b:
        return "N/A"
    return f"{b / 1e12:.2f} TB" if b >= 1e12 else f"{b / 1e9:.0f} GB"


def est_life_left(wear_used, hours):
    """Very rough: assumes the drive keeps wearing at the same rate as so far."""
    if wear_used is None or not hours:
        return "N/A"
    if wear_used < 1:
        return "N/A (too little wear)"
    left = (hours / (wear_used / 100) - hours) / 8760
    return "> 20 yrs" if left > 20 else f"~{left:.1f} yrs of use"


def cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
        if temps.get(key):
            return max(t.current for t in temps[key])
    return None


def throttle_count():
    total = 0
    for p in glob.glob("/sys/devices/system/cpu/cpu*/thermal_throttle/*_throttle_count"):
        try:
            total += int(open(p).read().strip())
        except (OSError, ValueError):
            pass
    return total


# ================================================================== tiers
def tier_cpu(cpu):
    t = cpu.get("threads") or 0
    return "High" if t >= 12 else ("Medium" if t >= 6 else "Low")


def tier_ram(ram):
    gb = ram["total_gb"]
    level = 1 if gb < 8 else (2 if gb < 16 else 3)
    if {m.get("type") for m in ram["modules"]} & {"DDR", "DDR2", "DDR3"}:
        level = 1 if gb < 8 else min(level, 2)
    return NUM_TIER[level]


def tier_disk(d):
    return {"NVMe": "High", "SSD": "Medium", "HDD": "Low", "USB": "Low"}.get(d["kind"], "Low")


def tier_gpu(g):
    if g["kind"] == "Integrated":
        return "Low"
    mb = g.get("vram_mb") or 0
    return "High" if mb >= 12000 else ("Medium" if mb >= 6000 else "Low")


# =================================================================== CPU
def collect_cpu():
    model = ""
    for line in sh(["lscpu"]).splitlines():
        if line.startswith("Model name"):
            model = line.split(":", 1)[1].strip()
    socket = None
    for line in sh(["dmidecode", "-t", "processor"]).splitlines():
        if line.strip().startswith("Upgrade:"):
            socket = line.split(":", 1)[1].strip()
            break
    freq = psutil.cpu_freq()
    mce = len(re.findall(r"machine check|\[Hardware Error\]", sh(["dmesg"]), re.I))
    return {
        "model": model, "socket": socket,
        "cores": psutil.cpu_count(logical=False), "threads": psutil.cpu_count(logical=True),
        "max_mhz": round(freq.max) if freq and freq.max else None,
        "temp_c": cpu_temp(), "mce_errors": mce, "stress": None,
    }


def score_cpu(cpu, bench):
    s, notes = 100, []
    temps = [cpu.get("temp_c"), (bench or {}).get("cpu_temp_load")]
    st = cpu.get("stress")
    if st and not st.get("error"):
        temps.append(st.get("peak_temp"))
    temps = [t for t in temps if t]
    temp = max(temps) if temps else None
    if temp:
        if temp > 90:
            s -= 30; notes.append(f"Temperature is very high ({temp:.0f} C)")
        elif temp > 80:
            s -= 15; notes.append(f"Temperature is high ({temp:.0f} C)")
        elif temp > 70:
            s -= 5; notes.append(f"Temperature is a bit high ({temp:.0f} C)")
    else:
        notes.append("Temperature could not be read (run: sudo sensors-detect --auto)")
    if cpu["mce_errors"]:
        s -= 30
        notes.append(f"{cpu['mce_errors']} hardware error(s) in the kernel log")
    if st and not st.get("error"):
        if not st["passed"]:
            return 1, ["Stress test FAILED (calculation errors or crash). CPU/board is unstable"]
        if st["throttle_events"]:
            s -= 15; notes.append(f"CPU throttled {st['throttle_events']} time(s) during the stress test")
        if st.get("sustained_pct") and st["sustained_pct"] < 85:
            s -= 10; notes.append(f"Clock dropped to {st['sustained_pct']:.0f}% of max under load")
    elif not st:
        notes.append("CPU stress test not run yet (press t in the TUI, or use the stress command)")
    return clamp(s), notes


def run_cpu_stress(say, seconds=60):
    if not shutil.which("stress-ng"):
        return {"error": "stress-ng is not installed (sudo apt install stress-ng)"}
    n = psutil.cpu_count(logical=True)
    max_mhz = (psutil.cpu_freq().max if psutil.cpu_freq() else 0) or 0
    thr0 = throttle_count()
    proc = subprocess.Popen(
        ["stress-ng", "--cpu", str(n), "--cpu-method", "matrixprod", "--verify",
         "--timeout", f"{seconds}s", "--metrics-brief"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    temps, freqs, t0 = [], [], time.time()
    while proc.poll() is None:
        time.sleep(1)
        t = cpu_temp()
        f = psutil.cpu_freq()
        temps.append(t)
        freqs.append(f.current if f else 0)
        say(f"CPU stress test: {int(time.time() - t0)}/{seconds}s, temp {t:.0f} C" if t
            else f"CPU stress test: {int(time.time() - t0)}/{seconds}s")
    out = proc.stdout.read() if proc.stdout else ""
    temps = [t for t in temps if t]
    half = freqs[len(freqs) // 2:] or freqs
    avg = sum(half) / len(half) if half else 0
    return {
        "seconds": seconds, "passed": proc.returncode == 0,
        "peak_temp": max(temps) if temps else None,
        "avg_mhz": round(avg), "max_mhz": round(max_mhz),
        "sustained_pct": (avg / max_mhz * 100) if max_mhz else None,
        "throttle_events": max(0, throttle_count() - thr0),
        "exit_code": proc.returncode, "log_tail": out.strip().splitlines()[-4:],
        "when": datetime.datetime.now().isoformat(timespec="seconds"),
    }


# =================================================================== GPU
GPU_VENDORS = {"10de": "NVIDIA", "1002": "AMD", "8086": "Intel"}
NV_FIELDS = ["name", "driver_version", "vbios_version", "memory.total", "temperature.gpu",
             "power.draw", "power.limit", "clocks.gr", "clocks.max.gr", "utilization.gpu",
             "pcie.link.gen.current", "pcie.link.gen.max", "pcie.link.width.current",
             "pcie.link.width.max"]
AMD_APU = re.compile(r"Renoir|Cezanne|Barcelo|Lucienne|Rembrandt|Phoenix|Raphael|Mendocino|Picasso|Raven|Hawk Point", re.I)
GT_TO_GEN = {"2.5": 1, "5.0": 2, "8.0": 3, "16.0": 4, "32.0": 5, "64.0": 6}


def detect_gpus():
    gpus = []
    for line in sh(["lspci", "-mm", "-nn"]).splitlines():
        try:
            f = shlex.split(line)
        except ValueError:
            continue
        if len(f) < 4 or not re.search(r"VGA|3D controller|Display controller", f[1]):
            continue
        m = re.search(r"\[([0-9a-f]{4})\]", f[2])
        vid = m.group(1) if m else ""
        vendor = GPU_VENDORS.get(vid) or re.sub(r"\s*\[[0-9a-f]{4}\]", "", f[2])
        dev = re.sub(r"\s*\[[0-9a-f]{4}\]\s*$", "", f[3])
        m = re.search(r"\[([^\]]+)\]", dev)
        name = m.group(1) if m else dev
        kind = "Integrated" if vid == "8086" or (vid == "1002" and AMD_APU.search(dev)) else "Discrete"
        gpus.append({"slot": f[0], "vendor": vendor, "vid": vid, "name": f"{vendor} {name}",
                     "kind": kind, "vram_mb": None, "driver": None, "vbios": None,
                     "temp_c": None, "power_w": None, "power_limit_w": None, "util": None,
                     "clock_mhz": None, "clock_max_mhz": None, "pcie_gen": None,
                     "pcie_gen_max": None, "pcie_width": None, "pcie_width_max": None,
                     "throttle": None})
    refresh_gpu_stats(gpus)
    return gpus


def nv_rows():
    if not shutil.which("nvidia-smi"):
        return []
    for extra in (["clocks_throttle_reasons.active"], ["clocks_event_reasons.active"], []):
        fields = NV_FIELDS + extra
        out = sh(["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"], 20)
        rows = [[x.strip() for x in l.split(",")] for l in out.strip().splitlines() if l.strip()]
        if rows and all(len(r) == len(fields) for r in rows):
            return [dict(zip(fields, r)) for r in rows]
    return []


def decode_throttle(raw):
    try:
        mask = int(raw, 16)
    except (TypeError, ValueError):
        return None
    why = [name for bit, name in ((0x8, "HW slowdown"), (0x20, "SW thermal"), (0x40, "HW thermal"),
                                  (0x4, "SW power cap"), (0x80, "power brake")) if mask & bit]
    return ", ".join(why) or None


def _read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return None


def refresh_gpu_stats(gpus):
    nv = nv_rows()
    nv_i = 0
    for g in gpus:
        if g["vid"] == "10de" and nv_i < len(nv):
            r, nv_i = nv[nv_i], nv_i + 1
            g.update(driver=r["driver_version"], vbios=r["vbios_version"],
                     vram_mb=flt(r["memory.total"]), temp_c=flt(r["temperature.gpu"]),
                     power_w=flt(r["power.draw"]), power_limit_w=flt(r["power.limit"]),
                     clock_mhz=flt(r["clocks.gr"]), clock_max_mhz=flt(r["clocks.max.gr"]),
                     util=flt(r["utilization.gpu"]),
                     pcie_gen=flt(r["pcie.link.gen.current"]), pcie_gen_max=flt(r["pcie.link.gen.max"]),
                     pcie_width=flt(r["pcie.link.width.current"]), pcie_width_max=flt(r["pcie.link.width.max"]),
                     throttle=decode_throttle(r.get("clocks_throttle_reasons.active")
                                              or r.get("clocks_event_reasons.active")))
            continue
        for dev in glob.glob("/sys/class/drm/card[0-9]*/device"):
            if not (_read(os.path.join(dev, "uevent")) or "").count(g["slot"]):
                continue
            vram = flt(_read(os.path.join(dev, "mem_info_vram_total")))
            if vram:
                g["vram_mb"] = vram / 1024**2
            g["util"] = flt(_read(os.path.join(dev, "gpu_busy_percent")))
            for hw in glob.glob(os.path.join(dev, "hwmon", "hwmon*")):
                t = flt(_read(os.path.join(hw, "temp1_input")))
                if t:
                    g["temp_c"] = t / 1000
                p = flt(_read(os.path.join(hw, "power1_average")) or _read(os.path.join(hw, "power1_input")))
                if p:
                    g["power_w"] = p / 1e6
            for key, name in (("current_link_speed", "pcie_gen"), ("max_link_speed", "pcie_gen_max")):
                m = re.search(r"([\d.]+) GT/s", _read(os.path.join(dev, key)) or "")
                if m:
                    g[name] = GT_TO_GEN.get(m.group(1))
            g["pcie_width"] = flt(_read(os.path.join(dev, "current_link_width")))
            g["pcie_width_max"] = flt(_read(os.path.join(dev, "max_link_width")))
            break


# =================================================================== RAM
MAKERS = [("KVR", "Kingston"), ("KHX", "Kingston"), ("HX", "Kingston HyperX"), ("CT", "Crucial"),
          ("BL", "Crucial Ballistix"), ("MT", "Micron"), ("M3", "Samsung"), ("M4", "Samsung"),
          ("HMT", "SK hynix"), ("HMA", "SK hynix"), ("HMC", "SK hynix"), ("NT", "Nanya"),
          ("F4-", "G.Skill"), ("F3-", "G.Skill"), ("CM", "Corsair"), ("AD", "ADATA"),
          ("AX", "ADATA"), ("PSD", "Patriot"), ("TF", "Transcend"), ("JM", "Transcend")]


def maker_name(manufacturer, part):
    m = (manufacturer or "").strip()
    if m and not re.fullmatch(r"[0-9A-Fa-f]{2,4}|Unknown|Undefined|Not Specified|0000|Manufacturer\d*", m, re.I):
        return m
    for prefix, name in MAKERS:
        if (part or "").upper().startswith(prefix):
            return f"{name} (from part no.)"
    return m or "Unknown"


def collect_ram():
    modules = []
    for block in sh(["dmidecode", "-t", "memory"]).split("Memory Device")[1:]:
        f = {}
        for line in block.splitlines():
            if ":" in line:
                k, v = line.strip().split(":", 1)
                f[k.strip()] = v.strip()
        if not f.get("Size") or f["Size"].startswith("No Module"):
            continue
        part = f.get("Part Number", "").strip()
        modules.append({
            "slot": f.get("Locator"), "size": f.get("Size"), "type": f.get("Type"),
            "rated_mts": num(f.get("Speed")),
            "configured_mts": num(f.get("Configured Memory Speed") or f.get("Configured Clock Speed")),
            "manufacturer": maker_name(f.get("Manufacturer"), part), "part": part,
            "serial": f.get("Serial Number"),
        })
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    return {"total_gb": round(vm.total / 1024**3, 1), "used_percent": vm.percent,
            "swap_percent": sw.percent, "modules": modules, "memtest": None,
            "memtest_mb": None, "memtest_when": None}


def default_memtest_mb():
    avail = psutil.virtual_memory().available / 1024**2
    return int(max(128, min(4096, avail * 0.5)))


def run_memtest(mb, say):
    if not shutil.which("memtester"):
        return None
    say(f"memtester running on {mb} MB (one pass, can take several minutes)...")
    r = subprocess.run(["memtester", f"{mb}M", "1"], capture_output=True, text=True)
    return "pass" if r.returncode == 0 else "fail"


def score_ram(ram):
    s, notes = 100, []
    for m in ram["modules"]:
        r, c = m["rated_mts"], m["configured_mts"]
        if r and c and c < r * 0.9:
            s -= 10
            notes.append(f"{m['part']}: running at {c} MT/s instead of rated {r} MT/s (enable XMP/DOCP in BIOS)")
    if len({m["rated_mts"] for m in ram["modules"]}) > 1:
        notes.append("Modules have different rated speeds")
    if ram["used_percent"] > 90:
        s -= 10; notes.append("RAM usage is above 90%")
    if ram["swap_percent"] > 50:
        s -= 10; notes.append("Swap usage is high")
    if ram["memtest"] == "fail":
        return 1, ["memtester found errors! Do not trust this RAM"]
    if ram["memtest"] is None:
        notes.append("memtester not run yet (press m in the TUI)")
    return clamp(s), notes


# ================================================================ Storage
def collect_disks():
    disks = []
    try:
        devs = json.loads(sh(["lsblk", "-J", "-d", "-b", "-o", "NAME,MODEL,SERIAL,SIZE,ROTA,TRAN,TYPE"]))["blockdevices"]
    except Exception:
        return disks
    for d in devs:
        if d.get("type") != "disk" or d["name"].startswith(("loop", "zram", "sr")):
            continue
        info = {
            "name": d["name"], "model": (d.get("model") or "").strip(),
            "serial": (d.get("serial") or "").strip(), "size_gb": round(int(d["size"]) / 1e9),
            "kind": "USB" if d.get("tran") == "usb" else "NVMe" if d.get("tran") == "nvme" else
                    "HDD" if str(d.get("rota")) in ("1", "True", "true") else "SSD",
            "firmware": None, "smart_ok": None, "temp_c": None, "wear_used": None,
            "media_errors": 0, "reallocated": 0, "pending": 0, "uncorrectable": 0, "crc_errors": 0,
            "bad_sectors": 0, "power_hours": None, "power_cycles": None, "written_bytes": None,
            "available_spare": None, "unsafe_shutdowns": None,
        }
        try:
            j = json.loads(sh(["smartctl", "-j", "-a", f"/dev/{d['name']}"]) or "{}")
        except Exception:
            j = {}
        info["firmware"] = j.get("firmware_version")
        info["smart_ok"] = j.get("smart_status", {}).get("passed")
        info["temp_c"] = j.get("temperature", {}).get("current")
        info["power_hours"] = j.get("power_on_time", {}).get("hours")
        info["power_cycles"] = j.get("power_cycle_count")
        nv = j.get("nvme_smart_health_information_log")
        if nv:
            info["wear_used"] = nv.get("percentage_used")
            info["media_errors"] = nv.get("media_errors", 0)
            info["available_spare"] = nv.get("available_spare")
            info["unsafe_shutdowns"] = nv.get("unsafe_shutdowns")
            if nv.get("data_units_written"):
                info["written_bytes"] = nv["data_units_written"] * 512000
        else:
            for a in j.get("ata_smart_attributes", {}).get("table", []):
                raw, aid, name = a.get("raw", {}).get("value", 0), a["id"], a.get("name", "")
                if aid == 5:
                    info["reallocated"] += raw
                elif aid == 197:
                    info["pending"] += raw
                elif aid in (198, 160):
                    info["uncorrectable"] += raw
                elif aid == 199:
                    info["crc_errors"] += raw
                if aid == 169 and 0 < raw <= 100:      # Transcend/Apacer: raw = remaining life %
                    info["wear_used"] = 100 - raw
                elif aid in (177, 231, 233) and info["wear_used"] is None:
                    info["wear_used"] = 100 - a["value"]
                if aid == 241 and raw:
                    if "32MiB" in name:
                        info["written_bytes"] = raw * 32 * 1024**2
                    elif "GiB" in name:
                        info["written_bytes"] = raw * 1024**3
                    elif "GB" in name.upper():
                        info["written_bytes"] = raw * 1e9
                    else:
                        info["written_bytes"] = raw * 512
        info["bad_sectors"] = info["reallocated"] + info["pending"] + info["uncorrectable"]
        disks.append(info)
    return disks


def score_disk(d):
    s, notes = 100, []
    if d["kind"] == "USB":
        return None, ["USB drive: no SMART data, so no health score"]
    if d["smart_ok"] is False:
        return 1, ["SMART self-assessment FAILED! Back up your data now"]
    if d["smart_ok"] is None and d["wear_used"] is None:
        return None, ["No SMART data available, cannot score"]
    if d["wear_used"] is not None:
        s -= d["wear_used"]
        notes.append(f"{d['wear_used']}% of rated life used")
    if d["media_errors"]:
        s -= min(40, 10 * d["media_errors"]); notes.append(f"{d['media_errors']} media error(s)")
    if d["bad_sectors"]:
        s -= min(40, 5 + d["bad_sectors"]); notes.append(f"{d['bad_sectors']} bad / pending / uncorrectable sector(s)")
    if d["temp_c"] and d["temp_c"] > 70:
        s -= 10; notes.append(f"Temperature is high ({d['temp_c']} C)")
    return clamp(s), notes


# ============================================================== Benchmark
def run_bench(directory, say):
    b = {}
    threads = str(psutil.cpu_count(logical=True))
    say("Benchmark: CPU (single + multi thread)...")
    for name, th in (("cpu_single", "1"), ("cpu_multi", threads)):
        m = re.search(r"events per second:\s+([\d.]+)", sh(["sysbench", "cpu", f"--threads={th}", "--time=10", "run"]))
        b[name] = float(m.group(1)) if m else None
    b["cpu_temp_load"] = cpu_temp()
    say("Benchmark: RAM...")
    for name, op in (("ram_read_mib", "read"), ("ram_write_mib", "write")):
        out = sh(["sysbench", "memory", f"--threads={threads}", "--memory-block-size=1M",
                  "--memory-total-size=10G", f"--memory-oper={op}", "run"])
        m = re.search(r"\(([\d.]+) MiB/sec\)", out)
        b[name] = float(m.group(1)) if m else None
    say("Benchmark: disk that holds your home folder...")
    fname = os.path.join(directory, "fio.tmp")
    base = ["fio", f"--filename={fname}", "--size=1G", "--direct=1", "--ioengine=libaio",
            "--runtime=10", "--time_based", "--output-format=json"]
    for name, args, side, key, div in (
            ("ssd_seq_write_mbs", ["--name=sw", "--rw=write", "--bs=1M", "--iodepth=16"], "write", "bw_bytes", 1e6),
            ("ssd_seq_read_mbs", ["--name=sr", "--rw=read", "--bs=1M", "--iodepth=16"], "read", "bw_bytes", 1e6),
            ("ssd_rand_read_iops", ["--name=rr", "--rw=randread", "--bs=4k", "--iodepth=32"], "read", "iops", 1)):
        try:
            b[name] = round(json.loads(sh(base + args))["jobs"][0][side][key] / div, 1)
        except Exception:
            b[name] = None
    if os.path.exists(fname):
        os.remove(fname)
    return b


# ================================================================ Collect
def rescore(data):
    data["cpu"]["score"], data["cpu"]["notes"] = score_cpu(data["cpu"], data.get("bench"))
    data["ram"]["score"], data["ram"]["notes"] = score_ram(data["ram"])
    for d in data["disks"]:
        d["score"], d["notes"] = score_disk(d)
    for g in data["gpus"]:
        g["score"], g["notes"] = None, []


def collect_all(say=lambda m: None, bench=False, memtest_mb=0, stress_s=0, keep=None):
    say("Reading CPU...")
    cpu = collect_cpu()
    say("Detecting GPU...")
    gpus = detect_gpus()
    say("Reading RAM...")
    ram = collect_ram()
    say("Reading disks (SMART)...")
    disks = collect_disks()
    data = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
            "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
            "cpu": cpu, "gpus": gpus, "ram": ram, "disks": disks, "bench": None}
    if keep:   # carry over test results from earlier in this session
        data["bench"] = keep.get("bench")
        cpu["stress"] = keep["cpu"].get("stress")
        for k in ("memtest", "memtest_mb", "memtest_when"):
            ram[k] = keep["ram"].get(k)
    if memtest_mb:
        ram["memtest"] = run_memtest(memtest_mb, say)
        ram["memtest_mb"], ram["memtest_when"] = memtest_mb, datetime.datetime.now().isoformat(timespec="seconds")
    if bench:
        data["bench"] = run_bench(base_dir(), say)
    if stress_s:
        cpu["stress"] = run_cpu_stress(say, stress_s)
    rescore(data)
    return data


def overall(data):
    scores = [data["cpu"]["score"], data["ram"]["score"]]
    ds = [d["score"] for d in data["disks"] if d["score"] is not None]
    if ds:
        scores.append(min(ds))
    tiers = [TIER_NUM[tier_cpu(data["cpu"])], TIER_NUM[tier_ram(data["ram"])]]
    tiers += [TIER_NUM[tier_gpu(g)] for g in data.get("gpus", []) if g["kind"] == "Discrete"][:1]
    internal = [TIER_NUM[tier_disk(d)] for d in data["disks"] if d["kind"] != "USB"]
    if internal:
        tiers.append(max(internal))
    return clamp(sum(scores) / len(scores)), NUM_TIER[int(round(sum(tiers) / len(tiers)))]


# ============================================================ Assessments
USAGE_COLORS = {"Like new": C["good"], "Lightly used": C["good"], "Moderately used": C["warn"],
                "Heavily used": C["orange"], "Worn out": C["bad"], "Not readable": C["dim"]}
COND_COLORS = {"Good": C["good"], "Caution": C["warn"], "Risky": C["bad"], "Unknown": C["dim"]}
USAGE_LEVELS = ["Like new", "Lightly used", "Moderately used", "Heavily used", "Worn out"]


def assess_disk(d):
    ev, flags, tips = [], [], []
    name = f"{d['model']} ({d['kind']} {d['size_gb']} GB)"
    dev = f"/dev/{d['name']}"
    if d["kind"] == "USB" or (d["smart_ok"] is None and d["wear_used"] is None and d["power_hours"] is None):
        return {"part": "Storage", "name": name, "usage": "Not readable", "condition": "Unknown",
                "evidence": ["This drive does not report SMART data (typical for USB sticks and some adapters)."],
                "flags": [], "tips": ["Test the REAL capacity with f3 (f3write / f3read). Fake-capacity drives are common on marketplaces.",
                                      "Copy a few GB of files back and forth and verify them."]}
    h, wear, wb = d["power_hours"], d["wear_used"], d["written_bytes"]
    size_tb = max(d["size_gb"] / 1000, 0.001)
    hours_lvl = None if h is None else 0 if h < 200 else 1 if h < 2500 else 2 if h < 26000 else 3
    wear_lvl = None if wear is None else 0 if wear < 1 else 1 if wear < 10 else 2 if wear < 30 else 3 if wear < 80 else 4
    wr = (wb / 1e12) / size_tb if wb else None
    write_lvl = None if wr is None else 0 if wr < 1 else 1 if wr < 10 else 2 if wr < 100 else 3
    main = [x for x in (wear_lvl, write_lvl) if x is not None]
    if main:
        lvl = max(main)
        if hours_lvl is not None and hours_lvl >= lvl + 2:
            lvl += 1
    else:
        lvl = hours_lvl if hours_lvl is not None else 0
    if h is not None:
        ev.append(f"Powered on for {fmt_hours(h)}")
    if d["power_cycles"] is not None:
        ev.append(f"{d['power_cycles']:,} power cycles")
    if wb:
        ev.append(f"{fmt_bytes(wb)} written in total ({wb / 1e12 / size_tb:.0f} TB per TB of capacity)")
    if wear is not None:
        ev.append(f"{wear}% of rated life used, about {est_life_left(wear, h)} left at the current rate")
    ev.append(f"Errors: {d['reallocated']} reallocated, {d['pending']} pending, "
              f"{d['uncorrectable']} uncorrectable, {d['media_errors']} media, {d['crc_errors']} CRC")
    if d["available_spare"] is not None:
        ev.append(f"Spare blocks left: {d['available_spare']}%")
    if d["smart_ok"] is False:
        flags.append(("risk", "SMART self-assessment FAILED"))
    if d["pending"] or d["uncorrectable"]:
        flags.append(("risk", "Pending/uncorrectable sectors: the drive is already losing data"))
    if d["media_errors"]:
        flags.append(("risk", f"{d['media_errors']} media error(s) reported"))
    if d["reallocated"]:
        flags.append(("risk" if d["reallocated"] > 50 else "warn", f"{d['reallocated']} reallocated sector(s): cells have started to fail"))
    if wear is not None and wear >= 80:
        flags.append(("risk", "Over 80% of rated life used: near end of life"))
    elif wear is not None and wear >= 50:
        flags.append(("warn", "More than half of the rated life is gone"))
    if d["crc_errors"]:
        flags.append(("warn", "CRC errors: usually a bad cable/port, not the drive itself"))
    if d["temp_c"] and d["temp_c"] > 70:
        flags.append(("warn", f"Running hot ({d['temp_c']} C)"))
    if d["available_spare"] is not None and d["available_spare"] < 20:
        flags.append(("warn", "NVMe spare capacity is low"))
    if d["kind"] == "HDD" and h and h > 40000:
        flags.append(("warn", "Over 40,000 power-on hours is old for a hard disk"))
    if h is not None and h < 50 and ((wear or 0) >= 5 or (wb or 0) > 1e12):
        flags.append(("warn", "Very low power-on hours but clear wear/writes: SMART counters may have been reset by a reseller"))
    cond = "Risky" if any(s == "risk" for s, _ in flags) else "Caution" if flags else "Good"
    tips = [f"Run a self-test before buying: sudo smartctl -t short {dev}, then smartctl -a {dev}",
            "Ask the seller for a SMART screenshot and compare it with what this tool shows on your own PC."]
    return {"part": "Storage", "name": name, "usage": USAGE_LEVELS[min(lvl, 4)], "condition": cond,
            "evidence": ev, "flags": flags, "tips": tips}


def assess_cpu(cpu):
    st = cpu.get("stress")
    ev = [f"{cpu['cores']} cores / {cpu['threads']} threads, max {cpu['max_mhz'] or 'N/A'} MHz",
          f"Idle temperature: {('%.0f C' % cpu['temp_c']) if cpu['temp_c'] else 'N/A'}, hardware errors in log: {cpu['mce_errors']}"]
    flags = []
    if st and st.get("error"):
        ev.append(st["error"])
    elif st:
        ev.append(f"{st['seconds']}s stress test: {'PASSED' if st['passed'] else 'FAILED'}, peak "
                  f"{('%.0f C' % st['peak_temp']) if st['peak_temp'] else 'N/A'}, sustained clock "
                  f"{('%.0f%%' % st['sustained_pct']) if st['sustained_pct'] else 'N/A'} of max, "
                  f"{st['throttle_events']} throttle event(s)")
        if not st["passed"]:
            flags.append(("risk", "Failed the stress test (calculation errors or crash)"))
        if st["throttle_events"]:
            flags.append(("warn", "CPU throttled under load (bad cooling or worn CPU/paste)"))
        if st.get("peak_temp") and st["peak_temp"] > 90:
            flags.append(("warn", "Peak temperature above 90 C (check cooler/paste before blaming the CPU)"))
        if st.get("sustained_pct") and st["sustained_pct"] < 85:
            flags.append(("warn", "Clock speed dropped well below max under load"))
    if cpu["mce_errors"]:
        flags.append(("risk", f"{cpu['mce_errors']} hardware error(s) in the kernel log"))
    if re.search(r"engineering|\bES\b|\bQS\b|0000", cpu["model"], re.I):
        flags.append(("warn", "Model name looks like an engineering/qualification sample"))
    m = re.search(r"@\s*([\d.]+)\s*GHz", cpu["model"])
    if m and cpu["max_mhz"] and cpu["max_mhz"] < float(m.group(1)) * 1000 * 0.9:
        flags.append(("warn", "Max clock is lower than the advertised base clock"))
    cond = "Unknown" if st is None else ("Risky" if any(s == "risk" for s, _ in flags)
                                          else "Caution" if flags else "Good")
    if st and st.get("error"):
        cond = "Unknown"
    return {"part": "CPU", "name": cpu["model"], "usage": "Not readable", "condition": cond,
            "evidence": ["A CPU keeps no hour counter, so its age/usage cannot be read from the chip."] + ev,
            "flags": flags,
            "tips": ["Run the stress test (key t) and watch temperature and clocks.",
                     "Inspect the pins/pads for bent pins, scratches, burn marks or scraped solder.",
                     "Confirm the model string matches the listing; refuse 'ES/QS' samples unless you know what they are."]}


def assess_gpu(g):
    ev = [f"{g['kind']} GPU" + (f", {g['vram_mb'] / 1024:.0f} GB VRAM" if g["vram_mb"] else "")]
    if g["driver"]:
        ev.append(f"Driver {g['driver']}, VBIOS {g['vbios']}")
    if g["temp_c"] is not None:
        ev.append(f"Temperature now: {g['temp_c']:.0f} C" + (f", power {g['power_w']:.0f} W" if g["power_w"] else ""))
    if g["pcie_gen"]:
        ev.append(f"PCIe link now: Gen{g['pcie_gen']:.0f} x{(g['pcie_width'] or 0):.0f} "
                  f"(max Gen{(g['pcie_gen_max'] or 0):.0f} x{(g['pcie_width_max'] or 0):.0f}; the link slows down at idle)")
    flags = []
    if g["throttle"]:
        flags.append(("warn", f"Throttling right now: {g['throttle']}"))
    if g["temp_c"] and g["temp_c"] > 85:
        flags.append(("warn", f"High temperature ({g['temp_c']:.0f} C)"))
    if g["kind"] == "Integrated":
        tips = ["Integrated graphics are part of the CPU, so judge them together with the CPU."]
    else:
        tips = ["Run a 10-15 minute GPU load (glmark2, Unigine Heaven/Superposition, a game) while watching the Live tab: "
                "artifacts, driver resets or temperatures above 85-90 C mean trouble.",
                "Compare VRAM size and VBIOS with the listing: cards with flashed fake specs exist.",
                "Check the fans (worn/loud), sticky or replaced thermal pads, and dust/oil residue. Ex-mining cards often show these.",
                "Check all display outputs and the PCIe connector for damage."]
    return {"part": "GPU", "name": g["name"], "usage": "Not readable",
            "condition": "Caution" if flags else "Unknown",
            "evidence": ["GPUs keep no hour counter, so usage cannot be read from the card."] + ev,
            "flags": flags, "tips": tips}


def assess_ram(ram):
    ev = [f"{ram['total_gb']} GB in {len(ram['modules'])} module(s)"]
    flags = []
    if ram["memtest"]:
        ev.append(f"memtester ({ram['memtest_mb']} MB, 1 pass): {ram['memtest'].upper()}")
    if ram["memtest"] == "fail":
        flags.append(("risk", "memtester found errors"))
    for m in ram["modules"]:
        if m["rated_mts"] and m["configured_mts"] and m["configured_mts"] < m["rated_mts"] * 0.9:
            flags.append(("warn", f"{m['slot']}: running slower than rated (XMP/DOCP off, or an incompatible mix)"))
    if len({m["rated_mts"] for m in ram["modules"]}) > 1:
        flags.append(("warn", "Modules of different speeds are mixed"))
    cond = "Unknown" if not ram["memtest"] else ("Risky" if any(s == "risk" for s, _ in flags)
                                                   else "Caution" if flags else "Good")
    return {"part": "RAM", "name": f"{ram['total_gb']} GB " + (ram["modules"][0]["type"] or "") if ram["modules"] else "RAM",
            "usage": "Not readable", "condition": cond,
            "evidence": ["RAM keeps no hour counter, so usage cannot be read. Only errors can be tested."] + ev,
            "flags": flags,
            "tips": ["Run the RAM test (key m). For used RAM, also run Memtest86+ from a USB stick for 2-4 passes.",
                     "Check the label: maker, part number and speed should match the listing."]}


def assessments(data):
    out = [assess_cpu(data["cpu"])]
    out += [assess_gpu(g) for g in data["gpus"]]
    out.append(assess_ram(data["ram"]))
    out += [assess_disk(d) for d in data["disks"]]
    return out


# =============================================================== Renderers
def bar(score, width=18):
    t = Text()
    if score is None:
        t.append("░" * width, style=C["dim"]); t.append("  N/A", style=C["dim"])
        return t
    filled = round(width * score / 100)
    col = health_color(score)
    t.append("█" * filled, style=col); t.append("░" * (width - filled), style=C["dim"])
    t.append(f"  {score}", style=f"bold {col}")
    return t


def badge(cond):
    col = COND_COLORS.get(cond, C["dim"])
    return Text(f" {cond.upper()} ", style=f"bold #1a1b26 on {col}")


def tier_text(tier):
    return Text(tier or "N/A", style=C["cyan"])


def kv(rows):
    t = Table.grid(padding=(0, 2))
    t.add_column(style=C["muted"], no_wrap=True)
    t.add_column()
    for k, v in rows:
        t.add_row(k, v if isinstance(v, Text) else Text(str(v), style=C["text"]))
    return t


def panel(title, body, border=None):
    return Panel(body, title=f"[bold {C['accent']}]{title}[/]", title_align="left", box=box.ROUNDED,
                 border_style=border or C["dim"], padding=(1, 2))


def card(title, name, score, tier, lines):
    body = Group(
        Text(name, style=f"bold {C['text']}", no_wrap=True, overflow="ellipsis"), Text(""), bar(score),
        Text.assemble((health_text(score), f"bold {health_color(score)}"), ("   Tier ", C["dim"]), (tier or "N/A", C["cyan"])),
        Text(""), *[l if isinstance(l, Text) else Text(l, style=C["muted"]) for l in lines])
    return panel(title, body)


def notes_block(data):
    rows = [("CPU", data["cpu"]["notes"]), ("RAM", data["ram"]["notes"])]
    rows += [(d["model"], d["notes"]) for d in data["disks"]]
    t = Text()
    for part, notes in rows:
        for n in notes:
            t.append("• ", style=C["accent"]); t.append(f"{part}: ", style=f"bold {C['text']}"); t.append(n + "\n", style=C["muted"])
    return t


def render_overview(data):
    cpu, ram = data["cpu"], data["ram"]
    o_score, o_tier = overall(data)
    up = data.get("uptime_hours")
    head = Text.assemble(("Overall health ", C["dim"]), (f"{o_score}/100 ", f"bold {health_color(o_score)}"),
                         (f"({health_text(o_score)})", health_color(o_score)), ("    System class ", C["dim"]),
                         (o_tier, f"bold {C['cyan']}"), ("    Uptime ", C["dim"]), (f"{up:.1f} h" if up is not None else "N/A", C["text"]),
                         ("    Scanned ", C["dim"]), (data["time"].replace("T", " "), C["text"]))
    st = cpu.get("stress")
    stress_line = (Text("Stress test: not run (press t)", style=C["dim"]) if not st else
                   Text(st["error"], style=C["warn"]) if st.get("error") else
                   Text(f"Stress {st['seconds']}s: {'passed' if st['passed'] else 'FAILED'}, peak "
                        f"{('%.0f C' % st['peak_temp']) if st['peak_temp'] else 'N/A'}",
                        style=C["good"] if st["passed"] else C["bad"]))
    cards = [card("CPU", cpu["model"], cpu["score"], tier_cpu(cpu), [
        f"{cpu['cores']} cores / {cpu['threads']} threads · max {cpu['max_mhz'] or 'N/A'} MHz",
        f"Temp {('%.0f C' % cpu['temp_c']) if cpu['temp_c'] else 'N/A'} · HW errors {cpu['mce_errors']}", stress_line])]
    for g in data["gpus"]:
        cards.append(card("GPU", g["name"], None, tier_gpu(g), [
            f"{g['kind']}" + (f" · {g['vram_mb'] / 1024:.0f} GB VRAM" if g["vram_mb"] else ""),
            f"Temp {('%.0f C' % g['temp_c']) if g['temp_c'] is not None else 'N/A'}",
            "No usage counter on GPUs; see Buyer check"]))
    mt = ("memtester: " + ram["memtest"].upper() if ram["memtest"] else "memtester: not run (press m)")
    cards.append(card("RAM", f"{ram['total_gb']} GB {ram['modules'][0]['type'] if ram['modules'] else ''}".strip(),
                      ram["score"], tier_ram(ram), [
        f"{len(ram['modules'])} module(s) @ {(ram['modules'][0]['configured_mts'] or ram['modules'][0]['rated_mts']) if ram['modules'] else '?'} MT/s",
        f"In use {ram['used_percent']}% · swap {ram['swap_percent']}%", Text(mt, style=C["bad"] if ram["memtest"] == "fail" else C["dim"])]))
    for d in data["disks"]:
        a = assess_disk(d)
        cards.append(card(d["kind"], d["model"], d["score"], tier_disk(d), [
            f"{d['size_gb']} GB · powered on {fmt_hours(d['power_hours'])}",
            f"Life used {str(d['wear_used']) + '%' if d['wear_used'] is not None else 'N/A'} · written {fmt_bytes(d['written_bytes'])}",
            Text.assemble((a["usage"], USAGE_COLORS.get(a["usage"], C["dim"])), ("  ·  ", C["dim"]), (a["condition"], COND_COLORS[a["condition"]]))]))
    verdicts = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {C['accent']}", border_style=C["dim"])
    for col in ("Part", "Name", "Usage", "Condition"):
        verdicts.add_column(col)
    for a in assessments(data):
        verdicts.add_row(a["part"], Text(a["name"], no_wrap=True, overflow="ellipsis"),
                         Text(a["usage"], style=USAGE_COLORS.get(a["usage"], C["dim"])), badge(a["condition"]))
    return Group(Panel(head, box=box.ROUNDED, border_style=C["accent"]), Columns(cards, equal=True, expand=True),
                 panel("Second-hand verdicts (details in the Buyer check tab)", verdicts))


def render_cpu(data):
    c, st = data["cpu"], data["cpu"].get("stress")
    rows = [("Model", c["model"]), ("Socket", c["socket"] or "N/A"), ("Cores / threads", f"{c['cores']} / {c['threads']}"),
            ("Max clock", f"{c['max_mhz'] or 'N/A'} MHz"), ("Temperature now", f"{c['temp_c']:.0f} C" if c["temp_c"] else "N/A"),
            ("Hardware errors", c["mce_errors"]), ("Health", Text.assemble(bar(c["score"]), "   ", (health_text(c["score"]), health_color(c["score"])))),
            ("Tier", tier_text(tier_cpu(c)))]
    parts = [panel("CPU", kv(rows))]
    if st and not st.get("error"):
        parts.append(panel("Stress test", kv([
            ("Result", Text("PASSED" if st["passed"] else "FAILED", style=f"bold {C['good'] if st['passed'] else C['bad']}")),
            ("Duration", f"{st['seconds']} s"), ("Peak temperature", f"{st['peak_temp']:.0f} C" if st["peak_temp"] else "N/A"),
            ("Average clock (2nd half)", f"{st['avg_mhz']} MHz of {st['max_mhz']} MHz"),
            ("Throttle events", st["throttle_events"]), ("Finished", st["when"].replace("T", " "))])))
    else:
        parts.append(panel("Stress test", Text(st["error"] if st else
                           "Not run yet. Press t to run a 60 s all-core stress test with result verification.", style=C["warn"] if st else C["dim"])))
    b = data.get("bench")
    if b:
        parts.append(panel("CPU benchmark", kv([("Single-thread", f"{b.get('cpu_single')} events/s"),
                                                ("Multi-thread", f"{b.get('cpu_multi')} events/s")])))
    parts.append(notes_for("CPU", c["notes"]))
    return Group(*parts)


def notes_for(title, notes):
    t = Text()
    for n in notes:
        t.append("• ", style=C["accent"]); t.append(n + "\n", style=C["muted"])
    return panel(f"{title} notes", t if notes else Text("Nothing to report.", style=C["dim"]))


def render_gpu(data):
    if not data["gpus"]:
        return panel("GPU", Text("No GPU detected (is pciutils/lspci installed?).", style=C["dim"]))
    out = []
    for g in data["gpus"]:
        gen = f"Gen{g['pcie_gen']:.0f} x{(g['pcie_width'] or 0):.0f} (max Gen{(g['pcie_gen_max'] or 0):.0f} x{(g['pcie_width_max'] or 0):.0f})" if g["pcie_gen"] else "N/A"
        out.append(panel("GPU", kv([
            ("Name", g["name"]), ("Type", g["kind"]), ("Tier", tier_text(tier_gpu(g))),
            ("VRAM", f"{g['vram_mb'] / 1024:.1f} GB" if g["vram_mb"] else "N/A"),
            ("Driver / VBIOS", f"{g['driver'] or 'N/A'} / {g['vbios'] or 'N/A'}"),
            ("Temperature", f"{g['temp_c']:.0f} C" if g["temp_c"] is not None else "N/A"),
            ("Power", f"{g['power_w']:.0f} W" + (f" of {g['power_limit_w']:.0f} W" if g["power_limit_w"] else "") if g["power_w"] else "N/A"),
            ("Clock", f"{g['clock_mhz']:.0f} MHz" if g["clock_mhz"] else "N/A"),
            ("Utilization", f"{g['util']:.0f}%" if g["util"] is not None else "N/A"),
            ("PCIe link", gen), ("Throttling", g["throttle"] or "none")])))
    out.append(panel("About GPU checks", Text(
        "GPUs have no usage history (no hour counter), so no health score is given. Use the Live tab while running a "
        "GPU load, and read the Buyer check tab for what to inspect.", style=C["dim"])))
    return Group(*out)


def render_ram(data):
    r = data["ram"]
    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {C['accent']}", border_style=C["dim"])
    for col in ("Slot", "Size", "Type", "Rated", "Running at", "Maker", "Part number"):
        t.add_column(col)
    for m in r["modules"]:
        t.add_row(m["slot"] or "-", m["size"] or "-", m["type"] or "-", f"{m['rated_mts']} MT/s" if m["rated_mts"] else "-",
                  f"{m['configured_mts']} MT/s" if m["configured_mts"] else "-", escape(m["manufacturer"] or "-"), escape(m["part"] or "-"))
    mt = (Text(f"{r['memtest'].upper()} ({r['memtest_mb']} MB, {r['memtest_when'].replace('T', ' ')})",
               style=C["good"] if r["memtest"] == "pass" else C["bad"]) if r["memtest"]
          else Text("Not run yet. Press m to test about half of the free RAM (one pass).", style=C["dim"]))
    return Group(
        panel("RAM", kv([("Total", f"{r['total_gb']} GB"), ("In use", f"{r['used_percent']}%"), ("Swap", f"{r['swap_percent']}%"),
                         ("Health", Text.assemble(bar(r["score"]), "   ", (health_text(r["score"]), health_color(r["score"])))),
                         ("Tier", tier_text(tier_ram(r))), ("Error test", mt)])),
        panel("Modules", t), notes_for("RAM", r["notes"]))


def render_storage(data):
    if not data["disks"]:
        return panel("Storage", Text("No disks found.", style=C["dim"]))
    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {C['accent']}", border_style=C["dim"])
    for col in ("Disk", "Health", "Powered on", "Cycles", "Written", "Life used", "Est. left", "Temp", "Errors r/p/u/m/crc"):
        t.add_column(col)
    for d in data["disks"]:
        t.add_row(Text(f"{d['model']}\n{d['kind']} {d['size_gb']} GB\nS/N {d['serial'] or 'n/a'} · FW {d['firmware'] or 'n/a'}"),
                  Text.assemble((fmt_score(d["score"]), health_color(d["score"]))), fmt_hours(d["power_hours"]),
                  str(d["power_cycles"]) if d["power_cycles"] is not None else "N/A", fmt_bytes(d["written_bytes"]),
                  f"{d['wear_used']}%" if d["wear_used"] is not None else "N/A", est_life_left(d["wear_used"], d["power_hours"]),
                  f"{d['temp_c']} C" if d["temp_c"] else "N/A",
                  f"{d['reallocated']}/{d['pending']}/{d['uncorrectable']}/{d['media_errors']}/{d['crc_errors']}")
    notes = Text()
    for d in data["disks"]:
        for n in d["notes"]:
            notes.append("• ", style=C["accent"]); notes.append(f"{d['model']}: ", style=f"bold {C['text']}"); notes.append(n + "\n", style=C["muted"])
    return Group(panel("Storage (SMART)", t), panel("Storage notes", notes),
                 Text("Errors: r = reallocated, p = pending, u = uncorrectable, m = media, crc = interface CRC", style=C["dim"]))


def render_buyer(data):
    intro = Text("Only storage keeps a real usage history (SMART). CPU, GPU and RAM have no hour counters, so for them this tool "
                 "tests their condition instead. Run this on a used part BEFORE paying.", style=C["dim"])
    out = [Panel(intro, box=box.ROUNDED, border_style=C["accent"])]
    for a in assessments(data):
        body = [Text.assemble(("Usage      ", C["muted"]), (a["usage"], f"bold {USAGE_COLORS.get(a['usage'], C['dim'])}")),
                Text.assemble(("Condition  ", C["muted"]), badge(a["condition"])), Text("")]
        body += [Text(f"• {e}", style=C["text"]) for e in a["evidence"]]
        for sev, msg in a["flags"]:
            body.append(Text(f"{'✖' if sev == 'risk' else '▲'} {msg}", style=C["bad"] if sev == "risk" else C["warn"]))
        body.append(Text(""))
        body.append(Text("Before you buy:", style=f"bold {C['purple']}"))
        body += [Text(f"  → {t}", style=C["muted"]) for t in a["tips"]]
        out.append(panel(f"{a['part']}  ·  {escape(a['name'])}", Group(*body), border=COND_COLORS[a["condition"]]))
    return Group(*out)


def pct(old, new):
    if not old or not new:
        return "-"
    p = (new - old) / old * 100
    col = C["good"] if p > 2 else (C["bad"] if p < -2 else C["dim"])
    return f"[{col}]{p:+.0f}%[/]"


def render_compare(old, new):
    out = [Text(f"Before: {old['time']}   ->   Now: {new['time']}", style=C["dim"])]
    ch = Text()
    ident = lambda d: f"{d['model']} {d['serial']}"
    od, nd = {ident(d): d for d in old["disks"]}, {ident(d): d for d in new["disks"]}
    for k in nd.keys() - od.keys():
        ch.append(f"+ New disk: {nd[k]['model']} ({nd[k]['size_gb']} GB), health {fmt_score(nd[k]['score'])}\n", style=C["good"])
    for k in od.keys() - nd.keys():
        ch.append(f"- Removed disk: {od[k]['model']}\n", style=C["bad"])
    if old["cpu"]["model"] != new["cpu"]["model"]:
        ch.append(f"CPU changed: {old['cpu']['model']} -> {new['cpu']['model']} (tier {tier_cpu(old['cpu'])} -> {tier_cpu(new['cpu'])})\n", style=C["good"])
    om = {(m["part"], m["serial"]) for m in old["ram"]["modules"]}
    nm = {(m["part"], m["serial"]) for m in new["ram"]["modules"]}
    if om != nm or old["ram"]["total_gb"] != new["ram"]["total_gb"]:
        ch.append(f"RAM changed: {old['ram']['total_gb']} GB -> {new['ram']['total_gb']} GB (tier {tier_ram(old['ram'])} -> {tier_ram(new['ram'])})\n", style=C["good"])
    og = {g["name"] for g in old.get("gpus", [])}
    ng = {g["name"] for g in new.get("gpus", [])}
    if og != ng:
        ch.append(f"GPU changed: {', '.join(sorted(og)) or 'none'} -> {', '.join(sorted(ng)) or 'none'}\n", style=C["good"])
    if not ch.plain:
        ch.append("No hardware change detected between the two snapshots.\n", style=C["warn"])
    out.append(panel("Hardware changes", ch))
    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style=f"bold {C['accent']}", border_style=C["dim"])
    t.add_column("Item"); t.add_column("Before", justify="right"); t.add_column("Now", justify="right"); t.add_column("Change", justify="right")
    t.add_row("CPU health", str(old["cpu"]["score"]), str(new["cpu"]["score"]), pct(old["cpu"]["score"], new["cpu"]["score"]))
    t.add_row("RAM health", str(old["ram"]["score"]), str(new["ram"]["score"]), pct(old["ram"]["score"], new["ram"]["score"]))
    os_ = [d["score"] for d in old["disks"] if d["score"] is not None]
    ns_ = [d["score"] for d in new["disks"] if d["score"] is not None]
    if os_ and ns_:
        t.add_row("Disk health (weakest)", str(min(os_)), str(min(ns_)), pct(min(os_), min(ns_)))
    oo, no = overall(old), overall(new)
    t.add_row("Overall health", str(oo[0]), str(no[0]), pct(oo[0], no[0]))
    t.add_row("Overall class", oo[1], no[1], "")
    ob, nb = old.get("bench"), new.get("bench")
    if ob and nb:
        for k, title in BENCH_NAMES.items():
            t.add_row(title, str(ob.get(k)), str(nb.get(k)), pct(ob.get(k), nb.get(k)))
        if ob.get("cpu_temp_load") and nb.get("cpu_temp_load"):
            t.add_row("CPU temperature under load (C)", f"{ob['cpu_temp_load']:.0f}", f"{nb['cpu_temp_load']:.0f}", "")
    else:
        out.append(Text("No benchmark comparison: run the benchmark (key b) before saving the baseline AND after the hardware change.", style=C["warn"]))
    out.append(panel("Scores and performance", t))
    return Group(*out)


BLOCKS = "▁▂▃▄▅▆▇█"


def spark(vals, lo, hi, width=70, style=None):
    vals = list(vals)[-width:]
    s = "".join(BLOCKS[max(0, min(7, int((v - lo) / (hi - lo) * 7.99)))] if v is not None else " " for v in vals)
    return Text(" " * (width - len(vals)) + s, style=style or C["accent"])


def render_live(L, gpus):
    cores = L["cores"]
    t = Table.grid(padding=(0, 2))
    t.add_column(style=C["dim"], no_wrap=True); t.add_column(); t.add_column(justify="right", no_wrap=True)
    t.add_row("CPU load", spark(L["cpu"], 0, 100), Text(f"{L['cpu'][-1]:.0f}%" if L["cpu"] else "-", style=C["text"]))
    t.add_row("CPU temp", spark(L["temp"], 30, 100, style=C["orange"]), Text(f"{L['temp'][-1]:.0f} C" if L["temp"] and L["temp"][-1] else "N/A", style=C["text"]))
    t.add_row("CPU clock", spark(L["freq"], 0, max(L["freq"] or [1]) * 1.05 or 1, style=C["purple"]), Text(f"{L['freq'][-1]:.0f} MHz" if L["freq"] else "-", style=C["text"]))
    t.add_row("RAM used", spark(L["ram"], 0, 100, style=C["cyan"]), Text(f"{L['ram'][-1]:.0f}%" if L["ram"] else "-", style=C["text"]))
    t.add_row("Disk read", spark(L["rd"], 0, max(max(L["rd"] or [1]), 1), style=C["good"]), Text(f"{L['rd'][-1]:.1f} MB/s" if L["rd"] else "-", style=C["text"]))
    t.add_row("Disk write", spark(L["wr"], 0, max(max(L["wr"] or [1]), 1), style=C["warn"]), Text(f"{L['wr'][-1]:.1f} MB/s" if L["wr"] else "-", style=C["text"]))
    for i, g in enumerate(gpus):
        if g["util"] is not None:
            t.add_row(f"GPU{i} load", spark(L["gpu_util"].get(i, []), 0, 100, style=C["good"]), Text(f"{g['util']:.0f}%", style=C["text"]))
        if g["temp_c"] is not None:
            t.add_row(f"GPU{i} temp", spark(L["gpu_temp"].get(i, []), 30, 100, style=C["orange"]), Text(f"{g['temp_c']:.0f} C", style=C["text"]))
    core_t = Table.grid(padding=(0, 3))
    for _ in range(2):
        core_t.add_column()
    cells = []
    for i, p in enumerate(cores):
        col = C["good"] if p < 60 else C["warn"] if p < 85 else C["bad"]
        n = round(p / 100 * 16)
        cells.append(Text.assemble((f"Core {i:<2} ", C["dim"]), ("█" * n, col), ("░" * (16 - n), C["dim"]), (f" {p:3.0f}%", C["text"])))
    for i in range(0, len(cells), 2):
        core_t.add_row(*(cells[i:i + 2] + [Text("")] * (2 - len(cells[i:i + 2]))))
    extra = ""
    if any(g["throttle"] for g in gpus):
        extra = "GPU throttling: " + ", ".join(g["throttle"] for g in gpus if g["throttle"])
    return Group(panel("Live (updates every second): start your own CPU/GPU load and watch here", t),
                 panel("Per-core load", core_t), Text(extra, style=C["warn"]))


def render_all_plain(data):
    return [render_overview(data), render_cpu(data), render_gpu(data), render_ram(data),
            render_storage(data), render_buyer(data)]


# ===================================================================== TUI
TABS = [("overview", "Overview"), ("live", "Live"), ("cpu", "CPU"), ("gpu", "GPU"), ("ram", "RAM"),
        ("storage", "Storage"), ("buyer", "Buyer check"), ("compare", "Compare")]


def load_baseline():
    p = os.path.join(base_dir(), "baseline.json")
    try:
        return json.load(open(p))
    except Exception:
        return None


def save_baseline(data):
    d = base_dir()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for p in (os.path.join(d, "baseline.json"), os.path.join(d, f"snapshot-{stamp}.json")):
        with open(p, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    fix_owner(d)
    return os.path.join(d, "baseline.json")


def run_tui():
    try:
        from textual.app import App
        from textual.binding import Binding
        from textual.containers import VerticalScroll
        from textual.widgets import Footer, Header, Static, TabbedContent, TabPane
    except ImportError:
        console.print("[red]The TUI needs the 'textual' package.[/red]\n"
                      "Setup:  python3 -m venv --system-site-packages ~/.hwcheck/venv && ~/.hwcheck/venv/bin/pip install textual\n"
                      "Run:    sudo ~/.hwcheck/venv/bin/python hwcheck.py")
        sys.exit(1)

    class HwCheckApp(App):
        TITLE = "hwcheck"
        SUB_TITLE = "Hardware health & second-hand checker"
        CSS = "TabPane { padding: 1 1; } .view { height: auto; }"
        BINDINGS = [
            Binding("r", "rescan", "Rescan"), Binding("b", "bench", "Benchmark"),
            Binding("t", "stress", "CPU stress"), Binding("m", "memtest", "RAM test"),
            Binding("s", "snapshot", "Save baseline"), Binding("q", "quit", "Quit"),
            *[Binding(str(i + 1), f"tab('{tid}')", title, show=False) for i, (tid, title) in enumerate(TABS)],
        ]

        def __init__(self):
            super().__init__()
            self.data, self.baseline, self.busy, self.tick_n = None, load_baseline(), None, 0
            self.live = {k: collections.deque(maxlen=90) for k in ("cpu", "temp", "freq", "ram", "rd", "wr")}
            self.live["cores"] = []
            self.live["gpu_util"], self.live["gpu_temp"] = {}, {}
            self.last_io = psutil.disk_io_counters()

        def compose(self):
            yield Header(show_clock=True)
            with TabbedContent(initial="overview"):
                for tid, title in TABS:
                    with TabPane(f"{title}", id=tid):
                        with VerticalScroll():
                            yield Static(Text("Scanning hardware...", style=C["dim"]), id=f"v-{tid}", classes="view")
            yield Footer()

        def on_mount(self):
            try:
                self.theme = "tokyo-night"
            except Exception:
                pass
            self.set_interval(1.0, self.tick)
            self.start("Scanning hardware", lambda say: self._set_data(collect_all(say, keep=self.data)))

        # ---- jobs (run in worker threads)
        def start(self, label, fn):
            if self.busy:
                self.notify(f"Busy: {self.busy}. Wait for it to finish.", severity="warning")
                return
            self.busy = label
            self.sub_title = label + "..."
            self.run_worker(lambda: self._job(label, fn), thread=True)

        def _job(self, label, fn):
            try:
                fn(lambda m: self.call_from_thread(setattr, self, "sub_title", m))
                self.call_from_thread(self.notify, f"{label}: done")
            except Exception as e:
                self.call_from_thread(self.notify, f"{label} failed: {e}", severity="error")
            finally:
                self.call_from_thread(self._done)

        def _done(self):
            self.busy = None
            self.sub_title = "Hardware health & second-hand checker"
            self.refresh_views()

        def _set_data(self, data):
            self.data = data

        # ---- actions
        def action_tab(self, tab_id):
            self.query_one(TabbedContent).active = tab_id

        def action_rescan(self):
            self.start("Scanning hardware", lambda say: self._set_data(collect_all(say, keep=self.data)))

        def action_bench(self):
            def job(say):
                self.data["bench"] = run_bench(base_dir(), say)
                rescore(self.data)
            if self.data:
                self.start("Benchmark", job)

        def action_stress(self):
            def job(say):
                self.data["cpu"]["stress"] = run_cpu_stress(say, 60)
                rescore(self.data)
            if self.data:
                self.start("CPU stress test", job)

        def action_memtest(self):
            def job(say):
                mb = default_memtest_mb()
                res = run_memtest(mb, say)
                r = self.data["ram"]
                r["memtest"], r["memtest_mb"] = res, mb
                r["memtest_when"] = datetime.datetime.now().isoformat(timespec="seconds")
                if res is None:
                    raise RuntimeError("memtester is not installed (sudo apt install memtester)")
                rescore(self.data)
            if self.data:
                self.start("RAM test", job)

        def action_snapshot(self):
            if not self.data:
                return
            path = save_baseline(self.data)
            self.baseline = json.loads(json.dumps(self.data, default=str))
            self.notify(f"Baseline saved: {path}")
            self.refresh_views()

        # ---- views
        def show(self, tid, renderable):
            self.query_one(f"#v-{tid}", Static).update(renderable)

        def refresh_views(self):
            if not self.data:
                return
            d = self.data
            try:
                self.show("overview", render_overview(d))
                self.show("cpu", render_cpu(d))
                self.show("gpu", render_gpu(d))
                self.show("ram", render_ram(d))
                self.show("storage", render_storage(d))
                self.show("buyer", render_buyer(d))
                if self.baseline:
                    self.show("compare", render_compare(self.baseline, d))
                else:
                    self.show("compare", panel("Compare", Text(
                        "No baseline yet. Press s to save the current state as the baseline BEFORE you change hardware, "
                        "then come back here after the swap.", style=C["dim"])))
            except Exception as e:
                self.notify(f"Render error: {e}", severity="error")

        def tick(self):
            try:
                L = self.live
                cores = psutil.cpu_percent(percpu=True)
                L["cores"] = cores
                L["cpu"].append(sum(cores) / len(cores))
                L["temp"].append(cpu_temp())
                f = psutil.cpu_freq()
                L["freq"].append(f.current if f else 0)
                L["ram"].append(psutil.virtual_memory().percent)
                io = psutil.disk_io_counters()
                if io and self.last_io:
                    L["rd"].append((io.read_bytes - self.last_io.read_bytes) / 1e6)
                    L["wr"].append((io.write_bytes - self.last_io.write_bytes) / 1e6)
                self.last_io = io
                gpus = self.data["gpus"] if self.data else []
                self.tick_n += 1
                if gpus and self.tick_n % 2 == 0 and not self.busy:
                    refresh_gpu_stats(gpus)
                    for i, g in enumerate(gpus):
                        L["gpu_util"].setdefault(i, collections.deque(maxlen=90)).append(g["util"] or 0)
                        L["gpu_temp"].setdefault(i, collections.deque(maxlen=90)).append(g["temp_c"] or 0)
                if self.query_one(TabbedContent).active == "live":
                    self.show("live", render_live(L, gpus))
            except Exception:
                pass

    HwCheckApp().run()


# ===================================================================== CLI
def main():
    p = argparse.ArgumentParser(prog="hwcheck", description="Hardware health & second-hand checker")
    p.add_argument("command", nargs="?", default="tui", choices=["tui", "report", "snapshot", "compare", "stress"])
    p.add_argument("--bench", action="store_true", help="run benchmarks (report/snapshot/compare)")
    p.add_argument("--memtest", type=int, default=0, metavar="MB", help="run memtester on MB megabytes")
    p.add_argument("--stress", action="store_true", help="also run the 60 s CPU stress test")
    p.add_argument("--quick", action="store_true", help=argparse.SUPPRESS)  # old flag, benchmarks are opt-in now
    args = p.parse_args()

    if os.geteuid() != 0:
        console.print(f"[red]Please run with sudo:[/red] sudo python3 {sys.argv[0]} {args.command if args.command != 'tui' else ''}")
        sys.exit(1)
    for tool in ("smartctl", "dmidecode"):
        if not shutil.which(tool):
            console.print(f"[red]{tool} is not installed. Run the apt install command first.[/red]")
            sys.exit(1)

    if args.command == "tui":
        run_tui()
        return

    say = lambda m: console.print(f"[{C['cyan']}]{m}[/]")
    data = collect_all(say, bench=args.bench and not args.quick, memtest_mb=args.memtest,
                       stress_s=60 if (args.stress or args.command == "stress") else 0)
    d = base_dir()
    with open(os.path.join(d, "latest.json"), "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    if args.command == "compare":
        old = load_baseline()
        if not old:
            console.print("[red]Run `snapshot` first, no baseline found.[/red]")
            sys.exit(1)
        console.print(render_overview(data))
        console.print(render_compare(old, data))
    else:
        for r in render_all_plain(data):
            console.print(r)
        if args.command == "snapshot":
            console.print(f"\n[green]Baseline saved:[/green] {save_baseline(data)}\nCopy it to a pendrive as a backup.")
    fix_owner(d)


if __name__ == "__main__":
    main()
