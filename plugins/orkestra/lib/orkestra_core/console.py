"""The dashboard's console: what a person can do from the page besides Pause / Abort (monitor.control_run).

Everything that drives an agent (start a run, answer it, approve its plan, continue it, cancel its job) goes to the
run service, the bridge (`bridge/orkestra_bridge.py`), over its local HTTP API. Its job supervisor (process groups, turn
budgets, auto-resume, notifications, recovery after a restart) stays the one place that starts agents, so two drivers
can never race for one run. The bridge token is read here, on the server, from ~/.orkestra/bridge.secret.json and never
reaches the browser. Everything else is read from files (run documents, the bridge's job records) or done through the
library functions the CLI itself uses (`clear`, `doctor`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import i18n, paths
from .util import read_json, read_text

SERVICE_PORT = 47615                       # the bridge's default (bridge.secret.json `port` wins)
SERVICE_TIMEOUT = 20.0                     # a start may wait for Cursor to hand out a chat id
HEALTH_TIMEOUT = 1.5
SERVICE_BOOT_SECONDS = 8.0
REQUEST_MIN, REQUEST_MAX = 3, 8000
REPLY_MAX = 8000
ENGINES = ("claude", "cursor", "copilot", "pi")
APPROVAL_MODES = ("autopilot", "manual")
JOB_OPEN = ("queued", "running", "awaiting_user", "blocked", "paused", "stalled")
JOB_REPLY = ("awaiting_user", "blocked", "paused", "stalled")      # what the bridge's reply_job accepts
JOB_TURNS_SHOWN = 8
JOB_MESSAGE_MAX = 6000
DOCS = (("request", "request.md"), ("context", "context.md"), ("spec", "spec.md"), ("plan", "plan.md"),
        ("verification", "verification.md"), ("report", "report.md"))
DOC_MAX = 256 * 1024
DOCTOR_TIMEOUT = 120.0
CLEAR_HOURS_MIN, CLEAR_HOURS_MAX = 1.0, 24.0 * 365
_PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_JOB_RE = re.compile(r"^[a-f0-9]{8,32}$")
_AGENT_ENV = ("CLAUDECODE", "CURSOR_AGENT", "CURSOR_CONVERSATION_ID", "CURSOR_INVOKED_AS", "ORKESTRA_HEADLESS")


class ConsoleError(Exception):
    """A request the console cannot carry out. `code` is language-free (the page words it); `detail` is the service's
    own text when it gave one (already in the request's language)."""

    def __init__(self, code: str, detail: str = "", status: int = 400):
        super().__init__(code)
        self.code, self.detail, self.status = code, detail, status

    def answer(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ok": False, "error": self.code}
        if self.detail:
            out["detail"] = self.detail[:400]
        return out


# ---------------------------------------------------------------- the run service (bridge)

def bridge_script() -> Optional[Path]:
    """bridge/orkestra_bridge.py of this checkout; None in a packaged install that ships the plugin only."""
    path = Path(__file__).resolve().parents[4] / "bridge" / "orkestra_bridge.py"
    return path if path.is_file() else None


def _secret_path() -> Path:
    return paths.orkestra_home() / "bridge.secret.json"


def _secret() -> Dict[str, Any]:
    data = read_json(_secret_path())
    return data if isinstance(data, dict) else {}


def _port(cfg: Optional[Dict[str, Any]] = None) -> int:
    try:
        port = int((cfg if cfg is not None else _secret()).get("port") or SERVICE_PORT)
    except (TypeError, ValueError):
        port = SERVICE_PORT
    return port if 0 < port < 65536 else SERVICE_PORT


# urllib would send a 127.0.0.1 request through HTTP(S)_PROXY when one is set: never for the local service
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def service_status() -> Dict[str, Any]:
    """{installed, configured, running, port}: can the page start agents right now, and if not, why."""
    cfg = _secret()
    port = _port(cfg)
    out = {"installed": bridge_script() is not None, "configured": bool(cfg.get("token")), "running": False, "port": port}
    if not out["configured"]:
        return out          # whatever answers on that port belongs to another setup: without our token it is not ours
    try:
        with _OPENER.open(urllib.request.Request(f"http://127.0.0.1:{port}/health"), timeout=HEALTH_TIMEOUT) as resp:
            body = json.loads(resp.read() or b"{}")
            out["running"] = bool(isinstance(body, dict) and body.get("ok"))
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return out


def _call(method: str, path: str, body: Optional[Dict[str, Any]] = None, lang: str = "",
          timeout: float = SERVICE_TIMEOUT) -> Dict[str, Any]:
    cfg = _secret()
    token = str(cfg.get("token") or "")
    if not token:
        raise ConsoleError("service_not_configured", status=503)
    port = _port(cfg)
    if lang in ("en", "tr"):
        path += ("&" if "?" in path else "?") + "lang=" + lang
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            said = json.loads(exc.read() or b"{}")
        except ValueError:
            said = {}
        detail = str(said.get("error") or exc.reason or "") if isinstance(said, dict) else ""
        raise ConsoleError("service_refused", detail=detail, status=409 if exc.code == 409 else 400)
    except (OSError, ValueError, urllib.error.URLError):
        raise ConsoleError("service_down", status=503)
    if not isinstance(data, dict):
        raise ConsoleError("service_refused", detail="unexpected answer", status=502)
    return data


def projects(lang: str = "") -> Dict[str, Any]:
    return _call("GET", "/projects", lang=lang)


def engines(project: str = "", lang: str = "") -> Dict[str, Any]:
    if project and not _PROJECT_RE.match(project):
        raise ConsoleError("bad_project")
    return _call("GET", "/engines" + ("?project=" + urllib.parse.quote(project) if project else ""), lang=lang)


def start(project: str, request: str, engine: str = "", approval_mode: str = "autopilot", lang: str = "") -> Dict[str, Any]:
    """Start a run in `project` through the run service. The request is the person's own words."""
    text = str(request or "").strip()
    if not _PROJECT_RE.match(str(project or "")) or project in (".", ".."):
        raise ConsoleError("bad_project")
    if len(text) < REQUEST_MIN:
        raise ConsoleError("request_short")
    if len(text) > REQUEST_MAX:
        raise ConsoleError("request_long")
    if engine and engine not in ENGINES:
        raise ConsoleError("bad_engine")
    if approval_mode not in APPROVAL_MODES:
        raise ConsoleError("bad_mode")
    if approval_mode == "manual":
        # the switch becomes the person's own sentence, in words every bridge version already understands
        # (intent.manual_approval_requested): the request then also tells the agent why it will wait at the plan
        text += "\n\n" + i18n.t("monitor.manual_sentence")
    job = _call("POST", "/jobs", {"project": project, "request": text, "engine": engine, "source": "dashboard"}, lang=lang)
    return {"ok": True, "job": job}


def _job_id(job_id: str) -> str:
    if not _JOB_RE.match(str(job_id or "")):
        raise ConsoleError("bad_job")
    return str(job_id)


def reply(job_id: str, text: str, lang: str = "") -> Dict[str, Any]:
    """Send the person's answer (a reply to a question, an approval, a change request) to a job's agent."""
    words = str(text or "").strip()
    if not words:
        raise ConsoleError("reply_empty")
    if len(words) > REPLY_MAX:
        raise ConsoleError("reply_long")
    return {"ok": True, "job": _call("POST", f"/jobs/{_job_id(job_id)}/reply", {"text": words}, lang=lang)}


def cancel(job_id: str, lang: str = "") -> Dict[str, Any]:
    return {"ok": True, "job": _call("POST", f"/jobs/{_job_id(job_id)}/cancel", {}, lang=lang)}


def continue_run(project: Path, run_id: str, lang: str = "") -> Dict[str, Any]:
    """Continue a run whose job is idle (paused, waiting, stalled): lift the dashboard's stop notes for it (the person
    is the one asking now) and send the engine's resume prompt as the next turn."""
    from . import engines as EN
    from . import state as ST
    job = job_for(project, run_id)
    if not job:
        raise ConsoleError("no_job", status=409)
    if job.get("status") not in JOB_REPLY:
        raise ConsoleError("job_busy" if job.get("status") in JOB_OPEN else "job_closed", status=409)
    ST.lift_stops(project, run_id)
    prompt = EN.resume_prompt(str(job.get("engine") or "claude"), project.name)
    return {"ok": True, "job": _call("POST", f"/jobs/{_job_id(str(job.get('id')))}/reply", {"text": prompt}, lang=lang)}


def start_service() -> Dict[str, Any]:
    """Start the run service in the background (set it up first when it never was). Never a second copy: a running one
    is left alone, and a launchd-managed one answers /health already."""
    status = service_status()
    if status["running"]:
        return dict(status, ok=True, started=False)
    script = bridge_script()
    if script is None:
        raise ConsoleError("service_not_installed", status=503)
    env = {k: v for k, v in os.environ.items() if k not in _AGENT_ENV}
    if not status["configured"]:
        # `init` writes the token file (0600) and prints the token: the output is dropped, the page never sees it
        cp = subprocess.run([sys.executable, str(script), "init"], capture_output=True, text=True, timeout=30, env=env,
                            cwd=str(script.parent))
        if cp.returncode != 0 or not _secret().get("token"):
            raise ConsoleError("service_setup_failed", status=500)
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                          "cwd": str(script.parent), "env": env}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True              # outlives the dashboard (and its re-exec on a code change)
    subprocess.Popen([sys.executable, str(script), "serve"], **kw)
    deadline = time.monotonic() + SERVICE_BOOT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.25)
        status = service_status()
        if status["running"]:
            return dict(status, ok=True, started=True)
    raise ConsoleError("service_start_failed", status=500)


