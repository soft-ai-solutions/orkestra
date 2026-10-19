"""Small shared helpers: atomic IO, locking, hashing, time, slugs."""
from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from .lazy import LazyModule

# Imported on first use: a hook that only reads state never pays for them (each costs 2 to 6 ms of start-up).
hashlib = LazyModule("hashlib")
random = LazyModule("random")
shutil = LazyModule("shutil")
unicodedata = LazyModule("unicodedata")

try:  # POSIX advisory locks (macOS/Linux).
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore

msvcrt = None
if os.name == "nt":  # Windows exclusive locks via stdlib msvcrt (absent on POSIX).
    import importlib
    msvcrt = importlib.import_module("msvcrt")

# Module-level so tests can simulate Windows: patching os.name itself would break pathlib.
_IS_WINDOWS = os.name == "nt"


def now_iso() -> str:
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def today() -> str:
    return _dt.date.today().isoformat()


def read_text(path: Path, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Latin-1 source, a binary with a text extension: the evidence checks only count and diff lines, and the git
        # baseline is decoded with "replace" too, so both sides stay comparable
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return default


def replace_file(src: Any, dst: Any) -> None:
    """os.replace that survives Windows sharing violations: replacing a file another process has open raises
    PermissionError there (hooks read state files while other hooks rewrite them), and the holder lets go within
    milliseconds. POSIX never retries: the error is final."""
    delay = 0.02
    for attempt in range(6):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not _IS_WINDOWS or attempt == 5:
                raise
            time.sleep(delay)
            delay *= 2


def _mkstemp(prefix: str, directory: str) -> Any:
    """(fd, path) of a new private file, as tempfile.mkstemp makes it, without importing tempfile (6 ms per process)."""
    flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NOINHERIT", 0))
    for _ in range(100):
        name = os.path.abspath(os.path.join(directory, prefix + os.urandom(5).hex()))
        try:
            return os.open(name, flags, 0o600), name
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "no usable temporary file name", directory)


