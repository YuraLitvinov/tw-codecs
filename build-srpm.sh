#!/bin/bash
# Rebuild source RPMs and collect the binary RPMs in $OUT.
# Runs directly on an openSUSE host, or inside the Dockerfile's container.
#
# usage: build-srpm.sh <file.src.rpm> [...]
# env:
#   OUT=./out                        where binary RPMs (and repodata) go
#   BUILD_DIR=~/.cache/srpm-build    scratch topdirs, one per SRPM
#   DEFINES="BUILD_ORIG=1 foo=bar"   extra rpm macros, space separated NAME=VALUE
#   FLAVORS="- drivers"              flavors to build ("-" = spec as shipped)
#   NOCHECK=1                        skip %check
#   WITH="x264 x265"                 spec bconds to switch on (rpmbuild --with NAME)
#   WITHOUT="vulkan"                 spec bconds to switch off (rpmbuild --without NAME)
#   SPEC_PATCH=file.sed              sed -E script applied to the spec before building
#                                    (e.g. ci/mesa-radeon.sed to trim drivers)
#   INSTALL_DEPS=1                   zypper-install the spec's BuildRequires first.
#                                    Off by default: rpmbuild then fails listing any
#                                    missing ones, and nothing is installed on the host.
#   LOCAL_REPO=1                     createrepo_c on $OUT and register it as a zypper
#                                    repo (priority 90) so later builds use RPMs you
#                                    built earlier. Off by default (edits repo list).
#   DEPS_REPO=/deps                  directory of RPMs from earlier CI stages; copied to
#                                    /tmp/deps, indexed and registered (priority 90) so they
#                                    satisfy BuildRequires. The directory is left untouched.
#
# INSTALL_DEPS and LOCAL_REPO need root; sudo is used automatically when not root.
set -euo pipefail

OUT=$(realpath -m "${OUT:-./out}")
BUILD_DIR=$(realpath -m "${BUILD_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/srpm-build}")
DEFINES=${DEFINES:-}
FLAVORS=${FLAVORS:--}
SUDO=; [[ $EUID -eq 0 ]] || SUDO=sudo

refresh_local_repo() {
    [[ -n ${LOCAL_REPO:-} ]] || return 0
    createrepo_c -q --update "$OUT"
    $SUDO zypper -n rr localout >/dev/null 2>&1 || true
    $SUDO zypper -n ar -f -G -p 90 "file://$OUT" localout >/dev/null
    $SUDO zypper -n --gpg-auto-import-keys ref >/dev/null
}

add_deps_repo() {
    [[ -n ${DEPS_REPO:-} ]] || return 0
    rm -rf /tmp/deps; cp -r "$DEPS_REPO" /tmp/deps
    createrepo_c -q /tmp/deps
    $SUDO zypper -n rr deps >/dev/null 2>&1 || true
    $SUDO zypper -n ar -f -G -p 90 file:///tmp/deps deps >/dev/null
    $SUDO zypper -n --gpg-auto-import-keys ref >/dev/null
}

mkdir -p "$OUT" "$BUILD_DIR"
refresh_local_repo
add_deps_repo

for srpm in "$@"; do
    srpm=$(realpath "$srpm")
    name=$(rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}' "$srpm" 2>/dev/null)
    top=$BUILD_DIR/$name
    rm -rf "$top"; mkdir -p "$top"
    echo "=== $name"
    rpm -ivh --define "_topdir $top" "$srpm" >/dev/null 2>&1
    spec=$(ls "$top"/SPECS/*.spec)
    [[ -z ${SPEC_PATCH:-} ]] || sed -E -i -f "$SPEC_PATCH" "$spec"

    for flavor in $FLAVORS; do
        defs=(--define "_topdir $top")
        for d in $DEFINES; do defs+=(--define "${d%%=*} ${d#*=}"); done

        for w in ${WITH:-}; do defs+=(--with "$w"); done
        for w in ${WITHOUT:-}; do defs+=(--without "$w"); done

        fspec=$spec
        if [[ $flavor != - ]]; then
            # Some specs hardcode "%global flavor %{nil}", which would override --define.
            fspec=${spec%.spec}.$flavor.spec
            sed -E "s/^%(global|define)[[:space:]]+flavor[[:space:]].*/%global flavor $flavor/" \
                "$spec" > "$fspec"
            defs+=(--define "flavor $flavor" --define "build_flavor $flavor")
        fi
        echo "=== $name flavor=$flavor defines=[$DEFINES]"

        if [[ -n ${INSTALL_DEPS:-} ]]; then
            rpmspec -q --buildrequires "${defs[@]}" "$fspec" | sort -u \
                | xargs -d '\n' -r $SUDO zypper -n in --no-recommends
        fi

        rpmbuild -bb "${defs[@]}" ${NOCHECK:+--nocheck} "$fspec"
    done

    find "$top/RPMS" -name '*.rpm' -exec cp -v {} "$OUT"/ \;
    refresh_local_repo
done