# ---------------------------------------------------------------- the bridge's job records (read-only)

_job_cache: Dict[str, Tuple[int, Dict[str, Any]]] = {}
_job_lock = threading.Lock()


def _jobs_dir() -> Path:
    return paths.orkestra_home() / "bridge" / "jobs"


def all_jobs() -> List[Dict[str, Any]]:
    """Every job record, oldest first. Each file is parsed again only when its mtime changed (a poll reads them all)."""
    d = _jobs_dir()
    out: List[Dict[str, Any]] = []
    seen = set()
    try:
        files = list(d.glob("*.json"))
    except OSError:
        files = []
    with _job_lock:
        for f in files:
            key = str(f)
            seen.add(key)
            try:
                stamp = f.stat().st_mtime_ns
            except OSError:
                continue
            hit = _job_cache.get(key)
            if hit and hit[0] == stamp:
                out.append(hit[1])
                continue
            data = read_json(f)
            if isinstance(data, dict) and _JOB_RE.match(str(data.get("id") or "")):
                _job_cache[key] = (stamp, data)
                out.append(data)
        for key in [k for k in _job_cache if k not in seen]:
            del _job_cache[key]
    return sorted(out, key=lambda j: str(j.get("created") or ""))


def job_for(project: Path, run_id: str) -> Optional[Dict[str, Any]]:
    """The newest job that drives this run (matched by project path and run id)."""
    want = str(project)
    found = None
    for job in all_jobs():
        if str(job.get("run_id") or "") == run_id and str(job.get("project_path") or "") == want:
            found = job
    return found


