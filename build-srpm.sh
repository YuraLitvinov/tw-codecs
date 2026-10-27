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
#   ARCHES="64 32"                   what to build: "64" (default) is the x86_64 RPMs, "32" adds the
#                                    -32bit RPMs (an i586 build repackaged by mkbaselibs); "32"
#                                    alone copies only the -32bit RPMs to $OUT
#   BASELIBS=file.conf               baselibs.conf to use for the -32bit packages instead of the one
#                                    in the SRPM (some are missing or name packages that no longer exist)
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
ARCHES=${ARCHES:-64}
SUDO=; [[ $EUID -eq 0 ]] || SUDO=sudo
want() { [[ " $ARCHES " == *" $1 "* ]]; }

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

# Build the i586 RPMs of a spec on this x86_64 machine and repackage them as -32bit RPMs.
# openSUSE publishes no i586 repository, so the 32-bit libraries come from the -32bit packages:
# the headers come from the 64-bit -devel package, the 32-bit libraries from its -32bit twin.
# usage: build_32bit <top> <spec> <rpmbuild args...>
build_32bit() {
    local top=$1 spec=$2; shift 2
    $SUDO zypper -n in --no-recommends gcc-32bit gcc-c++-32bit glibc-devel-32bit build-mkbaselibs >/dev/null
    # Twins that do not exist are skipped by zypper, hence the || true.
    rpm -qa --qf '%{NAME}\n' | grep -e '-devel$' | sed 's/$/-32bit/' | sort -u \
        | xargs -r $SUDO zypper -n in --no-recommends >/dev/null 2>&1 || true

    rm -rf "$top/RPMS/i586"
    # rpm keeps the machine's own triplet as %_host, so %configure would say --host=x86_64-suse-linux
    # and a configure script like x264's adds -m64 on top of -m32; define both as i586 for a native build.
    CC='gcc -m32' CXX='g++ -m32' PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig:/usr/share/pkgconfig \
        linux32 rpmbuild -bb --target=i586 --nodeps \
        --define "_host i586-suse-linux" --define "_build i586-suse-linux" "$@" "$spec"

    local conf=$top/baselibs.all.conf
    cat /usr/lib/build/baselibs_global.conf "${BASELIBS:-$top/SOURCES/baselibs.conf}" > "$conf" 2>/dev/null \
        || { echo "error: no baselibs.conf for $(basename "$top"): set BASELIBS= to one" >&2; return 1; }
    local out32=/usr/src/packages/RPMS/x86_64
    rm -f "$out32"/*-32bit*.rpm
    # Debug packages are not repackaged; mkbaselibs prints "no targets" for the rest.
    find "$top/RPMS/i586" -name '*.rpm' ! -name '*-debuginfo-*' ! -name '*-debugsource-*' -print0 \
        | xargs -0 /usr/lib/build/mkbaselibs -c "$conf"
    local found=0
    for f in "$out32"/*-32bit*.rpm; do
        [[ -e $f ]] || continue
        cp -v "$f" "$OUT"/; found=1
    done
    if [[ $found -eq 0 ]]; then
        echo "error: mkbaselibs made no -32bit RPM for $(basename "$top")" >&2
        return 1
    fi
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
    # Some specs date the build from <name>.changes (reproducible builds), and the SRPMs in
    # VideoLAN's repo do not carry that file. A stand-in dated with the SRPM's build time works.
    changes=$top/SOURCES/$(basename "${spec%.spec}").changes
    if [[ ! -e $changes ]]; then
        : > "$changes"
        touch -d "@$(rpm -qp --qf '%{BUILDTIME}' "$srpm" 2>/dev/null)" "$changes"
    fi
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

        if want 64; then
            rpmbuild -bb "${defs[@]}" ${NOCHECK:+--nocheck} "$fspec"
        fi
        if want 32; then
            build_32bit "$top" "$fspec" "${defs[@]}" ${NOCHECK:+--nocheck}
        fi
    done

    # The i586 RPMs are only an intermediate step and are never published.
    if want 64; then
        find "$top/RPMS" \( -path '*/x86_64/*' -o -path '*/noarch/*' \) -name '*.rpm' -exec cp -v {} "$OUT"/ \;
    fi
    refresh_local_repo
done
