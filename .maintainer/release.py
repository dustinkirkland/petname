#!/usr/bin/env python3
"""
release.py — petname / python-petname / golang-petname coordination

petname (this repo, shell) is the canonical upstream: its
usr/share/petname/{small,medium,large}/*.txt word lists and its README.md
are the single source of truth. python-petname and golang-petname each
embed a copy of the *small* list (petname/english.py, petname.go) and a
copy of the README, previously kept in sync by hand-run
debian/update-wordlists.sh scripts in each of those repos. This script
supersedes running those by hand: it re-derives the embedded copies
straight from this checkout (no network fetch, no push-first required),
runs each repo's own test suite, and — for `release` — walks each dirty
repo through a version bump, debian/changelog entry (via `dch`), commit,
tag, and push.

Usage:
    ./release.py status                    # version / git / wordlist-drift snapshot, read-only
    ./release.py sync    [--repo R]... [--check] [--no-readme]
    ./release.py test    [--repo R]... [--docker]
    ./release.py rc      [--no-docker] [--no-readme]
    ./release.py release [--repo R]... [--docker] [--interactive]

R is one of: petname, python-petname, golang-petname (repeatable; default: all three).
--check (sync only) reports drift without writing files.
--docker (test/release) or the default of `rc` runs the real test suites together in a
throwaway ubuntu:noble container with the full Build-Depends toolchain installed
(ispell/dictd/scowl/go) instead of whatever happens to be on this host.
--interactive additionally confirms the sync/test steps, not just commit/tag/push
(commit/tag/push always confirm, interactive or not).

`rc` is a pre-release gate, not a release-candidate cut: it syncs and runs the full
test suite across all three repos and reports pass/fail — it never commits, tags, or
pushes anything. Run it before `release` to check "if I released right now, would
everything be in sync and passing?"

Sibling repos are found at ../python-petname and ../golang-petname relative to
this repo, or via $PYTHON_PETNAME_SRC / $GOLANG_PETNAME_SRC.

This script does NOT touch PyPI, Snap, PPA, or Debian upload — those remain
manual; `release` prints a reminder checklist for them at the end.
"""

import argparse
import difflib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PETNAME_SRC = Path(__file__).resolve().parent.parent

REPO_ORDER = ["petname", "python-petname", "golang-petname"]

REPOS = {
    "petname": PETNAME_SRC,
    "python-petname": Path(os.environ.get(
        "PYTHON_PETNAME_SRC", PETNAME_SRC.parent / "python-petname")),
    "golang-petname": Path(os.environ.get(
        "GOLANG_PETNAME_SRC", PETNAME_SRC.parent / "golang-petname")),
}

_interactive = False


# ── small helpers ───────────────────────────────────────────────────────────

def die(msg):
    print(f"\n✗  {msg}", file=sys.stderr)
    sys.exit(1)


def banner(msg):
    w = 62
    print(f"\n{'━' * w}\n  {msg}\n{'━' * w}")


def section(msg):
    print(f"\n── {msg} " + "─" * max(0, 58 - len(msg)))


def indent(text, prefix="    "):
    return "\n".join(prefix + line for line in text.rstrip("\n").splitlines())


def confirm(prompt, skippable=False, always=False):
    """Prompt to proceed/skip/abort. Non-interactive auto-proceeds unless always=True."""
    if not _interactive and not always:
        print(f"\n  (auto-proceeding: {prompt[:60]}{'…' if len(prompt) > 60 else ''})")
        return True
    if always and not sys.stdin.isatty():
        die("This step needs an explicit yes from a terminal and stdin is not one.")
    opts = "[y/s/N]" if skippable else "[y/N]"
    ans = input(f"\n{prompt} {opts} ").strip().lower()
    if ans in ("y", "yes"):
        return True
    if skippable and ans in ("s", "skip"):
        print("  (skipped)")
        return False
    die("Aborted.")


