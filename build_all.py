#!/usr/bin/env python3
"""Build every package in ci/packages.txt in a container, then stage a zypper repo tree.

usage: build_all.py [--plan | --matrix] [--only NAME] [--deps DIR] [--stage-only] [--serve]
                    [--packages FILE]

Steps (default is all of them):
  1. fetch   download the current openSUSE Tumbleweed OSS source RPM for each name into
             srpms/ (ci/fetch_srpms.py); names OSS lacks come from the .src.rpm assets of
             this repository's GitHub releases
  2. order   read each SRPM's spec and sort the packages so one builds after the packages
             that provide its BuildRequires. A dependency is dropped when the srpm-builder
             image's repos (openSUSE OSS, VideoLAN) already provide the capability.
  3. build   build each SRPM in the srpm-builder container (build-srpm.sh), in that order,
             collecting binary RPMs in out-all/
  4. stage   site/: .nojekyll, index.html and repo/ (RPMs, repodata, signed repomd.xml)

--plan            print the build order and why, then exit; fetches and builds nothing
                  (dependencies are only pruned when the srpm-builder image already exists)
--matrix          fetch, then print {"stage1": [...], "stage2": [...]} as JSON for a CI matrix:
                  stage2 packages need a stage1 package built first; exits 1 if the chain
                  is deeper than two stages. Logs go to stderr.
--state FILE      with --matrix: what was built last time (default built-sources.txt); only the
                  packages whose source RPM or build configuration changed, and the packages
                  that depend on them, are listed ("dirty"); stage entries are
                  {"name": ..., "carry": "deps not rebuilt"}
--all             with --matrix: list every package, whatever the state says
--write-state F   with --matrix: write the sources this plan is based on to F, for the record job
--only NAME       fetch and build only that package and skip staging (one CI matrix job)
--deps DIR        RPMs from earlier stages; mounted read-only at /deps in the container
--carry-over URL  download the published site's RPMs of the packages named by --carry into
                  --deps DIR (with --only) or out-all/ (with --stage-only)
--carry NAMES     the packages whose RPMs to carry over, separated by spaces or commas
--stage-only      skip fetch and build, stage site/ from the RPMs already in out-all/
--serve           serve site/ on http://localhost:8080/ when done and print the zypper command

Signing: set GPG_PRIVATE_KEY_FILE (and GPG_PASSPHRASE if the key has one) to write
repo/repodata/repomd.xml.asc and repomd.xml.key. The public key is published as
RPM-GPG-KEY at the site root and in repo/: exported from the signing key when signing,
else a copy of the committed RPM-GPG-KEY.
"""
import argparse
import contextlib
import functools
import http.server
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Callable

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "ci"))
import fetch_srpms  # noqa: E402
import snapshot  # noqa: E402

IMAGE = "srpm-builder"
SRPMS = "srpms"
OUT = "out-all"
SITE = "site"
PORT = 8080
# build-srpm.sh variables a packages.txt line may set.
OPTION_VARS = {"WITH", "WITHOUT", "SPEC_PATCH", "DEFINES", "NOCHECK", "FLAVORS", "BASELIBS"}
# Options for build_all.py itself, not passed to build-srpm.sh as it is.
OWN_OPTIONS = {"ARCH", "SRC"}
ARCH_VALUES = {"64", "32"}
SRC_VALUES = {"videolan"}


class BuildError(Exception):
    pass


# --- choosing source RPMs ----------------------------------------------------------


version_key = fetch_srpms.version_key
srpm_version = fetch_srpms.srpm_version
matches_name = fetch_srpms.matches_name


def pick_srpm(name: str, candidates: list[str]) -> str | None:
    """Newest of the candidate paths whose file name is a source RPM of `name`."""
    found = [c for c in candidates if matches_name(os.path.basename(c), name)]
    if not found:
        return None
    return max(found, key=lambda c: version_key(srpm_version(os.path.basename(c), name)))


