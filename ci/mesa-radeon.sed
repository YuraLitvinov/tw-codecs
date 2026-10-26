# Trim Mesa to an AMD RX 7600S (radeonsi / RADV) plus the software fallbacks.
# Applied with: sed -E -i -f ci/mesa-radeon.sed Mesa.spec   (see SPEC_PATCH in build-srpm.sh)

# Driver lists. Only the lists naming radeonsi are touched, so the plain-flavor
# "-Dgallium-drivers=softpipe" lines stay as they are.
s/^( *-Dgallium-drivers=)[^ ]*radeonsi[^ ]*/\1radeonsi,llvmpipe,zink/
s/^( *%define vulkan_drivers ).*/\1swrast,amd/

# Options that require a driver we just dropped.
/^ *-Dgallium-d3d12-graphics=/d
/^ *-Dintel-rt=/d

# %files entries for packages whose driver is gone would fail with "File not found".
# Empty their file lists; the (empty) subpackages are then simply not produced.
/^%files -n (libvulkan_(intel|asahi|nouveau|powervr|dzn|virtio)|Mesa-dri-nouveau)$/,/^%(files|endif|if|ifarch|ifnarch|else)/{
  /^%(files|endif|if|ifarch|ifnarch|else)/!d
}
