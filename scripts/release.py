#!/usr/bin/env python3
"""Prepare an Orkestra release locally (standard library only). It never commits, tags, pushes or publishes.

    python3 scripts/release.py 1.7.0              dry run: show what would change (the default)
    python3 scripts/release.py 1.7.0 --apply      bump the version everywhere, date the CHANGELOG, build the archive
    python3 scripts/release.py --check            only check that every file carrying the version agrees

The version has one source of truth, `orkestra_core.__version__`; every other file listed in CARRIERS is rewritten
from the number given on the command line and `--check` (also run by the test-suite) fails on any drift. At the end it
PRINTS the `gh release create` command; running it is the owner's decision. Human text comes from the shared catalogs
(`install.release_*`), so it follows ORKESTRA_LANG.
"""
import argparse
import datetime
import gzip
import hashlib
import io
import os
import re
import shlex
import subprocess
import sys
import tarfile

HERE = os.path.dirname(os.path.realpath(__file__))
DEFAULT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(DEFAULT_ROOT, "plugins", "orkestra", "lib"))
from orkestra_core import i18n  # noqa: E402

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
NUM = r"(\d+\.\d+\.\d+)"
# (repo-relative path, regex whose group 1 is the version). Plugin and marketplace manifests carry no version on
# purpose (test_docs_drift pins that), so they are not listed.
CARRIERS = (
    ("plugins/orkestra/lib/orkestra_core/__init__.py", r'(?m)^__version__ = "' + NUM + '"'),
    ("pyproject.toml", r'(?m)^version = "' + NUM + '"'),
    ("README.md", r"(?m)^Version " + NUM + r" ·"),
    ("README.tr.md", r"(?m)^Sürüm " + NUM + r" ·"),
    ("ORKESTRA.md", r"\(version " + NUM + ","),
    ("ORKESTRA.tr.md", r"\(sürüm " + NUM + ","),
    ("docs/architecture.html", r'<p class="meta">Version ' + NUM + r" ·"),
    ("docs/orkestra-mimari.html", r'<p class="meta">Sürüm ' + NUM + r" ·"),
    (".github/ISSUE_TEMPLATE/bug_report.yml", r"placeholder: orkestra " + NUM),
    (".github/ISSUE_TEMPLATE/feature_request.yml", r"placeholder: orkestra " + NUM),
)
SOURCE = CARRIERS[0][0]


class ReleaseError(Exception):
    pass


def t(key, **kw):
    return i18n.t("install.release_" + key, **kw)


def read(root, rel):
    with io.open(os.path.join(root, rel), encoding="utf-8", newline="") as fh:
        return fh.read()


