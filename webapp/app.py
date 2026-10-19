#!/usr/bin/env python3
"""
Control centre: a small web app that runs only on your Mac (and your home Wi-Fi, if you set a passcode).

    python webapp/app.py            # then open http://localhost:8765

Pages: Dashboard (run now, last run) · Approved · Needs review · Leads (paste descriptions) · Applications
(your tracker) · Archived · Settings (roles, locations, sources, schedule).

Safety
  * Without WEBAPP_PASSCODE in .env it listens on this Mac only (localhost).
  * With a passcode it also listens on your home network (for your iPhone) and asks for the passcode.
  * It is never reachable from the internet unless you set that up yourself.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,  # noqa: E402
                   send_file, session, url_for)

from modules.resume_builder import LEDGER_COLUMNS, read_ledger, write_ledger  # noqa: E402
from webapp import charts  # noqa: E402
from modules import companies as comp  # noqa: E402
from modules import scheduler  # noqa: E402
from modules import onboarding as ob  # noqa: E402
from modules import doctor  # noqa: E402
from modules.settings import DATA_DIR, profile_dir  # noqa: E402

PORT = int(os.getenv("WEBAPP_PORT", "8765"))
PASSCODE = os.getenv("WEBAPP_PASSCODE", "").strip()
EVALUATED = DATA_DIR / "evaluated_jobs.json"
PENDING = DATA_DIR / "pending_jobs.json"
LEADS = DATA_DIR / "leads.json"
DECISIONS = DATA_DIR / "decisions.json"
LAST_RUN = DATA_DIR / "last_run.json"
TASK = DATA_DIR / "web_task.json"
DRAFTS = DATA_DIR / "tailored_drafts"
RESUMES = DATA_DIR / "resumes"
STATUSES = ["resume_ready", "applied", "interview", "offer", "rejected", "withdrawn", "archived"]

app = Flask(__name__)


def _secret() -> str:
    p = DATA_DIR / ".webapp_secret"
    if not p.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(32))
    return p.read_text().strip()


app.secret_key = _secret()


# --------------------------------------------------------------------------- #
# Data helpers
# --------------------------------------------------------------------------- #

def jread(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (ValueError, OSError):
        return default


def jwrite(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def drafts_by_job() -> dict[str, dict]:
    out = {}
    for p in DRAFTS.glob("*.json"):
        d = jread(p, {})
        if d.get("job", {}).get("job_id"):
            out[d["job"]["job_id"]] = {"path": p, "md": p.with_suffix(".md")}
    return out


def jobs_view() -> list[dict]:
    """Every analysed job with your decision, ledger status and resume files merged in."""
    evaluated = jread(EVALUATED, [])
    decisions = jread(DECISIONS, {})
    ledger = read_ledger()
    drafts = drafts_by_job()
    out = []
    for e in evaluated:
        jid = e["job"]["job_id"]
        dec = (decisions.get(jid) or {}).get("decision")
        row = ledger.get(jid, {})
        view = e.get("scored_analysis") or e.get("analysis") or {}
        status = e["status"]
        if dec == "approved":
            status = "approved_for_phase4"
        if dec == "archived" or row.get("status") == "archived":
            status = "user_archived"
        out.append({
            "id": jid, "key": hashlib.sha1(jid.encode()).hexdigest()[:12], "job": e["job"], "status": status,
            "system_status": e["status"], "decision": dec, "score": e.get("overall_score", 0),
            "hard": e.get("hard_match_rate"), "pref": e.get("preferred_match_rate"),
            "variant": e.get("target_id") or "", "variant_scores": e.get("target_scores", {}),
            "reasons": e.get("status_reasons", []), "reqs": view.get("requirements_evaluation", []),
            "missing": view.get("missing_keywords", []), "flags": view.get("risk_flags", []),
            "ledger": row.get("status", ""), "applied_on": row.get("applied_on", ""),
            "pdf": row.get("resume_pdf", ""), "docx": row.get("resume_docx", ""),
            "has_draft": jid in drafts, "error": e.get("error"),
        })
    out.sort(key=lambda j: -(j["score"] or 0))
    return out


def by_key(key: str) -> dict:
    j = next((x for x in jobs_view() if x["key"] == key), None)
    if not j:
        abort(404)
    return j


def set_decision(jid: str, decision: str | None) -> None:
    d = jread(DECISIONS, {})
    if decision:
        d[jid] = {"decision": decision, "at": datetime.now().isoformat(timespec="seconds")}
    else:
        d.pop(jid, None)
    jwrite(DECISIONS, d)


def set_ledger(job: dict, **fields) -> None:
    rows = read_ledger()
    jid = job["job_id"]
    row = rows.get(jid) or {c: "" for c in LEDGER_COLUMNS}
    row.update({"job_id": jid, "company": job.get("company", ""), "title": job.get("title", ""),
                "location": job.get("location", ""), "application_url": job.get("application_url", ""),
                "content_hash": job.get("content_hash", "")})
    row.update(fields)
    rows[jid] = row
    write_ledger(rows)


# --------------------------------------------------------------------------- #
# Background tasks (one at a time; the orchestrator's lock also prevents overlaps)
# --------------------------------------------------------------------------- #

RUN_STATUS = DATA_DIR / "run_status.json"
TRIGGER_LABELS = {"scheduled": "Morning run (scheduled)", "catch-up": "Morning run (catching up after sleep)",
                  "manual": "Run started from Terminal", "web": "Run"}
STEP_TITLES = {"model": "Model", "scrape": "Scrape", "analyze": "Analyse", "draft": "Draft", "build": "Resumes"}


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def task_state() -> dict:
    """The current or latest run, whoever started it (Run now, the morning schedule, Terminal)."""
    rs = jread(RUN_STATUS, {})
    web = jread(TASK, {})
    t: dict = {}
    if rs.get("pid"):
        running = bool(rs.get("running")) and _alive(rs["pid"])
        label = web.get("label") if web.get("pid") == rs.get("pid") or rs.get("trigger") == "web" and web.get("label") \
            else TRIGGER_LABELS.get(rs.get("trigger", "manual"), "Run")
        t = {"running": running, "label": label, "log": rs.get("log"), "started": rs.get("started"),
             "finished": rs.get("finished"), "trigger": rs.get("trigger"),
             "steps": [{"key": k, "name": STEP_TITLES.get(k, k), "state": v} for k, v in (rs.get("steps") or {}).items()]}
        if not running and rs.get("running"):          # the process died without finishing (e.g. Mac restarted)
            t["interrupted"] = True
    elif web.get("pid"):                                 # older single-job tasks
        t = {**web, "running": _alive(web["pid"]), "steps": []}
    lines = []
    log = Path(t["log"]) if t.get("log") else None
    if log and log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    t["tail"] = "\n".join(lines)
    prog = next((m for m in (re.search(r"\[(\d+)/(\d+)\]", ln) for ln in reversed(lines)) if m), None)
    current = next((s_["name"] for s_ in t.get("steps", []) if s_["state"] == "running"), "")
    t["progress"] = (f"{current}: job {prog.group(1)} of {prog.group(2)}" if prog and current in ("Analyse", "Draft")
                     else (f"{current}…" if current else ""))
    try:
        started = datetime.fromisoformat(t["started"])
        if t.get("running"):
            end = datetime.now()
        elif t.get("finished"):
            end = datetime.fromisoformat(t["finished"])
        else:
            end = datetime.fromtimestamp(log.stat().st_mtime)
        t["minutes"] = max(0, round((end - started).total_seconds() / 60))
        t["started_hm"] = started.strftime("%d %b, %H:%M")
        if not t.get("running"):
            t["finished_ago_s"] = int((datetime.now() - end).total_seconds())
    except (KeyError, ValueError, AttributeError, OSError, TypeError):
        pass
    return t


def next_run() -> str:
    """'today 06:30' / 'tomorrow 06:30' / 'Mon 06:30' / '' (off)."""
    s = read_search()["schedule"]
    if not s.get("enabled"):
        return ""
    days = scheduler.clean_days(s.get("days"))
    last = jread(DATA_DIR / "schedule_state.json", {}).get("last_scheduled_date")
    hh, mm = (s.get("time") or "06:30").split(":")
    now = datetime.now()
    for ahead in range(0, 8):
        day = now.date() + timedelta(days=ahead)
        if scheduler.DAYS[day.weekday()] not in days:
            continue
        due = datetime.combine(day, datetime.min.time()).replace(hour=int(hh), minute=int(mm))
        if ahead == 0:
            if last == day.isoformat():
                continue
            if now >= due:
                return f"today, as soon as possible (it was due at {s['time']})"
            return f"today {s['time']}"
        return f"{'tomorrow' if ahead == 1 else scheduler.DAY_NAMES[scheduler.DAYS[day.weekday()]]} {s['time']}"
    return ""


def catch_up_check() -> bool:
    """Start today's morning run if its time has passed and it has not run (e.g. the Mac was asleep)."""
    s = read_search()["schedule"]
    if not s.get("enabled") or (DATA_DIR / "run.lock").exists() or task_state().get("running"):
        return False
    try:
        hh, mm = (s.get("time") or "06:30").split(":")
        due = datetime.now().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except ValueError:
        return False
    last = jread(DATA_DIR / "schedule_state.json", {}).get("last_scheduled_date")
    if scheduler.DAYS[date.today().weekday()] not in scheduler.clean_days(s.get("days")):
        return False                                   # not a run day
    if datetime.now() < due or last == date.today().isoformat():
        return False
    cmd = [sys.executable, str(ROOT / "orchestrator.py"), "--trigger", "catch-up"]
    if sys.platform == "darwin" and Path("/usr/bin/caffeinate").exists():
        cmd = ["/usr/bin/caffeinate", "-i", *cmd]
    logs = DATA_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "scheduled-runs.log", "a", encoding="utf-8") as lf:
        subprocess.Popen(cmd, cwd=ROOT, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True,
                         env={**os.environ, "PYTHONUNBUFFERED": "1"})
    return True