def run(cmd, cwd=None, check=True, capture=False, env=None):
    kw = dict(cwd=str(cwd) if cwd else None, text=True, env=env)
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    r = subprocess.run(cmd, check=False, **kw)
    if check and r.returncode != 0:
        cmdstr = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        extra = ""
        if capture:
            extra = f"\n{(r.stdout or '')}{(r.stderr or '')}"
        die(f"command failed ({r.returncode}): {cmdstr}{extra}")
    return r


def preflight():
    section("Preflight: locating repos")
    for name in REPO_ORDER:
        path = REPOS[name]
        if not (path / ".git").is_dir():
            die(f"{name}: not found at {path} (a git checkout is required)\n"
                f"  Override with ${'PYTHON_PETNAME_SRC' if name == 'python-petname' else 'GOLANG_PETNAME_SRC'}"
                if name != "petname" else f"{name}: not found at {path}")
        origin = run(["git", "-C", str(path), "remote", "get-url", "origin"],
                     capture=True, check=False).stdout.strip()
        print(f"  {name:16s} {path}  ({origin})")


# ── canonical word lists (petname repo is the source of truth) ─────────────

def load_canonical_words():
    small = REPOS["petname"] / "usr" / "share" / "petname" / "small"
    words = {}
    for key in ("adjectives", "adverbs", "names"):
        f = small / f"{key}.txt"
        if not f.exists():
            die(f"canonical word list missing: {f}")
        words[key] = f.read_text().split()
    return words


def summarize_diff(key, old_words, new_words):
    old_set, new_set = set(old_words), set(new_words)
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    if not added and not removed:
        print(f"    {key}: unchanged ({len(new_words)} words)")
        return False
    print(f"    {key}: {len(old_words)} → {len(new_words)} words "
          f"(+{len(added)} / -{len(removed)})")
    if added:
        print(f"      + {', '.join(added[:10])}{' …' if len(added) > 10 else ''}")
    if removed:
        print(f"      - {', '.join(removed[:10])}{' …' if len(removed) > 10 else ''}")
    return True


# ── python-petname sync ─────────────────────────────────────────────────────

def _py_list_line(key, words):
    return f'{key} = [' + ", ".join(f'"{w}"' for w in words) + ']'


def sync_python_petname(words, check=False):
    path = REPOS["python-petname"] / "petname" / "english.py"
    text = path.read_text()
    new_text = text
    changed = False
    for key in ("adjectives", "adverbs", "names"):
        pattern = re.compile(rf"^{key} = \[.*\]$", re.MULTILINE)
        m = pattern.search(new_text)
        if not m:
            die(f"sync: could not find '{key} = [...]' in {path}")
        old_words = re.findall(r'"([^"]*)"', m.group(0))
        if summarize_diff(key, old_words, words[key]):
            changed = True
        new_text = pattern.sub(lambda _m: _py_list_line(key, words[key]), new_text, count=1)
    if changed and not check:
        path.write_text(new_text)
    return changed


# ── golang-petname sync ─────────────────────────────────────────────────────

def _go_list_line(key, words, width):
    literal = "{" + ", ".join(f'"{w}"' for w in words) + "}"
    return f"\t{key.ljust(width)}= [...]string{literal}"


def sync_golang_petname(words, check=False):
    path = REPOS["golang-petname"] / "petname.go"
    text = path.read_text()
    new_text = text
    changed = False
    width = len("adjectives") + 1
    for key in ("adjectives", "adverbs", "names"):
        pattern = re.compile(rf"^\t{key}\s*= \[\.\.\.\]string\{{.*\}}$", re.MULTILINE)
        m = pattern.search(new_text)
        if not m:
            die(f"sync: could not find '{key} = [...]string{{...}}' in {path}")
        old_words = re.findall(r'"([^"]*)"', m.group(0))
        if summarize_diff(key, old_words, words[key]):
            changed = True
        new_text = pattern.sub(lambda _m: _go_list_line(key, words[key], width), new_text, count=1)
    if changed and not check:
        path.write_text(new_text)
    return changed


# ── README propagation ──────────────────────────────────────────────────────

