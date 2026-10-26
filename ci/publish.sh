#!/bin/bash
# Run inside the srpm-builder image: merge freshly built RPMs into the repo tree and
# regenerate its metadata.
# usage: publish.sh <new-rpm-dir> <site-dir>      env: KEEP=2 (versions kept per package)
set -euo pipefail
new=$1 site=$2 keep=${KEEP:-2}
mkdir -p "$site/x86_64"

# Debug packages are not published (building them is what strips the real binaries).
# Neither are subpackages with no files: the trimmed spec leaves e.g. libvulkan_intel
# empty, but it would still Provide that name and could replace a working package.
while IFS= read -r -d '' f; do
    if [[ $(rpm -qlp "$f" 2>/dev/null) == "(contains no files)" ]]; then
        echo "skip empty: ${f##*/}"; continue
    fi
    cp -v "$f" "$site/x86_64/"
done < <(find "$new" -name '*.rpm' ! -name '*.src.rpm' ! -name '*-debuginfo-*' \
              ! -name '*-debugsource-*' -print0)

# Keep only the newest $keep versions of every package.
for f in "$site"/x86_64/*.rpm; do
    rpm -qp --qf '%{NAME}\t%{VERSION}-%{RELEASE}\t' "$f" 2>/dev/null; echo "$f"
done | sort -t$'\t' -k1,1 -k2,2Vr \
     | awk -F'\t' -v keep="$keep" '{ if (++n[$1] > keep) print $3 }' \
     | xargs -r rm -v

createrepo_c -q --no-database --general-compress-type gz "$site"