def _catch_up_loop() -> None:
    import time as _t
    _t.sleep(30)                       # let the Mac settle after waking / logging in
    while True:
        try:
            catch_up_check()
        except Exception:  # noqa: BLE001 - never let the checker stop the control centre
            pass
        _t.sleep(60)


def time_since(path: Path) -> float:
    try:
        return datetime.now().timestamp() - path.stat().st_mtime
    except OSError:
        return 1e9


def start_task(label: str, args: list[str]) -> bool:
    if task_state().get("running") or ((DATA_DIR / "run.lock").exists() and
                                       time_since(DATA_DIR / "run.lock") < 6 * 3600):
        flash("Something is already running. Wait for it to finish (see the Dashboard).", "warn")
        return False
    logs = DATA_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"web-{datetime.now():%Y%m%d-%H%M%S}.log"
    cmd = [sys.executable, str(ROOT / "orchestrator.py"), "--trigger", "web", *args]
    with open(log, "w", encoding="utf-8") as lf:
        lf.write(f"{label}\n$ {shlex.join(cmd)}\n\n")
        lf.flush()
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True,
                                env={**os.environ, "PYTHONUNBUFFERED": "1"})
    jwrite(TASK, {"pid": proc.pid, "label": label, "log": str(log),
                  "started": datetime.now().isoformat(timespec="seconds")})
    flash(f"Started: {label}. Progress shows on the Dashboard.", "ok")
    return True