def find_srpm(name: str, root: str | None = None, src: str | None = None) -> str | None:
    """Newest source RPM of `name` in srpms/, where fetch_all puts every download.

    Packages with SRC=videolan are kept in srpms/videolan/, so a same-named SRPM from another
    source (a release, openSUSE) is never mistaken for it.
    """
    directory = os.path.join(root or ROOT, SRPMS, *([src] if src else []))
    files = [os.path.join(directory, f) for f in os.listdir(directory)] if os.path.isdir(directory) else []
    return pick_srpm(name, files)


# --- build order ---------------------------------------------------------------------


PC_WILDCARD = "pkgconfig(*)"


def normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def capability(requirement: str) -> str | None:
    """Capability name of a dependency string: 'pkgconfig(x264) >= 1' -> 'pkgconfig(x264)'."""
    req = requirement.strip()
    if not req or req.startswith("("):  # rich dependencies are not matched
        return None
    return req.split()[0]


def edge_matches(buildrequires: dict[str, list[str]], provides: dict[str, set[str]]) -> list[tuple[str, str, str]]:
    """Return (package, needed package, capability) for each BuildRequires another listed package provides."""
    owners: dict[str, list[str]] = {}
    for pkg, caps in provides.items():
        for cap in caps:
            owners.setdefault(cap, []).append(pkg)
    matches = []
    for pkg, reqs in buildrequires.items():
        for req in reqs:
            cap = capability(req)
            if not cap:
                continue
            found = owners.get(cap, [])
            if not found and cap.startswith("pkgconfig("):
                # A package that globs its .pc files is matched by name: SVT-AV1 ships
                # pkgconfig(SvtAv1Enc), because "svtav1enc" starts with "svtav1".
                found = [o for o in owners.get(PC_WILDCARD, []) if normalized(cap[10:-1]).startswith(normalized(o))]
            matches += [(pkg, owner, cap) for owner in found if owner != pkg]
    return matches


def build_edges(buildrequires: dict[str, list[str]], provides: dict[str, set[str]],
                satisfied: Callable[[str], bool] | None = None) -> dict[str, dict[str, str]]:
    """Return {package: {needed package: BuildRequires string that causes it}}.

    `satisfied(cap)` returning True drops the edge for that capability, because a repo the
    build container can use already provides it, so the package need not wait.
    """
    edges: dict[str, dict[str, str]] = {pkg: {} for pkg in buildrequires}
    for pkg, owner, cap in edge_matches(buildrequires, provides):
        if satisfied is None or not satisfied(cap):
            edges[pkg].setdefault(owner, cap)
    return edges


def split_stages(order: list[str], edges: dict[str, dict[str, str]]) -> tuple[list[str], list[str]]:
    """Split `order` into (stage1, stage2): stage2 packages need a stage1 package built first.

    Raises BuildError when a chain needs a third stage; the CI workflow has exactly two.
    """
    level: dict[str, int] = {}
    for name in order:  # `order` lists needed packages first
        level[name] = 1 + max((level[dep] for dep in edges.get(name, {})), default=-1)
        if level[name] > 1:
            chain = ", ".join(f"{dep} (needs {', '.join(edges[dep])})" for dep in edges[name] if level[dep])
            raise BuildError(f"{name} needs {chain}: the chain is deeper than two stages")
    return [n for n in order if level[n] == 0], [n for n in order if level[n] == 1]


def order_packages(names: list[str], edges: dict[str, dict[str, str]]) -> list[str]:
    """Topological order (needed packages first); ties keep the order of `names`."""
    remaining = {n: set(edges.get(n, {})) for n in names}
    order: list[str] = []
    while remaining:
        ready = [n for n in names if n in remaining and not remaining[n]]
        if not ready:
            raise BuildError("build order has a cycle: " + describe_cycle(remaining, edges))
        pick = ready[0]
        order.append(pick)
        del remaining[pick]
        for deps in remaining.values():
            deps.discard(pick)
    return order


