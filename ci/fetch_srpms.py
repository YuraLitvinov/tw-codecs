#!/usr/bin/env python3
"""Download the current Tumbleweed source RPMs for the packages in ci/packages.txt.

usage: fetch_srpms.py <packages.txt> <built.txt> <outdir>

packages.txt has one package per line: a plain name (any case), then optional ENV=VAL
build options, '#' starts a comment. Names are matched case-insensitively, so "mesa"
selects the Mesa source package.

Reads the OSS source repo's primary.xml, picks the entry for each wanted name and
downloads it unless its file name is already listed in <built.txt> or already in <outdir>.
Prints the file names it downloaded, one per line.

Packages openSUSE does not ship come from the .src.rpm assets of this project's GitHub
releases (fetch_release_srpms); SRPM_RELEASE_REPO overrides the repository.
"""
import gzip
import io
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE = os.environ.get("SRC_REPO", "https://download.opensuse.org/source/tumbleweed/repo/oss/")
RELEASE_REPO = os.environ.get("SRPM_RELEASE_REPO", "YuraLitvinov/tw-codecs")
REPO = "{http://linux.duke.edu/metadata/repo}"
COMMON = "{http://linux.duke.edu/metadata/common}"


def parse_packages(text: str) -> list[tuple[str, dict[str, str]]]:
    """Return [(name, {ENV: VAL})] for each package line of a packages.txt text."""
    packages = []
    for line in text.splitlines():
        parts = shlex.split(line, comments=True)
        if not parts:
            continue
        env = {}
        for opt in parts[1:]:
            key, sep, val = opt.partition("=")
            if not sep or not key:
                raise ValueError(f"bad option {opt!r} for package {parts[0]}: expected ENV=VAL")
            env[key] = val
        packages.append((parts[0], env))
    return packages


class _KeepAuthOnSameHost(urllib.request.HTTPRedirectHandler):
    """GitHub redirects asset downloads to another host, which rejects our Authorization header."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


def fetch(url: str, headers: dict[str, str] | None = None) -> bytes:
    opener = urllib.request.build_opener(_KeepAuthOnSameHost)
    with opener.open(urllib.request.Request(url, headers=headers or {}), timeout=120) as r:
        return r.read()


def version_key(text: str) -> list:
    """Sort key approximating rpmvercmp: digit runs compare as numbers, after letter runs."""
    return [(1, int(t)) if t.isdigit() else (0, t) for t in re.findall(r"\d+|[A-Za-z]+", text)]


def srpm_version(filename: str, name: str) -> str:
    return re.sub(r"\.src\.rpm$", "", filename[len(name) + 1:])


def matches_name(filename: str, name: str) -> bool:
    """True for NAME-<digit...>.src.rpm; 'Mesa' does not match 'Mesa-drivers-26.src.rpm'."""
    return re.match(re.escape(name) + r"-\d.*\.src\.rpm$", filename, re.IGNORECASE) is not None


def decompress(name: str, data: bytes) -> bytes:
    if name.endswith(".zst"):
        return subprocess.run(["zstd", "-dc"], input=data, capture_output=True, check=True).stdout
    if name.endswith(".gz"):
        return gzip.decompress(data)
    return data


def parse_primary(xml: bytes, wanted: set[str]) -> dict[str, str]:
    """Return {wanted name: href} for wanted source packages (the last entry wins).

    Names match case-insensitively; the key is the spelling used in `wanted`.
    """
    by_lower = {w.lower(): w for w in wanted}
    found: dict[str, str] = {}
    for _, el in ET.iterparse(io.BytesIO(xml)):
        if el.tag != COMMON + "package":
            continue
        name = (el.findtext(COMMON + "name") or "").lower()
        if name in by_lower and el.findtext(COMMON + "arch") == "src":
            found[by_lower[name]] = el.find(COMMON + "location").get("href")
        el.clear()
    return found


def primary_href(repomd: bytes) -> str:
    for data in ET.fromstring(repomd).iter(REPO + "data"):
        if data.get("type") == "primary":
            return data.find(REPO + "location").get("href")
    raise SystemExit("no primary metadata in repomd.xml")


def fetch_srpms(names: set[str], outdir: str, built: set[str] = frozenset()) -> tuple[list[str], list[str]]:
    """Download the OSS source RPM of each name into outdir.

    Returns (downloaded file names, names not found in OSS). Files listed in `built`
    or already present in outdir are skipped.
    """
    os.makedirs(outdir, exist_ok=True)
    href = primary_href(fetch(BASE + "repodata/repomd.xml"))
    found = parse_primary(decompress(href, fetch(BASE + href)), names)
    downloaded = []
    for name, loc in sorted(found.items()):
        fn = os.path.basename(loc)
        if fn in built or os.path.exists(os.path.join(outdir, fn)):
            print(f"up to date: {fn}", file=sys.stderr)
            continue
        with open(os.path.join(outdir, fn), "wb") as f:
            f.write(fetch(BASE + loc))
        downloaded.append(fn)
    return downloaded, sorted(names - found.keys())


def release_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    """GitHub API headers; the token is optional and only raises the rate limit (or opens a private repo)."""
    headers = {"Accept": accept, "User-Agent": "tw-codecs-build"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    return headers


def release_assets(repo: str) -> list[dict]:
    """All .src.rpm assets of the repository's releases, as GitHub's asset JSON."""
    assets, page = [], 1
    while True:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
        releases = json.loads(fetch(url, release_headers()))
        assets += [a for r in releases for a in r.get("assets", []) if a["name"].endswith(".src.rpm")]
        if len(releases) < 100:
            return assets
        page += 1


def pick_assets(names: set[str], assets: list[dict]) -> dict[str, dict]:
    """For each name, the asset with the newest version (names match like matches_name)."""
    picked: dict[str, dict] = {}
    for name in names:
        found = [a for a in assets if matches_name(a["name"], name)]
        if found:
            picked[name] = max(found, key=lambda a: version_key(srpm_version(a["name"], name)))
    return picked


def fetch_release_srpms(names: set[str], outdir: str, repo: str = RELEASE_REPO) -> tuple[list[str], list[str]]:
    """Download the newest release .src.rpm of each name into outdir.

    Returns (downloaded file names, names with no matching release asset). Files already
    in outdir are skipped.
    """
    os.makedirs(outdir, exist_ok=True)
    picked = pick_assets(names, release_assets(repo))
    downloaded = []
    for name, asset in sorted(picked.items()):
        path = os.path.join(outdir, asset["name"])
        if os.path.exists(path):
            print(f"up to date: {asset['name']}", file=sys.stderr)
            continue
        with open(path, "wb") as f:
            f.write(fetch(asset["url"], release_headers("application/octet-stream")))
        downloaded.append(asset["name"])
    return downloaded, sorted(names - picked.keys())


def main() -> None:
    packages_file, built_file, outdir = sys.argv[1:4]
    with open(packages_file) as f:
        wanted = {name for name, _ in parse_packages(f.read())}
    built = set(open(built_file).read().split()) if os.path.exists(built_file) else set()

    downloaded, missing = fetch_srpms(wanted, outdir, built)
    for name in missing:
        print(f"warning: {name} not found in {BASE}", file=sys.stderr)
    for fn in downloaded:
        print(fn)


if __name__ == "__main__":
    main()