# --------------------------------------------------------------------------- #
# Settings (profiles/<name>/search.toml)
# --------------------------------------------------------------------------- #

def search_path() -> Path:
    return profile_dir() / "search.toml"


def read_search() -> dict:
    import tomllib
    p = search_path()
    base = {"roles": [], "locations": [], "country": "India", "sites": ["indeed", "naukri", "linkedin"],
            "scan_company_portals": True, "title_match": "broad", "max_age_days": 30, "full_refresh_every_days": 7,
            "schedule": {"enabled": False, "time": "06:30", "days": list(scheduler.DAYS)}, "notify": {"ntfy_topic": ""}}
    if p.exists():
        with open(p, "rb") as f:
            data = tomllib.load(f)
        base.update({k: v for k, v in data.items() if k not in ("schedule", "notify")})
        base["schedule"].update(data.get("schedule", {}))
        base["schedule"]["days"] = scheduler.clean_days(base["schedule"].get("days"))
        base["notify"].update(data.get("notify", {}))
    return base


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)        # TOML basic strings match JSON escaping


def write_search(d: dict) -> None:
    lines = ["# What the daily run searches for. Edited by the control centre's Settings page.", ""]
    for k in ("roles", "locations", "country", "sites", "scan_company_portals", "title_match", "max_age_days",
              "full_refresh_every_days"):
        lines.append(f"{k} = {_toml_value(d[k])}")
    for table in ("schedule", "notify"):
        lines += ["", f"[{table}]"] + [f"{k} = {_toml_value(v)}" for k, v in d[table].items()]
    search_path().write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #

@app.before_request
def require_login():
    if not PASSCODE or request.endpoint in ("login", "static"):
        return None
    if session.get("ok"):
        return None
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("passcode", ""), PASSCODE):
            session["ok"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Wrong passcode.", "warn")
    return render_template("login.html")


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

@app.context_processor
def counts():
    jobs = jobs_view()
    c = {"approved": 0, "review": 0, "archived": 0}
    for j in jobs:
        if j["status"] == "approved_for_phase4":
            c["approved"] += 1
        elif j["status"] in ("needs_review", "analysis_failed"):
            c["review"] += 1
        else:
            c["archived"] += 1
    c["leads"] = len(jread(LEADS, []))
    c["applications"] = sum(1 for r in read_ledger().values() if r.get("status") not in ("", "resume_ready", "archived"))
    from modules.settings import CFG
    return {"counts": c, "now": datetime.now(), "passcode_on": bool(PASSCODE), "profile_name": CFG["pipeline"]["profile"]}


SOURCE_NAMES = {"workday": "Company portals", "greenhouse": "Company portals", "lever": "Company portals",
                "ashby": "Company portals", "smartrecruiters": "Company portals", "successfactors": "Company portals",
                "linkedin": "LinkedIn", "indeed": "Indeed", "naukri": "Naukri", "pasted": "Pasted by you"}


def dashboard_charts() -> dict:
    from collections import Counter
    jobs = jobs_view()
    ledger = read_ledger().values()
    approved = [j for j in jobs if j["status"] == "approved_for_phase4"]
    ever_applied = {"applied", "interview", "offer", "rejected"}
    stages = [("Found", len(jobs) + len(jread(LEADS, []))),
              ("Approved", len(approved)),
              ("Resume ready", sum(1 for r in ledger if r.get("resume_pdf") or r.get("resume_docx"))),
              ("Applied", sum(1 for r in ledger if r.get("status") in ever_applied)),
              ("Interview", sum(1 for r in ledger if r.get("status") in ("interview", "offer"))),
              ("Offer", sum(1 for r in ledger if r.get("status") == "offer"))]
    scores = [j["score"] for j in jobs if j["reqs"]]
    src = Counter(SOURCE_NAMES.get(j["id"].split(":", 1)[0], j["id"].split(":", 1)[0].title()) for j in jobs)
    comp = Counter(j["job"]["company"] for j in approved)
    history = jread(DATA_DIR / "run_history.json", [])
    if not history and jread(LAST_RUN, {}):
        last = jread(LAST_RUN, {})
        history = [{"finished": last.get("finished", ""), "jobs_new": last.get("jobs_new") or 0,
                    "approved_new": len(last.get("new_approved", [])), "ok": True}]
    return {"funnel": charts.funnel(stages), "scores": charts.histogram(scores),
            "sources": charts.bars(src.most_common(), "var(--ice)", "Jobs by source"),
            "companies": charts.bars(comp.most_common(6), "var(--ok)", "Approved jobs by company"),
            "runs": charts.runs(history)}


def _ran(info: dict) -> dict:
    """'Ran 18 Oct, 06:30 and took 23 min' for the last orchestrated run."""
    try:
        a, b = datetime.fromisoformat(info["started"]), datetime.fromisoformat(info["finished"])
        return {"when": a.strftime("%d %b, %H:%M"), "minutes": max(0, round((b - a).total_seconds() / 60))}
    except (KeyError, ValueError):
        return {}


@app.route("/")
def dashboard():
    last = jread(LAST_RUN, {})
    health = jread(doctor.RESULT, {})
    issues = [c for c in health.get("checks", []) if c["status"] != "ok"]
    return render_template("dashboard.html", last=last, ran=_ran(last), task=task_state(), search=read_search(),
                           ch=dashboard_charts(), health=health, issues=issues, next_run=next_run(),
                           days_text=scheduler.days_label(read_search()["schedule"].get("days")))


@app.route("/doctor", methods=["GET", "POST"])
def doctor_page():
    if request.method == "POST":
        doctor.run(model=request.form.get("model") == "1")
        return redirect(url_for("doctor_page"))
    res = jread(doctor.RESULT, {})
    if not res:
        doctor.run()
        res = jread(doctor.RESULT, {})
    return render_template("doctor.html", res=res)


@app.route("/task")
def task():
    return jsonify(task_state())


@app.post("/run")
def run():
    mode = request.form.get("mode", "topup")
    start_task("Full refresh run" if mode == "fresh" else "Run now (top-up)", ["--fresh"] if mode == "fresh" else [])
    return redirect(url_for("dashboard"))


@app.post("/stop")
def stop():
    t = task_state()
    if t.get("running"):
        try:
            pid = int(jread(RUN_STATUS, {}).get("pid") or t.get("pid"))
            os.killpg(os.getpgid(pid), signal.SIGINT)   # like pressing Control+C: finished work is saved
            flash("Stopping... finished work is kept.", "ok")
        except OSError:
            pass
    return redirect(url_for("dashboard"))


@app.route("/approved")
def approved():
    return render_template("jobs.html", title="Approved", kind="approved",
                           jobs=[j for j in jobs_view() if j["status"] == "approved_for_phase4"])


@app.route("/review")
def review():
    return render_template("jobs.html", title="Needs review", kind="review",
                           jobs=[j for j in jobs_view() if j["status"] in ("needs_review", "analysis_failed")])


@app.route("/archived")
def archived():
    return render_template("jobs.html", title="Archived", kind="archived",
                           jobs=[j for j in jobs_view() if j["status"] in ("archived_below_threshold", "user_archived")])


@app.post("/job/<key>/<action>")
def job_action(key: str, action: str):
    j = by_key(key)
    back = request.form.get("back") or url_for("dashboard")
    if action == "approve":
        set_decision(j["id"], "approved")
        if j["has_draft"] and j["pdf"]:
            flash("Approved. Its resume already exists.", "ok")
        else:
            start_task(f"Draft + resume: {j['job']['title']} @ {j['job']['company']}",
                       ["--steps", "model,draft,build", "--job-id", j["id"]])
    elif action == "archive":
        set_decision(j["id"], "archived")
        set_ledger(j["job"], status="archived")
        flash("Archived. It will not come back in future runs.", "ok")
    elif action == "restore":
        set_decision(j["id"], None)
        rows = read_ledger()
        if rows.get(j["id"], {}).get("status") == "archived":
            set_ledger(j["job"], status="resume_ready" if j["pdf"] else "")
        flash("Restored.", "ok")
    elif action == "applied":
        set_ledger(j["job"], status="applied", applied_on=date.today().isoformat())
        flash("Marked as applied today.", "ok")
    elif action == "rebuild":
        start_task(f"Redraft + resume: {j['job']['title']}", ["--steps", "model,draft,build", "--job-id", j["id"]])
    return redirect(back)


@app.route("/draft/<key>")
def draft(key: str):
    j = by_key(key)
    d = drafts_by_job().get(j["id"])
    text = d["md"].read_text(encoding="utf-8") if d and d["md"].exists() else "No draft yet."
    return render_template("draft.html", j=j, text=text)


@app.route("/file")
def file():
    rel = request.args.get("p", "")
    path = (ROOT / rel).resolve()
    if not str(path).startswith(str(RESUMES.resolve())) or not path.is_file():
        abort(404)
    return send_file(path, as_attachment=request.args.get("dl") == "1")


@app.route("/leads", methods=["GET", "POST"])
def leads():
    items = jread(LEADS, [])
    if request.method == "POST":
        url_ = request.form.get("url", "")
        desc = request.form.get("description", "").strip()
        lead = next((x for x in items if x.get("url") == url_), None)
        if lead and request.form.get("action") == "dismiss":
            jwrite(LEADS, [x for x in items if x.get("url") != url_])
            flash("Lead removed.", "ok")
            return redirect(url_for("leads"))
        if not lead or len(desc) < 300:
            flash("Paste the full job description (at least a few paragraphs).", "warn")
            return redirect(url_for("leads"))
        pending = jread(PENDING, [])
        norm = re.sub(r"\s+", " ", desc).strip().lower()
        pending.append({"job_id": f"pasted:{hashlib.sha1(url_.encode()).hexdigest()[:12]}",
                        "company": lead.get("company") or "Unknown", "title": lead.get("title", ""),
                        "location": lead.get("location") or "Not specified", "application_url": url_,
                        "raw_description": desc, "content_hash": hashlib.sha256(norm.encode()).hexdigest()})
        jwrite(PENDING, pending)
        jwrite(LEADS, [x for x in items if x.get("url") != url_])
        start_task(f"Analyse pasted job: {lead.get('title')} @ {lead.get('company')}",
                   ["--steps", "model,analyze,draft,build"])
        return redirect(url_for("leads"))
    return render_template("leads.html", leads=items)


@app.route("/applications", methods=["GET", "POST"])
def applications():
    rows = read_ledger()
    if request.method == "POST":
        jid = request.form.get("job_id", "")
        if jid in rows:
            rows[jid]["status"] = request.form.get("status", rows[jid].get("status", ""))
            rows[jid]["applied_on"] = request.form.get("applied_on", "").strip()
            rows[jid]["notes"] = request.form.get("notes", "").strip()
            write_ledger(rows)
            flash("Saved.", "ok")
        return redirect(url_for("applications"))
    shown = sorted(rows.values(), key=lambda r: (r.get("status") == "archived", r.get("status") == "resume_ready",
                                                 r.get("applied_on") or "", r.get("company", "")))
    return render_template("applications.html", rows=shown, statuses=STATUSES)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    d = read_search()
    if request.method == "POST":
        f = request.form
        lines = lambda key: [x.strip() for x in f.get(key, "").splitlines() if x.strip()]   # noqa: E731
        d["roles"] = lines("roles") or d["roles"]
        d["locations"] = lines("locations")
        d["country"] = f.get("country", "").strip()
        d["sites"] = [s for s in ("indeed", "naukri", "linkedin") if f.get(f"site_{s}")]
        d["scan_company_portals"] = bool(f.get("portals"))
        d["title_match"] = "strict" if f.get("title_match") == "strict" else "broad"
        d["max_age_days"] = max(1, int(f.get("max_age_days") or 30))
        d["full_refresh_every_days"] = max(1, int(f.get("full_refresh_every_days") or 7))
        d["schedule"]["enabled"] = bool(f.get("schedule_enabled"))
        picked = [x for x in scheduler.DAYS if f.get(f"day_{x}")]
        if d["schedule"]["enabled"] and not picked:
            flash("Pick at least one day for the morning run; all days were kept.", "warn")
        d["schedule"]["days"] = picked or list(scheduler.DAYS)
        t = f.get("schedule_time", "06:30")
        d["schedule"]["time"] = t if re.fullmatch(r"\d{2}:\d{2}", t) else "06:30"
        d["notify"]["ntfy_topic"] = re.sub(r"[^A-Za-z0-9_-]", "", f.get("ntfy_topic", ""))
        write_search(d)
        ok, msg = scheduler.apply_schedule()
        flash(f"Settings saved. {msg}" if ok else msg, "ok" if ok else "warn")
        return redirect(url_for("settings"))
    st = scheduler.status()
    return render_template("settings.html", s=d, st=st, urls=phone_urls(),
                           days_text=scheduler.days_label(st["schedule"].get("days")))


def phone_urls() -> list[str]:
    host = socket.gethostname().split(".")[0]
    urls = [f"http://{host}.local:{PORT}"]
    try:  # this Mac's address on your home network (nothing is sent anywhere)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.168.0.1", 9))
            urls.append(f"http://{sk.getsockname()[0]}:{PORT}")
    except OSError:
        pass
    return urls


@app.post("/settings/background")
def settings_background():
    ok, msg = scheduler.set_background(request.form.get("on") == "1")
    flash(msg, "ok" if ok else "warn")
    return redirect(url_for("settings"))


@app.post("/settings/passcode")
def settings_passcode():
    ok, msg = scheduler.set_passcode(request.form.get("passcode", "") if request.form.get("action") == "set" else "")
    if ok:
        if scheduler.is_loaded(scheduler.WEB_LABEL):
            import threading
            threading.Timer(1.5, scheduler.restart_background).start()   # after this page has been sent
            msg += " The control centre restarts in a moment; reload this page in 10 seconds."
        else:
            msg += " Restart the control centre (Control+C in its Terminal window, then start it again) to apply it."
    flash(msg, "ok" if ok else "warn")
    return redirect(url_for("settings"))


@app.post("/settings/test-notification")
def settings_test_notification():
    topic = read_search()["notify"].get("ntfy_topic", "").strip()
    if not topic:
        flash("Save an ntfy topic first.", "warn")
        return redirect(url_for("settings"))
    import urllib.request
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data="Test from your job pipeline".encode(),
                                     headers={"Title": "Job pipeline"})
        urllib.request.urlopen(req, timeout=15)
        flash("Test notification sent. It should appear on your iPhone within a few seconds.", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Could not send: {e}", "warn")
    return redirect(url_for("settings"))


# --------------------------------------------------------------------------- #
# Companies
# --------------------------------------------------------------------------- #

def _split(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n]", text or "") if x.strip()]


def _test_link(name: str, link: dict) -> None:
    s = read_search()
    role = (s.get("roles") or ["Engineer"])[0]
    loc = ", ".join(s.get("locations") or [])
    res = comp.test_portal(name, link["portal"], role, loc, s.get("country", ""))
    link["test"] = {"at": comp.stamp(), **res}


@app.route("/companies")
def companies():
    from collections import Counter
    from modules import scraper as sc
    items = comp.load()
    added_keys = set(comp.registry_entries())
    builtin = sorted(k.title() for k in sc.load_registry() if k not in added_keys)
    profile_watch = []
    try:
        import tomllib
        with open(profile_dir() / "domain.toml", "rb") as f:
            profile_watch = [w["name"] for w in tomllib.load(f).get("watch_companies", [])]
    except (OSError, ValueError):
        pass
    tally = Counter(l.get("name") or l.get("platform") for c in items for l in c.get("links", [])
                    if not l.get("supported") and l.get("platform") not in ("unknown", None))
    return render_template("companies.html", items=items, builtin=builtin, profile_watch=profile_watch,
                           tally=tally.most_common(), skipped=sorted(sc.SKIP_PORTALS))


@app.post("/companies/add")
def companies_add():
    f = request.form
    name, url_ = f.get("name", "").strip(), f.get("url", "").strip()
    if not name:
        flash("Type the company's name.", "warn")
        return redirect(url_for("companies"))
    items = comp.load()
    c = comp.find(items, name)
    if not c:
        c = {"name": name, "aliases": [], "watch": bool(f.get("watch")), "links": [], "added": comp.stamp()}
        items.append(c)
    c["aliases"] = list(dict.fromkeys([*c.get("aliases", []), *_split(f.get("aliases", ""))]))
    if url_:
        if any(l["url"] == url_ for l in c["links"]):
            flash("That link is already saved for this company.", "warn")
        else:
            info = comp.detect(url_)
            link = {"url": url_, "platform": info["platform"], "name": info["name"], "supported": info["supported"],
                    "portal": info["portal"], "added": comp.stamp()}
            if info["supported"]:
                _test_link(name, link)
            c["links"].append(link)
            flash(f"{name}: {info['note']}" + (f" {link['test']['message']}" if link.get("test") else ""),
                  "ok" if info["supported"] else "warn")
    else:
        flash(f"{name} added to your watch list (searched on LinkedIn, Indeed and Naukri).", "ok")
    comp.save(items)
    return redirect(url_for("companies"))


@app.post("/companies/<int:i>/<action>")
def companies_action(i: int, action: str):
    items = comp.load()
    if not 0 <= i < len(items):
        abort(404)
    c = items[i]
    li = request.form.get("link", type=int)
    if action == "remove":
        items.pop(i)
        flash(f"{c['name']} removed.", "ok")
    elif action == "remove-link" and li is not None and 0 <= li < len(c["links"]):
        c["links"].pop(li)
        flash("Link removed.", "ok")
    elif action == "test" and li is not None and 0 <= li < len(c["links"]) and c["links"][li].get("supported"):
        _test_link(c["name"], c["links"][li])
        flash(f"{c['name']}: {c['links'][li]['test']['message']}", "ok" if c["links"][li]["test"]["ok"] else "warn")
    elif action == "update":
        c["aliases"] = _split(request.form.get("aliases", ""))
        c["watch"] = bool(request.form.get("watch"))
        flash("Saved.", "ok")
    comp.save(items)
    return redirect(url_for("companies"))


# --------------------------------------------------------------------------- #
# Guided setup (new profile from your resumes)
# --------------------------------------------------------------------------- #

def _bg_extract(slug: str) -> None:
    """Background: read every uploaded resume, ask the local model for a proposal, verify and merge."""
    import threading

    files = sorted((ob.folder(slug) / "source_resumes").glob("*"))
    st = ob.load_state(slug)
    st["reading"] = {"status": "running", "done": 0, "total": len(files), "error": "", "log": []}
    ob.save_state(slug, st)              # marked as running before the page reloads

    def work():
        try:
            from orchestrator import ensure_model
            from modules.local_analyzer import make_llm
            if not ensure_model():
                raise RuntimeError("the local model is not available (open LM Studio and load your model)")
            call = make_llm("")
            props = []
            for f in files:
                st = ob.load_state(slug)
                st["reading"]["log"].append(f"Reading {f.name} ...")
                ob.save_state(slug, st)
                text = ob.read_text(f)
                props.append((ob.propose(text, call), text, f.name))
                st = ob.load_state(slug)
                st["reading"]["done"] += 1
                ob.save_state(slug, st)
            st = ob.load_state(slug)
            st["proposal"] = ob.merge(props)
            st["reading"]["status"] = "done"
            st["step"] = 3
        except Exception as e:  # noqa: BLE001
            st = ob.load_state(slug)
            st["reading"].update(status="error", error=f"{type(e).__name__}: {str(e)[:300]}")
        ob.save_state(slug, st)
    threading.Thread(target=work, daemon=True).start()


@app.route("/setup")
def setup_home():
    return render_template("setup.html", step=0, drafts=ob.in_progress(), st={}, slug="")


@app.post("/setup/start")
def setup_start():
    f = request.form
    name = f.get("name", "").strip()
    slug = ob.slugify(f.get("slug") or name.split()[0] if name else "")
    if not name or not slug:
        flash("Type your name to start.", "warn")
        return redirect(url_for("setup_home"))
    if (ob.folder(slug) / "fact_bank.json").exists() and not ob.state_path(slug).exists():
        flash(f"A profile called '{slug}' already exists. Choose another short name.", "warn")
        return redirect(url_for("setup_home"))
    st = ob.load_state(slug)
    st["basics"] = {k: f.get(k, "").strip() for k in ("name", "email", "phone", "location", "linkedin")}
    st["step"] = max(2, st.get("step", 2))
    ob.save_state(slug, st)
    return redirect(url_for("setup_step", slug=slug))


@app.route("/setup/<slug>")
def setup_step(slug: str):
    st = ob.load_state(slug)
    if not st:
        abort(404)
    step = int(request.args.get("step") or st.get("step", 2))
    needs = {3: "proposal", 4: "confirmed", 5: "variants_form"}
    while step in needs and not st.get(needs[step]):   # never show a step whose input is not ready yet
        step -= 1
    step = min(step, max(st.get("step", 2), 2)) if step == 6 and st.get("step") != 6 else step
    files = sorted(p.name for p in (ob.folder(slug) / "source_resumes").glob("*"))
    return render_template("setup.html", step=step, st=st, slug=slug, files=files, levels=list(ob.LEVELS),
                           sections=ob.SECTIONS, packs=sorted(p.stem for p in ob.PACKS_DIR.glob("*.toml")))


@app.get("/setup/<slug>/status")
def setup_status(slug: str):
    return jsonify(ob.load_state(slug).get("reading", {}))


@app.post("/setup/<slug>/upload")
def setup_upload(slug: str):
    dest = ob.folder(slug) / "source_resumes"
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in request.files.getlist("resumes"):
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(f.filename or "").name)
        if name and Path(name).suffix.lower() in (".docx", ".pdf", ".txt"):
            f.save(dest / name)
            n += 1
    flash(f"{n} resume(s) added." if n else "Choose .docx, .pdf or .txt files.", "ok" if n else "warn")
    return redirect(url_for("setup_step", slug=slug, step=2))


