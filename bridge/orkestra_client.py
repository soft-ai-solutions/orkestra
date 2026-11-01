#!/usr/bin/env python3
"""Tiny client for the orkestra bridge — for OpenClaw tools, shell scripts, cron, iOS Shortcuts over SSH ...

    orkestra_client.py start <project> <request...> [--chat-id ID] [--source openclaw]
    orkestra_client.py reply <job-id> <text...>
    orkestra_client.py reply --chat-id ID <text...>        # the job of that chat waiting for an answer
    orkestra_client.py status [<job-id>] [--chat-id ID]
    orkestra_client.py wait <job-id> [--timeout 1800]       # block until the job stops running, print its message
    orkestra_client.py cancel --chat-id ID                  # stop the latest open job of that chat
    orkestra_client.py runs [--json]                        # live view: every open run, its phase, what it does now
    orkestra_client.py projects                             # project folders the bridge may start (allowlist applied)
    orkestra_client.py start ... --owner                    # owner command (the OpenClaw plugin checked the passphrase)
    orkestra_client.py ensure                               # start the bridge when it is not running
    orkestra_client.py revive [--app NAME]...               # bring back the bridge, the dashboard and the IDE apps

Reads the bearer token from ~/.orkestra/bridge.secret.json (same machine). Prints JSON (or the message for `wait`).
Exit codes: 0 ok · 1 bridge/HTTP error · 2 usage.
Text meant for a person (usage, "bridge not reachable") follows orkestra_core.i18n: English by default, Turkish when
ORKESTRA_LANG / ~/.orkestra/config.json / the OS locale say so. The JSON the bridge returns is language-neutral.
"""
from __future__ import annotations

import errno
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "plugins" / "orkestra" / "lib"))
from orkestra_core.i18n import t  # noqa: E402
HOME = Path(os.environ.get("ORKESTRA_HOME") or "~/.orkestra").expanduser()


def _cfg() -> Dict[str, Any]:
    try:
        return json.loads((HOME / "bridge.secret.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(t("bridge.client.not_configured", script=HERE / "orkestra_bridge.py"))


# A connection that never reached the bridge is safe to try again, whatever the method: the request was not sent.
# Seen in the wild: EADDRNOTAVAIL (no free local port for a moment on a busy Mac) and ECONNREFUSED while the bridge
# restarts. A request that reached the bridge is never repeated (a second POST /jobs would start a second job).
CONNECT_ERRNOS = {errno.ECONNREFUSED, errno.EADDRNOTAVAIL, errno.ENETUNREACH, errno.EHOSTUNREACH}
RETRY_DELAYS = (0.3, 1.0, 2.0)
# urllib would route a 127.0.0.1 request through HTTP(S)_PROXY when one is set in the caller's environment
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _body(raw: bytes) -> Any:
    """The JSON of an answer; a non-JSON answer (a proxy page, a half-written body) becomes an error object."""
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {"error": raw.decode("utf-8", "replace").strip()[:300] or "empty answer"}


def _not_sent(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, OSError) and getattr(reason, "errno", None) in CONNECT_ERRNOS


def _call(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    cfg = _cfg()
    url = f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 47615)}{path}"
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(len(RETRY_DELAYS) + 1):
        req = urllib.request.Request(url, method=method, data=data,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['token']}"})
        try:
            with _OPENER.open(req, timeout=30) as r:
                return r.status, _body(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _body(e.read())
        except (urllib.error.URLError, OSError) as e:
            if _not_sent(e) and attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            reason = getattr(e, "reason", e)
            if isinstance(reason, (socket.timeout, TimeoutError)) or isinstance(e, (socket.timeout, TimeoutError)):
                reason = "timed out after 30 s"
            return 0, {"error": t("bridge.client.unreachable", reason=reason, script=HERE / "orkestra_bridge.py")}
    return 0, {"error": "unreachable"}            # not reached: the loop always returns


# ---------------------------------------------------------------- bringing the tools back (`ensure`, `revive`)
# Started from the OpenClaw gateway (Telegram /ide, an owner command while the bridge is down). Children
# are fully detached (own session, no inherited pipes): the caller's command runner must not wait for them.

LAUNCHD_LABEL = "com.orkestra.bridge"
DASHBOARD_PORT = 47616
ORKESTRA_BIN = HERE.parent / "plugins" / "orkestra" / "bin" / "orkestra"
APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}$")


def _run(argv: List[str], timeout: float = 20) -> int:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _spawn(argv: List[str], log_name: str) -> None:
    logs = HOME / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / log_name, "ab") as out:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                         cwd=str(HERE.parent), close_fds=True)


def _wait_port(host: str, port: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.4)
    return _port_open(host, port)


def ensure_bridge(wait: float = 25.0) -> Dict[str, Any]:
    cfg = _cfg()
    host, port = str(cfg.get("host") or "127.0.0.1"), int(cfg.get("port") or 47615)
    if _port_open(host, port):
        return {"state": "running", "port": port}
    how = "process"
    if sys.platform == "darwin":
        target = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        if _run(["launchctl", "kickstart", target]) == 0:
            how = "launchd"
        elif plist.exists() and _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)]) == 0:
            _run(["launchctl", "kickstart", target])
            how = "launchd"
    if how == "process":
        _spawn([sys.executable, str(HERE / "orkestra_bridge.py"), "serve"], "bridge.out")
    if _wait_port(host, port, wait):
        return {"state": "started", "port": port, "how": how}
    return {"state": "failed", "port": port, "how": how,
            "error": t("bridge.client.unreachable", reason="no answer after start", script=HERE / "orkestra_bridge.py")}