def describe_cycle(remaining: dict[str, set[str]], edges: dict[str, dict[str, str]]) -> str:
    start = next(iter(remaining))
    path = [start]
    while True:
        nxt = next(d for d in remaining[path[-1]] if d in remaining)
        if nxt in path:
            cycle = path[path.index(nxt):] + [nxt]
            return "; ".join(f"{a} needs {b} because of BuildRequires {edges[a][b]}"
                             for a, b in zip(cycle, cycle[1:]))
        path.append(nxt)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def spec_info(srpm: str, env: dict[str, str]) -> tuple[list[str], set[str]]:
    """Return (BuildRequires, provided capability names) of an SRPM's spec."""
    with tempfile.TemporaryDirectory() as tmp:
        cpio = subprocess.run(f"rpm2cpio {shlex.quote(srpm)} | cpio -idm --quiet", shell=True, cwd=tmp,
                              capture_output=True, text=True)
        specs = [f for f in os.listdir(tmp) if f.endswith(".spec")]
        if cpio.returncode or not specs:
            raise BuildError(f"cannot extract a spec from {srpm}: {cpio.stderr.strip()}")
        spec = os.path.join(tmp, specs[0])
        flags = []
        for w in env.get("WITH", "").split():
            flags += ["--with", w]
        for w in env.get("WITHOUT", "").split():
            flags += ["--without", w]
        try:
            reqs = run(["rpmspec", "-q", "--buildrequires", *flags, spec]).stdout.splitlines()
            provs = run(["rpmspec", "-q", "--provides", *flags, spec]).stdout.splitlines()
            names = run(["rpmspec", "-q", "--qf", "%{NAME}\\n", *flags, spec]).stdout.splitlines()
            parsed = run(["rpmspec", "-P", *flags, spec]).stdout
        except subprocess.CalledProcessError as e:
            raise BuildError(f"rpmspec failed on {srpm}: {e.stderr.strip()}") from e
        pcs = pkgconfig_caps(parsed)
    caps = {c for c in map(capability, provs) if c} | set(names) | pcs
    return [r.strip() for r in reqs if r.strip()], caps


def pkgconfig_caps(parsed_spec: str) -> set[str]:
    """pkgconfig(NAME) for each NAME.pc the spec's %files lists, plus PC_WILDCARD for a glob.

    rpm adds these Provides while building, so rpmspec cannot report them; BuildRequires
    such as pkgconfig(x264) would otherwise never be matched to the package shipping x264.pc.
    """
    caps = {f"pkgconfig({m})" for m in re.findall(r"/pkgconfig/([^\s/*?\[\]]+)\.pc\b", parsed_spec)}
    if re.search(r"/pkgconfig/\S*[*?\[]\S*\.pc\b", parsed_spec):
        caps.add(PC_WILDCARD)  # %files globs the .pc files, so their names are unknown
    return caps


def load_packages(path: str) -> list[tuple[str, dict[str, str]]]:
    with open(path) as f:
        packages = fetch_srpms.parse_packages(f.read())
    allowed = OPTION_VARS | OWN_OPTIONS
    for name, env in packages:
        unknown = set(env) - allowed
        if unknown:
            raise BuildError(f"{name}: unknown option(s) {', '.join(sorted(unknown))}; "
                             f"allowed: {', '.join(sorted(allowed))}")
        arches = env.get("ARCH", "64").split()
        if not arches or not set(arches) <= ARCH_VALUES:
            raise BuildError(f"{name}: ARCH must be '64', '32' or '64 32', not {env['ARCH']!r}")
        if env.get("SRC", "videolan") not in SRC_VALUES:
            raise BuildError(f"{name}: SRC must be one of {', '.join(sorted(SRC_VALUES))}, not {env['SRC']!r}")
        for key in ("SPEC_PATCH", "BASELIBS"):
            if env.get(key) and not os.path.isfile(env[key] if os.path.isabs(env[key]) else os.path.join(ROOT, env[key])):
                raise BuildError(f"{name}: {key} file {env[key]!r} does not exist")
    return packages


