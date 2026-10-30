"""Localization for everything a HUMAN reads (English + Turkish).

Scope: dashboards, `watch`, activity lines, run artifacts (templates), CLI status text. Model-facing protocol text
(`orkestra next` hints, guard messages, skills, agents) stays English, and the user-intent vocabulary (`intent.py`)
stays bilingual on purpose: a Turkish user can say "commitle", an English user "commit this".

Language resolution, first hit wins:
    0. use_lang(code)            (one thread, for the length of a `with` block: the bridge answers one request in the
                                  language that request asked for)
    1. env  ORKESTRA_LANG        (en | tr | auto)
    2. file ~/.orkestra/config.json  "lang"   (en | tr | auto)
    3. the OS locale: LC_ALL, LC_MESSAGES, LANG, LANGUAGE (a `tr…` locale selects Turkish); on Windows the
       user locale name when none of those is set
    4. English

Catalogs live in `orkestra_core/messages/<area>.py` as `EN` / `TR` dicts of `"area.key": "text with {named} fields"`.
Rules the test-suite enforces: every key exists in both languages, with the same `{fields}`; the English text holds
no Turkish letters; nothing but the catalogs, `intent.py` and `util.py` may contain Turkish letters.

Stdlib only; importing this module never touches the disk.
"""
from __future__ import annotations

import contextlib
import functools
import importlib
import os
import re
import string
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

SUPPORTED: Tuple[str, ...] = ("en", "tr")
DEFAULT = "en"
AREAS: Tuple[str, ...] = ("common", "dispatch", "activity", "watchdog", "monitor", "tempo", "evidence", "runs", "cli",
                          "help", "bridge", "protocol", "install", "risk")
_LOCALE_VARS = ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE")
_NAMES = {"en": "en", "english": "en", "ingilizce": "en", "tr": "tr", "turkish": "tr", "turkce": "tr", "türkçe": "tr"}

_catalogs: Optional[Dict[str, Dict[str, str]]] = None


def normalize(code: Any) -> Optional[str]:
    """`tr`, `TR`, `tr_TR.UTF-8`, `tr-TR`, `Turkish` → `tr`; `en_US.UTF-8` → `en`; anything else (`auto`, `C`, `de`) → None."""
    if not isinstance(code, str):
        return None
    raw = code.strip().lower()
    if not raw:
        return None
    if raw in _NAMES:
        return _NAMES[raw]
    head = re.split(r"[._@:\-]", raw, maxsplit=1)[0]
    return _NAMES.get(head) if head in ("en", "tr") else None


def _from_locale(env: Dict[str, str]) -> Optional[str]:
    for var in _LOCALE_VARS:
        value = env.get(var) or ""
        for part in value.split(":"):               # LANGUAGE is a colon list: "tr:en"
            code = normalize(part)
            if code:
                return code
    return None


