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
runs each repo's own test suite, and — for `final` — walks each repo
through an auto-filled debian/changelog entry, commit, tag, and push.

The three repos share ONE version number (by request): `final` and
`open-dev` always act on all three together, never a subset — see
"Lockstep versioning" below.

Usage:
    ./release.py status                    # version / git / wordlist-drift snapshot, read-only
    ./release.py sync     [--repo R]... [--check] [--no-readme]
    ./release.py test     [--repo R]... [--docker]
    ./release.py rc       [--no-docker] [--no-readme]
    ./release.py final    [--docker] [--interactive]   # alias: release; always all 3 repos
    ./release.py open-dev                              # always all 3 repos

R (sync/test only) is one of: petname, python-petname, golang-petname (repeatable;
default: all three). final/open-dev have no --repo — see "Lockstep versioning".
--check (sync only) reports drift without writing files.
--docker (test/final) or the default of `rc` runs the real test suites together in a
throwaway ubuntu:noble container with the full Build-Depends toolchain installed
(ispell/dictd/scowl/go) instead of whatever happens to be on this host.
--interactive (final only) additionally confirms the sync/test steps, not just
commit/tag/push (commit/tag/push always confirm, interactive or not).

`rc` is a pre-release gate, not a release-candidate cut: it syncs and runs the full
test suite across all three repos and reports pass/fail — it never commits, tags, or
pushes anything. Run it before `final` to check "if I released right now, would
everything be in sync and passing?"

Lockstep versioning: petname, python-petname, and golang-petname share one version
number across the board, even though each has its own independent change history.
shared_target_version() computes it as the highest base version any of the three
currently reports; `final` always releases ALL THREE to that version, and `open-dev`
always opens the next shared dev cycle for all three at once — no --repo filtering
on either. A repo with no real changes since its last release still gets bumped and
tagged, with a placeholder bullet (_EMPTY_RELEASE_BULLET) saying so — an accepted
tradeoff ("might mean some empty versions from time to time") for "petname family
vX.Y" meaning the same thing in all three places. open-dev refuses to open the next
cycle unless all three are currently released (none UNRELEASED) at the exact same
version — that invariant only holds right after final has released all three
together.