def official_provides(engine: str, caps: set[str]) -> set[str]:
    """The subset of `caps` that the srpm-builder image's repos (openSUSE OSS, VideoLAN) provide.

    One container and one repo refresh answer all of them; zypper exits 104 for no match.
    """
    if not caps:
        return set()
    script = 'for c in "$@"; do zypper -n --gpg-auto-import-keys se -x --provides -t package "$c" ' \
             '>/dev/null 2>&1 && echo "$c"; done; true'
    proc = subprocess.run([engine, "run", "--rm", "--entrypoint", "bash", IMAGE, "-c", script, "sh", *sorted(caps)],
                          capture_output=True, text=True)
    if proc.returncode:
        raise BuildError(f"could not query the repos in image {IMAGE}: {proc.stderr.strip()}")
    return set(proc.stdout.split())


def image_exists(engine: str) -> bool:
    return subprocess.run([engine, "image", "inspect", IMAGE], capture_output=True).returncode == 0


def make_official(require_image: bool) -> Callable[[set[str]], set[str]] | None:
    """A capability checker backed by the builder image, or None when edges cannot be pruned.

    With require_image the image is (re)built first; otherwise a missing image means no pruning.
    """
    try:
        engine = container_engine()
    except BuildError:
        if require_image:
            raise
        return None
    if require_image:
        build_image(engine)  # cached layers make this cheap, and it picks up Dockerfile changes
    elif not image_exists(engine):
        print(f"note: image {IMAGE} is not built, so dependencies on official packages are not pruned",
              file=sys.stderr)
        return None
    return functools.partial(official_provides, engine)


def builds_32bit(env: dict[str, str]) -> bool:
    return "32" in env.get("ARCH", "64").split()


def resolve(packages: list[tuple[str, dict[str, str]]],
            official: Callable[[set[str]], set[str]] | None = None
            ) -> tuple[list[str], dict[str, dict[str, str]], dict[str, str]]:
    """Return (build order, edges, {name: srpm path}) for the packages.

    `official(caps)` returns which capabilities the build container's repos provide; edges
    for those are dropped.
    """
    srpms: dict[str, str] = {}
    for name, env in packages:
        path = find_srpm(name, src=env.get("SRC"))
        if not path:
            raise BuildError(f"no source RPM for {name!r} in {SRPMS}/; run without --plan to download it "
                             f"from openSUSE OSS or from a release of {fetch_srpms.RELEASE_REPO}")
        srpms[name] = path
    reqs, provs = {}, {}
    for name, env in packages:
        reqs[name], provs[name] = spec_info(srpms[name], env)
    satisfied = None
    matches = edge_matches(reqs, provs)
    if official and matches:
        provided = official({cap for _, _, cap in matches})
        satisfied = provided.__contains__
        for pkg, owner, cap in matches:
            if cap in provided:
                print(f"{pkg} does not wait for {owner}: {cap} comes from an official repo", file=sys.stderr)
    edges = build_edges(reqs, provs, satisfied)
    # "An official repo provides it" only holds for the 64-bit build. A 32-bit build needs the
    # -32bit packages, which only our own build of the provider makes, so that edge stays.
    envs = dict(packages)
    for pkg, owner, cap in matches:
        if satisfied and satisfied(cap) and builds_32bit(envs[pkg]) and builds_32bit(envs[owner]):
            edges[pkg].setdefault(owner, cap)
    return order_packages([n for n, _ in packages], edges), edges, srpms


def print_plan(order: list[str], edges: dict[str, dict[str, str]], srpms: dict[str, str]) -> None:
    for i, name in enumerate(order, 1):
        print(f"{i:2}. {name}  ({os.path.relpath(srpms[name], ROOT)})")
        for needed, cap in sorted(edges[name].items()):
            print(f"      needs {needed}, because of BuildRequires {cap}")


# --- building ------------------------------------------------------------------------


def container_engine() -> str:
    """podman or docker; set CONTAINER_ENGINE to choose one (the CI workflow uses docker)."""
    for engine in ([os.environ["CONTAINER_ENGINE"]] if os.environ.get("CONTAINER_ENGINE") else ["podman", "docker"]):
        if shutil.which(engine):
            return engine
    raise BuildError("neither podman nor docker is installed; install one to build in a container")


def container_engine_or_none() -> str | None:
    try:
        return container_engine()
    except BuildError:
        return None


