"""Verification gates: detection per ecosystem, baseline, verify (no-regression rule).

Regression rule (deterministic): every gate that PASSED at baseline must PASS at verify.
Gates that already failed at baseline are reported as pre-existing and never blamed on the run.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import i18n
from .lazy import LazyModule
from .util import now_iso, read_json, tail_lines, write_json_atomic, write_text_atomic

# Imported on first use: the hooks load this module for normalize_pkg only (each import costs 3 to 4 ms).
shutil = LazyModule("shutil")
subprocess = LazyModule("subprocess")

DEFAULT_TIMEOUT = 900


# ---------------------------------------------------------------- detection

def _pkg_manager(project: Path) -> str:
    if (project / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (project / "yarn.lock").exists():
        return "yarn"
    if (project / "bun.lockb").exists() or (project / "bun.lock").exists():
        return "bun"
    declared = str((read_json(project / "package.json", default={}) or {}).get("packageManager") or "")
    name = declared.split("@", 1)[0]
    return name if name in ("pnpm", "yarn", "bun") else "npm"


def _run_script(pm: str, script: str) -> str:
    if pm == "npm":
        return "npm test" if script == "test" else f"npm run {script}"
    if pm == "yarn":
        return f"yarn {script}"
    if pm == "bun":
        return f"bun run {script}"
    return f"pnpm run {script}"


def _gate(name: str, cmd: str, kind: str, timeout: int = DEFAULT_TIMEOUT, cwd: str = ".") -> Dict[str, Any]:
    return {"name": name, "cmd": cmd, "kind": kind, "timeout": timeout, "cwd": cwd}


SUBPROJECT_DIRS = ("backend", "server", "api", "web", "frontend", "mobile", "app", "client")


def detect(project: Path) -> List[Dict[str, Any]]:
    """Gates for the project root plus (for multi-stack repos) conventional sub-projects.

    Detection is a proposal: onboarding verifies it against AGENTS.md/CLAUDE.md and the user can edit
    .orkestra/config.json (set "enabled": false on slow gates).
    """
    project = Path(project)
    gates: List[Dict[str, Any]] = []
    names: set = set()

    def add(g: Dict[str, Any]) -> None:
        base = g["name"]
        n, i = base, 2
        while n in names:
            n = f"{base}-{i}"
            i += 1
        g["name"] = n
        names.add(n)
        gates.append(g)

    _detect_dir(project, ".", add)
    root_has_workspaces = bool((read_json(project / "package.json", default={}) or {}).get("workspaces")) \
        or (project / "pnpm-workspace.yaml").exists()
    # a workspace root that declares no gates of its own: its apps/* and packages/* carry them
    scan_children = not root_has_workspaces or not gates
    if scan_children:
        subs: List[Path] = []
        for d in (SUBPROJECT_DIRS if not root_has_workspaces else ()):
            if (project / d).is_dir():
                subs.append(project / d)
        for parent in (("apps", "packages", "services") if not root_has_workspaces else ("apps", "packages")):
            if (project / parent).is_dir():
                subs += sorted(p for p in (project / parent).iterdir() if p.is_dir() and not p.name.startswith("."))
        for sub in subs[:12]:
            rel = str(sub.relative_to(project))
            prefix = sub.name
            _detect_dir(sub, rel, lambda g, prefix=prefix: add(dict(g, name=f"{prefix}:{g['name']}")))
    if not gates and (project / "android").is_dir():
        _detect_dir(project / "android", "android", lambda g: add(dict(g, name=f"android:{g['name']}")))
    return gates


# a real unittest suite: a TestCase subclass or a plain `import unittest` (not `from unittest.mock import patch`)
_UNITTEST_USE_RE = re.compile(r"^\s*class\s+\w+\s*\([^)]*TestCase\b|^\s*import\s+unittest\s*(?:#.*)?$", re.M)


def _uses_unittest_only(d: Path, pyproject: str, test_dirs: List[str], root_py_tests: List[Path]) -> bool:
    """True when the tests are plain unittest and pytest is not a declared dependency or configured."""
    if "pytest" in pyproject or any((d / f).exists() for f in ("pytest.ini", "conftest.py", "tox.ini")):
        return False
    for f in ("requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg", "Pipfile"):
        if (d / f).exists() and "pytest" in (d / f).read_text(encoding="utf-8", errors="replace"):
            return False
    files = list(root_py_tests)
    for x in test_dirs:
        files += sorted((d / x).glob("test*.py"))[:5]
    for f in files[:8]:
        try:
            if _UNITTEST_USE_RE.search(f.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False


def _detect_dir(d: Path, cwd: str, add: Any) -> None:
    pj = d / "package.json"
    if pj.exists():
        data = read_json(pj, default={}) or {}
        scripts: Dict[str, str] = data.get("scripts") or {}
        pm = _pkg_manager(d)
        tsc_script = next((s for s in ("typecheck", "type-check", "check-types", "types", "tsc") if s in scripts), None)
        if tsc_script:
            add(_gate("typecheck", _run_script(pm, tsc_script), "typecheck", cwd=cwd))
        elif (d / "tsconfig.json").exists():
            add(_gate("typecheck", "npx --no-install tsc --noEmit", "typecheck", cwd=cwd))
        if "lint" in scripts:
            add(_gate("lint", _run_script(pm, "lint"), "lint", cwd=cwd))
        test_script = "test" if scripts.get("test") and "no test specified" not in scripts.get("test", "") else \
            next((s for s in ("test:unit", "test:all", "test:web") if s in scripts), None)
        if test_script:
            add(_gate("test", _run_script(pm, test_script), "test", cwd=cwd))
        if "build" in scripts:
            add(_gate("build", _run_script(pm, "build"), "build", timeout=1800, cwd=cwd))

    root_py_tests = sorted(d.glob("test_*.py")) if not pj.exists() else []      # a stray script in a JS repo is no suite
    if (d / "pyproject.toml").exists() or (d / "requirements.txt").exists() or (d / "setup.py").exists() or root_py_tests:
        runner = "uv run " if (d / "uv.lock").exists() else ("poetry run " if (d / "poetry.lock").exists() else "")
        py = "python3 -m " if not runner else ""
        if not runner:
            for venv in (".venv", "venv"):
                if (d / venv / "bin" / "python").exists():
                    py = f"{venv}/bin/python -m "
                    break
        pyproject = (d / "pyproject.toml").read_text(encoding="utf-8", errors="replace") if (d / "pyproject.toml").exists() else ""
        test_dirs = [x for x in ("tests", "test") if (d / x).is_dir()]
        src_tests = sorted(d.glob("src/**/tests"))[:1] if (d / "src").is_dir() else []
        has_tests = bool(test_dirs or src_tests or root_py_tests) or "pytest" in pyproject
        if has_tests:
            if _uses_unittest_only(d, pyproject, test_dirs, root_py_tests):
                start = test_dirs[0] if test_dirs else "."
                add(_gate("unittest", f"{runner}{py}unittest discover -s {start} -q", "test", cwd=cwd))
            else:
                add(_gate("pytest", f"{runner}{py}pytest -q", "test", cwd=cwd))
        if "[tool.ruff" in pyproject or (d / "ruff.toml").exists() or (d / ".ruff.toml").exists():
            add(_gate("ruff", f"{runner}{py}ruff check .", "lint", cwd=cwd))
        if "[tool.mypy" in pyproject or (d / "mypy.ini").exists():
            add(_gate("mypy", f"{runner}{py}mypy .", "typecheck", cwd=cwd))

    if (d / "go.mod").exists():
        add(_gate("go-build", "go build ./...", "build", cwd=cwd))
        add(_gate("go-vet", "go vet ./...", "lint", cwd=cwd))
        add(_gate("go-test", "go test ./...", "test", cwd=cwd))

    if (d / "Cargo.toml").exists():
        add(_gate("cargo-build", "cargo build --quiet", "build", timeout=1800, cwd=cwd))
        add(_gate("cargo-test", "cargo test --quiet", "test", timeout=1800, cwd=cwd))

    pub = d / "pubspec.yaml"
    if pub.exists():
        txt = pub.read_text(encoding="utf-8", errors="replace")
        tool = "flutter" if re.search(r"^\s*flutter\s*:", txt, re.M) else "dart"
        add(_gate(f"{tool}-analyze", f"{tool} analyze", "lint", cwd=cwd))
        if (d / "test").is_dir():
            add(_gate(f"{tool}-test", f"{tool} test", "test", timeout=1800, cwd=cwd))

    if (d / "pom.xml").exists():
        mvn = "./mvnw" if (d / "mvnw").exists() else "mvn"
        add(_gate("maven-test", f"{mvn} -q test", "test", timeout=2400, cwd=cwd))
    elif ((d / "build.gradle").exists() or (d / "build.gradle.kts").exists()) and (d / "gradlew").exists() and not pj.exists():
        add(_gate("gradle-test", "./gradlew test --quiet", "test", timeout=2400, cwd=cwd))
    elif not pj.exists() and any((d / f).exists() for f in ("settings.gradle", "settings.gradle.kts", "build.gradle",
                                                              "build.gradle.kts")):
        gradle = "./gradlew" if (d / "gradlew").exists() else "gradle"
        add(_gate("gradle-test", f"{gradle} test --quiet", "test", timeout=2400, cwd=cwd))

    slns = list(d.glob("*.sln"))
    if slns or list(d.glob("*.csproj")):
        add(_gate("dotnet-build", "dotnet build --nologo -v q", "build", timeout=1800, cwd=cwd))
        add(_gate("dotnet-test", "dotnet test --nologo -v q", "test", timeout=1800, cwd=cwd))
    else:
        test_projs = sorted(p for p in d.glob("*/*/*.csproj") if "test" in p.name.lower())[:3]
        for tp in test_projs:
            rel = str(tp.parent.relative_to(d))
            add(_gate("dotnet-test", f"dotnet test {rel} --nologo -v q", "test", timeout=1800, cwd=cwd))
        # no test project two levels down: one solution / project set one level down (src/App.sln, src/App.csproj)
        for np_ in ([] if test_projs else (sorted(d.glob("*/*.sln"))[:1] or sorted(d.glob("*/*.csproj"))[:3])):
            rel = str(np_.relative_to(d))
            if "test" not in np_.name.lower():
                add(_gate("dotnet-build", f"dotnet build {rel} --nologo -v q", "build", timeout=1800, cwd=cwd))
            if np_.suffix == ".sln" or "test" in np_.name.lower():
                add(_gate("dotnet-test", f"dotnet test {rel} --nologo -v q", "test", timeout=1800, cwd=cwd))

    if (d / "Package.swift").exists():
        add(_gate("swift-build", "swift build", "build", timeout=1800, cwd=cwd))
        add(_gate("swift-test", "swift test", "test", timeout=1800, cwd=cwd))

    if (d / "composer.json").exists():
        data = read_json(d / "composer.json", default={}) or {}
        if "test" in (data.get("scripts") or {}):
            add(_gate("composer-test", "composer test", "test", cwd=cwd))

    if (d / "Gemfile").exists():
        if (d / "spec").is_dir() or (d / ".rspec").exists():
            add(_gate("rspec", "bundle exec rspec", "test", cwd=cwd))
        elif (d / "Rakefile").exists() or (d / "test").is_dir():
            add(_gate("rake-test", "bundle exec rake test", "test", cwd=cwd))

    if (d / "mix.exs").exists():
        add(_gate("mix-test", "mix test", "test", timeout=1800, cwd=cwd))

    if (d / "deno.json").exists() or (d / "deno.jsonc").exists():
        add(_gate("deno-test", "deno test", "test", cwd=cwd))

    cml = d / "CMakeLists.txt"
    if cml.exists() and re.search(r"enable_testing|add_test|include\(CTest\)",
                                  cml.read_text(encoding="utf-8", errors="replace"), re.I):
        add(_gate("ctest", "cmake -S . -B build && cmake --build build && ctest --test-dir build --output-on-failure",
                  "test", timeout=1800, cwd=cwd))

    mk = d / "Makefile"
    if mk.exists() and cwd == "." and not any((d / m).exists() for m in ("package.json", "go.mod", "Cargo.toml", "pom.xml")):
        txt = mk.read_text(encoding="utf-8", errors="replace")
        for target, kind in (("test", "test"), ("lint", "lint"), ("build", "build")):
            if re.search(rf"^{target}\s*:", txt, re.M):
                add(_gate(f"make-{target}", f"make {target}", kind, cwd=cwd))
        if not re.search(r"^test\s*:", txt, re.M) and re.search(r"^check\s*:", txt, re.M):
            add(_gate("make-check", "make check", "test", cwd=cwd))


# ---------------------------------------------------------------- running


_IS_WINDOWS = os.name == "nt"           # a module constant so tests can exercise the Windows branches (not os.name: pathlib)


def _resolve_argv(argv, env: Optional[dict]) -> List[str]:
    """Windows only: CreateProcess does not apply PATHEXT, so `npm` never finds `npm.cmd` (nor yarn, mvn, gradlew).
    Resolve the program through PATH the way a shell does; elsewhere the list is used as given."""
    out = [str(a) for a in argv]
    if _IS_WINDOWS and out:
        exe = shutil.which(out[0], path=(env or {}).get("PATH") or None)
        if exe:
            out[0] = exe
    return out


def _spawn_failure(exc: OSError) -> Tuple[int, str]:
    """(exit code, output) for a command that could not even start, as a shell would report it (127 / 126)."""
    return (126 if isinstance(exc, PermissionError) else 127), f"[orkestra] could not start the command: {exc}"


def _run_cmd(cmd, *, cwd: str, env: dict, timeout: int):
    """Run a gate command. A list runs as argv without a shell. A string runs through the platform shell (sh, or
    cmd.exe on Windows) exactly as typed in a terminal: `cd sub && npm test`, quoting, `VAR=1 cmd` and the .cmd shims
    of npm/yarn/mvn all behave the same way they do there."""
    if isinstance(cmd, (list, tuple)):
        return subprocess.run(_resolve_argv(cmd, env), shell=False, cwd=cwd, env=env, capture_output=True,
                              timeout=timeout)
    return subprocess.run(str(cmd), shell=True, cwd=cwd, env=env, capture_output=True, timeout=timeout)


# Streaming runner: every gate gets its own process group, its output reaches the log file as it arrives, and a
# timeout / idle stall / interrupt kills the WHOLE group (SIGTERM, then SIGKILL after KILL_GRACE s).
KILL_GRACE = 3.0
_OUTPUT_KEEP = 2_000_000                # bytes of output kept in memory for the tail; the log file holds everything
_DRAIN_WAIT = 2.0                       # s a leader's exit waits for its output pipe before leftovers are killed


def _kill_tree(proc: "subprocess.Popen", grace: float = KILL_GRACE) -> None:
    """Terminate the command and every descendant it started (never raises)."""
    if _IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
    else:
        import signal
        pgid = proc.pid                     # start_new_session=True: the leader's pid is the group id
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            proc.poll()                     # reap the leader so only live members keep the group alive
            try:
                os.killpg(pgid, 0)
            except OSError:
                break                       # nobody left in the group
            time.sleep(0.05)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _stream_cmd(cmd, *, cwd: str, env: dict, timeout: int, idle_timeout: int, log_file: Any,
                abort: Any = None) -> Tuple[int, bytes, str]:
    """Run `cmd` (list = argv, string = platform shell) and stream merged output into `log_file` (a binary file
    object) as it arrives. Returns (exit code, kept output, reason) where reason is '' | timeout | idle | aborted.
    Raises OSError when the program cannot start; kills the whole process group on any BaseException."""
    import threading
    kw: Dict[str, Any] = {"cwd": cwd, "env": env, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                          "stderr": subprocess.STDOUT}
    if _IS_WINDOWS:
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True      # its own process group: the kill reaches every descendant
    if isinstance(cmd, (list, tuple)):
        proc = subprocess.Popen(_resolve_argv(cmd, env), shell=False, **kw)
    else:
        proc = subprocess.Popen(str(cmd), shell=True, **kw)
    kept = bytearray()
    last = [time.monotonic()]

    def pump() -> None:
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    return
                last[0] = time.monotonic()
                log_file.write(chunk)
                log_file.flush()
                kept.extend(chunk)
                if len(kept) > _OUTPUT_KEEP:
                    del kept[:len(kept) - _OUTPUT_KEEP]
        except (OSError, ValueError):
            return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    started = time.monotonic()
    reason = ""
    try:
        while True:
            try:
                proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if abort is not None and abort.is_set():
                reason = "aborted"
            elif timeout and now - started >= timeout:
                reason = "timeout"
            elif idle_timeout and now - last[0] >= idle_timeout:
                reason = "idle"
            if reason:
                _kill_tree(proc)
                break
        reader.join(_DRAIN_WAIT)
        if reader.is_alive():               # a descendant outlived the leader and holds the pipe: kill the leftovers
            _kill_tree(proc)
            reader.join(_DRAIN_WAIT)
    except BaseException:
        _kill_tree(proc)
        reader.join(_DRAIN_WAIT)
        try:
            log_file.write(b"\n[orkestra] INTERRUPTED\n")
            log_file.flush()
        except (OSError, ValueError):
            pass
        raise
    finally:
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            pass
    code = proc.returncode if proc.returncode is not None else 124
    if reason in ("timeout", "idle"):
        code = 124
    return code, bytes(kept), reason


def _run_tree(cmd, *, cwd: str, env: dict, timeout: int) -> "subprocess.CompletedProcess":
    """Evidence / TDD runner: like _run_cmd, but through _stream_cmd, so the command has its own process group, a
    leader's exit waits at most _DRAIN_WAIT for a leaked background descendant, and a timeout kills the whole tree.
    Output is merged (stdout holds both streams). Raises subprocess.TimeoutExpired like subprocess.run would."""
    import io
    sink = io.BytesIO()
    code, kept, reason = _stream_cmd(cmd, cwd=cwd, env=env, timeout=timeout, idle_timeout=0, log_file=sink)
    if reason == "timeout":
        raise subprocess.TimeoutExpired(cmd, timeout, output=kept)
    return subprocess.CompletedProcess(cmd, code, stdout=kept, stderr=b"")


_ENV_ERROR_RE = re.compile(
    r"command not found|could not start the command|spawn \S+ ENOENT|could not determine executable to run"
    r"|npx canceled due to missing packages|node_modules[/\\]\.bin[/\\][^\s:]*:?\s*(?:not found|No such file)", re.I)
# A missing JS module is the gate's own dependency only when it is a bare package name; './x' is the code under test.
_JS_MODULE_RE = re.compile(r"^(?!.*\bTS2307\b).*Cannot find module ['\"]([^'\"]+)['\"]", re.I | re.M)
_PY_MODULE_RE = re.compile(r"No module named ['\"]?([\w.]+)", re.I)
_PY_TOOL_MODULES = frozenset((
    "pytest", "_pytest", "pluggy", "ruff", "mypy", "pip", "unittest", "black", "flake8", "pylint", "coverage", "tox",
    "nox", "poetry", "setuptools", "wheel", "build", "twine"))


def is_env_error(code: int, output: str, cmd: str = "") -> bool:
    """True when a failed gate could not run at all (missing tool or dependency), as opposed to a real failure.
    A missing module counts only when it is the gate's own tool (pytest, the `-m` module) or a bare JS package;
    code under test that fails to import ('No module named app.x', "Cannot find module './x'", TS2307) is a real red."""
    if code in (126, 127):
        return True
    tail = tail_lines(output, 30, 3000)
    if _ENV_ERROR_RE.search(tail):
        return True
    own = set(_PY_TOOL_MODULES)
    own.update(m.split(".")[0] for m in re.findall(r"-m\s+([\w.]+)", cmd or ""))
    for m in _PY_MODULE_RE.finditer(tail):
        if m.group(1).split(".")[0] in own:
            return True
    for m in _JS_MODULE_RE.finditer(tail):
        if not m.group(1).startswith((".", "/", "@/", "~")):
            return True
    return False


def run_gate(project: Path, gate: Dict[str, Any], log_dir: Path, label: str, abort: Any = None) -> Dict[str, Any]:
    log_dir.mkdir(parents=True, exist_ok=True)
    cwd = (Path(project) / gate.get("cwd", ".")).resolve(strict=False)
    env = dict(os.environ)
    env.setdefault("CI", "1")
    env.setdefault("FORCE_COLOR", "0")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    started = time.time()
    timed_out = False
    idle = False
    timeout = int(gate.get("timeout", DEFAULT_TIMEOUT))
    idle_timeout = int(gate.get("idle_timeout") or 0)       # 0 = off; e2e gates: 300 is a sensible value (see onboard)
    log_path = log_dir / f"{label}-{gate['name']}.log"
    output = ""
    with open(log_path, "wb") as lf:
        lf.write(f"$ {gate['cmd']}\n(cwd={cwd})\n\n".encode("utf-8", "replace"))
        lf.flush()
        try:
            code, raw, reason = _stream_cmd(gate["cmd"], cwd=str(cwd), env=env, timeout=timeout,
                                            idle_timeout=idle_timeout, log_file=lf, abort=abort)
            output = raw.decode("utf-8", "replace")
            if reason == "timeout":
                timed_out = True
                output += "\n[orkestra] TIMEOUT " + i18n.t("runs.gate_timeout_hint", seconds=timeout)
            elif reason == "idle":
                timed_out = idle = True
                output += f"\n[orkestra] TIMEOUT-IDLE (no output for {idle_timeout}s)"
            elif reason == "aborted":
                output += "\n[orkestra] ABORTED"
            if reason:
                lf.write(("\n" + output.rsplit("\n", 1)[-1]).encode("utf-8", "replace"))
        except OSError as exc:              # the program is missing or not executable: a failed gate, not a crash
            code, output = _spawn_failure(exc)
            lf.write(output.encode("utf-8", "replace"))
        lf.write(f"\n[exit {code}]\n".encode("utf-8"))
    duration = round(time.time() - started, 1)
    res = {
        "name": gate["name"], "cmd": gate["cmd"], "kind": gate.get("kind", "other"),
        "exit": code, "ok": code == 0, "timed_out": timed_out, "duration_s": duration,
        "log": str(log_path), "tail": tail_lines(output, 30, 3000) if code != 0 else "",
        "at": now_iso(),
    }
    if idle:
        res["idle_timeout"] = True
    if code != 0 and not timed_out and is_env_error(code, output, str(gate.get("cmd") or "")):
        res["env_error"] = True
    return res


_CONCURRENT_KINDS = ("typecheck", "lint", "test")
_SERIAL_ORDER = {"build": 0, "e2e": 2}              # after the concurrent wave: build, anything else, then e2e


def effective_parallel(requested: Any) -> int:
    """Worker count for the concurrent wave: min(requested, cpu_count / 2), at least 1."""
    try:
        n = int(requested)
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, (os.cpu_count() or 2) // 2))


_TEST_CMD = re.compile(
    r"(?:^|[\s;&|])(?:python3?(?:\s+-m)?\s+)?(?:unittest|pytest)\b"
    r"|(?:^|[\s;&|])(?:go|cargo)\s+test\b"
    r"|(?:^|[\s;&|])(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b"
)


def _concurrent(g: Dict[str, Any]) -> bool:
    if g.get("exclusive") or g.get("parallel") is False:
        return False
    if g.get("parallel") or g.get("kind") in _CONCURRENT_KINDS:
        return True
    # A hand-written unittest/pytest gate has no kind. It still belongs in the test wave.
    return _TEST_CMD.search(str(g.get("cmd") or "")) is not None


def run_all(project: Path, gates: List[Dict[str, Any]], log_dir: Path, label: str,
            only: Optional[List[str]] = None, *, parallel: Any = 1, on_result: Any = None,
            reuse: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Run the selected gates. `reuse` maps a gate name to a ready result (carried, skipped, resumed): not executed.
    `on_result(results_so_far)` is called after every finished gate (config order) so progress is persisted at once.
    parallel > 1 runs typecheck/lint/test gates concurrently first, then the rest serially (build, other, e2e);
    with parallel 1 the order is exactly the config order. The returned list always follows the config order."""
    selected = []
    for g in gates:
        if only and g["name"] not in only:
            continue
        if g.get("enabled") is False and not only:
            continue
        selected.append(g)
    done: Dict[int, Dict[str, Any]] = {}

    def emit(i: int, res: Dict[str, Any]) -> None:
        done[i] = res
        if on_result is not None:
            on_result([done[k] for k in sorted(done)])

    todo = []
    for i, g in enumerate(selected):
        if reuse and g["name"] in reuse:
            emit(i, reuse[g["name"]])
        else:
            todo.append(i)
    workers = effective_parallel(parallel)
    wave = [i for i in todo if _concurrent(selected[i])] if workers > 1 else []
    if len(wave) < 2:
        wave = []
    rest = [i for i in todo if i not in wave]
    if wave:
        rest.sort(key=lambda i: _SERIAL_ORDER.get(selected[i].get("kind"), 1))
    import threading
    abort = threading.Event()
    try:
        if wave:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=min(workers, len(wave))) as pool:
                futs = {pool.submit(run_gate, project, selected[i], log_dir, label, abort): i for i in wave}
                try:
                    for fut in as_completed(futs):
                        emit(futs[fut], fut.result())
                except BaseException:
                    abort.set()             # the running gates kill their own process groups; queued ones never start
                    for fut, i in futs.items():
                        if i in done or fut.cancelled():
                            continue
                        try:
                            emit(i, fut.result(timeout=5))  # keep a sibling that finished or finishes at once
                        except BaseException:
                            pass
                    for f in futs:
                        f.cancel()
                    raise
        for i in rest:
            emit(i, run_gate(project, selected[i], log_dir, label, abort))
    except BaseException:
        abort.set()
        raise
    return [done[i] for i in range(len(selected))]


