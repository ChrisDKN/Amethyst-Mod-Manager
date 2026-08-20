#!/bin/sh
# Runtime companion for Amethyst's fuse-overlayfs VFS backend.
#
# This file is copied into the deployed profile so a Flatpak launch can run it
# on the host, where /dev/fuse, bubblewrap and Proton are available.  Keep it
# POSIX-sh compatible: SteamOS guarantees /bin/sh, but not a particular shell.

set -u

if [ "$#" -lt 10 ]; then
    echo "amethyst-vfs: invalid runtime invocation" >&2
    exit 64
fi

vfs_state=$1
game_root=$2
data_root=$3
root_layer=$4
data_layer=$5
root_upper=$6
data_upper=$7
root_work=$8
data_work=$9
shift 9

if [ "${1-}" != "--" ]; then
    echo "amethyst-vfs: missing command separator" >&2
    exit 64
fi
shift
if [ "$#" -eq 0 ]; then
    echo "amethyst-vfs: no launch command supplied" >&2
    exit 64
fi

for vfs_tool in bwrap fuse-overlayfs fusermount3 mountpoint flock; do
    if ! command -v "$vfs_tool" >/dev/null 2>&1; then
        echo "amethyst-vfs: required host tool not found: $vfs_tool" >&2
        exit 69
    fi
done
if [ ! -r /dev/fuse ] || [ ! -w /dev/fuse ]; then
    echo "amethyst-vfs: /dev/fuse is unavailable to this process" >&2
    exit 69
fi

mount_root=$vfs_state/mount/root
mount_data=$vfs_state/mount/data
mkdir -p "$mount_root" "$mount_data"

# Only one process may own a profile's mountpoints/work directories.  flock's
# lock is released by the kernel after a crash, so stale files are harmless.
exec 9>"$vfs_state/runtime.lock"
if ! flock -n 9; then
    echo "amethyst-vfs: this profile already has an active VFS launch" >&2
    exit 75
fi

root_pid=
data_pid=

unmount_one() {
    vfs_mount=$1
    if mountpoint -q "$vfs_mount"; then
        fusermount3 -u "$vfs_mount" >/dev/null 2>&1 || \
            fusermount3 -uz "$vfs_mount" >/dev/null 2>&1 || true
    fi
}

cleanup_vfs() {
    unmount_one "$mount_data"
    unmount_one "$mount_root"
    if [ -n "$data_pid" ]; then
        kill "$data_pid" >/dev/null 2>&1 || true
        wait "$data_pid" >/dev/null 2>&1 || true
    fi
    if [ -n "$root_pid" ]; then
        kill "$root_pid" >/dev/null 2>&1 || true
        wait "$root_pid" >/dev/null 2>&1 || true
    fi
}

trap cleanup_vfs EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# A prior process killed with SIGKILL can leave a disconnected FUSE mount.
# The profile lock proves it is not an active Amethyst launch, so clear it.
unmount_one "$mount_data"
unmount_one "$mount_root"

wait_for_mount() {
    vfs_mount=$1
    vfs_pid=$2
    vfs_tries=0
    while [ "$vfs_tries" -lt 100 ]; do
        if mountpoint -q "$vfs_mount"; then
            return 0
        fi
        if ! kill -0 "$vfs_pid" >/dev/null 2>&1; then
            wait "$vfs_pid" || true
            return 1
        fi
        sleep 0.05
        vfs_tries=$((vfs_tries + 1))
    done
    echo "amethyst-vfs: timed out mounting $vfs_mount" >&2
    return 1
}

fuse-overlayfs -f \
    -o "lowerdir=$root_layer:$game_root,upperdir=$root_upper,workdir=$root_work" \
    "$mount_root" &
root_pid=$!
if ! wait_for_mount "$mount_root" "$root_pid"; then
    echo "amethyst-vfs: could not mount the virtual game root" >&2
    exit 70
fi

fuse-overlayfs -f \
    -o "lowerdir=$data_layer:$data_root,upperdir=$data_upper,workdir=$data_work" \
    "$mount_data" &
data_pid=$!
if ! wait_for_mount "$mount_data" "$data_pid"; then
    echo "amethyst-vfs: could not mount the virtual data directory" >&2
    exit 70
fi

# Bind the two FUSE views onto their real paths only inside this private mount
# namespace. The actual game directory is never mounted over or modified.
bwrap --die-with-parent --dev-bind / / \
    --bind "$mount_root" "$game_root" \
    --bind "$mount_data" "$data_root" \
    -- "$@"
