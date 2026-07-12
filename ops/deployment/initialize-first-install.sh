#!/usr/bin/env bash
# Expose only the staged data-tier units/config on a new host so a baseline backup and first schema
# migration can happen before normal activation. Refuses to run once any release is current.
set -Eeuo pipefail

release_root=${MELD7T_RELEASE_ROOT:-$HOME/.local/lib/meld7t}
config_root=${MELD7T_CONFIG_ROOT_BASE:-$HOME/.config/meld7t}
quadlet_root=${MELD7T_QUADLET_ROOT:-$HOME/.config/containers/systemd}
release_id=${1:?usage: initialize-first-install.sh RELEASE_ID --confirm-new-host}
[[ ${2:-} == --confirm-new-host ]] || { printf 'missing --confirm-new-host\n' >&2; exit 64; }
target="$release_root/releases/$release_id"
config_target="$config_root/releases/$release_id"
[[ ! -e $release_root/current && ! -e $config_root/current && ! -e $quadlet_root/meld7t-current ]] \
  || { printf 'first-install initialization is forbidden after a current release exists\n' >&2; exit 1; }
[[ -f $target/release-receipt/images.lock && -d $config_target/quadlets ]] \
  || { printf 'release/config is not staged\n' >&2; exit 1; }

atomic_link() { mkdir -p "$(dirname "$2")"; ln -sfn "$1" "$2.new"; mv -Tf "$2.new" "$2"; }
atomic_link "$target" "$release_root/current"
atomic_link "$config_target" "$config_root/current"
atomic_link "$config_target/quadlets" "$quadlet_root/meld7t-current"
systemctl --user daemon-reload
systemctl --user start postgres.service redis.service immudb.service orthanc.service \
  harmonization-postgres.service harmonization-orthanc.service
printf 'first-install data tier started; create the non-admin immudb runtime user, then take the signed baseline backup\n'
