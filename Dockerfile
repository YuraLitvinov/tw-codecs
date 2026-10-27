FROM registry.opensuse.org/opensuse/tumbleweed:latest

# The base image excludes /usr/share/doc from installs, but Mesa's %install copies
# libglvnd's gl.pc/headers from /usr/share/doc/packages/libglvnd.
#
# VideoLAN's repo supplies libx264-devel and libx265-devel, which openSUSE does not ship.
#
# libopenssl-3-devel is installed up front because four packages provide
# pkgconfig(openssl) (libopenssl-3-devel, libopenssl-devel, libressl-devel, aws-lc-devel)
# and zypper picks libressl-devel, which ffmpeg's configure rejects for GPL builds.
# OBS settles such choices with a project-wide Prefer; here the preinstall does it.
#
# perl: the ffmpeg specs run perl -pe on enable_encoders/enable_decoders, and perl provides
# pod2man for the man pages; OBS build roots always have perl, this image has to ask.
#
# zstd: some source tarballs (VideoLAN's x264, x265, fdk-aac) are .tar.zst.
RUN rm -f /usr/etc/zypp/zypp.conf.d/excludedocs.conf && \
    zypper -n ar -f -p 70 https://download.videolan.org/pub/vlc/SuSE/Tumbleweed/ videolan && \
    zypper -n --gpg-auto-import-keys ref && \
    zypper -n in --no-recommends \
        rpm-build createrepo_c \
        gcc gcc-c++ make patch tar gzip bzip2 xz cpio findutils which \
        libopenssl-3-devel perl zstd \
        && zypper clean -a

# Hardened compiler flags. openSUSE's build service sets %optflags in its project config,
# so packages built there get FORTIFY_SOURCE=3, the strong stack protector, stack-clash
# protection and CET (-fcf-protection). This container only has rpm's older default
# (FORTIFY_SOURCE=2, plain -fstack-protector), so it would build weaker binaries. The
# flags are the ones a Tumbleweed build uses. The i586 line (for the -32bit packages) adds the
# 32-bit architecture flags rpm's own i586 default carries and leaves out -fcf-protection, which
# gcc rejects together with -march=i586. They go in /etc/rpmrc, not in a macro file:
# rpm defines %optflags at the rpmrc level, which outranks anything in /etc/rpm/macros*.
RUN printf '%s\n' \
    'optflags: x86_64 -fcf-protection -O2 -Wall -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=3 -fstack-protector-strong -funwind-tables -fasynchronous-unwind-tables -fstack-clash-protection -Werror=return-type -g' \
    'optflags: i586 -O2 -Wall -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=3 -fstack-protector-strong -funwind-tables -fasynchronous-unwind-tables -fstack-clash-protection -Werror=return-type -g -m32 -march=i586 -mtune=i686' \
    > /etc/rpmrc

# RPM vendor. openSUSE's build service sets %vendor in its project config; here it is
# undefined, so every RPM would have an empty Vendor tag: zypper shows no repository name
# and has no vendor to keep stable on upgrades. zypper compares the first word of the
# vendor string when deciding whether a vendor change is allowed.
RUN echo '%vendor tw-codecs' > /etc/rpm/macros.vendor

COPY build-srpm.sh /usr/local/bin/build-srpm
RUN chmod +x /usr/local/bin/build-srpm

# /in  - source RPMs; /out - binary RPMs + repodata, also a local repo for later builds
ENV OUT=/out BUILD_DIR=/build LOCAL_REPO=1 INSTALL_DEPS=1
VOLUME ["/in", "/out"]
WORKDIR /in
ENTRYPOINT ["/usr/local/bin/build-srpm"]
