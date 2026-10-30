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

Reads the bearer token from ~/.orkestra/bridge.secret.json (same machine). Prints JSON (or the message for `wait`).
Exit codes: 0 ok · 1 bridge/HTTP error · 2 usage.
Text meant for a person (usage, "bridge not reachable") follows orkestra_core.i18n: English by default, Turkish when
ORKESTRA_LANG / ~/.orkestra/config.json / the OS locale say so. The JSON the bridge returns is language-neutral.
"""
from __future__ import annotations

import errno
import json
import os
import socket
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
    if cmd == "start":
        if len(args) < 2:
            print(t("bridge.client.help"))
            return 2
        request = {"project": args[0], "request": " ".join(args[1:]), "chat_id": chat, "source": source}
        if engine:
            request["engine"] = engine
        code, body = _call("POST", "/jobs", request)
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