def ensure_dashboard(wait: float = 15.0) -> Dict[str, Any]:
    if _port_open("127.0.0.1", DASHBOARD_PORT):
        return {"state": "running", "port": DASHBOARD_PORT}
    _spawn([sys.executable, str(ORKESTRA_BIN), "dashboard", "--port", str(DASHBOARD_PORT)], "dashboard.out")
    return {"state": "started" if _wait_port("127.0.0.1", DASHBOARD_PORT, wait) else "failed", "port": DASHBOARD_PORT}


def _app_running(bundle: Path) -> bool:
    """Whether the app's main executable runs (`ps`: `pgrep -f` misses some signed apps' arguments on macOS)."""
    try:
        out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    prefix = f"{bundle}/Contents/MacOS/"
    return any(line.strip().startswith(prefix) for line in out.splitlines())


def ensure_app(name: str) -> Dict[str, Any]:
    if not APP_NAME.match(name):
        return {"app": name, "state": "invalid"}
    if sys.platform != "darwin":
        return {"app": name, "state": "unsupported"}
    bundle = next((d / f"{name}.app" for d in (Path("/Applications"), Path.home() / "Applications")
                   if (d / f"{name}.app").is_dir()), None)
    if bundle is None:
        return {"app": name, "state": "missing"}
    if _app_running(bundle):
        return {"app": name, "state": "running"}
    return {"app": name, "state": "started" if _run(["open", "-a", str(bundle)]) == 0 else "failed"}


def revive(apps: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"bridge": ensure_bridge(), "dashboard": ensure_dashboard(),
                           "apps": [ensure_app(a) for a in apps]}
    out["ok"] = all(x["state"] in ("running", "started") for x in [out["bridge"], out["dashboard"], *out["apps"]])
    return out


def _opt(args: List[str], name: str, default: Optional[str] = None) -> Tuple[Optional[str], List[str]]:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1], args[:i] + args[i + 2:]
    return default, args


def _safe_stdio() -> None:
    """Same guard as orkestra_bridge.safe_stdio (kept local: this client stays importable on its own): a character the
    console cannot encode becomes a replacement instead of a UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")      # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: List[str]) -> int:
    _safe_stdio()
    if not argv or argv[0] in ("-h", "--help"):
        print(t("bridge.client.help"))
        return 2
    cmd, args = argv[0], argv[1:]
    engine, args = _opt(args, "--engine")
    chat, args = _opt(args, "--chat-id")
    source, args = _opt(args, "--source", "cli")
    timeout, args = _opt(args, "--timeout", "1800")
    owner = "--owner" in args
    args = [a for a in args if a != "--owner"] if cmd == "start" else args
    if cmd == "start":
        if len(args) < 2:
            print(t("bridge.client.help"))
            return 2
        request = {"project": args[0], "request": " ".join(args[1:]), "chat_id": chat, "source": source}
        if engine:
            request["engine"] = engine
        if owner:
            request["owner"] = True
        code, body = _call("POST", "/jobs", request)
    elif cmd == "ensure":
        body = ensure_bridge()
        code = 200 if body["state"] in ("running", "started") else 503
    elif cmd == "revive":
        apps: List[str] = []
        while "--app" in args:
            i = args.index("--app")
            if i + 1 < len(args):
                apps.append(args[i + 1])
            del args[i:i + 2]
        body = revive(apps)
        code = 200 if body["ok"] else 503
    elif cmd == "reply":
        if chat:
            code, body = _call("POST", "/reply", {"chat_id": chat, "text": " ".join(args)})
        elif len(args) >= 2:
            code, body = _call("POST", f"/jobs/{args[0]}/reply", {"text": " ".join(args[1:])})
        else:
            print(t("bridge.client.help"))
            return 2
    elif cmd == "status":
        if args:
            code, body = _call("GET", f"/jobs/{args[0]}")
        else:
            code, body = _call("GET", "/jobs" + (f"?chat_id={urllib.parse.quote(chat)}" if chat else ""))
    elif cmd == "projects":
        code, body = _call("GET", "/projects")
    elif cmd == "runs":
        code, body = _call("GET", "/runs")
        if code == 200 and "--json" not in args:
            sys.path.insert(0, str(HERE))
            from orkestra_bridge import live_text
            print(live_text(body.get("runs") or [], limit=100_000))
            return 0
    elif cmd == "cancel":
        if not chat:
            print(t("bridge.client.help"))
            return 2
        # "/iptal" and "/cancel" are the same command; the older spelling also works against a bridge started before
        # the English aliases existed, so a client and a long-running bridge of different ages still agree.
        code, body = _call("POST", "/telegram", {"chat_id": chat, "text": "/iptal"})
    elif cmd == "wait":
        if not args:
            print(t("bridge.client.help"))
            return 2
        deadline = time.time() + float(timeout or 1800)
        while True:
            code, body = _call("GET", f"/jobs/{args[0]}")
            if code != 200 or body.get("status") != "running" or time.time() > deadline:
                break
            time.sleep(5)
        if code == 200:
            print(f"[{body.get('project')}] {body.get('status')} · {body.get('phase') or ''}\n\n{body.get('last_message') or ''}")
            return 0
    else:
        print(t("bridge.client.help"))
        return 2
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return 0 if 200 <= code < 300 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