def sync_readme(repo_name, check=False):
    src = (REPOS["petname"] / "README.md").read_text()
    dst_path = REPOS[repo_name] / "README.md"
    old = dst_path.read_text() if dst_path.exists() else ""
    if old == src:
        print(f"    README.md: unchanged")
        return False
    diff = list(difflib.unified_diff(
        old.splitlines(), src.splitlines(),
        fromfile=f"{repo_name}/README.md (current)", tofile="petname/README.md (source)",
        lineterm="", n=1))
    print(f"    README.md: {len(diff)} diff line(s)")
    for line in diff[:20]:
        print(f"      {line}")
    if len(diff) > 20:
        print(f"      … ({len(diff) - 20} more)")
    if not check:
        dst_path.write_text(src)
    return True


def do_sync(repo_name, check=False, readme=True):
    section(f"Sync: {repo_name}" + (" (--check, no writes)" if check else ""))
    words = load_canonical_words()
    if repo_name == "python-petname":
        changed = sync_python_petname(words, check=check)
    elif repo_name == "golang-petname":
        changed = sync_golang_petname(words, check=check)
    else:
        print("  (petname is the canonical source — nothing to sync)")
        return False
    if readme:
        changed = sync_readme(repo_name, check=check) or changed
    if not changed:
        print(f"  {repo_name}: already in sync with petname")
    return changed


# ── testing ──────────────────────────────────────────────────────────────

def test_petname():
    section("Test: petname (checkwords)")
    repo = REPOS["petname"]
    script = repo / "debian" / "tests" / "checkwords"
    ok = True
    for subdir, chars, syll in (
        ("usr/share/petname/small", "8", "3"),
        ("usr/share/petname/medium", "12", "4"),
        ("usr/share/petname/large", "9999", "9999"),
    ):
        r = run(["sh", str(script), subdir, chars, syll], cwd=repo, check=False, capture=True)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            print(f"  ✓ checkwords {subdir}")
        else:
            ok = False
            print(f"  ✗ checkwords {subdir} (exit {r.returncode})")
            print(indent(out, "      "))
    return ok