def job_view(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What the page shows of a job: status, the agent's last message, the person's recent prompts, and what can be done."""
    if not job:
        return None
    status = str(job.get("status") or "")
    turns = [{"at": str(t.get("at") or ""), "prompt": str(t.get("prompt") or "")[:500],
              "exit": t.get("exit"), "interrupted": str(t.get("interrupted") or "")[:200]}
             for t in (job.get("turns") or []) if isinstance(t, dict)][-JOB_TURNS_SHOWN:]
    return {"id": str(job.get("id")), "status": status, "engine": str(job.get("engine") or "claude"),
            "source": str(job.get("source") or ""), "created": str(job.get("created") or ""),
            "updated": str(job.get("updated") or ""), "message": str(job.get("last_message") or "")[:JOB_MESSAGE_MAX],
            "turns": turns, "approval_mode": str(job.get("approval_mode") or "autopilot"),
            "can_reply": status in JOB_REPLY, "can_cancel": status in JOB_OPEN, "project": str(job.get("project") or ""),
            "run_id": str(job.get("run_id") or "")}


def jobs(limit: int = 30) -> List[Dict[str, Any]]:
    """The newest jobs first, as the page shows them (the Tools panel lists the ones no run card carries)."""
    return [v for v in (job_view(j) for j in reversed(all_jobs()[-limit:])) if v]


# ---------------------------------------------------------------- run documents (read-only)

def docs_of(project: Path, run_id: str) -> List[str]:
    """The run documents that exist, in reading order (request, context, spec, plan, verification, report)."""
    fdir = paths.feature_dir(project, run_id)
    out = []
    for name, filename in DOCS:
        try:
            if (fdir / filename).is_file():
                out.append(name)
        except OSError:
            continue
    return out


def doc(project: Path, run_id: str, name: str) -> Dict[str, Any]:
    files = dict(DOCS)
    if name not in files:
        raise ConsoleError("bad_doc")
    path = paths.feature_dir(project, run_id) / files[name]
    if not path.is_file():
        raise ConsoleError("no_doc", status=404)
    text = read_text(path)
    cut = len(text.encode("utf-8")) > DOC_MAX
    if cut:
        text = text.encode("utf-8")[:DOC_MAX].decode("utf-8", "ignore")
    return {"ok": True, "name": name, "text": text, "truncated": cut, "path": paths.display_path(path)}


# ---------------------------------------------------------------- maintenance (what `orkestra clear` / `doctor` do)

def clear_runs(hours: Any, apply: bool) -> Dict[str, Any]:
    """`orkestra clear` with a preview: which open runs nothing touched for `hours` would be closed, and which registry
    entries point at nothing. `apply` closes them through the normal status path (ledger + activity row)."""
    from . import clear as C
    try:
        h = float(hours)
    except (TypeError, ValueError):
        raise ConsoleError("bad_hours")
    if not CLEAR_HOURS_MIN <= h <= CLEAR_HOURS_MAX:
        raise ConsoleError("bad_hours")
    res = C.clear(older_than_hours=h, dry_run=not apply)

    def row(r: Dict[str, Any]) -> Dict[str, Any]:
        return {"project": Path(str(r.get("project") or "")).name, "run_id": str(r.get("run_id") or ""),
                "title": str(r.get("title") or ""), "status": str(r.get("status") or ""), "idle": int(r.get("idle") or 0),
                "why": str(r.get("why") or "")}
    return {"ok": True, "applied": bool(apply), "hours": h, "closed": [row(r) for r in res.get("closed") or []],
            "kept": [row(r) for r in res.get("kept") or []], "dropped": len(res.get("dropped") or []),
            "errors": [str(e.get("target") or "") + ": " + str(e.get("error") or "") for e in res.get("errors") or []]}


def doctor(lang: str = "") -> Dict[str, Any]:
    """`orkestra doctor`, as its text: every check with OK / !! (read-only)."""
    bin_path = Path(__file__).resolve().parents[2] / "bin" / "orkestra"
    env = {k: v for k, v in os.environ.items() if k not in _AGENT_ENV}
    if lang in ("en", "tr"):
        env["ORKESTRA_LANG"] = lang
    try:
        cp = subprocess.run([sys.executable, str(bin_path), "doctor"], capture_output=True, encoding="utf-8",
                            errors="replace", timeout=DOCTOR_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ConsoleError("doctor_timeout", status=504)
    text = "\n".join(line for line in (cp.stdout + ("\n" + cp.stderr if cp.stderr.strip() else "")).splitlines()
                     if not line.startswith("[orkestra:"))
    return {"ok": True, "passed": cp.returncode == 0, "text": text.strip()[:64 * 1024]}
