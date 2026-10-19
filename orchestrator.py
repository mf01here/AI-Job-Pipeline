#!/usr/bin/env python3
"""
Orchestrator: the whole pipeline in one command
===============================================

  1. Local model ready   - LM Studio: starts its server and loads your model ("lms" tool); other providers
                           (Ollama, llama.cpp, ...): checks the server answers
  2. Scrape              - top-up by default (adds new jobs); a full refresh every N days drops expired ones
  3. Analyse             - only jobs not analysed before (Phase 2/3)
  4. Draft               - only newly approved jobs (Phase 4)
  5. Resumes             - only for new drafts (Phase 5), plus the application ledger
  6. Summary             - data/last_run.json, printed, and (optional) a push notification

What to search is in profiles/<name>/search.toml (edited by the Settings page later).

Usage
    python orchestrator.py                 # the normal daily run
    python orchestrator.py --fresh         # full refresh of the job list instead of a top-up
    python orchestrator.py --steps analyze,draft,build     # only some steps
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from modules.settings import CFG, DATA_DIR, profile_dir  # noqa: E402

log = logging.getLogger("orchestrator")
STEPS = ["model", "scrape", "analyze", "draft", "build"]
LOCK = DATA_DIR / "run.lock"
STATUS = DATA_DIR / "run_status.json"     # live progress for the control centre, whoever started the run
STEP_NAMES = {"model": "Model", "scrape": "Scrape", "analyze": "Analyse", "draft": "Draft", "build": "Resumes"}
_STATUS: dict = {}


def set_status(**kw) -> None:
    _STATUS.update(kw)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATUS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_STATUS, indent=2), encoding="utf-8")
        os.replace(tmp, STATUS)
    except OSError:
        pass


def step_state(key: str, state: str) -> None:
    steps = dict(_STATUS.get("steps", {}))
    steps[key] = state
    set_status(steps=steps, current=key if state == "running" else _STATUS.get("current"))
LAST_RUN = DATA_DIR / "last_run.json"
LOG_DIR = DATA_DIR / "logs"
PY = sys.executable


def load_search(profile: str | None) -> dict:
    path = profile_dir(profile) / "search.toml"
    defaults = {"roles": ["Design Engineer"], "locations": [], "country": "India",
                "sites": ["indeed", "naukri", "linkedin"], "scan_company_portals": True, "title_match": "broad",
                "max_age_days": 30, "full_refresh_every_days": 7, "schedule": {}, "notify": {}}
    if not path.exists():
        return defaults
    with open(path, "rb") as f:
        return {**defaults, **tomllib.load(f)}


# --------------------------------------------------------------------------- #
# Step 1: local model
# --------------------------------------------------------------------------- #

def _server_up() -> bool:
    url = CFG["local_llm"]["base_url"].rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def _lms() -> str | None:
    for c in (shutil.which("lms"), str(Path.home() / ".lmstudio" / "bin" / "lms"),
              str(Path.home() / ".cache" / "lm-studio" / "bin" / "lms")):
        if c and Path(c).exists():
            return c
    return None


def ensure_model() -> bool:
    """Make sure the local model server answers. With LM Studio it is started and the model loaded if needed."""
    lc = CFG["local_llm"]
    provider = lc.get("provider", "lmstudio")
    if provider == "lmstudio":
        model = lc.get("model") or CFG.get("orchestrator", {}).get("model", "qwen/qwen3.5-9b")
        ctx = str(CFG.get("orchestrator", {}).get("context_length", 16384))
        lms = _lms()
        if lms:
            if not _server_up():
                log.info("Starting LM Studio's local server...")
                subprocess.run([lms, "server", "start"], capture_output=True, timeout=120)
            loaded = subprocess.run([lms, "ps"], capture_output=True, text=True, timeout=60).stdout
            if model.split("/")[-1].lower() not in loaded.lower():
                log.info("Loading %s (context %s)...", model, ctx)
                r = subprocess.run([lms, "load", model, "--context-length", ctx, "-y"],
                                   capture_output=True, text=True, timeout=600)
                if r.returncode != 0:
                    log.warning("Could not load the model automatically: %s", (r.stderr or r.stdout).strip()[:300])
    for _ in range(30):
        if _server_up():
            if provider != "lmstudio" and not lc.get("model"):
                log.error("Set the model's name in config.toml ([local_llm] model = ...) for provider '%s'.", provider)
                return False
            return True
        time.sleep(2)
    hint = {"lmstudio": "Open LM Studio, load your model and start the server (or install LM Studio's 'lms' tool "
                        "so this happens automatically).",
            "ollama": "Start Ollama (open the Ollama app, or run 'ollama serve' in Terminal)."}
    log.error("The local model server is not answering at %s. %s", lc["base_url"],
              hint.get(provider, "Start your local model server."))
    return False


# --------------------------------------------------------------------------- #
# Steps 2-5: the existing phase scripts
# --------------------------------------------------------------------------- #

def run_step(name: str, cmd: list[str], logfile: Path) -> int:
    key = next((k for k, v in STEP_NAMES.items() if v == name), name.lower())
    step_state(key, "running")
    log.info("\n=== %s ===", name)
    t0 = time.time()
    with open(logfile, "a", encoding="utf-8") as lf:
        lf.write(f"\n\n===== {name}  {datetime.now():%Y-%m-%d %H:%M:%S} =====\n$ {' '.join(cmd)}\n")
        lf.flush()
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        for line in proc.stdout:          # show progress live and keep a copy in the log file
            sys.stdout.write(line)
            lf.write(line)
        proc.wait()
    log.info("--- %s finished in %.0f min (exit %d)", name, (time.time() - t0) / 60, proc.returncode)
    step_state(key, "done" if proc.returncode == 0 else "failed")
    return proc.returncode


def scrape_cmd(search: dict, fresh: bool, profile: str | None) -> list[str]:
    cmd = [PY, "modules/scraper.py", "--role", "; ".join(search["roles"]),
           "--location", ", ".join(search["locations"]), "--country", search["country"] or "worldwide",
           "--company", "", "--titles", search["title_match"], "--days", str(search["max_age_days"]),
           "--sites", ",".join(search["sites"]) or "none"]
    if search.get("scan_company_portals"):
        cmd.append("--scan-all-ats")
    else:
        cmd.append("--no-ats")
    if not fresh:
        cmd.append("--add")
    if profile:
        cmd += ["--profile", profile]
    return cmd


def needs_full_refresh(search: dict) -> bool:
    state = _read(DATA_DIR / "scraper_state.json", {})
    last = state.get("last_full_refresh")
    if not last:
        return True
    return datetime.now() - datetime.fromisoformat(last) > timedelta(days=int(search["full_refresh_every_days"]))


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (ValueError, OSError):
        return default


def _mark_full_refresh() -> None:
    path = DATA_DIR / "scraper_state.json"
    state = _read(path, {})
    state["last_full_refresh"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Step 6: summary + notification
# --------------------------------------------------------------------------- #

def summarize(started: datetime, results: dict, approved_before: set[str]) -> dict:
    scrape = _read(DATA_DIR / "last_scrape.json", {})
    evaluated = _read(DATA_DIR / "evaluated_jobs.json", [])
    counts: dict[str, int] = {}
    for e in evaluated:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    new_approved = [f"{e['job']['title']} @ {e['job']['company']} ({e['overall_score']:.0f}%)"
                    for e in evaluated if e["status"] == "approved_for_phase4"
                    and e["job"]["job_id"] not in approved_before]
    summary = {"started": started.isoformat(timespec="seconds"),
               "finished": datetime.now().isoformat(timespec="seconds"),
               "steps": results, "jobs_new": scrape.get("jobs_new"), "jobs_total": scrape.get("jobs_total"),
               "leads": scrape.get("leads"), "linkedin_blocked": scrape.get("linkedin_blocked"),
               "status_counts": counts, "new_approved": new_approved}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    hist_path = DATA_DIR / "run_history.json"
    history = _read(hist_path, [])
    history.append({"finished": summary["finished"], "jobs_new": summary.get("jobs_new") or 0,
                    "approved_new": len(new_approved), "approved_total": counts.get("approved_for_phase4", 0),
                    "ok": all(v in (0, "skipped") for v in results.values())})
    hist_path.write_text(json.dumps(history[-60:], indent=2), encoding="utf-8")
    return summary


def notify(search: dict, summary: dict) -> None:
    topic = (search.get("notify") or {}).get("ntfy_topic", "").strip()
    if not topic:
        return
    failed = [k for k, v in summary["steps"].items() if v not in (0, "skipped")]
    text = (f"{summary.get('jobs_new') or 0} new jobs, {len(summary['new_approved'])} newly approved"
            + (f"; problem in: {', '.join(failed)}" if failed else "; resumes ready"))
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"),
                                     headers={"Title": "Job pipeline: morning run done"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.warning("Notification not sent: %s", e)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Run the whole job pipeline")
    ap.add_argument("--profile", help="profile folder under profiles/ (default: config.toml)")
    ap.add_argument("--fresh", action="store_true", help="full refresh of the job list instead of a top-up")
    ap.add_argument("--steps", default=",".join(STEPS), help=f"comma-separated subset of: {','.join(STEPS)}")
    ap.add_argument("--trigger", default="manual", help="who started the run: manual, web, scheduled, catch-up")
    ap.add_argument("--job-id", action="append", metavar="ID",
                    help="draft and build just this job (e.g. one you approved on the review page)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    steps = [s.strip() for s in args.steps.split(",") if s.strip() in STEPS]
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 6 * 3600:
        log.error("Another run seems to be in progress (data/run.lock). If not, delete that file and try again.")
        return 1
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(str(os.getpid()))
    started = datetime.now()
    logfile = LOG_DIR / f"run-{started:%Y%m%d-%H%M}.log"
    logfile.touch()
    if args.trigger in ("scheduled", "catch-up"):
        sp = DATA_DIR / "schedule_state.json"
        sp.write_text(json.dumps({"last_scheduled_date": started.strftime("%Y-%m-%d"), "trigger": args.trigger}),
                      encoding="utf-8")
    set_status(pid=os.getpid(), trigger=args.trigger, started=started.isoformat(timespec="seconds"),
               log=str(logfile), running=True, finished=None, current=None,
               steps={k: "pending" for k in STEPS if k in steps or (k == "model" and {"analyze", "draft"} & set(steps))},
               job_ids=args.job_id or [])
    search = load_search(args.profile)
    prof = ["--profile", args.profile] if args.profile else []
    approved_before = {e["job"]["job_id"] for e in _read(DATA_DIR / "evaluated_jobs.json", [])
                       if e["status"] == "approved_for_phase4"}
    results: dict[str, object] = {s: "skipped" for s in STEPS}
    try:
        log.info("Run started %s | roles: %s | locations: %s", f"{started:%Y-%m-%d %H:%M}",
                 ", ".join(search["roles"]), ", ".join(search["locations"]) or "any")
        model_ok = True
        if "model" in steps or {"analyze", "draft"} & set(steps):
            step_state("model", "running")
            model_ok = ensure_model()
            results["model"] = 0 if model_ok else 1
            step_state("model", "done" if model_ok else "failed")
        if "scrape" in steps:
            fresh = args.fresh or needs_full_refresh(search)
            log.info("Job list: %s", "full refresh" if fresh else "top-up (adding new jobs)")
            results["scrape"] = run_step("Scrape", scrape_cmd(search, fresh, args.profile), logfile)
            if fresh and results["scrape"] == 0:
                _mark_full_refresh()
        if "analyze" in steps and model_ok:
            results["analyze"] = run_step("Analyse", [PY, "modules/local_analyzer.py", *prof], logfile)
        only = [x for jid in (args.job_id or []) for x in ("--job-id", jid)]
        if "draft" in steps and model_ok:
            results["draft"] = run_step("Draft", [PY, "modules/cloud_generator.py", *only, *prof], logfile)
        if "build" in steps:
            results["build"] = run_step("Resumes", [PY, "modules/resume_builder.py", *(only or ["--new-only"]), *prof],
                                        logfile)
    except KeyboardInterrupt:
        log.warning("\nStopped by you.")
    finally:
        LOCK.unlink(missing_ok=True)
        set_status(running=False, finished=datetime.now().isoformat(timespec="seconds"), current=None)

    summary = summarize(started, results, approved_before)
    log.info("\n==================== SUMMARY ====================")
    log.info("New jobs: %s (list now %s)   Leads without description: %s", summary["jobs_new"],
             summary["jobs_total"], summary["leads"])
    log.info("Status: %s", ", ".join(f"{v} {k}" for k, v in sorted(summary["status_counts"].items())))
    log.info("Newly approved (%d):%s", len(summary["new_approved"]),
             "".join(f"\n  - {x}" for x in summary["new_approved"]) or " none")
    if summary.get("linkedin_blocked"):
        log.info("LinkedIn limited requests this run; it will try again next run.")
    log.info("Steps: %s", ", ".join(f"{k}={v}" for k, v in results.items()))
    log.info("Full log: %s", logfile)
    notify(search, summary)
    return 0 if all(v in (0, "skipped") for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