def test_python_petname():
    section("Test: python-petname (unittest)")
    repo = REPOS["python-petname"]
    r = run([sys.executable, "-m", "unittest"], cwd=repo, check=False, capture=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(indent(out, "  "))
    ok = r.returncode == 0
    print("  ✓ passed" if ok else f"  ✗ failed (exit {r.returncode})")
    return ok


def test_golang_petname():
    section("Test: golang-petname (go test)")
    if not shutil.which("go"):
        print("  ⚠ go not installed — skipping (https://go.dev/dl/)")
        return None
    repo = REPOS["golang-petname"]
    r = run(["go", "test", "./..."], cwd=repo, check=False, capture=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(indent(out, "  "))
    ok = r.returncode == 0
    print("  ✓ passed" if ok else f"  ✗ failed (exit {r.returncode})")
    return ok


TEST_FNS = {
    "petname": test_petname,
    "python-petname": test_python_petname,
    "golang-petname": test_golang_petname,
}


# ── Docker test mode ─────────────────────────────────────────────────────
#
# The host running this script often won't have petname's full test
# toolchain installed (Lingua::EN::Syllable, ispell, dictd+scowl, go — see
# debian/control's Build-Depends) — checkwords tests 08+ need the former,
# golang-petname needs `go`. Rather than install those system-wide on the
# maintainer's machine, --docker runs all three repos' real test suites
# together in a throwaway ubuntu:noble container with the actual
# Build-Depends installed, mounting each repo read-only.

DOCKER_IMAGE = "ubuntu:noble"

_DOCKER_APT_PACKAGES = [
    "dict", "liblingua-en-syllable-perl", "dictd", "dict-gcide", "dict-wn",
    "scowl", "ispell", "ienglish-common", "iamerican",
    "golang-go", "python3", "ca-certificates",
]

_DOCKER_TEST_SCRIPT = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
echo "--- apt-get install: """ + " ".join(_DOCKER_APT_PACKAGES) + r""" ---"
apt-get update -qq
apt-get install -y --no-install-recommends """ + " ".join(_DOCKER_APT_PACKAGES) + r""" 2>&1 | tail -20

echo "--- starting dictd ---"
service dictd start 2>/dev/null || (dictd 2>/dev/null & sleep 1) || true

echo "=== petname: checkwords ==="
cd /src/petname
./debian/tests/checkwords usr/share/petname/small 8 3
./debian/tests/checkwords usr/share/petname/medium 12 4
./debian/tests/checkwords usr/share/petname/large 9999 9999

echo "=== python-petname: unittest ==="
cd /src/python-petname
python3 -m unittest

echo "=== golang-petname: go test ==="
cd /src/golang-petname
go test ./...

echo "=== ALL DOCKER TESTS PASSED ==="
"""


def run_docker_tests():
    section(f"Test (Docker, {DOCKER_IMAGE}): petname + python-petname + golang-petname")
    if not shutil.which("docker"):
        die("docker not found — required for --docker test mode.")
    print("  This installs the real Build-Depends toolchain in a throwaway "
          "container — takes a minute…")
    r = run([
        "docker", "run", "--rm",
        "-v", f"{REPOS['petname']}:/src/petname:ro",
        "-v", f"{REPOS['python-petname']}:/src/python-petname:ro",
        "-v", f"{REPOS['golang-petname']}:/src/golang-petname:ro",
        DOCKER_IMAGE, "bash", "-c", _DOCKER_TEST_SCRIPT,
    ], check=False, capture=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(indent(out, "  "))
    ok = r.returncode == 0
    print("  ✓ all Docker tests passed" if ok else f"  ✗ Docker tests FAILED (exit {r.returncode})")
    return ok


# ── version / changelog helpers ─────────────────────────────────────────

def changelog_top(repo_name):
    """Return (version, distribution) from debian/changelog's newest stanza."""
    path = REPOS[repo_name] / "debian" / "changelog"
    text = path.read_text()
    m = re.match(r"^\S+ \(([^)]+)\)\s+(\S+);", text)
    if not m:
        die(f"{repo_name}: could not parse debian/changelog top stanza")
    return m.group(1), m.group(2)


def changelog_identity(repo_name):
    """Return (name, email) from debian/changelog's newest signoff line."""
    path = REPOS[repo_name] / "debian" / "changelog"
    text = path.read_text()
    m = re.search(r"^ -- (.+) <(.+)>  ", text, re.MULTILINE)
    return (m.group(1), m.group(2)) if m else (None, None)


def setup_py_version(repo_dir):
    text = (repo_dir / "setup.py").read_text()
    m = re.search(r"version='([^']+)'", text)
    return m.group(1) if m else None


def bump_version(v):
    parts = v.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def git_state(repo_name):
    path = REPOS[repo_name]
    branch = run(["git", "-C", str(path), "branch", "--show-current"], capture=True).stdout.strip()
    dirty = bool(run(["git", "-C", str(path), "status", "--porcelain"], capture=True).stdout.strip())
    tags = run(["git", "-C", str(path), "tag", "--list"], capture=True).stdout.split()
    latest_tag = tags[-1] if tags else None
    ahead_behind = run(
        ["git", "-C", str(path), "rev-list", "--left-right", "--count",
         f"origin/{branch}...{branch}"],
        capture=True, check=False,
    ).stdout.split()
    behind, ahead = (ahead_behind + ["?", "?"])[:2]
    return dict(branch=branch, dirty=dirty, latest_tag=latest_tag, ahead=ahead, behind=behind)


# ── commands ────────────────────────────────────────────────────────────

def cmd_status(args):
    preflight()
    for name in REPO_ORDER:
        banner(name)
        ver, dist = changelog_top(name)
        g = git_state(name)
        print(f"  changelog version: {ver}  ({dist})")
        if name == "python-petname":
            spv = setup_py_version(REPOS[name])
            flag = "" if spv == ver else "  ⚠ MISMATCH with changelog"
            print(f"  setup.py version:   {spv}{flag}")
        print(f"  latest git tag:     {g['latest_tag']}"
              + ("" if g["latest_tag"] == ver else "  ⚠ does not match changelog version"))
        print(f"  branch:             {g['branch']}"
              f"  dirty={g['dirty']}  ahead={g['ahead']} behind={g['behind']}"
              f"  (local knowledge — run git fetch to refresh)")
        if name != "petname":
            section(f"wordlist drift vs. petname (dry-run)")
            do_sync(name, check=True)


def cmd_sync(args):
    preflight()
    targets = [r for r in (args.repo or REPO_ORDER) if r != "petname"]
    if not targets:
        print("Nothing to sync (petname is the source).")
        return
    for name in targets:
        do_sync(name, check=args.check, readme=not args.no_readme)


def cmd_test(args):
    preflight()
    if args.docker:
        if args.repo:
            print("  (--docker tests all three repos together; ignoring --repo)")
        ok = run_docker_tests()
        sys.exit(0 if ok else 1)
    targets = args.repo or REPO_ORDER
    results = {}
    for name in targets:
        results[name] = TEST_FNS[name]()
    banner("Test summary")
    failed = False
    for name, ok in results.items():
        mark = "✓" if ok else ("⚠ skipped" if ok is None else "✗ FAILED")
        print(f"  {mark}  {name}")
        if ok is False:
            failed = True
    if failed:
        sys.exit(1)


def cmd_rc(args):
    """Pre-release gate: sync + full test suite across all 3 repos.

    Unlike `release`, this never commits, tags, or pushes anything — it just
    answers "if I released right now, would everything be in sync and
    passing?" Defaults to --docker so the gate reflects the real
    Build-Depends toolchain (ispell/dictd/scowl/go), not just whatever
    happens to be on this host; pass --no-docker to use the host instead.
    """
    preflight()
    banner("RC gate")

    section("Sync")
    any_synced = False
    for name in REPO_ORDER:
        if name != "petname":
            if do_sync(name, readme=not args.no_readme):
                any_synced = True
    if any_synced:
        print("\n  ⚠ sync modified files on disk (see `git status` in each repo) — "
              "not committed. Review before running `release`.")
    else:
        print("\n  Nothing to sync — all repos already match petname's word lists/README.")

    if args.no_docker:
        results = {name: TEST_FNS[name]() for name in REPO_ORDER}
        failed = any(ok is False for ok in results.values())
    else:
        ok = run_docker_tests()
        results = {"all three (docker)": ok}
        failed = not ok

    banner("RC gate summary")
    for name, ok in results.items():
        mark = "✓" if ok else ("⚠ skipped" if ok is None else "✗ FAILED")
        print(f"  {mark}  {name}")
    if failed:
        print("\n  ✗ RC gate FAILED — fix the above before releasing.")
        sys.exit(1)
    print("\n  ✓ RC gate passed. Nothing was committed, tagged, or pushed.")
    print("  Run `release` when ready to cut versions.")


def cmd_release(args):
    preflight()
    targets = args.repo or REPO_ORDER
    global _interactive
    _interactive = args.interactive

    # Pass 1: sync every non-petname target from petname's canonical word
    # lists/README *before* testing — otherwise a combined --docker run
    # would test the pre-sync state.
    for name in targets:
        if name != "petname":
            do_sync(name, readme=not args.no_readme)

    # Pass 2: test. --docker runs all three together in one container, so
    # do it once up front rather than per repo.
    docker_ok = run_docker_tests() if args.docker else None

    released = {}
    for name in targets:
        banner(f"Release: {name}")

        ok = docker_ok if args.docker else TEST_FNS[name]()
        if ok is False:
            if not confirm(f"{name}: tests FAILED — continue to release anyway?", skippable=True):
                continue

        path = REPOS[name]
        dirty = run(["git", "-C", str(path), "status", "--porcelain"], capture=True).stdout
        if not dirty.strip():
            print(f"  {name}: no changes — nothing to release")
            continue

        section(f"{name}: pending changes")
        print(indent(dirty, "    "))
        if not confirm(f"Commit, version-bump, tag, and push {name}?", skippable=True, always=True):
            continue

        cur_ver, distro = changelog_top(name)
        suggested = bump_version(cur_ver)
        new_ver = input(f"  New version for {name} [{suggested}]: ").strip() or suggested

        debfullname, debemail = changelog_identity(name)
        env = os.environ.copy()
        if debfullname:
            env["DEBFULLNAME"] = debfullname
        if debemail:
            env["DEBEMAIL"] = debemail
        print(f"  Opening $EDITOR via dch for the {name} ({new_ver}) changelog entry…")
        run(["dch", "--newversion", new_ver, "--distribution", distro], cwd=path, env=env)

        if name == "python-petname":
            setup_path = path / "setup.py"
            text = setup_path.read_text()
            new_text = re.sub(r"version='[^']+'", f"version='{new_ver}'", text, count=1)
            if new_text == text:
                die(f"could not update version= in {setup_path}")
            setup_path.write_text(new_text)
            print(f"  Updated setup.py version to {new_ver}")

        run(["git", "-C", str(path), "add", "-A"])
        run(["git", "-C", str(path), "commit", "-m", f"Release {name} {new_ver}"])
        run(["git", "-C", str(path), "tag", new_ver])
        print(f"  ✓ committed + tagged {name} {new_ver}")

        if confirm(f"Push {name} commit and tag {new_ver} to origin?", always=True):
            run(["git", "-C", str(path), "push", "origin", "HEAD"])
            run(["git", "-C", str(path), "push", "origin", new_ver])
            print(f"  ✓ pushed {name} {new_ver}")
            released[name] = new_ver
        else:
            print(f"  (committed+tagged locally; push skipped)")

    if released:
        banner("Manual follow-up (not automated by this script)")
        if "python-petname" in released:
            print(f"  • PyPI upload for python-petname {released['python-petname']}:")
            print(f"      cd {REPOS['python-petname']} && python3 -m build && twine upload dist/*")
        if "petname" in released:
            print(f"  • Snap release for petname {released['petname']} (snapcraft.yaml)")
        print(f"  • Debian/PPA uploads, if applicable, for: {', '.join(released)}")
        print(f"  • GitHub release notes, if desired: gh release create <tag> --generate-notes")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_repo_arg(sp):
        sp.add_argument("--repo", action="append", choices=REPO_ORDER,
                         help="repeatable; default: all three, in order")

    sp = sub.add_parser("status", help="version/git/wordlist-drift snapshot (read-only)")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("sync", help="propagate wordlists + README from petname")
    add_repo_arg(sp)
    sp.add_argument("--check", action="store_true", help="report drift only, write nothing")
    sp.add_argument("--no-readme", action="store_true", help="skip README.md propagation")
    sp.set_defaults(fn=cmd_sync)

    sp = sub.add_parser("test", help="run each repo's test suite")
    add_repo_arg(sp)
    sp.add_argument("--docker", action="store_true",
                     help=f"run all 3 repos' real test suites together in a throwaway "
                          f"{DOCKER_IMAGE} container with the full Build-Depends "
                          f"toolchain installed (ispell/dictd/scowl/go), instead of "
                          f"whatever happens to be on this host")
    sp.set_defaults(fn=cmd_test)

    sp = sub.add_parser("rc", help="pre-release gate: sync + full test suite, no commit/tag/push")
    sp.add_argument("--no-readme", action="store_true", help="skip README.md propagation")
    sp.add_argument("--no-docker", action="store_true",
                     help="test on this host instead of a throwaway Docker container "
                          "(the default, for the real Build-Depends toolchain)")
    sp.set_defaults(fn=cmd_rc)

    sp = sub.add_parser("release", help="sync + test + version bump + tag + push, per repo")
    add_repo_arg(sp)
    sp.add_argument("--no-readme", action="store_true", help="skip README.md propagation")
    sp.add_argument("--docker", action="store_true",
                     help="test via a throwaway Docker container (see `test --docker`)")
    sp.add_argument("-i", "--interactive", action="store_true",
                     help="also confirm sync/test steps (commit/tag/push always confirm)")
    sp.set_defaults(fn=cmd_release)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