def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _mkstemp(".tmp-", str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:        # LF on every OS (no CRLF on Windows)
            fh.write(text)
        replace_file(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:          # a BOM (Windows editors) is not corruption
            return json.load(fh)
    except (FileNotFoundError, NotADirectoryError):
        return default
    except ValueError:              # not JSON, or not even text (UnicodeDecodeError): the same as "nothing usable"
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    write_text_atomic(Path(path), json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


def append_record(path: Path, data: bytes, header: bytes = b"") -> None:
    """Append `data` to `path` with ONE O_APPEND write: concurrent writers neither lose nor interleave records, and
    the cost does not grow with the file. `header` goes first, in the same write, only while the file is still empty
    (checked again under the file lock, so the header is written once). Windows has no atomic append: always locked."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)

    def put(chunk: bytes) -> None:
        fd = os.open(str(path), flags, 0o666)
        try:
            view = memoryview(chunk)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)

    def empty() -> bool:
        try:
            return path.stat().st_size == 0
        except FileNotFoundError:
            return True

    if _IS_WINDOWS or (header and empty()):
        with file_lock(path, on_timeout="proceed"):         # bookkeeping: a duplicate header is better than a stall
            put(header + data if header and empty() else data)
    else:
        put(data)


# flock errors that mean "this file system cannot lock", not "somebody else holds the lock": NFS without lockd,
# some container and network mounts. Work goes on unlocked there, as it does where no locking module exists.
_NO_LOCKING = {getattr(errno, n) for n in ("ENOLCK", "ENOSYS", "EINVAL", "ENOTSUP", "EOPNOTSUPP") if hasattr(errno, n)}
# what a NON-BLOCKING attempt reports when another process holds the lock (POSIX: EWOULDBLOCK; Windows: a held byte
# is EACCES or EDEADLOCK)
_LOCK_BUSY = {getattr(errno, n) for n in ("EAGAIN", "EWOULDBLOCK", "EACCES", "EDEADLK", "EDEADLOCK") if hasattr(errno, n)}

HOOK_LOCK_TIMEOUT = 2.0         # a hook must answer well inside the host's 15 s kill: past this it degrades
CLI_LOCK_TIMEOUT = 10.0         # a command waits longer, then fails with a message that names the holder
_HOOK_SCRIPTS = ("dispatch.py", "cursor_hook.py", "copilot_hook.py")
_lock_policy: Optional[str] = None
_LOGGED_BUSY: set = set()       # lock files already reported in hooks.log by this process
# the poll loop never calls time.* directly, so tests can drive a virtual clock
_clock = time.monotonic
_pause = time.sleep


def set_lock_policy(kind: Optional[str]) -> None:
    """"hook" | "cli" | None (None: follow the entry script, see lock_policy)."""
    global _lock_policy
    _lock_policy = kind if kind in ("hook", "cli") else None


def lock_policy() -> str:
    """"hook" inside a hook script (short budget, bookkeeping degrades), else "cli" (longer budget, errors)."""
    if _lock_policy:
        return _lock_policy
    script = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    return "hook" if script in _HOOK_SCRIPTS else "cli"


def lock_budget(kind: Optional[str] = None) -> float:
    """Seconds a lock may be waited for. ORKESTRA_LOCK_TIMEOUT overrides both policies."""
    raw = os.environ.get("ORKESTRA_LOCK_TIMEOUT", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return HOOK_LOCK_TIMEOUT if (kind or lock_policy()) == "hook" else CLI_LOCK_TIMEOUT


class LockTimeout(OSError):
    """A lock stayed busy for the whole waiting budget. The one-line message names the holder."""

    def __init__(self, lock_path: Path, waited: float, holder: Optional[dict] = None):
        self.lock_path = Path(lock_path)
        self.waited = waited
        self.holder = dict(holder or {})
        super().__init__(_busy_text(self.lock_path, waited, self.holder))


def _busy_text(lock_path: Path, waited: float, holder: dict) -> str:
    name = lock_path.name[:-5] if lock_path.name.endswith(".lock") else lock_path.name
    name = f"{lock_path.parent.name}/{name}"
    try:
        from . import i18n
        if holder.get("pid"):
            who = i18n.t("common.lock_holder", pid=holder["pid"], cmd=holder.get("cmd") or "?",
                         held=holder.get("held", 0))
        else:
            who = i18n.t("common.lock_holder_unknown")
        return " ".join(i18n.t("common.lock_busy", name=name, waited=f"{waited:.1f}", holder=who).split())
    except Exception:                   # a broken catalog must not hide the timeout itself
        return f"{name} is busy ({waited:.1f} s), held by pid {holder.get('pid', '?')}"


_HOLDER_RE = re.compile(r"pid=(\d+) since=(\S+) cmd=(.*)")


def _stamp_holder(fd: int, keep: int) -> None:
    """Write "pid=.. since=.. cmd=.." into the lock file we now hold (after its first `keep` bytes), so a waiter that
    gives up can say who it was waiting for."""
    try:
        argv = [os.path.basename(sys.argv[0] or "")] + [str(a) for a in sys.argv[1:3]] if sys.argv else []
        cmd = " ".join(" ".join(argv).split())[:80] or "?"
        os.ftruncate(fd, keep)
        os.write(fd, f"pid={os.getpid()} since={now_iso()} cmd={cmd}\n".encode("utf-8", "replace"))
    except (OSError, ValueError):
        pass


def _read_holder(lock_path: Path) -> dict:
    """What a lock file says about its owner ({} when nothing usable): pid, since, cmd, held (seconds)."""
    try:
        with open(lock_path, "rb") as fh:
            if _IS_WINDOWS:
                fh.seek(1)              # byte 0 is the locked byte: reading it fails on Windows
            raw = fh.read(512)
        age = max(0, int(time.time() - os.stat(lock_path).st_mtime))
    except OSError:
        return {}
    m = _HOLDER_RE.search(raw.replace(b"\0", b"").decode("utf-8", "replace"))
    if not m:
        return {}
    return {"pid": int(m.group(1)), "since": m.group(2), "cmd": m.group(3).strip(), "held": age}


class _Busy(Exception):
    """Another process holds the lock (internal)."""


def _nb_flag(module: Any, name: str) -> Optional[int]:
    value = getattr(module, name, None)
    return value if isinstance(value, int) else None


def _lock_attempt(fh: Any, shared: bool = False) -> str:
    """One try at the OS lock: the kind now held ("fcntl" | "msvcrt"), "" when this file system cannot lock, _Busy
    when another process holds it. A locking module without a non-blocking flag (never a real fcntl/msvcrt) is used
    the old way: blocking. `shared` asks for a flock shared lock (readers of one critical section do not queue on each
    other); msvcrt has no shared mode and takes the exclusive one."""
    fd = fh.fileno()
    if fcntl is not None:
        nb = _nb_flag(fcntl, "LOCK_NB")
        mode = fcntl.LOCK_SH if shared and isinstance(getattr(fcntl, "LOCK_SH", None), int) else fcntl.LOCK_EX
        try:
            fcntl.flock(fd, mode | nb if nb is not None else mode)
            return "fcntl"
        except OSError as exc:
            if nb is not None and exc.errno in _LOCK_BUSY:
                raise _Busy() from None
            if exc.errno in _NO_LOCKING:
                return ""
            raise
    if msvcrt is not None:
        nb = _nb_flag(msvcrt, "LK_NBLCK")
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")        # at least one byte must exist for msvcrt.locking to cover
        os.lseek(fd, 0, os.SEEK_SET)    # msvcrt.locking locks from the CURRENT position: every process locks byte 0
        try:
            msvcrt.locking(fd, nb if nb is not None else msvcrt.LK_LOCK, 1)
            return "msvcrt"
        except OSError as exc:
            if nb is not None and exc.errno in _LOCK_BUSY:
                raise _Busy() from None
            raise
    return ""


def _log_unlocked(exc: "LockTimeout") -> None:
    """One line per lock file and process in hooks.log: a hook went on without a lock it could not get."""
    key = str(exc.lock_path)
    if key in _LOGGED_BUSY:
        return
    _LOGGED_BUSY.add(key)
    try:
        from . import paths
        who = f"pid {exc.holder['pid']} ({exc.holder.get('cmd')})" if exc.holder.get("pid") else "an unknown process"
        logs = paths.logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "hooks.log", "a", encoding="utf-8") as out:
            out.write(f"{now_iso()} lock busy for {exc.waited:.1f}s ({exc.lock_path}, held by {who}); "
                      f"continuing without the lock\n")
    except Exception:
        pass


def _acquire(fh: Any, lock_path: Path, budget: float, proceed: bool, shared: bool = False) -> str:
    """Poll the lock without ever blocking in the kernel: a holder that is frozen or wedged cannot hang the caller.
    Short growing naps with jitter; a holder that dies drops its flock and is noticed within one nap."""
    started = _clock()
    delay = 0.002
    while True:
        try:
            return _lock_attempt(fh, shared) if shared else _lock_attempt(fh)
        except _Busy:
            waited = _clock() - started
            if waited >= budget:
                exc = LockTimeout(lock_path, waited, _read_holder(lock_path))
                if proceed:
                    _log_unlocked(exc)
                    return ""
                raise exc from None
            _pause(min(delay * random.uniform(0.75, 1.25), budget - waited))
            delay = min(delay * 1.6, 0.05)


@contextlib.contextmanager
def file_lock(path: Path, timeout: Optional[float] = None, on_timeout: Optional[str] = None,
              shared: bool = False) -> Iterator[None]:
    """Exclusive advisory lock on <path>.lock (fcntl, else msvcrt 1-byte, else no-op).

    Never waits forever: it polls for `timeout` seconds (default lock_budget(): 2 s in a hook, 10 s in the CLI) and
    then raises LockTimeout, whose message names the holder (every holder writes pid, start time and command into
    the lock file). on_timeout="proceed" goes on WITHOUT the lock instead, with one hooks.log line: the default in a
    hook, whose bookkeeping must not hold the host up. on_timeout="raise" is for writes that must never run unlocked.
    shared=True takes a flock shared lock (several holders at once, excluded only by an exclusive holder): for appenders
    that merely must not overlap a rewrite of the file."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    policy = lock_policy()
    budget = lock_budget(policy) if timeout is None else max(0.0, float(timeout))
    proceed = on_timeout == "proceed" or (on_timeout is None and policy == "hook")
    fh = open(lock_path, "a+b", buffering=0)
    held = ""
    try:
        held = _acquire(fh, lock_path, budget, proceed, shared) if shared else _acquire(fh, lock_path, budget, proceed)
        if held and not (shared and held == "fcntl"):       # shared holders do not overwrite each other's stamp
            _stamp_holder(fh.fileno(), 1 if held == "msvcrt" else 0)
        yield
    finally:
        fd = fh.fileno()
        if held == "fcntl":
            with contextlib.suppress(Exception):
                fcntl.flock(fd, fcntl.LOCK_UN)
        elif held == "msvcrt":
            with contextlib.suppress(Exception):
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        fh.close()


def flock_held_elsewhere(path: Path) -> bool:
    """Does another process hold an exclusive flock on `path`? A transient shared-lock probe; False where there is no
    flock, no such file can be opened, or on any doubt. Nothing is written."""
    if fcntl is None:
        return False
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except Exception:
        return False
    finally:
        os.close(fd)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, limit: Optional[int] = None) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            read = 0
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
                read += len(chunk)
                if limit is not None and read > limit:
                    return None
        return h.hexdigest()
    except (FileNotFoundError, IsADirectoryError, PermissionError, NotADirectoryError):
        return None


_TR_MAP = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
                         "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"})


