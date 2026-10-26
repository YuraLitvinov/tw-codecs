FROM registry.opensuse.org/opensuse/tumbleweed:latest

# The base image excludes /usr/share/doc from installs, but Mesa's %install copies
# libglvnd's gl.pc/headers from /usr/share/doc/packages/libglvnd.
#
# VideoLAN's repo supplies libx264-devel and libx265-devel, which openSUSE does not ship.
RUN rm -f /usr/etc/zypp/zypp.conf.d/excludedocs.conf && \
    zypper -n ar -f -p 70 https://download.videolan.org/pub/vlc/SuSE/Tumbleweed/ videolan && \
    zypper -n --gpg-auto-import-keys ref && \
    zypper -n in --no-recommends \
        rpm-build createrepo_c \
        gcc gcc-c++ make patch tar gzip bzip2 xz cpio findutils which \
        && zypper clean -a

COPY build-srpm.sh /usr/local/bin/build-srpm
RUN chmod +x /usr/local/bin/build-srpm

# /in  - source RPMs; /out - binary RPMs + repodata, also a local repo for later builds
ENV OUT=/out BUILD_DIR=/build LOCAL_REPO=1 INSTALL_DEPS=1
VOLUME ["/in", "/out"]
WORKDIR /in
ENTRYPOINT ["/usr/local/bin/build-srpm"]
