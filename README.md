# tw-codecs

Rebuilds the codec-enabled multimedia packages of an openSUSE Tumbleweed system from
source RPMs and publishes them as a zypper repository on GitHub Pages. The aim is a
system that no longer depends on the Packman repository.

Everything is driven by `build_all.py` (Python 3, standard library only) and
`ci/packages.txt`. The builds run in a container (podman or docker) from `Dockerfile`.

## Packages

`ci/packages.txt` has one package per line: a plain name in any case, then optional
`ENV=VAL` build options.

```
mesa SPEC_PATCH=ci/mesa-radeon.sed DEFINES=BUILD_ORIG=1
libheif WITH=x264
ffmpeg-9
```

The options are the variables `build-srpm.sh` reads: `WITH`, `WITHOUT`, `SPEC_PATCH`,
`DEFINES`, `NOCHECK`, `FLAVORS`. The order of the file does not matter: the build order
comes from each spec's `BuildRequires`.

## Where the source RPMs come from

No source RPM is committed. `build_all.py` downloads them into `srpms/`:

1. the current openSUSE Tumbleweed OSS source repository, matched by name (`mesa`
   selects `Mesa`);
2. for names openSUSE does not ship, the `.src.rpm` assets of this repository's GitHub
   releases (override the repository with `SRPM_RELEASE_REPO`). The newest version of
   each name is used, whichever release it is attached to.

If a name is in neither place the build stops and says which one to add. `GITHUB_TOKEN`
is used for the release lookup when it is set; it is optional for a public repository.

## Build order and stages

`BuildRequires` that another listed package provides become dependencies, unless the
builder image's repositories (openSUSE OSS and VideoLAN, for `libx264-devel` and
`libx265-devel`) already provide them; then the package does not wait.

```
python3 build_all.py --plan        # print the order and why (downloads nothing)
python3 build_all.py --matrix      # print {"stage1": [...], "stage2": [...]} as JSON
python3 build_all.py               # download, build everything, stage site/
python3 build_all.py --only NAME   # download and build one package, no staging
python3 build_all.py --only NAME --deps DIR   # ... with RPMs of earlier stages in DIR
python3 build_all.py --stage-only  # stage site/ from the RPMs in out-all/
python3 build_all.py --serve       # serve site/ on http://localhost:8080/
```

Binary RPMs go to `out-all/`. Debug packages and RPMs without files are dropped.
`site/` gets `repo/` (RPMs and `repodata`), `index.html` pages and `.nojekyll`.
Signing: set `GPG_PRIVATE_KEY_FILE` (and `GPG_PASSPHRASE` for a protected key) to write
`repomd.xml.asc` and `repomd.xml.key`.

To try the result with zypper:

```
sudo zypper ar -f -p 90 http://localhost:8080/repo/ tw-codecs-local
```

## GitHub Actions

`.github/workflows/build-matrix.yml` builds each package as its own matrix job in two
stages (`stage2` packages need a `stage1` package) and publishes `site/` with GitHub
Pages. Set *Settings -> Pages -> Source* to "GitHub Actions". Add `GPG_PRIVATE_KEY` (and
`GPG_PASSPHRASE`) as secrets to sign the repository. `.github/workflows/build.yml` is the
older nightly job that pushes a `gh-pages` branch.

## Limits

- Pages sites are capped at 1 GB; a full Mesa build can be a large part of that.
- No `-32bit` packages: those come from OBS's `baselibs.conf` step, not from `rpmbuild`.
- The matrix has two stages; a dependency chain deeper than that makes `--matrix` fail.

## Tests

```
python3 -m unittest tests.test_build_all
```

`scripts/generate-repo-index.sh` comes from
[github-pages-rpm-repo](https://github.com/sirredbeard/github-pages-rpm-repo) (MIT, see
`LICENSE.github-pages-rpm-repo`).