def slugify(text: str, max_len: int = 40) -> str:
    src = text or ""
    text = src.translate(_TR_MAP)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    if not text:
        digest = hashlib.sha1((src or "").encode("utf-8")).hexdigest()[:6]
        return f"feature-{digest}" if src.strip() else "feature"
    return text


def norm_lower(text: str) -> str:
    """Lower-case that behaves for Turkish input (drops the combining dot of İ)."""
    return (text or "").lower().replace("̇", "")


def tail_lines(text: str, n: int = 40, max_chars: int = 4000) -> str:
    lines = text.splitlines()[-n:]
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[-max_chars:]
    return out


def is_relative_to(path: Path, base: Path) -> bool:
    try:
        Path(path).relative_to(base)
        return True
    except ValueError:
        return False


def resolve_path(p: str, cwd: Optional[str] = None) -> Path:
    path = Path(os.path.expanduser(p))
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    # resolve symlinks where possible, but never fail on non-existent files
    try:
        return path.resolve(strict=False)
    except Exception:  # pragma: no cover
        return Path(os.path.abspath(str(path)))


def hook_python() -> str:
    """The interpreter that hook command lines (Cursor/Copilot hooks.json) and PATH shims bake in at install time.

    POSIX: not a virtualenv's python (it disappears with the venv): its base, else `python3` on PATH, else this
    interpreter. Windows: `python3` is usually absent, or is the Microsoft Store stub that opens the Store instead of
    running anything, so the interpreter running this code is used (its base python.exe when inside a venv)."""
    venv = getattr(sys, "base_prefix", sys.prefix) != sys.prefix
    if _IS_WINDOWS:
        if venv:
            base = Path(sys.base_prefix) / "python.exe"
            if base.exists():
                return str(base)
        return sys.executable or shutil.which("python") or shutil.which("py") or "python"
    if venv:
        base = Path(sys.base_prefix) / "bin" / "python3"
        if base.exists():
            return str(base)
    return shutil.which("python3") or sys.executable or "python3"
