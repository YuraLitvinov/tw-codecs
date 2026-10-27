#!/usr/bin/env python3
"""Detect a new openSUSE Tumbleweed snapshot, for the scheduled workflow.

usage:
  snapshot.py current             print the current snapshot id
  snapshot.py check FILE          compare it with the id stored in FILE; print
                                  'snapshot=ID' and 'changed=true|false' (GITHUB_OUTPUT format)
  snapshot.py write FILE ID       store ID in FILE

A new snapshot does not mean our packages changed, so the library part below also tracks,
per package, which source RPM and which build configuration produced the published RPMs
(built-sources.txt), and says which packages are out of date ("dirty").

Tumbleweed publishes a new snapshot roughly daily. Its id is the second line of
media.1/media on the OSS repository, e.g. openSUSE-20261024-x86_64-Build6.174; the build
number changes when a snapshot is respun, so it identifies the exact content.
"""
import hashlib
import os
import re
import sys
import urllib.request

MEDIA_URL = os.environ.get(
    "SNAPSHOT_MEDIA_URL", "https://download.opensuse.org/tumbleweed/repo/oss/media.1/media"
)
ID_PATTERN = re.compile(r"openSUSE-\d{8}-\S+")


class SnapshotError(Exception):
    pass


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode()


def parse_media(text: str) -> str:
    """Return the snapshot id from the text of media.1/media."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not ID_PATTERN.fullmatch(lines[1]):
        raise SnapshotError(f"unexpected media.1/media content: {text[:200]!r}")
    return lines[1]


def current_snapshot(fetch=None) -> str:
    return parse_media((fetch or fetch_text)(MEDIA_URL))


def read_state(path: str) -> str:
    """The id stored in `path`, or '' when the file does not exist yet."""
    try:
        with open(path) as f:
            return f.readline().strip()
    except FileNotFoundError:
        return ""


def write_state(path: str, snapshot: str) -> None:
    if not ID_PATTERN.fullmatch(snapshot):
        raise SnapshotError(f"not a snapshot id: {snapshot!r}")
    with open(path, "w") as f:
        f.write(snapshot + "\n")


def check(path: str, fetch=None) -> dict[str, str]:
    """Compare the current snapshot with the stored one. A missing file counts as changed."""
    snapshot = current_snapshot(fetch)
    return {"snapshot": snapshot, "changed": "true" if snapshot != read_state(path) else "false"}


# --- per-package state (built-sources.txt) -----------------------------------------------

# name -> (source RPM file name, build configuration hash)
Sources = dict[str, tuple[str, str]]


def read_sources(path: str) -> Sources:
    """Read built-sources.txt: lines 'name<TAB>source RPM<TAB>config hash'. Missing file: empty."""
    sources: Sources = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) != 3 or not all(parts):
                    raise SnapshotError(f"{path}: bad line {line!r}, expected name<TAB>srpm<TAB>hash")
                sources[parts[0]] = (parts[1], parts[2])
    except FileNotFoundError:
        pass
    return sources


def write_sources(path: str, sources: Sources) -> None:
    with open(path, "w") as f:
        f.write("# name, source RPM that was built, hash of the build configuration\n")
        for name in sorted(sources, key=str.lower):
            f.write(f"{name}\t{sources[name][0]}\t{sources[name][1]}\n")


def config_hash(name: str, env: dict[str, str], files: dict[str, str]) -> str:
    """Hash of everything besides the source RPM that decides what a package build produces.

    `env` are the package's options from ci/packages.txt; `files` maps a label to the content
    of a file that matters (its SPEC_PATCH, the Dockerfile, build-srpm.sh). Changing any of
    them changes the hash, so the package is rebuilt.
    """
    h = hashlib.sha256()
    h.update(name.lower().encode() + b"\0")
    for key in sorted(env):
        h.update(f"{key}={env[key]}".encode() + b"\0")
    for label in sorted(files):
        h.update(label.encode() + b"\0" + files[label].encode() + b"\0")
    return h.hexdigest()[:16]


def dirty_packages(current: Sources, built: Sources, edges: dict[str, dict[str, str]]) -> set[str]:
    """Packages that must be rebuilt.

    A package is dirty when it was never built, or its source RPM or configuration differs from
    what was built. Dirtiness spreads to every package that has a build dependency on a dirty
    one (`edges[pkg]` lists the packages `pkg` needs), because those link against the new build.
    """
    dirty = {name for name, now in current.items() if built.get(name) != now}
    changed = True
    while changed:
        changed = False
        for name, deps in edges.items():
            if name not in dirty and name in current and any(d in dirty for d in deps):
                dirty.add(name)
                changed = True
    return dirty


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["current"] and len(argv) == 1:
            print(current_snapshot())
        elif argv[:1] == ["check"] and len(argv) == 2:
            for key, value in check(argv[1]).items():
                print(f"{key}={value}")
        elif argv[:1] == ["write"] and len(argv) == 3:
            write_state(argv[1], argv[2])
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (SnapshotError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