`final` cuts the actual release: it never opens an editor. debian/changelog is
auto-filled from `git log` — the commit subjects since the last ACTUALLY-RELEASED
(non-UNRELEASED) stanza (see commits_since_last_release) become the new stanza's
bullets via `dch --append`/`--newversion`, non-interactively. That anchor spans
across any open-dev bump commit, so commits landed between a release and the
following open-dev bump are never missed even if HEAD is currently that bump
commit itself (unlike byobu's HEAD-message guard, which this tool deliberately
doesn't use — see _OPEN_DEV_MSG_RE's comment). If open-dev already opened an
UNRELEASED stanza at the shared target version, final just fills and closes it
(`dch --release`) rather than bumping again; if a repo is instead already
released but behind the shared target (e.g. it wasn't part of an earlier
lockstep cycle), final bumps it straight to the target version to catch up.
After a successful python-petname push, it builds and uploads to PyPI
(https://pypi.org/project/petname/) via `python3 -m build` + `twine upload`, the
way byobu's release.py publishes trustmux — confirmed first, same as
commit/tag/push; skipped with instructions if `build`/`twine` aren't installed.
`release` still works as an alias for `final`.

`open-dev`, run after `final`, bumps all three repos straight to the next shared
dev version (changelog stanza + setup.py where applicable) and commits "bump
version to X.Y and open for development" — so later work accrues under the next
version instead of silently piling onto the one just released. No tag. Unlike
every other command here, open-dev never prompts once its alignment check
passes: bump/commit/push happen unconditionally for every clean repo.

Sibling repos are found at ../python-petname and ../golang-petname relative to
this repo, or via $PYTHON_PETNAME_SRC / $GOLANG_PETNAME_SRC.

This script does NOT touch Snap, PPA, or Debian upload — those remain manual;
`final` prints a reminder checklist for them at the end. PyPI IS automated (see
above).
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


def changelog_second_distro(repo_name):
    """Return the distribution of the SECOND changelog stanza.

    Used when closing out an UNRELEASED stanza (opened by `open-dev`): that
    stanza doesn't know its real target distro, so we reuse whatever the
    previously-released stanza used. (dch --release's own "inherit from
    previous entry" behavior was tested and found unreliable on this
    system — it picked the host's Ubuntu devel codename instead — so this
    is computed explicitly rather than relying on that.)
    """
    path = REPOS[repo_name] / "debian" / "changelog"
    text = path.read_text()
    matches = re.findall(r"^\S+ \([^)]+\)\s+(\S+);", text, re.MULTILINE)
    return matches[1] if len(matches) > 1 else (matches[0] if matches else "stable")


def commits_since_last_release(repo_name):
    """Commit subjects on HEAD since the last actually-released (i.e. not
    UNRELEASED) changelog stanza was created — used both to decide whether
    there's anything to release and to auto-fill the next stanza's bullets.

    Anchoring on the last REAL release (not just line 1) matters: if an
    open-dev stanza is already open, commits can still land between the
    previous release and that open-dev bump commit (this happened for
    real — see golang-petname's e19a26ec/8afa6b64, pushed but never tagged
    because an earlier version of this script only checked working-tree
    dirtiness, not commit history). Anchoring on line 1 alone would keep
    missing commits like that; anchoring on the last real release catches
    them retroactively as well as going forward. Excludes merge commits and
    the open-dev bump commit itself (it carries no real content of its own).
    """
    path = REPOS[repo_name]
    text = (path / "debian" / "changelog").read_text()
    line_no = None
    for i, line in enumerate(text.splitlines(), start=1):
        m = re.match(rf"^{re.escape(repo_name)} \([^)]+\)\s+(\S+);", line)
        if m and m.group(1) != "UNRELEASED":
            line_no = i
            break
    if line_no is None:
        die(f"{repo_name}: could not find a released (non-UNRELEASED) "
            f"stanza in debian/changelog")
    blame = run(["git", "-C", str(path), "blame", "--porcelain",
                 "-L", f"{line_no},{line_no}", "--", "debian/changelog"],
                capture=True).stdout
    since = blame.split()[0] if blame.strip() else None
    if not since or set(since) == {"0"}:
        die(f"{repo_name}: could not determine the commit that introduced "
            f"the last released debian/changelog stanza (line {line_no})")
    log = run(["git", "-C", str(path), "log", "--format=%s", "--no-merges",
               f"{since}..HEAD"], capture=True).stdout
    subjects = [line for line in log.splitlines() if line.strip()]
    return [s for s in subjects if not _OPEN_DEV_MSG_RE.match(s)]


def setup_py_version(repo_dir):
    text = (repo_dir / "setup.py").read_text()
    m = re.search(r"version='([^']+)'", text)
    return m.group(1) if m else None


def bump_version(v):
    parts = v.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def set_setup_py_version(new_ver):
    """Set python-petname/setup.py's version=, in place.

    Uses re.subn's match COUNT to detect failure, not text equality: if
    open-dev already set setup.py to this exact version (the normal case —
    final's new_ver, when closing an open-dev-opened UNRELEASED stanza, IS
    the version open-dev already wrote there), the substitution is a
    correct no-op, not an error. Only zero regex matches is a real failure.
    """
    setup_path = REPOS["python-petname"] / "setup.py"
    text = setup_path.read_text()
    new_text, n = re.subn(r"version='[^']+'", f"version='{new_ver}'", text, count=1)
    if n == 0:
        die(f"could not find version='...' in {setup_path}")
    if new_text != text:
        setup_path.write_text(new_text)
        print(f"  Updated setup.py version to {new_ver}")
    else:
        print(f"  setup.py version already {new_ver}")


# ── lockstep versioning ──────────────────────────────────────────────────
#
# petname, python-petname, and golang-petname each used to version
# independently. By request, they now share one number: `open-dev` bumps
# all three to the same next version together, and `final` always releases
# all three together — a repo with no real changes since its last release
# still gets bumped and tagged (with a placeholder changelog bullet saying
# so), so the number never drifts apart. This costs the occasional
# no-op/empty release for a repo with nothing new to say, in exchange for
# "petname family vX.Y" meaning the same thing everywhere.

_EMPTY_RELEASE_BULLET = (
    "No changes — version bumped for release alignment across "
    "petname/python-petname/golang-petname."
)


def _version_tuple(v):
    return tuple(int(p) for p in v.split("."))


def shared_target_version():
    """The version `final` should converge all three repos to: the highest
    base version any of them currently reports (released or still an open
    UNRELEASED dev cycle). Normally that's whatever `open-dev` last opened
    for everyone together; if one repo is behind (e.g. it was released
    independently before lockstep started, or missed a cycle), `final`
    catches it up to this same target rather than requiring it be aligned
    first.
    """
    versions = {name: _version_tuple(changelog_top(name)[0]) for name in REPO_ORDER}
    return ".".join(str(p) for p in max(versions.values()))


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
    print("  Run `final` (alias: `release`) when ready to cut versions.")


# A commit message left by `open-dev`. byobu's release.py refuses to run
# `final` when HEAD matches this — a blunt proxy for "nothing real to
# release". This tool doesn't need that guard: commits_since_last_release()
# already anchors on the last REAL release rather than on HEAD, so it's
# correct even when HEAD happens to be the open-dev commit itself (real
# work can land before a bump that hasn't been followed by anything yet —
# this happened for real with golang-petname's e19a26ec/8afa6b64, and a
# HEAD-message guard would have kept blocking their release forever). This
# regex is kept only to filter the open-dev commit's own line out of the
# derived subjects list, since it carries no real content of its own.
_OPEN_DEV_MSG_RE = re.compile(r"^bump version to .* and open for development$")


def _missing_pypi_tools():
    missing = []
    if not shutil.which("twine"):
        missing.append("twine")
    r = subprocess.run([sys.executable, "-c", "import build"], capture_output=True)
    if r.returncode != 0:
        missing.append("build")
    return missing


def publish_pypi(new_ver):
    """Build + upload python-petname to PyPI (https://pypi.org/project/petname/),
    the way byobu's release.py publishes trustmux — but there's no GitHub
    Actions here to trigger on a tag push, so this runs `python3 -m build`
    and `twine upload` directly. Requires `pip install build twine` and
    PyPI credentials (an API token in ~/.pypirc, or TWINE_USERNAME=__token__
    + TWINE_PASSWORD=<token> in the environment) — twine itself will prompt
    for these if missing, which needs a real terminal same as our confirm().
    """
    section(f"PyPI: build + upload python-petname {new_ver}")
    missing = _missing_pypi_tools()
    if missing:
        path = REPOS["python-petname"]
        print(f"  ⚠ missing: {', '.join(missing)} — install with: pip install --user build twine")
        print(f"  Skipping PyPI upload. Run manually once installed:")
        print(f"    cd {path} && python3 -m build && twine upload dist/*")
        return

    path = REPOS["python-petname"]
    dist = path / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    run([sys.executable, "-m", "build"], cwd=path)
    artifacts = sorted(dist.glob("*"))
    if not artifacts:
        die(f"build produced no artifacts in {dist}")
    print(f"  Built: {', '.join(a.name for a in artifacts)}")

    if not confirm(f"Upload python-petname {new_ver} to PyPI "
                    f"(https://pypi.org/project/petname/) via twine?", always=True):
        print(f"  (PyPI upload skipped — run manually: cd {path} && twine upload dist/*)")
        return
    run(["twine", "upload"] + [str(a) for a in artifacts], cwd=path)
    print(f"  ✓ uploaded python-petname {new_ver} to PyPI")


def cmd_final(args):
    preflight()
    global _interactive
    _interactive = args.interactive

    # Lockstep: always all three, converged to whichever repo currently
    # reports the highest version (see shared_target_version).
    target_ver = shared_target_version()
    section(f"Lockstep target version: {target_ver}")

    # Pass 1: sync every non-petname repo from petname's canonical word
    # lists/README *before* testing — otherwise a combined --docker run
    # would test the pre-sync state.
    for name in REPO_ORDER:
        if name != "petname":
            do_sync(name, readme=not args.no_readme)

    # Pass 2: test. --docker runs all three together in one container, so
    # do it once up front rather than per repo.
    docker_ok = run_docker_tests() if args.docker else None

    released = {}
    for name in REPO_ORDER:
        banner(f"Final: {name}")

        path = REPOS[name]
        ok = docker_ok if args.docker else TEST_FNS[name]()
        if ok is False:
            if not confirm(f"{name}: tests FAILED — continue to release anyway?", skippable=True):
                continue

        cur_ver, distro = changelog_top(name)
        if distro != "UNRELEASED" and cur_ver == target_ver:
            print(f"  {name}: already at the shared version {target_ver} — nothing to do")
            continue

        # "Real changes" = commits since the last release, or uncommitted
        # working-tree changes (e.g. from sync in Pass 1, if word lists
        # actually changed). Absent either, this repo still gets released
        # at target_ver to stay in lockstep — with a placeholder bullet
        # saying so, per the "some empty versions from time to time"
        # tradeoff of sharing one version across all three.
        subjects = commits_since_last_release(name)
        dirty = run(["git", "-C", str(path), "status", "--porcelain"], capture=True).stdout
        if not subjects and dirty.strip():
            dirty_files = [line[3:] for line in dirty.strip().splitlines()]
            subjects = [f"Sync word lists/README from petname ({', '.join(dirty_files)})"]
        elif not subjects:
            subjects = [_EMPTY_RELEASE_BULLET]

        section(f"{name}: changelog entry to be auto-filled from git log")
        for s in subjects:
            print(f"    * {s}")
        if dirty.strip():
            print(f"  plus uncommitted working-tree changes:")
            print(indent(dirty, "    "))
        if not confirm(f"Version-bump/close changelog (via dch, no editor) to {target_ver}, "
                        f"commit, tag, and push {name}?", skippable=True, always=True):
            continue

        debfullname, debemail = changelog_identity(name)
        env = os.environ.copy()
        if debfullname:
            env["DEBFULLNAME"] = debfullname
        if debemail:
            env["DEBEMAIL"] = debemail

        if distro == "UNRELEASED":
            # open-dev already opened this stanza at target_ver — just fill
            # in its bullets and close it out (dch --release), reusing the
            # distro this repo actually ships to.
            assert cur_ver == target_ver, f"{name}: UNRELEASED at {cur_ver}, expected {target_ver}"
            target_distro = changelog_second_distro(name)
            for s in subjects:
                run(["dch", "--append", s], cwd=path, env=env)
            run(["dch", "--release", "--distribution", target_distro, ""], cwd=path, env=env)
        else:
            # This repo is behind (released, but at an older version than
            # target_ver) — catch it up directly, reusing its own last
            # real distro.
            run(["dch", "--newversion", target_ver, "--distribution", distro, subjects[0]],
                cwd=path, env=env)
            for s in subjects[1:]:
                run(["dch", "--append", s], cwd=path, env=env)
        print(f"  ✓ changelog for {name} {target_ver} auto-filled from {len(subjects)} entr"
              f"{'y' if len(subjects) == 1 else 'ies'} (no editor opened)")

        if name == "python-petname":
            set_setup_py_version(target_ver)

        run(["git", "-C", str(path), "add", "-A"])
        run(["git", "-C", str(path), "commit", "-m", f"Release {name} {target_ver}"])
        run(["git", "-C", str(path), "tag", target_ver])
        print(f"  ✓ committed + tagged {name} {target_ver}")

        if confirm(f"Push {name} commit and tag {target_ver} to origin?", always=True):
            run(["git", "-C", str(path), "push", "origin", "HEAD"])
            run(["git", "-C", str(path), "push", "origin", target_ver])
            print(f"  ✓ pushed {name} {target_ver}")
            released[name] = target_ver
            if name == "python-petname":
                publish_pypi(target_ver)
        else:
            print(f"  (committed+tagged locally; push skipped)")

    if released:
        banner("Manual follow-up (not automated by this script)")
        if "petname" in released:
            print(f"  • Snap release for petname {released['petname']} (snapcraft.yaml)")
        print(f"  • Debian/PPA uploads, if applicable, for: {', '.join(released)}")
        print(f"  • GitHub release notes, if desired: gh release create <tag> --generate-notes")


def cmd_open_dev(args):
    """Bump all three repos to the next shared dev version right after a
    lockstep release.

    Mirrors byobu's open-dev: a standalone commit per repo, message "bump
    version to X.Y and open for development" (matched by _OPEN_DEV_MSG_RE,
    so `final` can filter it out of the derived changelog subjects),
    opening a new UNRELEASED stanza so future work has somewhere to accrue
    instead of silently piling onto the one just released. No tag.

    Lockstep: refuses to open the next cycle unless all three are
    currently released (none UNRELEASED) at the exact same version — that
    invariant only holds right after `final` has released all three
    together, which is the point at which the next cycle should open.
    Unlike every other command here, once that invariant holds, this
    never prompts: bump, commit, and push happen unconditionally for
    every clean repo.
    """
    preflight()

    states = {name: changelog_top(name) for name in REPO_ORDER}
    open_ones = [name for name, (_, distro) in states.items() if distro == "UNRELEASED"]
    if open_ones:
        print(f"Already open for development: {', '.join(open_ones)} — "
              f"run `final` to close {'it' if len(open_ones) == 1 else 'them'} out "
              f"before opening the next shared cycle.")
        return
    versions = {name: ver for name, (ver, _) in states.items()}
    if len(set(versions.values())) != 1:
        die(f"Repos are out of lockstep: {versions}\n  Run `final` to realign first.")
    cur_ver = next(iter(versions.values()))
    next_ver = bump_version(cur_ver)
    section(f"Opening shared dev version {next_ver} (was {cur_ver})")

    for name in REPO_ORDER:
        banner(f"Open-dev: {name}")
        path = REPOS[name]

        dirty = run(["git", "-C", str(path), "status", "--porcelain"], capture=True).stdout
        if dirty.strip():
            print(f"  {name}: working tree is dirty — commit or stash first. Skipping.")
            continue

        debfullname, debemail = changelog_identity(name)
        env = os.environ.copy()
        if debfullname:
            env["DEBFULLNAME"] = debfullname
        if debemail:
            env["DEBEMAIL"] = debemail
        run(["dch", "--newversion", next_ver, "--distribution", "UNRELEASED",
             f"Open development for {next_ver}."], cwd=path, env=env)

        if name == "python-petname":
            set_setup_py_version(next_ver)

        commit_msg = f"bump version to {next_ver} and open for development"
        run(["git", "-C", str(path), "add", "-A"])
        run(["git", "-C", str(path), "commit", "-m", commit_msg])
        print(f"  ✓ {name}: {commit_msg}")

        run(["git", "-C", str(path), "push", "origin", "HEAD"])
        print(f"  ✓ pushed {name}")


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

    sp = sub.add_parser("final", aliases=["release"],
                         help="sync + test + lockstep version bump + tag + push, all 3 repos "
                              "together ('release' still works as an alias)")
    sp.add_argument("--no-readme", action="store_true", help="skip README.md propagation")
    sp.add_argument("--docker", action="store_true",
                     help="test via a throwaway Docker container (see `test --docker`)")
    sp.add_argument("-i", "--interactive", action="store_true",
                     help="also confirm sync/test steps (commit/tag/push always confirm)")
    sp.set_defaults(fn=cmd_final)

    sp = sub.add_parser("open-dev",
                         help="bump all 3 repos to the next shared dev version after a "
                              "lockstep release (no tag; commit message matches byobu's "
                              "convention). Never prompts: bump/commit/push happen "
                              "unconditionally once all 3 are released and aligned.")
    sp.set_defaults(fn=cmd_open_dev)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
