#!/usr/bin/env python3
"""Cross-platform Orkestra installer (standard library only). Prefer this on Windows; Unix may also use install.sh.

    python install.py                      install: tests, engine setup, PATH shim, health check
    python install.py --dry-run            print the plan, change nothing
    python install.py --uninstall          remove what this installer set up (plugin registration, Cursor and Copilot
                                           hooks, PATH shim); ~/.orkestra (settings, runs, bridge token) is kept
    python install.py --uninstall --purge  ...and delete ~/.orkestra too (asks first, unless --yes)

Safe to run again: every step is idempotent. Messages follow ORKESTRA_LANG, then the OS locale (English or Turkish).

This file deliberately has no `from __future__ import annotations` (a SyntaxError before Python 3.7), no
walrus and no Turkish letters in the source (the Turkish texts are written as \\u escapes, so the file is plain ASCII
in any code page): a Python that is too old must reach the version check and print a readable message.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN = ROOT / "plugins" / "orkestra"
BIN = PLUGIN / "bin" / "orkestra"
MIN_PYTHON = (3, 9)
MARKETPLACE = "orkestra-local"
PLUGIN_ID = "orkestra@" + MARKETPLACE

# English / Turkish, one pair per message. Both texts use the same {fields}. No em dash in new text.
_MESSAGES = {
    "help_dry_run": ("print the plan and change nothing", "plan\u0131 yazd\u0131r, hi\u00e7bir \u015fey de\u011fi\u015ftirme"),
    "help_uninstall": ("remove what this installer set up (keeps ~/.orkestra unless --purge)",
                       "bu kurulumun ekledi\u011fi her \u015feyi kald\u0131r (--purge yoksa ~/.orkestra kal\u0131r)"),
    "help_purge": ("with --uninstall: also delete the Orkestra home (settings, run history, bridge token)",
                   "--uninstall ile: Orkestra klas\u00f6r\u00fcn\u00fc de sil (ayarlar, \u00e7al\u0131\u015fma ge\u00e7mi\u015fi, bridge tokeni)"),
    "purge_needs_uninstall": ("--purge only works together with --uninstall",
                              "--purge yaln\u0131z --uninstall ile birlikte \u00e7al\u0131\u015f\u0131r"),
    "dry_banner": ("Dry run: nothing is changed. This is the plan:",
                   "Deneme modu: hi\u00e7bir \u015fey de\u011fi\u015ftirilmez. Plan \u015fu:"),
    "dry_done": ("Dry run finished. Nothing was changed.", "Deneme bitti. Hi\u00e7bir \u015fey de\u011fi\u015fmedi."),
    "step_prereq": ("Prerequisites", "\u00d6n ko\u015fullar"),
    "python_ok": ("Python {version} ({path})", "Python {version} ({path})"),
    "python_old": ("Python {need} or newer is required, but this is Python {version} ({path}). Install a newer "
                   "Python ({hint}) and start this installer with it.",
                   "Python {need} veya \u00fcst\u00fc gerekli, ama bu Python {version} ({path}). Daha yeni bir Python kurun "
                   "({hint}) ve kurulumu onunla ba\u015flat\u0131n."),
    "hint_python_windows": ("winget install Python.Python.3.12, or https://www.python.org/downloads/",
                            "winget install Python.Python.3.12 veya https://www.python.org/downloads/"),
    "hint_python_macos": ("brew install python, or https://www.python.org/downloads/",
                          "brew install python veya https://www.python.org/downloads/"),
    "hint_python_linux": ("for example: sudo apt install python3, or your distribution's package manager",
                          "\u00f6rne\u011fin: sudo apt install python3 veya da\u011f\u0131t\u0131m\u0131n\u0131z\u0131n paket y\u00f6neticisi"),
    "git_missing": ("git was not found on PATH. Install it ({hint}) and run this installer again.",
                    "git PATH i\u00e7inde bulunamad\u0131. Kurun ({hint}) ve kurulumu yeniden \u00e7al\u0131\u015ft\u0131r\u0131n."),
    "hint_git_windows": ("winget install Git.Git, or https://git-scm.com/downloads",
                         "winget install Git.Git veya https://git-scm.com/downloads"),
    "hint_git_macos": ("xcode-select --install, or brew install git", "xcode-select --install veya brew install git"),
    "hint_git_linux": ("for example: sudo apt install git", "\u00f6rne\u011fin: sudo apt install git"),
    "git_ok": ("git ({path})", "git ({path})"),
    "engine_found": ("{name} detected", "{name} bulundu"),
    "engine_none": ("No engine detected yet (Claude Code, Cursor, Copilot or Pi): the PATH shim is still set up.",
                    "Hen\u00fcz motor bulunamad\u0131 (Claude Code, Cursor, Copilot veya Pi): PATH k\u0131sayolu yine de kurulur."),
    "step_tests": ("Running the plugin and bridge test suites (a few minutes; depends on system load)",
                   "Eklenti ve bridge testleri \u00e7al\u0131\u015f\u0131yor (birka\u00e7 dakika; sistem y\u00fck\u00fcne g\u00f6re de\u011fi\u015fir)"),
    "would_tests": ("would run the plugin and bridge test suites", "eklenti ve bridge testleri \u00e7al\u0131\u015ft\u0131r\u0131l\u0131rd\u0131"),
    "tests_skipped": ("Tests skipped (--skip-tests)", "Testler atland\u0131 (--skip-tests)"),
    "tests_failed": ("The tests failed: install stopped. Fix them or use --skip-tests.",
                     "Testler ge\u00e7medi: kurulum durduruldu. D\u00fczeltin ya da --skip-tests kullan\u0131n."),
    "step_setup": ("Setting up the detected engines and the PATH shim", "Bulunan motorlar ve PATH k\u0131sayolu kuruluyor"),
    "step_doctor": ("Health check", "Sa\u011fl\u0131k kontrol\u00fc"),
    "doctor_warned": ("doctor warned about some checks (see above)", "doctor baz\u0131 kontrollerde uyard\u0131 (yukar\u0131ya bak)"),
    "setup_nothing": ("setup found nothing to install (no engine detected)",
                      "setup kuracak bir \u015fey bulamad\u0131 (motor yok)"),
    "would_run": ("would run: {cmd}", "\u00e7al\u0131\u015ft\u0131r\u0131l\u0131rd\u0131: {cmd}"),
    "would_claude": ("would register the Claude Code marketplace {name} and install the plugin {plugin}",
                     "Claude Code marketplace {name} kaydedilirdi ve {plugin} eklentisi kurulurdu"),
    "would_line": ("would {action}", "\u015funu yapard\u0131: {action}"),
    "act_remove": ("remove {path}", "kald\u0131r {path}"),
    "act_delete": ("delete {path}", "sil {path}"),
    "plan_unavailable": ("could not plan the {name} step: {error}", "{name} ad\u0131m\u0131 planlanamad\u0131: {error}"),
    "usage": ("Use it in Claude Code, Cursor or Copilot, in your workspace folder:",
              "Claude Code, Cursor veya Copilot i\u00e7inde, \u00e7al\u0131\u015fma klas\u00f6r\u00fcn\u00fczde kullan\u0131n:"),
    "usage_cli": ("Command line: {cmd}", "Komut sat\u0131r\u0131: {cmd}"),
    "path_hint": ("`orkestra` is not on PATH in this shell yet: open a new terminal, or add {path} to PATH.",
                  "`orkestra` bu kabukta hen\u00fcz PATH i\u00e7inde de\u011fil: yeni bir terminal a\u00e7\u0131n ya da {path} klas\u00f6r\u00fcn\u00fc PATH'e ekleyin."),
    "step_uninstall": ("Removing what this installer set up", "Bu kurulumun ekledikleri kald\u0131r\u0131l\u0131yor"),
    "claude_plugin_removed": ("Claude Code plugin {plugin} removed", "Claude Code eklentisi {plugin} kald\u0131r\u0131ld\u0131"),
    "claude_market_removed": ("Claude Code marketplace {name} removed", "Claude Code marketplace {name} kald\u0131r\u0131ld\u0131"),
    "claude_failed": ("could not remove {what} from Claude Code. Run it yourself: {cmd}",
                      "{what} Claude Code'dan kald\u0131r\u0131lamad\u0131. Kendiniz \u00e7al\u0131\u015ft\u0131r\u0131n: {cmd}"),
    "launchd_removed": ("bridge LaunchAgent removed ({path})", "bridge LaunchAgent kald\u0131r\u0131ld\u0131 ({path})"),
    "uninstall_nothing": ("Nothing to remove: Orkestra is not installed here.",
                          "Kald\u0131r\u0131lacak bir \u015fey yok: Orkestra burada kurulu de\u011fil."),
    "uninstall_done": ("Orkestra was uninstalled.", "Orkestra kald\u0131r\u0131ld\u0131."),
    "home_kept": ("Kept {path} (settings, run history, bridge token). Add --purge to delete it too.",
                  "{path} korundu (ayarlar, \u00e7al\u0131\u015fma ge\u00e7mi\u015fi, bridge tokeni). Onu da silmek i\u00e7in --purge ekleyin."),
    "purge_ask": ("Delete {path} with all settings, run history and the bridge token? This cannot be undone.",
                  "{path} t\u00fcm ayarlar\u0131, \u00e7al\u0131\u015fma ge\u00e7mi\u015fi ve bridge tokeniyle silinsin mi? Geri al\u0131namaz."),
    "purge_removed": ("{path} deleted", "{path} silindi"),
    "purge_declined": ("{path} kept", "{path} korundu"),
    "purge_projects": ("Per-project .orkestra folders inside your projects were not touched.",
                       "Projelerinizin i\u00e7indeki .orkestra klas\u00f6rlerine dokunulmad\u0131."),
    "purge_failed": ("could not delete {path}: {error}", "{path} silinemedi: {error}"),
    "purge_refused": ("{path} was not deleted: {why}. Delete it yourself if you are sure it is the right folder.",
                      "{path} silinmedi: {why}. Do\u011fru klas\u00f6r oldu\u011fundan eminseniz kendiniz silin."),
    "purge_why_root": ("it is the root of a drive", "bir s\u00fcr\u00fcc\u00fcn\u00fcn k\u00f6k dizini"),
    "purge_why_user": ("it is your user folder or a folder above it",
                       "kullan\u0131c\u0131 klas\u00f6r\u00fcn\u00fcz ya da onun \u00fcst\u00fcnde bir klas\u00f6r"),
    "purge_why_cwd": ("it is the current directory or a folder above it",
                      "ge\u00e7erli klas\u00f6r ya da onun \u00fcst\u00fcnde bir klas\u00f6r"),
    "purge_why_checkout": ("it contains this Orkestra checkout", "bu Orkestra klonunu i\u00e7eriyor"),
    "purge_why_foreign": ("it does not look like an Orkestra home (no config.json, sessions or bridge folder) and it "
                          "holds other files",
                          "bir Orkestra klas\u00f6r\u00fcne benzemiyor (config.json, sessions ya da bridge klas\u00f6r\u00fc yok) "
                          "ve ba\u015fka dosyalar i\u00e7eriyor"),
    "purge_why_file": ("it is not a folder", "bir klas\u00f6r de\u011fil"),
    "setup_failed": ("`orkestra setup` failed although an engine was detected: at least one engine was not set up. See "
                     "the messages above, fix them and run this installer again.",
                     "Motor bulunmas\u0131na ra\u011fmen `orkestra setup` ba\u015far\u0131s\u0131z oldu: en az bir motor kurulamad\u0131. "
                     "Yukar\u0131daki mesajlara bak\u0131n, d\u00fczeltin ve kurulumu yeniden \u00e7al\u0131\u015ft\u0131r\u0131n."),
    "install_not_ready": ("Orkestra is not ready: an engine was detected, but the health check reported failed required "
                          "checks (see above).",
                          "Orkestra haz\u0131r de\u011fil: bir motor bulundu, ama sa\u011fl\u0131k kontrol\u00fc ba\u015far\u0131s\u0131z gerekli "
                          "kontroller bildirdi (yukar\u0131ya bak\u0131n)."),
    "uninstall_step_failed": ("{name}: could not remove Orkestra ({error}). Nothing of that step was changed. Fix the file "
                              "and run the uninstall again.",
                              "{name}: Orkestra kald\u0131r\u0131lamad\u0131 ({error}). O ad\u0131mda hi\u00e7bir \u015fey de\u011fi\u015fmedi. "
                              "Dosyay\u0131 d\u00fczeltin ve kald\u0131rmay\u0131 yeniden \u00e7al\u0131\u015ft\u0131r\u0131n."),
    "answer_yes": ("y/N", "e/H"),
}

_YES = ("y", "yes", "e", "evet")


# ---------------------------------------------------------------- language and output

def _config_lang():
    """`lang` from <ORKESTRA_HOME>/config.json, or None."""
    home = os.environ.get("ORKESTRA_HOME") or os.path.join(os.path.expanduser("~"), ".orkestra")
    try:
        with open(os.path.join(home, "config.json"), "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        value = data.get("lang") if isinstance(data, dict) else None
        return value if isinstance(value, str) else None
    except (OSError, ValueError):
        return None


def _code(value):
    """'tr', 'en' or None for a language name or locale string ('tr_TR.UTF-8', 'Turkish', 'en-GB' ...)."""
    v = str(value or "").strip().lower()
    for code, names in (("tr", ("turkish", "turkce", "t\u00fcrk\u00e7e")), ("en", ("english", "ingilizce"))):
        if v == code or v in names or (v[:2] == code and v[2:3] in ("_", "-", ".", "@")):
            return code
    return None


def _lang():
    """'tr' or 'en'. Same order as orkestra itself: ORKESTRA_LANG, config.json, the OS locale, English."""
    try:
        if str(PLUGIN / "lib") not in sys.path:
            sys.path.insert(0, str(PLUGIN / "lib"))
        from orkestra_core import i18n
        return "tr" if i18n.lang() == "tr" else "en"
    except Exception:
        pass
    explicit = os.environ.get("ORKESTRA_LANG", "").strip().lower()
    if explicit and explicit != "auto":
        return _code(explicit) or "en"                    # an explicit but unsupported value means English
    cfg = _config_lang()
    if cfg and cfg.strip().lower() != "auto" and _code(cfg):
        return _code(cfg)
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        for part in os.environ.get(var, "").split(":"):
            if _code(part):
                return _code(part)
    return "en"


def _m(key, **fields):
    """The message `key` in the user's language, {fields} filled in."""
    en, tr = _MESSAGES[key]
    text = tr if _lang() == "tr" else en
    try:
        return text.format(**fields)
    except (KeyError, IndexError, ValueError):
        return text


