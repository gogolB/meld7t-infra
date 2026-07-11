#!/usr/bin/env bash
# Manual fallback for mounting the TrueNAS/ZFS durable data tier (spec §28).
#
# The persistent, version-controlled path is the systemd automount installed by
#   ansible-playbook -i ansible/inventory.ini ansible/bootstrap.yml --tags storage -K
# Use this script only for a quick, one-off manual mount (it mounts the SAME export
# with the SAME options, so the two never disagree).
#
# §28 mount contract — why each option (do NOT drop any):
#   context="…container_file_t"  NFS presents every file as type nfs_t and cannot hold the
#                                security.selinux xattr that a :z/:Z bind-relabel writes, so
#                                bind-mounting an NFS path with :z SILENTLY NO-OPS and the
#                                confined container_t domain is DENIED. A whole-export fixed
#                                label applied at mount time is the fix. Corollary: never put
#                                :z on a bind mount whose source is under this NFS tree.
#   hard                         durability — writes must eventually succeed, never fail
#                                silently (the point of retaining intermediates). Not `soft`.
#   noatime                      no metadata write on every read.
#   nconnect=N                   parallel TCP streams for bulk sequential rsync throughput.
#   vers=4.2                     NFSv4.2.
#   sec=sys                      switch to krb5 if a KDC (the AD in §9) is reachable.
set -euo pipefail

NAS_HOST="${NAS_HOST:-192.168.5.10}"
NAS_EXPORT="${NAS_EXPORT:-/mnt/tank/data/Projects/meld7t/data}"
MOUNT_POINT="${MOUNT_POINT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/data}"
NCONNECT="${NCONNECT:-4}"
SEC="${SEC:-sys}"
CONTEXT="${CONTEXT:-system_u:object_r:container_file_t:s0}"

OPTS="vers=4.2,hard,noatime,nconnect=${NCONNECT},sec=${SEC},context=\"${CONTEXT}\""

if mountpoint -q "$MOUNT_POINT"; then
  echo "already mounted: $MOUNT_POINT"
  echo "  current options: $(findmnt -no OPTIONS "$MOUNT_POINT")"
  if ! findmnt -no OPTIONS "$MOUNT_POINT" | grep -q "context="; then
    echo ">> WARNING: mounted WITHOUT an SELinux context= label — container_t will be denied."
    echo ">>          Unmount and re-run this script:  sudo umount ${MOUNT_POINT} && $0"
  fi
  exit 0
fi

mkdir -p "$MOUNT_POINT"
echo "mounting ${NAS_HOST}:${NAS_EXPORT} -> ${MOUNT_POINT}"
echo "  options: ${OPTS}"
sudo mount -t nfs4 -o "${OPTS}" "${NAS_HOST}:${NAS_EXPORT}" "${MOUNT_POINT}"

echo "== SELinux label check (must be container_file_t, NOT nfs_t) =="
ls -Zd "$MOUNT_POINT"