@app.post("/setup/<slug>/remove-file")
def setup_remove_file(slug: str):
    (ob.folder(slug) / "source_resumes" / Path(request.form.get("file", "")).name).unlink(missing_ok=True)
    return redirect(url_for("setup_step", slug=slug, step=2))


@app.post("/setup/<slug>/read")
def setup_read(slug: str):
    if not list((ob.folder(slug) / "source_resumes").glob("*")):
        flash("Add at least one resume first.", "warn")
    elif ob.load_state(slug).get("reading", {}).get("status") == "running":
        flash("Already reading your resumes.", "warn")
    else:
        _bg_extract(slug)
    return redirect(url_for("setup_step", slug=slug, step=2))


@app.post("/setup/<slug>/review")
def setup_review(slug: str):
    st, f = ob.load_state(slug), request.form
    prop = st.get("proposal", {})
    roles = []
    for i, r in enumerate(prop.get("roles", [])):
        if not f.get(f"keep_role_{i}"):
            continue
        bullets = [f.get(f"b_{i}_{k}_text", "").strip() for k in range(len(r["bullets"])) if f.get(f"b_{i}_{k}_keep")]
        bullets += [x.strip(" -•\t") for x in f.get(f"new_{i}", "").splitlines() if x.strip()]
        roles.append({"employer": f.get(f"emp_{i}", "").strip(), "title": f.get(f"title_{i}", "").strip(),
                      "location": f.get(f"loc_{i}", "").strip(), "start": f.get(f"start_{i}", "").strip(),
                      "end": f.get(f"end_{i}", "").strip(), "context": f.get(f"ctx_{i}", "").strip(),
                      "bullets": [b for b in bullets if b]})
    skills = [{"text": f.get(f"s_{j}_text", "").strip(), "level": f.get(f"s_{j}_level", "expert"),
               "section": f.get(f"s_{j}_section", "tools")}
              for j in range(len(prop.get("skills", []))) if f.get(f"s_{j}_keep") and f.get(f"s_{j}_text", "").strip()]
    skills += [{"text": x.strip(), "level": "expert", "section": "tools"} for x in f.get("new_skills", "").splitlines() if x.strip()]
    edu = [{"degree": f.get(f"e_{k}_degree", "").strip(), "institution": f.get(f"e_{k}_inst", "").strip(),
            "year": f.get(f"e_{k}_year", "").strip()} for k in range(len(prop.get("education", []))) if f.get(f"e_{k}_keep")]
    if f.get("new_degree", "").strip():
        edu.append({"degree": f["new_degree"].strip(), "institution": f.get("new_inst", "").strip(), "year": f.get("new_year", "").strip()})
    certs = [{"name": f.get(f"c_{k}_name", "").strip(), "issuer": f.get(f"c_{k}_issuer", "").strip(),
              "status": f.get(f"c_{k}_status", "completed")} for k in range(len(prop.get("certifications", []))) if f.get(f"c_{k}_keep")]
    adds = [f.get(f"a_{k}_text", "").strip() for k in range(len(prop.get("additional", []))) if f.get(f"a_{k}_keep")]
    adds += [x.strip() for x in f.get("new_additional", "").splitlines() if x.strip()]
    st["confirmed"] = {"roles": roles, "skills": skills, "education": edu, "certifications": certs,
                       "languages": [x.strip() for x in f.get("languages", "").splitlines() if x.strip()],
                       "additional": [a for a in adds if a], "final_year_project": f.get("final_year_project", "").strip()}
    if not any(r["bullets"] for r in roles):
        flash("Keep at least one role with at least one bullet.", "warn")
        ob.save_state(slug, st)
        return redirect(url_for("setup_step", slug=slug, step=3))
    st["step"] = 4
    ob.save_state(slug, st)
    return redirect(url_for("setup_step", slug=slug))