def build_image(engine: str) -> None:
    print(f"== building image {IMAGE} with {engine}")
    # `build -q` prints the image id on stdout; --matrix needs stdout to carry only its JSON.
    subprocess.run([engine, "build", "-q", "-t", IMAGE, ROOT], check=True, stdout=sys.stderr)


def container_cmd(engine: str, name: str, srpm: str, env: dict[str, str], deps: str | None = None) -> list[str]:
    """The command that builds one SRPM; paths in the options are made absolute inside /in.

    `deps` is a directory of RPMs from earlier stages, mounted read-only at /deps.
    """
    cmd = [engine, "run", "--rm", "-v", f"{ROOT}:/in:ro,Z", "-v", f"{os.path.join(ROOT, OUT)}:/out:Z"]
    if deps:
        cmd += ["-v", f"{os.path.abspath(deps)}:/deps:ro,Z", "-e", "DEPS_REPO=/deps"]
    for key, val in env.items():
        if key == "SRC":
            continue  # only decides where build_all.py gets the SRPM
        if key == "ARCH":
            key = "ARCHES"
        if key in ("SPEC_PATCH", "BASELIBS") and not os.path.isabs(val):
            val = "/in/" + val
        cmd += ["-e", f"{key}={val}"]
    return cmd + [IMAGE, "/in/" + os.path.relpath(srpm, ROOT)]


def is_debug_rpm(filename: str) -> bool:
    return re.search(r"-(debuginfo|debugsource)-[^-]+-[^-]+\.[^.]+\.rpm$", filename) is not None


def prune_out(out_dir: str) -> None:
    """Remove debug packages and RPMs without files, as ci/publish.sh does for the site."""
    for fn in sorted(os.listdir(out_dir)):
        if not fn.endswith(".rpm") or fn.endswith(".src.rpm"):
            continue
        path = os.path.join(out_dir, fn)
        empty = subprocess.run(["rpm", "-qlp", path], capture_output=True, text=True).stdout.strip() \
            == "(contains no files)"
        if is_debug_rpm(fn) or empty:
            os.remove(path)


def build_all(order: list[str], envs: dict[str, dict[str, str]], srpms: dict[str, str],
              deps: str | None = None) -> None:
    engine = container_engine()
    build_image(engine)
    out_dir = os.path.join(ROOT, OUT)
    os.makedirs(out_dir, exist_ok=True)
    for i, name in enumerate(order, 1):
        print(f"== [{i}/{len(order)}] building {name}")
        proc = subprocess.run(container_cmd(engine, name, srpms[name], envs[name], deps))
        if proc.returncode:
            raise BuildError(f"build of {name} failed (exit {proc.returncode}); "
                             f"{i - 1} of {len(order)} packages built before it")
        prune_out(out_dir)


# --- staging -------------------------------------------------------------------------


def run_createrepo(engine_or_none: str | None, repo_dir: str) -> None:
    if shutil.which("createrepo_c"):
        subprocess.run(["createrepo_c", "-q", "--no-database", repo_dir], check=True)
    elif engine_or_none:
        subprocess.run([engine_or_none, "run", "--rm", "-v", f"{repo_dir}:/repo:Z", "--entrypoint",
                        "createrepo_c", IMAGE, "-q", "--no-database", "/repo"], check=True)
    else:
        raise BuildError("createrepo_c is not installed and there is no container engine to run it in")