@functools.lru_cache(maxsize=1)
def _windows_locale_name() -> str:
    """The Windows user locale name (`tr-TR`), or "" anywhere else or when it cannot be read. Windows sets none of
    the POSIX locale variables, so without this a Turkish Windows would always get English."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(85)                      # LOCALE_NAME_MAX_LENGTH
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, len(buf)):    # type: ignore[attr-defined]
            return buf.value
    except Exception:
        pass
    return ""


def _from_os(env: Optional[Dict[str, str]], posix: Dict[str, str]) -> Optional[str]:
    """The OS locale: the POSIX variables first, then the Windows user locale. The Windows lookup is for the real
    process environment only (never a caller's `env` argument) and only when no POSIX locale variable is defined at
    all: even an empty `LANG=` means "this is a POSIX shell, answer from the variables"."""
    found = _from_locale(posix)
    if found or env is not None or any(var in os.environ for var in _LOCALE_VARS):
        return found
    return normalize(_windows_locale_name())


_config_cache: Dict[str, Tuple[Any, Optional[str]]] = {}


def _config_lang() -> Optional[str]:
    """`lang` from ~/.orkestra/config.json, read directly (no import cycle with paths; tolerant of a broken or
    missing file). Cached per file mtime: hooks call this on every message."""
    path = Path(os.environ.get("ORKESTRA_HOME") or "~/.orkestra").expanduser() / "config.json"
    try:
        stamp: Any = (path.stat().st_mtime_ns, path.stat().st_size)
    except OSError:
        return None
    hit = _config_cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    value: Optional[str] = None
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8-sig"))     # tolerate a BOM (Windows editors)
        value = normalize(data.get("lang")) if isinstance(data, dict) else None
    except Exception:
        value = None
    _config_cache[str(path)] = (stamp, value)
    return value


_here = threading.local()


@contextlib.contextmanager
def use_lang(code: Any) -> Iterator[None]:
    """Answer in `code` (`en` | `tr`, any spelling `normalize` knows) for the calling THREAD only, for the length of
    the `with` block. A long-running server (the bridge) uses it to answer one request in the language that request
    asked for while other requests, and the process itself, keep theirs. None or an unsupported value changes nothing."""
    previous = getattr(_here, "code", None)
    _here.code = normalize(code) or previous
    try:
        yield
    finally:
        _here.code = previous


def lang(env: Optional[Dict[str, str]] = None) -> str:
    """The language for human-facing text in this process (see the module docstring for the order)."""
    here = getattr(_here, "code", None)
    if here and env is None:                        # `use_lang` of this thread beats every other source
        return here
    e = os.environ if env is None else env
    forced = normalize(e.get("ORKESTRA_LANG"))
    if forced:
        return forced
    if str(e.get("ORKESTRA_LANG") or "").strip().lower() not in ("", "auto"):
        return DEFAULT                              # an explicit but unsupported value (e.g. "de") never falls through
    return _config_lang() or _from_os(env, {v: e[v] for v in _LOCALE_VARS if v in e}) or DEFAULT


def job_locale() -> str:
    """A `LANG` value for a background job (a LaunchAgent) that speaks the language of this process. launchd starts jobs
    without the user's locale, so a Turkish user would get English notifications from them. `lang` saved in
    config.json and `ORKESTRA_LANG` still win over it when they are set for the job."""
    return "tr_TR.UTF-8" if lang() == "tr" else "en_US.UTF-8"


def lang_source(env: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    """`(language, where it came from)`: `env` | `config` | `locale` | `default`. Same order as `lang()`, which it
    mirrors on purpose (tests pin `lang_source(e)[0] == lang(e)`); used by `orkestra lang show` and `doctor`."""
    here = getattr(_here, "code", None)
    if here and env is None:
        return here, "request"
    e = os.environ if env is None else env
    forced = normalize(e.get("ORKESTRA_LANG"))
    if forced:
        return forced, "env"
    if str(e.get("ORKESTRA_LANG") or "").strip().lower() not in ("", "auto"):
        return DEFAULT, "env"
    configured = _config_lang()
    if configured:
        return configured, "config"
    located = _from_os(env, {v: e[v] for v in _LOCALE_VARS if v in e})
    if located:
        return located, "locale"
    return DEFAULT, "default"


def _load() -> Dict[str, Dict[str, str]]:
    global _catalogs
    if _catalogs is None:
        out: Dict[str, Dict[str, str]] = {code: {} for code in SUPPORTED}
        for area in AREAS:
            mod = importlib.import_module(f"{__package__}.messages.{area}")
            for code in SUPPORTED:
                out[code].update(getattr(mod, code.upper(), {}))
        _catalogs = out
    return _catalogs


_by_area: Dict[str, Dict[str, Dict[str, str]]] = {}


def _table(key: str) -> Dict[str, Dict[str, str]]:
    """The catalogs `t(key)` reads. Keys are `<area>.<name>` and each area module holds only its own keys, so a hook
    loads the one or two areas it speaks from (~0.1 ms each) instead of all 13 (~1.5 ms). A fully loaded (or
    replaced) table is always preferred; a key outside the known areas gets the full table."""
    if _catalogs is not None:
        return _catalogs
    area = key.partition(".")[0]
    if area not in AREAS:
        return _load()
    got = _by_area.get(area)
    if got is None:
        mod = importlib.import_module(f"{__package__}.messages.{area}")
        got = _by_area[area] = {code: getattr(mod, code.upper(), {}) for code in SUPPORTED}
    return got


def catalog(code: str) -> Dict[str, str]:
    """The full message table for one language (a copy)."""
    return dict(_load()[code])


def fields(template: str) -> List[str]:
    """The `{named}` fields a message uses, sorted (used by the parity test)."""
    return sorted({name for _, name, _, _ in string.Formatter().parse(template) if name})


def t(key: str, /, **kw: Any) -> str:
    """Message `key` in the current language. Missing in that language → English → the key itself; a template
    that cannot be formatted with `kw` is returned unformatted (a log line must never crash a hook)."""
    cats = _table(key)
    text = cats.get(lang(), {}).get(key)
    if text is None:
        text = cats[DEFAULT].get(key, key)
    if not kw:
        return text
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        return text


def pick(en: str, tr: str) -> str:
    """Inline choice for the rare one-off string that does not deserve a catalog key."""
    return tr if lang() == "tr" else en


def plural(n: int, one: str, many: str) -> str:
    """`1 task` / `3 tasks`; both languages use the singular form for 1."""
    return one if n == 1 else many


def duration(seconds: Optional[float], sec_until: int = 90) -> str:
    """Compact human duration in the current language: `45s` · `12m` · `2h 5m` (English), `45 sn` · `12 dk` · `2 sa 5 dk`
    (Turkish; this is the format `orkestra watch` always printed). Seconds are shown below `sec_until` (90 by default,
    capped at one hour), whole minutes up to an hour, then hours and minutes; `?` when unknown."""
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    if s < min(sec_until, 3600):
        return t("tempo.dur_s", n=s)
    if s < 3600:
        return t("tempo.dur_m", n=s // 60)
    return t("tempo.dur_hm", h=s // 3600, m=s % 3600 // 60)


def ago(seconds: Optional[float], sec_until: int = 90) -> str:
    """`3m ago` / `3 dk önce` (`?` when unknown)."""
    return "?" if seconds is None else t("common.ago", dur=duration(seconds, sec_until))


# ------------------------------------------------------------------ protocol headings (shared by parsers + templates)

def heading(section_id: str, code: Optional[str] = None) -> str:
    """The heading text of a plan/spec/request section in `code` (default: the current language)."""
    from .messages import protocol
    pair = protocol.SECTIONS[section_id]
    return pair[1] if (code or lang()) == "tr" else pair[0]


def heading_aliases(section_id: str) -> Tuple[str, ...]:
    """Every accepted spelling of a section heading (all languages): parsers match any of them."""
    from .messages import protocol
    return tuple(protocol.SECTIONS[section_id])


def template_dir(templates_root: Path, code: Optional[str] = None) -> Path:
    """`templates/<lang>/` when it exists, else `templates/en/`, else the root (pre-localisation layout)."""
    for candidate in (templates_root / (code or lang()), templates_root / DEFAULT):
        if candidate.is_dir():
            return candidate
    return templates_root


def all_langs() -> Iterable[str]:
    return SUPPORTED