def glob_match(pattern: str, rel: str) -> bool:
    """Project-relative glob: `*` and `?` stay inside a path segment, `**` crosses segments."""
    while pattern.startswith("./"):
        pattern = pattern[2:]
    rx, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            rx += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            rx += ".*"
            i += 2
            continue
        rx += "[^/]*" if c == "*" else ("[^/]" if c == "?" else re.escape(c))
        i += 1
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return re.fullmatch(rx, rel) is not None


def compare(baseline: List[Dict[str, Any]], current: List[Dict[str, Any]]) -> Dict[str, Any]:
    b = {r["name"]: r for r in baseline}
    c = {r["name"]: r for r in current}
    regressions, fixed, pre_existing, new_fail, new_pass, missing = [], [], [], [], [], []
    for name, cur in c.items():
        if name in b:
            was = b[name]["ok"]
            if was and not cur["ok"]:
                regressions.append(name)
            elif not was and cur["ok"]:
                fixed.append(name)
            elif not was and not cur["ok"]:
                pre_existing.append(name)
        else:
            (new_pass if cur["ok"] else new_fail).append(name)
    for name in b:
        if name not in c:
            missing.append(name)
    ok = not regressions and not new_fail and not missing
    env_errors = [n for n, cur in c.items() if cur.get("env_error") and not cur["ok"]
                  and n not in regressions and n not in new_fail]
    pre_existing = [n for n in pre_existing if n not in env_errors]
    return {"ok": ok, "regressions": regressions, "fixed": fixed, "pre_existing_failures": pre_existing,
            "new_gate_failures": new_fail, "new_gate_passes": new_pass, "not_run": missing,
            "env_errors": env_errors}