@app.post("/setup/<slug>/variants")
def setup_variants(slug: str):
    st, f = ob.load_state(slug), request.form
    n_roles = len(st.get("confirmed", {}).get("roles", []))
    variants = []
    for v in (1, 2):
        label = f.get(f"v{v}_label", "").strip()
        if not label:
            continue
        picked = [int(x) for x in f.getlist(f"v{v}_roles") if x.isdigit() and int(x) < n_roles]
        variants.append({"label": label, "titles": f.get(f"v{v}_titles", ""), "headline": f.get(f"v{v}_headline", "").strip(),
                         "summary": f.get(f"v{v}_summary", "").strip(), "role_index": picked})
    if not variants:
        flash("Give your first resume variant a name, e.g. 'Mechanical Design Engineer'.", "warn")
        return redirect(url_for("setup_step", slug=slug, step=4))
    st["variants_form"] = variants
    st["pack"] = f.get("pack", "general")
    st["step"] = 5
    ob.save_state(slug, st)
    return redirect(url_for("setup_step", slug=slug))


@app.post("/setup/<slug>/finish")
def setup_finish(slug: str):
    st, f = ob.load_state(slug), request.form
    st["search"] = {"roles": [x.strip() for x in f.get("roles", "").splitlines() if x.strip()],
                    "locations": [x.strip() for x in f.get("locations", "").splitlines() if x.strip()],
                    "country": f.get("country", "").strip()}
    tpl = None
    up = request.files.get("template")
    if up and up.filename and up.filename.lower().endswith(".docx"):
        tpl = ob.folder(slug) / "source_resumes" / "_layout_template.docx"
        tpl.parent.mkdir(parents=True, exist_ok=True)
        up.save(tpl)
    # role indexes -> role ids (ids are created by the builder, in the same order)
    fb_preview, _, _ = ob.build_pack({**st, "variants": [{**v, "roles": []} for v in st["variants_form"]]})
    ids = [r["role_id"] for r in fb_preview["roles"]]
    st["variants"] = [{**v, "roles": [ids[i] for i in v["role_index"] if i < len(ids)]} for v in st["variants_form"]]
    problems = ob.write_pack(slug, st, tpl)
    if tpl:
        tpl.unlink(missing_ok=True)
    if problems:
        st["step"] = 5
        ob.save_state(slug, st)
        flash("Not saved yet: " + " ".join(problems)[:600], "warn")
        return redirect(url_for("setup_step", slug=slug))
    st["step"] = 6
    ob.save_state(slug, st)
    return redirect(url_for("setup_step", slug=slug))


@app.post("/setup/<slug>/use")
def setup_use(slug: str):
    from modules import settings as cfg
    ob.use_profile(slug)
    cfg.CFG["pipeline"]["profile"] = slug          # this control centre switches straight away
    ob.state_path(slug).unlink(missing_ok=True)    # setup finished
    flash(f"Profile '{slug}' is now active. Press Run now on the Dashboard to search with it.", "ok")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------- #

def main() -> None:
    host = "0.0.0.0" if PASSCODE else "127.0.0.1"
    print(f"\nControl centre running.\n  On this Mac:   http://localhost:{PORT}")
    if PASSCODE:
        name = socket.gethostname().split(".")[0]
        print(f"  On your iPhone (same Wi-Fi):  http://{name}.local:{PORT}   (passcode required)")
    else:
        print("  iPhone access is off: add WEBAPP_PASSCODE=<your passcode> to .env to turn it on.")
    print("Press Control+C to stop.\n")
    import threading
    threading.Thread(target=_catch_up_loop, daemon=True).start()   # runs a missed morning run after sleep
    app.run(host=host, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
