#!/usr/bin/env python3
"""
Doctor: checks everything PipelineHQ needs and says how to fix what is wrong.

    python modules/doctor.py            # quick checks
    python modules/doctor.py --model    # also sends the local model one tiny test request (a few seconds)

Each check returns ok / warn / fail with a plain-English fix. The latest result is saved in data/doctor.json for the
control centre's Dashboard.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.settings import CFG, DATA_DIR, ROOT as _R, profile_dir  # noqa: E402,F401

RESULT = DATA_DIR / "doctor.json"


def _c(name: str, status: str, detail: str, fix: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _get_json(url: str, timeout: float = 3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def check_python() -> dict:
    v = sys.version_info
    ok = v >= (3, 11)
    return _c("Python", "ok" if ok else "fail", f"{v.major}.{v.minor}.{v.micro}",
              "" if ok else "Install Python 3.11 or newer from python.org, then recreate the venv.")


def check_packages() -> dict:
    needed = {"flask": "flask", "docx": "python-docx", "openai": "openai", "requests": "requests",
              "bs4": "beautifulsoup4", "pydantic": "pydantic", "dotenv": "python-dotenv"}
    optional = {"jobspy": "python-jobspy (Indeed/Naukri)", "pypdf": "pypdf (reading PDF resumes in setup)"}
    missing = [pip for mod, pip in needed.items() if importlib.util.find_spec(mod) is None]
    opt = [pip for mod, pip in optional.items() if importlib.util.find_spec(mod) is None]
    if missing:
        return _c("Python packages", "fail", "missing: " + ", ".join(missing), "Run:  pip install -r requirements.txt")
    if opt:
        return _c("Python packages", "warn", "optional missing: " + ", ".join(opt), "Run:  pip install -r requirements.txt")
    return _c("Python packages", "ok", "all installed")


def check_profile() -> dict:
    from modules.profile_pack import load_profile
    folder = profile_dir()
    try:
        p = load_profile(folder)
    except SystemExit as e:
        return _c("Profile", "fail", f"{folder.name}: {str(e)[:200]}", "Fix the file named above, or create a profile with Set up.")
    tpl = (folder / "resume_template.docx").exists()
    name = folder.name + (" (the fictional sample)" if folder.name == "example" else "")
    if not tpl:
        return _c("Profile", "warn", f"{name}: no resume_template.docx",
                  "Run:  python modules/resume_builder.py --make-template your_resume.docx")
    return _c("Profile", "warn" if folder.name == "example" else "ok",
              f"{name}: {len(p.quotable())} facts, variants: {', '.join(p.targets)}",
              "Create your own profile: Dashboard > Set up my profile." if folder.name == "example" else "")


def check_lm_studio() -> list[dict]:
    lc = CFG["local_llm"]
    provider = lc.get("provider", "lmstudio")
    base = lc["base_url"].rstrip("/")
    names = {"lmstudio": "LM Studio", "ollama": "Ollama"}
    pname = names.get(provider, provider)
    try:
        listed = _get_json(base + "/models").get("data", [])
    except Exception:  # noqa: BLE001
        fix = {"lmstudio": "Open LM Studio, go to the Developer tab, load your model and start the server.",
               "ollama": "Start Ollama: open the Ollama app, or run 'ollama serve' in Terminal."}
        return [_c("Local model server", "fail", f"{pname}: no answer at {base}",
                   fix.get(provider, "Start your local model server, or correct base_url in config.toml."))]
    out = [_c("Local model server", "ok", f"{pname} answering at {base}")]
    if provider != "lmstudio":
        model = lc.get("model", "")
        ids = [m.get("id", "") for m in listed]
        if not model:
            out.append(_c("Model", "fail", "no model name set", 'Set [local_llm] model = "..." in config.toml.'))
        elif ids and not any(model == i or model in i for i in ids):
            out.append(_c("Model", "fail", f"'{model}' is not available on {pname}",
                          f"Download it first (e.g. 'ollama pull {model}'), or pick one of: {', '.join(ids[:6])}"))
        else:
            out.append(_c("Model", "ok", model))
        out.append(_c("Context length", "warn", f"cannot be read from {pname}",
                      "Make sure it is 16384 or more (Ollama: set OLLAMA_CONTEXT_LENGTH=16384 or the app's context "
                      "setting); a shorter one silently cuts prompts. Run the model test below."))
        return out
    root = base[:-3] if base.endswith("/v1") else base
    try:
        models = _get_json(root + "/api/v0/models").get("data", [])
        loaded = [m for m in models if m.get("state") == "loaded" and m.get("type", "llm") in ("llm", "vlm")]
        if not loaded:
            out.append(_c("Model loaded", "warn", "none loaded right now",
                          "Load your model in LM Studio (a run also tries to load it by itself)."))
        else:
            m = loaded[0]
            ctx = m.get("loaded_context_length") or m.get("context_length")
            out.append(_c("Model loaded", "ok", m.get("id", "?")))
            if ctx:
                need = int(CFG.get("orchestrator", {}).get("context_length", 16384))
                out.append(_c("Context length", "ok" if int(ctx) >= need else "fail", f"{ctx} tokens",
                              "" if int(ctx) >= need else f"Reload the model with Context Length {need} or more."))
    except Exception:  # noqa: BLE001
        out.append(_c("Model loaded", "warn", "could not ask LM Studio which model is loaded", ""))
    return out


def check_model_reply() -> dict:
    from modules.local_analyzer import extract_json, make_llm
    try:
        call = make_llm("")
        reply = call([{"role": "system", "content": "Output ONLY one JSON object."},
                      {"role": "user", "content": 'Reply with {"ok": true}'}])
    except (Exception, SystemExit) as e:  # noqa: BLE001
        return _c("Model test", "fail", f"{type(e).__name__}: {str(e)[:150]}", "Check that the model is loaded and the server is on.")
    info = getattr(call, "last", {}) or {}
    try:
        extract_json(reply)
        valid = True
    except ValueError:
        valid = False
    if info.get("thinking"):
        return _c("Model test", "fail", f"{call.model_name}: it is 'thinking' before answering (slow)",
                  "Switch thinking off. LM Studio: model settings > Prompt Template, add "
                  "{%- set enable_thinking = false %} as the first line and reload. Other tools: use their "
                  "'think' / reasoning option, or a non-thinking model.")
    if not valid:
        return _c("Model test", "warn", f"{call.model_name}: reply was not valid JSON", "Try another model, e.g. Qwen 3.5 9B.")
    secs = info.get("seconds", 0)
    return _c("Model test", "ok", f"{call.model_name}: valid answer in {secs:.0f}s, no thinking")


def check_pdf() -> dict:
    from modules.resume_builder import _soffice
    if _soffice():
        return _c("PDF converter", "ok", "LibreOffice (silent)")
    if importlib.util.find_spec("docx2pdf"):
        return _c("PDF converter", "warn", "Microsoft Word via docx2pdf (asks 'Grant access' per folder)",
                  "Install LibreOffice (free) so scheduled runs never stop for a prompt.")
    return _c("PDF converter", "warn", "none: only Word files are made", "Install LibreOffice (free) from libreoffice.org.")


def check_cloud() -> dict:
    from modules.cloud_generator import configured_providers
    n = configured_providers()
    return _c("Cloud polish keys", "ok" if n else "warn", ", ".join(n) if n else "none (optional)",
              "" if n else "Optional: add a free key (e.g. Groq or Gemini) to .env for smoother wording.")


def check_disk() -> dict:
    free = shutil.disk_usage(ROOT).free / 1e9
    return _c("Disk space", "ok" if free >= 20 else "warn", f"{free:.0f} GB free",
              "" if free >= 20 else "Free some space: models, swap and resumes need room (keep 20 GB+ free).")


def check_schedule() -> dict:
    from modules import scheduler
    st = scheduler.status()
    if not st["mac"]:
        return _c("Morning run", "warn", "scheduling is macOS-only", "Run  python orchestrator.py  yourself, or use your OS scheduler.")
    if st["schedule"]["enabled"] and not st["morning_installed"]:
        return _c("Morning run", "fail", "switched on but not installed", "Open Settings and press Save settings.")
    last = {}
    try:
        last = json.loads((DATA_DIR / "last_run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    detail = f"on at {st['schedule']['time']}" if st["schedule"]["enabled"] else "off"
    if last.get("finished"):
        age = (datetime.now() - datetime.fromisoformat(last["finished"])).days
        detail += f", last run {age} day(s) ago"
        if st["schedule"]["enabled"] and age > 2:
            return _c("Morning run", "warn", detail, "The Mac may have been off or asleep; see data/logs/scheduled-runs.log.")
    return _c("Morning run", "ok", detail)


def run(model: bool = False) -> list[dict]:
    checks = [check_python(), check_packages(), check_profile(), *check_lm_studio()]
    if model and checks and any(c["name"] == "Local model server" and c["status"] == "ok" for c in checks):
        checks.append(check_model_reply())
    for fn in (check_pdf, check_cloud, check_disk, check_schedule):
        try:
            checks.append(fn())
        except Exception as e:  # noqa: BLE001
            checks.append(_c(fn.__name__.replace("check_", "").title(), "warn", f"check failed: {e}"))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "checks": checks}, indent=2),
                      encoding="utf-8")
    return checks


def main() -> int:
    checks = run(model="--model" in sys.argv)
    mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    for c in checks:
        print(f"{mark[c['status']]}  {c['name']:<20} {c['detail']}")
        if c["fix"] and c["status"] != "ok":
            print(f"      fix: {c['fix']}")
    return 1 if any(c["status"] == "fail" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
