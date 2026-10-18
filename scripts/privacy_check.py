#!/usr/bin/env python3
"""
Privacy check: run before every push.      python scripts/privacy_check.py

Looks at every file git would publish and stops you if it finds personal data:
  * files that must stay private (.env, data/, your profile folder, config.local.toml, logs, backups)
  * API keys (Google, Groq, OpenAI-style, NVIDIA, Cerebras, OpenRouter)
  * your own name, e-mail and phone number (read from your private profiles), and other e-mails / phone numbers
Exit code 0 = safe to push, 1 = something to fix first.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_PATHS = re.compile(r"^(\.env$|config\.local\.toml$|data/|backup_before_|venv/|\.venv/)|\.log$|^profiles/(?!example/|_packs/)")
KEYS = re.compile(r"AIza[0-9A-Za-z_-]{30,}|gsk_[0-9A-Za-z]{20,}|sk-(?:or-)?[0-9A-Za-z_-]{20,}|nvapi-[0-9A-Za-z_-]{20,}"
                  r"|csk-[0-9A-Za-z]{20,}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"(?:\+91[\s-]?)?\b[6-9]\d{4}[\s-]?\d{5}\b")
OK_EMAIL = re.compile(r"@(example\.(com|org)|users\.noreply\.github\.com)$", re.I)
TEXT_SUFFIX = {".py", ".md", ".toml", ".json", ".txt", ".html", ".css", ".csv", ".example", ".cfg", ".ini", ".yml", ".yaml", ""}


def files_to_publish() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        print("This folder is not a git repository yet. Run 'git init' first (see README).")
        sys.exit(1)
    return [f for f in out.splitlines() if f]


def personal_strings() -> list[str]:
    """Your name, e-mail and phone from every private profile (never from the example)."""
    found = []
    for rp in (ROOT / "profiles").glob("*/resume_profile.json"):
        if rp.parent.name == "example":
            continue
        try:
            c = json.loads(rp.read_text(encoding="utf-8")).get("candidate", {})
        except ValueError:
            continue
        for k in ("name", "email", "phone"):
            v = str(c.get(k, "")).strip()
            if len(v) >= 6:
                found.append(v)
        if c.get("phone"):
            found.append(re.sub(r"\D", "", c["phone"])[-10:])
    return [s for s in dict.fromkeys(found) if s]


def main() -> int:
    files = files_to_publish()
    mine = personal_strings()
    problems: list[str] = []
    for f in files:
        if PRIVATE_PATHS.search(f):
            problems.append(f"{f}: private file would be published")
            continue
        p = ROOT / f
        if p.suffix.lower() not in TEXT_SUFFIX or not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if KEYS.search(text):
            problems.append(f"{f}: looks like an API key")
        for s in mine:
            if s.lower() in text.lower() and f not in ("LICENSE", "README.md"):
                problems.append(f"{f}: contains your personal detail '{s[:3]}…'")
        for e in EMAIL.findall(text):
            if not OK_EMAIL.search(e) and not e.endswith((".png", ".jpg")) and "@" in e and "." in e.split("@")[1]:
                problems.append(f"{f}: e-mail address {e}")
        if PHONE.search(text) and f != "scripts/privacy_check.py":
            problems.append(f"{f}: looks like a phone number")
    print(f"Checked {len(files)} file(s) that would be published.")
    if problems:
        print("\nNOT SAFE TO PUSH yet:")
        for x in dict.fromkeys(problems):
            print("  - " + x)
        print("\nFix these (or add the file to .gitignore), then run this check again.")
        return 1
    print("Safe to push: no private files, keys or personal details found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