def write(root, rel, text):
    with io.open(os.path.join(root, rel), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def versions(root):
    """{path: version} for every carrier; a missing file or pattern is a ReleaseError."""
    found = {}
    for rel, pattern in CARRIERS:
        try:
            text = read(root, rel)
        except OSError:
            raise ReleaseError(t("carrier_missing", path=rel))
        hit = re.search(pattern, text)
        if not hit:
            raise ReleaseError(t("carrier_missing", path=rel))
        found[rel] = hit.group(1)
    return found


def check(root):
    """The one version (that of the source of truth), or ReleaseError naming the first file that disagrees."""
    found = versions(root)
    expected = found[SOURCE]
    for rel, value in found.items():
        if value != expected:
            raise ReleaseError(t("drift", path=rel, found=value, expected=expected))
    return expected


def newer(new, old):
    key = lambda v: tuple(int(p) for p in v.split("."))  # noqa: E731
    return key(new) > key(old)


def bumped(root, new):
    """{path: (old, new text)} with the version replaced in every carrier."""
    out = {}
    for rel, pattern in CARRIERS:
        text = read(root, rel)
        hit = re.search(pattern, text)
        out[rel] = (hit.group(1), text[:hit.start(1)] + new + text[hit.end(1):])
    return out


UNRELEASED = re.compile(r"(?m)^## Unreleased[ \t]*\r?\n")
NEXT_SECTION = re.compile(r"(?m)^## ")


def release_changelog(text, version, date):
    """(new text, release notes): the Unreleased body moves under a dated heading, a new empty Unreleased opens."""
    head = UNRELEASED.search(text)
    if not head:
        raise ReleaseError(t("no_unreleased"))
    following = NEXT_SECTION.search(text, head.end())
    end = following.start() if following else len(text)
    body = text[head.end():end].strip("\r\n")
    if not body.strip():
        raise ReleaseError(t("no_unreleased"))
    new = (text[:head.start()] + "## Unreleased\n\n## %s - %s\n" % (version, date) + "\n" + body + "\n\n" + text[end:])
    return new.rstrip("\n") + "\n", body.strip() + "\n"


def git(root, *args):
    try:
        cp = subprocess.run(["git", "-C", root] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, universal_newlines=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError(t("not_git", root=root))
    if cp.returncode != 0:
        raise ReleaseError(t("not_git", root=root))
    return cp.stdout


def repo_slug(root):
    """owner/name from the origin remote, or ''."""
    try:
        url = git(root, "remote", "get-url", "origin").strip()
    except ReleaseError:
        return ""
    hit = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url)
    return hit.group(1) if hit else ""


def tracked_files(root):
    names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    return sorted(n for n in set(names) if n and os.path.lexists(os.path.join(root, n)))


def build_archive(root, version, out_dir):
    """dist/orkestra-X.tar.gz (deterministic: sorted, fixed mtime and owner) and SHA256SUMS; returns both paths."""
    os.makedirs(out_dir, exist_ok=True)
    name = "orkestra-%s" % version
    archive = os.path.join(out_dir, name + ".tar.gz")
    with open(archive, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for rel in tracked_files(root):
                    path = os.path.join(root, rel)
                    if os.path.realpath(path).startswith(os.path.realpath(out_dir) + os.sep):
                        continue
                    info = tar.gettarinfo(path, arcname=name + "/" + rel)
                    info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
                    info.mode = 0o755 if info.mode & 0o111 else 0o644
                    if info.isreg():
                        with open(path, "rb") as fh:
                            tar.addfile(info, fh)
                    else:
                        tar.addfile(info)
    digest = hashlib.sha256()
    with open(archive, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    sums = os.path.join(out_dir, "SHA256SUMS")
    with io.open(sums, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("%s  %s\n" % (digest.hexdigest(), os.path.basename(archive)))
    return archive, sums


def gh_command(slug, version, archive, sums, notes):
    argv = ["gh", "release", "create", "v" + version, archive, sums, "--title", "Orkestra " + version,
            "--notes-file", notes, "--draft"]
    if slug:
        argv += ["--repo", slug]
    return " ".join(shlex.quote(a) for a in argv)


def build_parser():
    ap = argparse.ArgumentParser(description=t("description"), add_help=False)
    ap.add_argument("-h", "--help", action="help", help=i18n.t("help.opt.help"))
    ap.add_argument("version", nargs="?", help=t("help_version"))
    ap.add_argument("--apply", action="store_true", help=t("help_apply"))
    ap.add_argument("--dry-run", action="store_true", help=t("help_dry_run"))
    ap.add_argument("--check", action="store_true", help=t("help_check"))
    ap.add_argument("--allow-dirty", action="store_true", help=t("help_allow_dirty"))
    ap.add_argument("--out", help=t("help_out"))
    ap.add_argument("--date", help=t("help_date"))
    ap.add_argument("--root", default=DEFAULT_ROOT, help=t("help_root"))
    return ap


def run(args, say):
    root = os.path.abspath(args.root)
    if args.apply and args.dry_run:
        raise ReleaseError(t("conflict"))
    current = check(root)
    if args.check:
        say(t("check_ok", count=len(CARRIERS), version=current))
        return 0
    version = args.version
    if not version:
        raise ReleaseError(t("needs_version"))
    if not VERSION_RE.match(version):
        raise ReleaseError(t("bad_version", version=version))
    if not newer(version, current):
        raise ReleaseError(t("not_newer", version=version, current=current))
    if not git(root, "rev-parse", "--is-inside-work-tree").strip() == "true":
        raise ReleaseError(t("not_git", root=root))
    if git(root, "status", "--porcelain").strip() and not args.allow_dirty:
        raise ReleaseError(t("dirty"))
    if git(root, "tag", "--list", "v" + version).strip():
        raise ReleaseError(t("tag_exists", version=version))
    date = args.date or datetime.date.today().isoformat()
    changelog, notes = release_changelog(read(root, "CHANGELOG.md"), version, date)
    changes = bumped(root, version)
    out_dir = os.path.abspath(args.out) if args.out else os.path.join(root, "dist")
    archive = os.path.join(out_dir, "orkestra-%s.tar.gz" % version)
    sums = os.path.join(out_dir, "SHA256SUMS")
    notes_path = os.path.join(out_dir, "release-notes-%s.md" % version)
    apply = args.apply
    for rel, (old, text) in changes.items():
        say(t("did_bump" if apply else "plan_bump", path=rel, old=old, new=version))
        if apply:
            write(root, rel, text)
    say(t("did_changelog" if apply else "plan_changelog", version=version, date=date))
    if apply:
        write(root, "CHANGELOG.md", changelog)
        os.makedirs(out_dir, exist_ok=True)
        with io.open(notes_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(notes)
        build_archive(root, version, out_dir)
    say(t("did_archive" if apply else "plan_archive", archive=archive, sums=sums))
    if not apply:
        say(t("dry_done"))
    say(t("next_steps"))
    say("    " + gh_command(repo_slug(root), version, archive, sums, notes_path))
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args, lambda line: print(line, flush=True))
    except ReleaseError as exc:
        print("release: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