def all_env_error(results: List[Dict[str, Any]]) -> bool:
    """Every gate that ran failed because it could not start (nothing was actually checked)."""
    ran = [r for r in results if not r.get("skipped")]
    return bool(ran) and all(r.get("env_error") and not r["ok"] for r in ran)


def format_results(results: List[Dict[str, Any]], comparison: Optional[Dict[str, Any]] = None,
                   baseline: Optional[List[Dict[str, Any]]] = None) -> str:
    bmap = {r["name"]: r for r in (baseline or [])}
    lines = [i18n.t("runs.gate_table_header"), "|---|---|---|---|---|"]
    for r in results:
        b = bmap.get(r["name"])
        bcell = ("PASS" if b["ok"] else "FAIL") if b else "—"
        now_cell = "SKIPPED" if r.get("skipped") else (
            ("PASS (carried)" if r.get("carried_from") else "PASS") if r["ok"] else ("TIMEOUT" if r["timed_out"] else "FAIL"))
        lines.append(f"| {r['name']} | {bcell} | {now_cell} "
                     f"| {r['duration_s']}s | `{r['cmd']}` |")
    out = "\n".join(lines)
    skipped = [r["name"] for r in results if r.get("skipped")]
    if skipped:
        out += "\n\n" + i18n.t("runs.gates_unprotected", names=", ".join(skipped))
    carried = [r["name"] for r in results if r.get("carried_from")]
    if carried:
        out += "\n\n" + i18n.t("runs.gates_carried", names=", ".join(carried))
    if comparison is not None:
        out += "\n\n" + ("NO REGRESSIONS" if comparison["ok"] else "REGRESSION DETECTED")
        if all_env_error(results):
            out += "\n" + i18n.t("runs.gates_none_ran")
        for key in ("regressions", "new_gate_failures", "not_run", "pre_existing_failures", "fixed"):
            if comparison.get(key):
                out += f"\n- {key}: {', '.join(comparison[key])}"
    env = [r["name"] for r in results if r.get("env_error") and not r["ok"]]
    if env:
        out += "\n\n" + i18n.t("runs.gates_env_error", count=len(env), names=", ".join(env))
    for r in results:
        if not r["ok"]:
            out += f"\n\n--- {r['name']} (exit {r['exit']}) tail — full log: {r['log']}\n{r['tail']}"
    return out


