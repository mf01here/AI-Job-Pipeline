#!/usr/bin/env python3
"""
Scheduling on macOS (launchd, built into every Mac; nothing to install)
======================================================================

Two background jobs, each a small file in ~/Library/LaunchAgents:
  com.jobpipeline.morning        runs the orchestrator every day at your chosen time (Settings page)
  com.jobpipeline.controlcentre  keeps the control centre running, so your iPhone can reach it any time

If the Mac is asleep at the scheduled time, macOS runs the job as soon as it wakes. During a run the Mac is kept
awake (caffeinate) so a long run is not cut off.

Usage (the Settings page does this for you):
    python modules/scheduler.py --status
    python modules/scheduler.py --apply              # morning run on/off and time, from search.toml
    python modules/scheduler.py --background on|off  # control centre always running
    python modules/scheduler.py --passcode 4821      # turn on iPhone access with this passcode
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.settings import DATA_DIR, profile_dir  # noqa: E402

AGENTS = Path.home() / "Library" / "LaunchAgents"
RUN_LABEL = "com.jobpipeline.morning"
WEB_LABEL = "com.jobpipeline.controlcentre"
PY = sys.executable
PATH = ":".join([str(Path(PY).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin",
                 "/sbin", str(Path.home() / ".lmstudio" / "bin")])


def _is_mac() -> bool:
    return sys.platform == "darwin" and shutil.which("launchctl") is not None


def _uid() -> int:
    return os.getuid()


def _plist_path(label: str) -> Path:
    return AGENTS / f"{label}.plist"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _install(label: str, spec: dict) -> tuple[bool, str]:
    if not _is_mac():
        return False, "Scheduling works on macOS only."
    AGENTS.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    path = _plist_path(label)
    with open(path, "wb") as f:
        plistlib.dump({"Label": label, **spec}, f)
    _launchctl("bootout", f"gui/{_uid()}/{label}")          # remove an older copy (ignore "not loaded")
    r = _launchctl("bootstrap", f"gui/{_uid()}", str(path))
    if r.returncode != 0:                                    # older macOS
        r = _launchctl("load", "-w", str(path))
    ok = r.returncode == 0 or is_loaded(label)
    return ok, "" if ok else (r.stderr or r.stdout).strip()[:300]


def _remove(label: str) -> tuple[bool, str]:
    if not _is_mac():
        return True, ""
    _launchctl("bootout", f"gui/{_uid()}/{label}")
    _plist_path(label).unlink(missing_ok=True)
    return True, ""


def is_loaded(label: str) -> bool:
    return _is_mac() and _launchctl("print", f"gui/{_uid()}/{label}").returncode == 0


# --------------------------------------------------------------------------- #
# Morning run
# --------------------------------------------------------------------------- #

def read_schedule(profile: str | None = None) -> dict:
    p = profile_dir(profile) / "search.toml"
    if not p.exists():
        return {"enabled": False, "time": "06:30"}
    with open(p, "rb") as f:
        s = tomllib.load(f).get("schedule", {})
    return {"enabled": bool(s.get("enabled")), "time": str(s.get("time", "06:30"))}


def apply_schedule(profile: str | None = None) -> tuple[bool, str]:
    s = read_schedule(profile)
    if not s["enabled"]:
        _remove(RUN_LABEL)
        return True, "Morning run is off."
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s["time"])
    hour, minute = (int(m.group(1)), int(m.group(2))) if m else (6, 30)
    args = ["/usr/bin/caffeinate", "-i", PY, str(ROOT / "orchestrator.py")]
    if profile:
        args += ["--profile", profile]
    ok, err = _install(RUN_LABEL, {
        "ProgramArguments": args, "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(DATA_DIR / "logs" / "scheduled-runs.log"),
        "StandardErrorPath": str(DATA_DIR / "logs" / "scheduled-runs.log"),
        "EnvironmentVariables": {"PATH": PATH, "PYTHONUNBUFFERED": "1"}})
    return ok, (f"Morning run scheduled every day at {hour:02d}:{minute:02d}." if ok
                else f"Could not schedule the morning run: {err}")


# --------------------------------------------------------------------------- #
# Control centre always running
# --------------------------------------------------------------------------- #

def set_background(on: bool) -> tuple[bool, str]:
    if not on:
        _remove(WEB_LABEL)
        return True, "The control centre no longer runs in the background."
    ok, err = _install(WEB_LABEL, {
        "ProgramArguments": [PY, str(ROOT / "webapp" / "app.py")], "WorkingDirectory": str(ROOT),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15,
        "StandardOutPath": str(DATA_DIR / "logs" / "control-centre.log"),
        "StandardErrorPath": str(DATA_DIR / "logs" / "control-centre.log"),
        "EnvironmentVariables": {"PATH": PATH, "PYTHONUNBUFFERED": "1"}})
    return ok, ("The control centre now runs in the background and starts by itself when you log in. "
                "If you also started it in Terminal, close that window; the background copy takes over within "
                "15 seconds." if ok else f"Could not start it in the background: {err}")


def restart_background() -> None:
    if is_loaded(WEB_LABEL):
        _launchctl("kickstart", "-k", f"gui/{_uid()}/{WEB_LABEL}")


# --------------------------------------------------------------------------- #
# iPhone passcode (.env)
# --------------------------------------------------------------------------- #

def set_passcode(code: str) -> tuple[bool, str]:
    code = code.strip()
    if code and (len(code) < 4 or not re.fullmatch(r"[A-Za-z0-9_-]+", code)):
        return False, "Use at least 4 letters or digits (no spaces)."
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.startswith("WEBAPP_PASSCODE=")]
    if code:
        lines.append(f"WEBAPP_PASSCODE={code}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True, ("iPhone access is on." if code else "iPhone access is off.")


def status() -> dict:
    s = read_schedule()
    return {"mac": _is_mac(), "schedule": s, "morning_installed": is_loaded(RUN_LABEL),
            "background": is_loaded(WEB_LABEL)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Morning run and background control centre (macOS)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--apply", action="store_true", help="install or remove the morning run from search.toml")
    ap.add_argument("--background", choices=["on", "off"])
    ap.add_argument("--passcode", metavar="CODE", help="set the iPhone passcode ('' turns iPhone access off)")
    ap.add_argument("--profile")
    args = ap.parse_args()
    if args.passcode is not None:
        ok, msg = set_passcode(args.passcode)
        print(msg)
        restart_background()
    if args.apply:
        print(apply_schedule(args.profile)[1])
    if args.background:
        print(set_background(args.background == "on")[1])
    if args.status or not (args.apply or args.background or args.passcode is not None):
        st = status()
        print(f"Morning run: {'on at ' + st['schedule']['time'] if st['schedule']['enabled'] else 'off'} "
              f"({'installed' if st['morning_installed'] else 'not installed'})")
        print(f"Control centre in background: {'yes' if st['background'] else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