def sign_repomd(repo_dir: str) -> bytes | None:
    """Sign repodata/repomd.xml; return the exported public key, or None when not signing."""
    key_file = os.environ.get("GPG_PRIVATE_KEY_FILE")
    if not key_file:
        return None
    repodata = os.path.join(repo_dir, "repodata")
    with tempfile.TemporaryDirectory() as home:
        os.chmod(home, 0o700)
        gpg = ["gpg", "--homedir", home, "--batch", "--yes"]
        sign = gpg + ["--pinentry-mode", "loopback", "--detach-sign", "--armor",
                      "-o", os.path.join(repodata, "repomd.xml.asc"), os.path.join(repodata, "repomd.xml")]
        passphrase = os.environ.get("GPG_PASSPHRASE") or None  # an empty secret means no passphrase
        if passphrase is not None:  # on stdin, so it never shows up in the process list
            sign[len(gpg):len(gpg)] = ["--passphrase-fd", "0"]
        try:
            subprocess.run(gpg + ["--import", key_file], check=True, capture_output=True)
            subprocess.run(sign, check=True, capture_output=True, input=(passphrase or "").encode())
            key = subprocess.run(gpg + ["--armor", "--export"], check=True, capture_output=True).stdout
        except subprocess.CalledProcessError as e:
            raise BuildError("gpg failed while signing repomd.xml (set GPG_PASSPHRASE if the key "
                             f"has one): {e.stderr.decode(errors='replace').strip()}") from e
        with open(os.path.join(repodata, "repomd.xml.key"), "wb") as f:
            f.write(key)
    return key


PUBLIC_KEY_HEADER = b"-----BEGIN PGP PUBLIC KEY BLOCK-----"


def check_public_key(data: bytes, source: str) -> bytes:
    """Only ever publish an armored public key; a private key block must never reach the site."""
    if not data.lstrip().startswith(PUBLIC_KEY_HEADER) or b"PRIVATE" in data:
        raise BuildError(f"{source} is not a PGP public key block (or it contains a private key); "
                         "not publishing it")
    return data


def public_key_for_site(signed_key: bytes | None, root: str) -> bytes | None:
    """The key to publish: the signing key when signing, else RPM-GPG-KEY in `root`, else None."""
    if signed_key is not None:
        return check_public_key(signed_key, "the exported signing key")
    path = os.path.join(root, "RPM-GPG-KEY")
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return check_public_key(f.read(), path)
    return None


def stage_site() -> None:
    out_dir, site = os.path.join(ROOT, OUT), os.path.join(ROOT, SITE)
    rpms = sorted(f for f in os.listdir(out_dir)
                  if f.endswith(".rpm") and not f.endswith(".src.rpm") and not is_debug_rpm(f)) \
        if os.path.isdir(out_dir) else []
    if not rpms:
        raise BuildError(f"no binary RPMs in {OUT}/; run without --stage-only to build them first")
    shutil.rmtree(site, ignore_errors=True)
    repo = os.path.join(site, "repo")
    os.makedirs(repo)
    for fn in rpms:
        shutil.copy2(os.path.join(out_dir, fn), repo)
    with open(os.path.join(repo, "manifest.txt"), "w") as f:
        f.write("\n".join(rpms) + "\n")
    run_createrepo(container_engine_or_none(), repo)
    signed_key = sign_repomd(repo)
    key = public_key_for_site(signed_key, ROOT)
    if key is not None:
        for target in (site, repo):
            path = os.path.join(target, "RPM-GPG-KEY")
            with open(path, "wb") as f:
                f.write(key)
            os.chmod(path, 0o644)
    env = dict(os.environ, PAGES_OWNER=os.environ.get("PAGES_OWNER", "USER"),
               PAGES_REPO=os.environ.get("PAGES_REPO", "REPO"))
    subprocess.run([os.path.join(ROOT, "scripts/generate-repo-index.sh"), site],
                   check=True, env=env)
    published = "no public key published" if key is None else \
        f"RPM-GPG-KEY from the {'signing key' if signed_key is not None else 'committed RPM-GPG-KEY'}"
    print(f"== staged {len(rpms)} RPMs in {SITE}/repo/ "
          f"({'signed' if signed_key is not None else 'unsigned'} repomd.xml; {published})")