def _t(key, english):
    """Installer text from the shared catalog (ORKESTRA_LANG, config.json, OS locale; English by default). If the
    library cannot be loaded the English fallback is used, so a broken checkout still gets a readable message."""
    try:
        if str(PLUGIN / "lib") not in sys.path:
            sys.path.insert(0, str(PLUGIN / "lib"))
        from orkestra_core import i18n
        text = i18n.t(key)
        return english if text == key else text
    except Exception:
        return english


def _say(text):
    print("==> " + text, flush=True)


def _ok(text):
    print(" ok  " + text, flush=True)


def _warn(text):
    print(" !   " + text, flush=True)


def _plan(text):
    print("  - " + text, flush=True)


def _utf8_streams():
    """A legacy console codec must not turn text into a crash: unencodable characters become '?'. Windows pipes (the
    PowerShell wrapper, CI logs) are read as UTF-8, so there the encoding is forced; a console keeps its own."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if os.name == "nt" and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------- environment

def _which(name):
    return shutil.which(name)


def _python_version():
    return tuple(sys.version_info[:3])


def _platform():
    """'windows', 'macos' or 'linux'."""
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def check_python():
    """None when this Python is new enough, else the message to print."""
    version = _python_version()
    if version[:2] >= MIN_PYTHON:
        return None
    return _m("python_old", need="%d.%d" % MIN_PYTHON, version=".".join(str(p) for p in version),
              path=sys.executable or "python", hint=_m("hint_python_" + _platform()))


def check_git():
    """(path, None) when git is on PATH, else (None, the message to print)."""
    path = _which("git")
    if path:
        return path, None
    return None, _m("git_missing", hint=_m("hint_git_" + _platform()))


def _shim_dir():
    """Where `orkestra` can be found after this install: the PATH shim's folder if there is a shim, else bin/."""
    cur = _kits()[0]
    try:
        shim = cur.shim_path() if cur else None
        if shim and (shim.exists() or shim.is_symlink() or (shim.parent / "orkestra.cmd").exists()):
            return shim.parent
    except Exception:
        pass
    return BIN.parent


