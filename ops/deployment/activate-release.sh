#!/usr/bin/env bash
# Atomically switch release-scoped code/config/Quadlet symlinks, restart the complete stack, and
# restore every previous symlink if the new deployment does not become ready.
set -Eeuo pipefail

release_root=${MELD7T_RELEASE_ROOT:-$HOME/.local/lib/meld7t}
config_root=${MELD7T_CONFIG_ROOT_BASE:-$HOME/.config/meld7t}
quadlet_root=${MELD7T_QUADLET_ROOT:-$HOME/.config/containers/systemd}
user_unit_root=${MELD7T_USER_UNIT_ROOT:-$HOME/.config/systemd/user}
release_id=${1:?usage: activate-release.sh RELEASE_ID --confirm-migrated}
confirmation=${2:-}
[[ $confirmation == --confirm-migrated ]] || {
  printf 'activation requires --confirm-migrated after backup + migrate.sh\n' >&2; exit 64; }

target="$release_root/releases/$release_id"
config_target="$config_root/releases/$release_id"
[[ -d $target && -f $target/release-receipt/images.lock && -d $config_target/quadlets \
   && -f $config_target/systemd/meld7t-worker.service ]] || {
  printf 'release code/config is not completely staged: %s\n' "$release_id" >&2; exit 1; }

atomic_link() {
  local target_path=$1 link_path=$2
  mkdir -p "$(dirname "$link_path")"
  ln -sfn "$target_path" "$link_path.new"
  mv -Tf "$link_path.new" "$link_path"
}
link_target() { [[ -L $1 ]] && readlink -f "$1" || true; }

previous_release=$(link_target "$release_root/current")
previous_config=$(link_target "$config_root/current")
previous_quadlets=$(link_target "$quadlet_root/meld7t-current")
readonly -a app_units=(postgres.service redis.service immudb.service orthanc.service registry.service \
  api.service ohif.service caddy.service meld7t-worker.service)

systemctl --user stop "${app_units[@]}" 2>/dev/null || true
atomic_link "$target" "$release_root/current"
atomic_link "$config_target" "$config_root/current"
atomic_link "$config_target/quadlets" "$quadlet_root/meld7t-current"
for unit in meld7t-worker.service meld7t-health.service meld7t-health.timer; do
  atomic_link "$config_root/current/systemd/$unit" "$user_unit_root/$unit"
done
systemctl --user daemon-reload
systemctl --user enable meld7t-worker.service meld7t-health.timer >/dev/null

ready=false
if systemctl --user start "${app_units[@]}"; then
  for _attempt in $(seq 1 60); do
    api_health=$(podman inspect api --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)
    caddy_health=$(podman inspect caddy --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)
    if [[ $api_health == healthy && $caddy_health == healthy ]] \
       && systemctl --user is-active --quiet meld7t-worker.service \
       && podman exec api python -c \
          'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/readyz", timeout=5).read()' \
          >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 3
  done
fi
if $ready; then
  systemctl --user start meld7t-health.timer || true
  printf 'activated complete release/config/image set %s\n' "$release_id"
  exit 0
fi

printf 'new release failed readiness; restoring prior release-scoped symlinks\n' >&2
systemctl --user stop "${app_units[@]}" 2>/dev/null || true
if [[ -n $previous_release && -n $previous_config && -n $previous_quadlets ]]; then
  atomic_link "$previous_release" "$release_root/current"
  atomic_link "$previous_config" "$config_root/current"
  atomic_link "$previous_quadlets" "$quadlet_root/meld7t-current"
  systemctl --user daemon-reload
  systemctl --user start "${app_units[@]}" 2>/dev/null || true
else
  rm -f -- "$release_root/current" "$config_root/current" "$quadlet_root/meld7t-current"
  systemctl --user daemon-reload
fi
printf 'database migration was not reversed; verify expand/contract compatibility before retrying\n' >&2
exit 1