def save_results(path: Path, results: List[Dict[str, Any]], extra: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {"at": now_iso(), "results": results}
    if extra:
        payload.update(extra)
    write_json_atomic(path, payload)


# ---------------------------------------------------------------- dependency manifests

def dependency_sets(project: Path) -> Dict[str, List[str]]:
    """Best-effort set of declared dependency names per manifest (for the deps check)."""
    project = Path(project)
    out: Dict[str, List[str]] = {}
    pj = read_json(project / "package.json")
    if isinstance(pj, dict):
        names = set()
        for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            names |= set((pj.get(k) or {}).keys())
        out["package.json"] = sorted(names)
    req = project / "requirements.txt"
    if req.exists():
        names = set()
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#")[0].strip()
            if line and not line.startswith("-"):
                names.add(re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].lower())
        out["requirements.txt"] = sorted(names)
    pp = project / "pyproject.toml"
    if pp.exists():
        txt = pp.read_text(encoding="utf-8", errors="replace")
        names = set()
        m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", txt, re.S | re.M)
        if m:
            for item in re.findall(r"[\"']([^\"']+)[\"']", m.group(1)):
                names.add(re.split(r"[<>=!~\[; ]", item, maxsplit=1)[0].lower())
        for block in re.finditer(r"^\[tool\.poetry\.(?:dev-)?dependencies\]\s*\n(.*?)(?=^\[|\Z)", txt, re.S | re.M):
            for line in block.group(1).splitlines():
                mm = re.match(r"^([A-Za-z0-9_.\-]+)\s*=", line)
                if mm and mm.group(1).lower() != "python":
                    names.add(mm.group(1).lower())
        out["pyproject.toml"] = sorted(names)
    gm = project / "go.mod"
    if gm.exists():
        txt = gm.read_text(encoding="utf-8", errors="replace")
        names = set(re.findall(r"^\s*([a-zA-Z0-9.\-_/]+\.[a-z]+/[^\s]+)\s+v", txt, re.M))
        out["go.mod"] = sorted(names)
    ct = project / "Cargo.toml"
    if ct.exists():
        txt = ct.read_text(encoding="utf-8", errors="replace")
        names = set()
        for block in re.finditer(r"^\[(?:dev-|build-)?dependencies\]\s*\n(.*?)(?=^\[|\Z)", txt, re.S | re.M):
            for line in block.group(1).splitlines():
                mm = re.match(r"^([A-Za-z0-9_\-]+)\s*=", line)
                if mm:
                    names.add(mm.group(1))
        out["Cargo.toml"] = sorted(names)
    ps = project / "pubspec.yaml"
    if ps.exists():
        txt = ps.read_text(encoding="utf-8", errors="replace")
        names = set()
        for block in re.finditer(r"^(?:dev_)?dependencies:\s*\n((?:[ \t]+.*\n?)*)", txt, re.M):
            for line in block.group(1).splitlines():
                mm = re.match(r"^  ([A-Za-z0-9_]+)\s*:", line)
                if mm:
                    names.add(mm.group(1))
        out["pubspec.yaml"] = sorted(names)
    cj = read_json(project / "composer.json")
    if isinstance(cj, dict):
        out["composer.json"] = sorted(set((cj.get("require") or {}).keys()) | set((cj.get("require-dev") or {}).keys()))
    return out


def new_dependencies(before: Dict[str, List[str]], after: Dict[str, List[str]]) -> List[str]:
    added: List[str] = []
    for manifest, names in after.items():
        prev = set(before.get(manifest, []))
        for n in names:
            if n not in prev:
                added.append(n)
    return sorted(set(added))


def normalize_pkg(name: str) -> str:
    name = name.strip().strip("'\"")
    if name.startswith("@"):
        # scoped npm: @scope/pkg@1.2 -> @scope/pkg
        parts = name.split("@")
        return "@" + parts[1] if len(parts) > 1 else name
    name = re.split(r"[@<>=!~\[;: ]", name, maxsplit=1)[0]
    return name.lower() if not re.search(r"[A-Z]", name) else name