def _shown(argv):
    if os.name == "nt":
        return subprocess.list2cmdline([str(a) for a in argv])
    try:
        import shlex
        return " ".join(shlex.quote(str(a)) for a in argv)
    except Exception:
        return " ".join(str(a) for a in argv)


def run(argv, check=True, dry=False):
    """Run a command from the checkout (printed first). In a dry run it is only announced."""
    if dry:
        _plan(_m("would_run", cmd=_shown(argv)))
        return 0
    print("==>", _shown(argv), flush=True)
    cp = subprocess.run([str(a) for a in argv], cwd=str(ROOT))
    if check and cp.returncode != 0:
        raise SystemExit(cp.returncode)
    return cp.returncode


def _capture(argv):
    """Output of a short read-only command ('' when it cannot run)."""
    try:
        cp = subprocess.run([str(a) for a in argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, timeout=120, universal_newlines=True, errors="replace")
        return cp.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _kits():
    """(cursorkit, copilotkit, pikit) from this checkout, or (None, None, None) when the library cannot be loaded."""
    try:
        if str(PLUGIN / "lib") not in sys.path:
            sys.path.insert(0, str(PLUGIN / "lib"))
        from orkestra_core import copilotkit, cursorkit, pikit
        return cursorkit, copilotkit, pikit
    except Exception as exc:
        _warn(_m("plan_unavailable", name="orkestra_core", error=str(exc)))
        return None, None, None


def _engines():
    """{'Claude Code': bool, 'Cursor': bool, 'Copilot': bool, 'Pi': bool}, detected the way `orkestra setup` does."""
    cur, cop, pik = _kits()
    return {
        "Claude Code": bool(_which("claude")),
        "Cursor": bool(cur and cur.cursor_home().is_dir()),
        "Copilot": bool(cop and cop.copilot_home().is_dir()),
        "Pi": bool(pik and pik.detected()),
    }


# ---------------------------------------------------------------- install

def _plan_install(args):
    """What an install would do. Reads only: the libraries' own dry-run modes list the file changes."""
    _plan(_m("tests_skipped") if args.skip_tests else _m("would_tests"))
    found = _engines()
    cur, cop, pik = _kits()
    _plan(_m("would_run", cmd=_shown([sys.executable, BIN, "setup"] + (["--yes"] if args.yes else []))))
    if found["Claude Code"]:
        _plan(_m("would_claude", name=MARKETPLACE, plugin=PLUGIN_ID))
    for name, kit, call in (("Cursor", cur, "install"), ("Copilot", cop, "install"), ("Pi", pik, "install")):
        if not (kit and found[name]):
            continue
        try:
            for action in getattr(kit, call)(dry=True):
                _plan(_m("would_line", action=action))
        except Exception as exc:
            _warn(_m("plan_unavailable", name=name, error=str(exc)))
    if cur:
        try:
            for action in cur.ensure_path_shim(dry=True):
                _plan(_m("would_line", action=action))
        except Exception as exc:
            _warn(_m("plan_unavailable", name="PATH shim", error=str(exc)))
    _plan(_m("would_run", cmd=_shown([sys.executable, BIN, "doctor"])))


def install(args):
    dry = args.dry_run
    if dry:
        print(_m("dry_banner"), flush=True)
    _say(_m("step_prereq"))
    _ok(_m("python_ok", version=".".join(str(p) for p in _python_version()), path=sys.executable))
    git, problem = check_git()
    if problem:
        print(problem, file=sys.stderr)
        return 2
    _ok(_m("git_ok", path=git))
    found = _engines()
    for name in sorted(n for n, present in found.items() if present):
        _ok(_m("engine_found", name=name))
    if not any(found.values()):
        _warn(_m("engine_none"))
    if dry:
        _plan_install(args)
        print(_m("dry_done"), flush=True)
        return 0

    py = sys.executable
    if not args.skip_tests:
        _say(_m("step_tests"))
        for suite in (PLUGIN / "tests", ROOT / "bridge" / "tests"):
            if run([py, "-m", "unittest", "discover", "-s", str(suite), "-q"], check=False) != 0:
                print(_m("tests_failed"), file=sys.stderr)
                return 1
    else:
        _warn(_m("tests_skipped"))
    # PATH shim + engine setup (detect-only via setup --yes)
    _say(_m("step_setup"))
    setup = [py, str(BIN), "setup"]
    if args.yes:
        setup.append("--yes")
    problems = []                                           # failures that must reach the exit code (CI, provisioning)
    if run(setup, check=False) != 0:
        if any(found.values()):
            problems.append(_m("setup_failed"))
        else:
            _warn(_m("setup_nothing"))
    _say(_m("step_doctor"))
    if run([py, str(BIN), "doctor"], check=False) != 0:
        _warn(_m("doctor_warned"))
        if any(found.values()) and not problems:
            problems.append(_m("install_not_ready"))
    if os.name == "nt":                                     # LaunchAgent / launchd is macOS-only: skipped here
        print(_t("install.windows_note", "Note: LaunchAgent / launchd steps are macOS-only and were skipped."))
    if not (_which("orkestra") or _which("orkestra.cmd")):
        _warn(_m("path_hint", path=_shim_dir()))
    print(_m("usage"))
    print("    /orkestra:run <request>    /orkestra:status    /orkestra:resume")
    print(_m("usage_cli", cmd=_shown([py, str(BIN), "doctor"])))
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(_t("install.finished", "Orkestra install.py finished."))
    return 0


# ---------------------------------------------------------------- uninstall

def _confirm(question, yes):
    """True for --yes or an affirmative answer; no terminal (EOF) means no."""
    if yes:
        return True
    try:
        answer = input("%s [%s] " % (question, _m("answer_yes"))).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in _YES


def _remove_claude(dry):
    """Unregister the Claude Code plugin and marketplace this installer added. Returns whether anything was found."""
    claude = _which("claude")
    if not claude:
        return False
    found = False
    plugins = _capture([claude, "plugin", "list"])
    if PLUGIN_ID in plugins or ("orkestra" in plugins and MARKETPLACE in plugins):
        found = True
        cmd = [claude, "plugin", "uninstall", PLUGIN_ID]
        if dry:
            _plan(_m("would_run", cmd=_shown(cmd)))
        elif run(cmd, check=False) == 0:
            _ok(_m("claude_plugin_removed", plugin=PLUGIN_ID))
        else:
            _warn(_m("claude_failed", what=PLUGIN_ID, cmd=_shown(cmd)))
    if MARKETPLACE in _capture([claude, "plugin", "marketplace", "list"]):
        found = True
        cmd = [claude, "plugin", "marketplace", "remove", MARKETPLACE]
        if dry:
            _plan(_m("would_run", cmd=_shown(cmd)))
        elif run(cmd, check=False) == 0:
            _ok(_m("claude_market_removed", name=MARKETPLACE))
        else:
            _warn(_m("claude_failed", what=MARKETPLACE, cmd=_shown(cmd)))
    return found


def _remove_launch_agent(dry):
    """macOS only: the bridge LaunchAgent that install.sh may have created. Returns whether it was there."""
    if sys.platform != "darwin":
        return False
    plist = Path(os.path.expanduser("~")) / "Library" / "LaunchAgents" / "com.orkestra.bridge.plist"
    try:
        mine = plist.is_file() and "orkestra" in plist.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        mine = False
    if not mine:
        return False
    if dry:
        _plan(_m("would_line", action=_m("act_remove", path=plist)))
        return True
    if _which("launchctl"):
        subprocess.run(["launchctl", "unload", str(plist)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        plist.unlink()
        _ok(_m("launchd_removed", path=plist))
    except OSError as exc:
        _warn(_m("plan_unavailable", name="LaunchAgent", error=str(exc)))
    return True


# What an Orkestra home holds (paths.py in the library). --purge deletes a folder only if it has one of these, or is empty.
_HOME_MARKERS = ("config.json", "bridge.secret.json", "active-runs.json", "watchdog.json", "sessions", "grants", "logs",
                 "bridge", "cursor")


def _orkestra_home():
    """The Orkestra home as `orkestra` itself reads it: `~` is expanded and a relative value is made absolute."""
    return Path(os.path.abspath(os.path.expanduser(os.environ.get("ORKESTRA_HOME") or os.path.join("~", ".orkestra"))))


def _delete_home(home):
    """Delete the Orkestra home. shutil.rmtree refuses a symlink, and a symlinked home (a dotfiles setup) is common: the
    folder it points to (already checked by _purge_blocker) is deleted, then the link itself."""
    if os.path.islink(str(home)):
        target = os.path.realpath(str(home))
        if os.path.isdir(target):
            shutil.rmtree(target)
        try:
            os.unlink(str(home))
        except OSError:
            os.rmdir(str(home))                             # a directory link on Windows
        return
    shutil.rmtree(str(home))


def _purge_blocker(home):
    """Why --purge must not delete `home` (a translated reason), or '' when it is safe. ORKESTRA_HOME is whatever the
    environment says, so the folder has to look like an Orkestra home and must not be the root of a drive, your user
    folder, the current directory (or a folder above either), or the folder this checkout lives in."""
    def real(p):
        return Path(os.path.realpath(str(p)))

    def inside(inner, outer):                                # inner is outer or lies below it
        return inner == outer or outer in inner.parents

    target = real(home)
    if target == Path(target.anchor):
        return _m("purge_why_root")
    if inside(real(os.path.expanduser("~")), target):
        return _m("purge_why_user")
    if inside(real(os.getcwd()), target):
        return _m("purge_why_cwd")
    if inside(real(ROOT), target):
        return _m("purge_why_checkout")
    if not home.is_dir():
        return _m("purge_why_file")
    try:
        entries = list(home.iterdir())
    except OSError:
        entries = []
    if entries and not any((home / marker).exists() for marker in _HOME_MARKERS):
        return _m("purge_why_foreign")
    return ""


def uninstall(args):
    dry = args.dry_run
    if dry:
        print(_m("dry_banner"), flush=True)
    _say(_m("step_uninstall"))
    anything = False
    failed = False
    anything = _remove_claude(dry) or anything
    cur, cop, _ = _kits()
    for name, kit in (("Cursor", cur), ("Copilot", cop)):
        if kit is None:
            continue
        try:
            actions = kit.uninstall(dry=dry)
        except Exception as exc:                            # a hooks.json we do not understand: nothing was changed
            if dry:
                _warn(_m("plan_unavailable", name=name, error=str(exc)))
            else:
                _warn(_m("uninstall_step_failed", name=name, error=str(exc)))
            failed = True                                   # ...but Orkestra is still registered there: no "uninstalled"
            continue
        for action in actions:
            (_plan(_m("would_line", action=action)) if dry else _ok(action))
        anything = anything or bool(actions)
    anything = _remove_launch_agent(dry) or anything

    home = _orkestra_home()
    if args.purge:
        if home.exists():
            anything = True
            blocker = _purge_blocker(home)
            if blocker:                                     # never delete a folder that is not clearly Orkestra's
                _warn(_m("purge_refused", path=home, why=blocker))
                failed = True
            elif dry:
                _plan(_m("would_line", action=_m("act_delete", path=home)))
            elif _confirm(_m("purge_ask", path=home), args.yes):
                try:
                    _delete_home(home)
                    _ok(_m("purge_removed", path=home))
                except OSError as exc:
                    _warn(_m("purge_failed", path=home, error=str(exc)))
                    failed = True
            else:
                _warn(_m("purge_declined", path=home))
        print(_m("purge_projects"))
    elif home.exists():
        print(_m("home_kept", path=home))
    if failed:
        return 1                                            # what was asked for did not happen: no "uninstalled"
    if dry:
        print(_m("dry_done"), flush=True)
    elif anything:
        print(_m("uninstall_done"))
    else:
        print(_m("uninstall_nothing"))
    return 0


# ---------------------------------------------------------------- entry point

def build_parser():
    """The argument parser. With the library at hand, `--help`, the usage line and the errors speak the user's language
    (the same parser classes `orkestra` itself uses); without it this is plain argparse in English."""
    parser_class, formatter_class, retitle = argparse.ArgumentParser, argparse.HelpFormatter, None
    try:
        if str(PLUGIN / "lib") not in sys.path:
            sys.path.insert(0, str(PLUGIN / "lib"))
        from orkestra_core import cliparse
        parser_class, formatter_class, retitle = cliparse.LocalizedParser, cliparse._Formatter, cliparse._retitle
    except Exception:
        pass
    ap = parser_class(description=_t("install.description", "Install Orkestra (cross-platform)"),
                      formatter_class=formatter_class, add_help=False)
    ap.add_argument("-h", "--help", action="help", help=_t("help.opt.help", "show this help message and exit"))
    ap.add_argument("--yes", "-y", action="store_true", help=_t("install.help_yes", "non-interactive defaults"))
    ap.add_argument("--skip-tests", action="store_true",
                    help=_t("install.help_skip_tests", "skip the plugin and bridge test suites"))
    ap.add_argument("--no-mcp", action="store_true",
                    help=_t("install.help_no_mcp", "accepted and ignored: only install.sh has MCP steps"))
    ap.add_argument("--dry-run", action="store_true", help=_m("help_dry_run"))
    ap.add_argument("--uninstall", action="store_true", help=_m("help_uninstall"))
    ap.add_argument("--purge", action="store_true", help=_m("help_purge"))
    if retitle:
        retitle(ap)
    return ap


def main(argv=None):
    if "--dry-run" in (sys.argv[1:] if argv is None else list(argv)):
        sys.dont_write_bytecode = True                      # before the first import of the library (build_parser loads it)
    _utf8_streams()
    problem = check_python()
    if problem:
        print(problem, file=sys.stderr)
        return 2
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.purge and not args.uninstall:
        ap.error(_m("purge_needs_uninstall"))
    if args.uninstall:
        return uninstall(args)
    return install(args)


if __name__ == "__main__":
    raise SystemExit(main())