def serve() -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=os.path.join(ROOT, SITE))
    print(f"== serving {SITE}/ on http://localhost:{PORT}/ (Ctrl-C to stop)")
    print(f"   sudo zypper ar -f -p 90 http://localhost:{PORT}/repo/ packmanless-local")
    with http.server.ThreadingHTTPServer(("", PORT), handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


# --- main ----------------------------------------------------------------------------


def fetch_all(packages: list[tuple[str, dict[str, str]]]) -> None:
    """Put the newest source RPM of every package in srpms/.

    Packages with SRC=videolan come from VideoLAN's repo; the others from openSUSE OSS, and
    what OSS lacks from the releases of this repository.
    """
    outdir = os.path.join(ROOT, SRPMS)
    from_videolan = {n for n, env in packages if env.get("SRC") == "videolan"}
    downloaded: list[str] = []
    absent: list[str] = []
    if from_videolan:
        got, missing = fetch_srpms.fetch_srpms(from_videolan, os.path.join(outdir, "videolan"),
                                               base=fetch_srpms.VIDEOLAN_BASE)
        downloaded += got
        absent += [n for n in missing if not find_srpm(n, src="videolan")]
    others = {n for n, _ in packages} - from_videolan
    got, not_in_oss = fetch_srpms.fetch_srpms(others, outdir)
    downloaded += got
    if not_in_oss:
        print("not in openSUSE OSS, looking in releases of "
              f"{fetch_srpms.RELEASE_REPO}: " + ", ".join(not_in_oss))
        try:
            more, _ = fetch_srpms.fetch_release_srpms(set(not_in_oss), outdir)
            downloaded += more
        except OSError as e:  # URLError and HTTPError; a copy already in srpms/ still works
            print(f"warning: cannot read the releases of {fetch_srpms.RELEASE_REPO}: {e}", file=sys.stderr)
    for fn in downloaded:
        print(f"fetched {fn}")
    absent += [n for n in not_in_oss if not find_srpm(n)]
    if absent:
        raise BuildError(f"no source RPM for {', '.join(absent)}: add the .src.rpm to a release of "
                         f"{fetch_srpms.RELEASE_REPO} (or put it in {SRPMS}/); a package with "
                         "SRC=videolan must exist in VideoLAN's repo")


def read_text(path: str) -> str:
    with open(path) as f:
        return f.read()


def current_sources(packages: list[tuple[str, dict[str, str]]]) -> snapshot.Sources:
    """For each package: the source RPM available now and a hash of its build configuration.

    The file names come from the OSS source repo's metadata and the release assets, so nothing is
    downloaded. The hash covers the package's options, its SPEC_PATCH file, the Dockerfile and
    build-srpm.sh: a change to any of them rebuilds the package.
    """
    names = {n for n, _ in packages}
    from_videolan = {n for n, env in packages if env.get("SRC") == "videolan"}
    files = fetch_srpms.oss_source_files(from_videolan, fetch_srpms.VIDEOLAN_BASE) if from_videolan else {}
    others = names - from_videolan
    files.update(fetch_srpms.oss_source_files(others))
    rest = others - files.keys()
    if rest:
        files.update(fetch_srpms.release_source_files(rest))
    missing = sorted(names - files.keys())
    if missing:
        raise BuildError(f"no source RPM for {', '.join(missing)} in openSUSE OSS, VideoLAN (where SRC=videolan) "
                         f"or a release of {fetch_srpms.RELEASE_REPO}")
    shared = {"Dockerfile": read_text(os.path.join(ROOT, "Dockerfile")),
              "build-srpm.sh": read_text(os.path.join(ROOT, "build-srpm.sh"))}
    sources = {}
    for name, env in packages:
        inputs = dict(shared)
        for key in ("SPEC_PATCH", "BASELIBS"):
            if env.get(key):
                path = env[key]
                inputs[key] = read_text(path if os.path.isabs(path) else os.path.join(ROOT, path))
        sources[name] = (files[name], snapshot.config_hash(name, env, inputs))
    return sources


def plan_matrix(order: list[str], edges: dict[str, dict[str, str]], current: snapshot.Sources,
                built: snapshot.Sources, everything: bool = False) -> dict:
    """The CI plan: which packages to rebuild, in which stage, and which RPMs to carry over.

    A package is rebuilt when its source or configuration changed since `built`, or when it
    depends on one that is (or when `everything`). A rebuilt package may depend on one that is
    not rebuilt; that dependency's published RPMs are listed in its "carry" so the build can use them.
    """
    dirty = set(current) if everything else snapshot.dirty_packages(current, built, edges)
    rebuilt = [n for n in order if n in dirty]
    among_rebuilt = {n: {d: c for d, c in edges.get(n, {}).items() if d in dirty} for n in rebuilt}
    stage1, stage2 = split_stages(rebuilt, among_rebuilt)

    def entry(name: str) -> dict[str, str]:
        return {"name": name, "carry": " ".join(sorted(d for d in edges.get(name, {}) if d not in dirty))}

    return {"stage1": [entry(n) for n in stage1], "stage2": [entry(n) for n in stage2],
            "dirty": rebuilt, "carry": [n for n in order if n not in dirty]}


def split_names(text: str | None) -> list[str]:
    return [n for n in re.split(r"[,\s]+", text or "") if n]


def carry_rpms(site_url: str, names: list[str], dest: str) -> None:
    """Copy the published RPMs of `names` into dest; fail if the site has none for a name."""
    downloaded, missing = fetch_srpms.carry_over(site_url, set(names), dest)
    if missing:
        raise BuildError(f"the published site has no RPMs built from {', '.join(missing)}: "
                         "run a full rebuild (the 'full' input) to publish them")
    print(f"carried over {len(downloaded)} RPMs for {', '.join(names)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print the build order and exit")
    mode.add_argument("--matrix", action="store_true", help="print the CI stages as JSON and exit")
    ap.add_argument("--only", metavar="NAME", help="fetch and build just this package, no staging")
    ap.add_argument("--deps", metavar="DIR", help="RPMs from earlier stages, mounted at /deps")
    ap.add_argument("--state", metavar="FILE", default=os.path.join(ROOT, "built-sources.txt"),
                    help="what was built last time (built-sources.txt)")
    ap.add_argument("--all", action="store_true", help="with --matrix: every package, not just changed ones")
    ap.add_argument("--write-state", metavar="FILE", help="with --matrix: write the current sources to FILE")
    ap.add_argument("--carry-over", metavar="URL", help="published site to take unchanged packages' RPMs from")
    ap.add_argument("--carry", metavar="NAMES", help="packages whose RPMs --carry-over downloads")
    ap.add_argument("--stage-only", action="store_true", help="stage site/ from the RPMs in out-all/")
    ap.add_argument("--serve", action="store_true", help="serve site/ on localhost:8080 when done")
    ap.add_argument("--packages", default=os.path.join(ROOT, "ci/packages.txt"))
    args = ap.parse_args(argv)
    try:
        carry = split_names(args.carry)
        if args.carry_over and carry:
            if args.stage_only:
                carry_rpms(args.carry_over, carry, os.path.join(ROOT, OUT))
            elif args.only and args.deps:
                carry_rpms(args.carry_over, carry, args.deps)
            else:
                raise BuildError("--carry-over needs --stage-only, or --only together with --deps")
        if args.stage_only:
            stage_site()
        else:
            packages = load_packages(args.packages)
            if args.only:
                packages = [p for p in packages if p[0].lower() == args.only.lower()]
                if not packages:
                    raise BuildError(f"{args.only!r} is not listed in {args.packages}")
            if args.matrix:
                with contextlib.redirect_stdout(sys.stderr):  # stdout carries only the JSON
                    official = None if len(packages) == 1 else make_official(require_image=True)
                    fetch_all(packages)
                    order, edges, _ = resolve(packages, official)
                    current = current_sources(packages)
                    plan = plan_matrix(order, edges, current, snapshot.read_sources(args.state), args.all)
                    if args.write_state:
                        snapshot.write_sources(args.write_state, current)
                print(json.dumps(plan))
                return 0
            # With a single package there is nothing to order, so no capability queries.
            official = None if len(packages) == 1 else make_official(require_image=not args.plan)
            if not args.plan:
                fetch_all(packages)
            order, edges, srpms = resolve(packages, official)
            print_plan(order, edges, srpms)
            if args.plan:
                return 0
            build_all(order, dict(packages), srpms, args.deps)
            if not args.only:
                stage_site()
        if args.serve:
            serve()
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
