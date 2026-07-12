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
   && -f $config_target/quadlets/harmonization-postgres.container \
   && -f $config_target/quadlets/harmonization-orthanc.container \
   && -f $config_target/systemd/meld7t-worker.service \
   && -f $config_target/systemd/meld7t-harmonization-builder.service \
   && -f $config_target/systemd/meld7t-health.service \
   && -f $config_target/systemd/meld7t-health.timer ]] || {
  printf 'release code/config is not completely staged: %s\n' "$release_id" >&2; exit 1; }

atomic_link() {
  local target_path=$1 link_path=$2
  mkdir -p "$(dirname "$link_path")"
  ln -sfn "$target_path" "$link_path.new"
  mv -Tf "$link_path.new" "$link_path"
}
link_target() { [[ -L $1 ]] && readlink -f "$1" || true; }

readonly -a managed_user_units=(meld7t-worker.service \
  meld7t-harmonization-builder.service meld7t-health.service meld7t-health.timer)

units_for_config() {
  local config_path=$1
  local -a units=(postgres.service redis.service immudb.service orthanc.service)
  [[ -f $config_path/quadlets/harmonization-postgres.container ]] \
    && units+=(harmonization-postgres.service)
  [[ -f $config_path/quadlets/harmonization-orthanc.container ]] \
    && units+=(harmonization-orthanc.service)
  units+=(registry.service api.service ohif.service caddy.service)
  [[ -f $config_path/systemd/meld7t-worker.service ]] && units+=(meld7t-worker.service)
  [[ -f $config_path/systemd/meld7t-harmonization-builder.service ]] \
    && units+=(meld7t-harmonization-builder.service)
  printf '%s\n' "${units[@]}"
}

link_user_units() {
  local config_path=$1 unit
  for unit in "${managed_user_units[@]}"; do
    if [[ -f $config_path/systemd/$unit ]]; then
      atomic_link "$config_path/systemd/$unit" "$user_unit_root/$unit"
    else
      rm -f -- "$user_unit_root/$unit"
    fi
  done
}

disable_user_units_absent_from() {
  local config_path=$1 unit
  for unit in meld7t-worker.service meld7t-harmonization-builder.service \
    meld7t-health.timer; do
    if [[ ! -f $config_path/systemd/$unit ]]; then
      systemctl --user disable "$unit" >/dev/null 2>&1 || true
    fi
  done
}

enable_user_units_present_in() {
  local config_path=$1 unit
  for unit in meld7t-worker.service meld7t-harmonization-builder.service \
    meld7t-health.timer; do
    if [[ -f $config_path/systemd/$unit ]]; then
      systemctl --user enable "$unit" >/dev/null
    fi
  done
}

previous_release=$(link_target "$release_root/current")
previous_config=$(link_target "$config_root/current")
previous_quadlets=$(link_target "$quadlet_root/meld7t-current")
mapfile -t app_units < <(units_for_config "$config_target")

systemctl --user stop meld7t-health.timer "${app_units[@]}" 2>/dev/null || true
disable_user_units_absent_from "$config_target"
atomic_link "$target" "$release_root/current"
atomic_link "$config_target" "$config_root/current"
atomic_link "$config_target/quadlets" "$quadlet_root/meld7t-current"
link_user_units "$config_target"
systemctl --user daemon-reload
enable_user_units_present_in "$config_target"

ready=false
if systemctl --user start "${app_units[@]}"; then
  for _attempt in $(seq 1 60); do
    api_health=$(podman inspect api --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)
    caddy_health=$(podman inspect caddy --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)
    harmonization_orthanc_health=$(podman inspect harmonization-orthanc \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)
    if [[ $api_health == healthy && $caddy_health == healthy \
          && $harmonization_orthanc_health == healthy ]] \
       && systemctl --user is-active --quiet meld7t-worker.service \
       && systemctl --user is-active --quiet meld7t-harmonization-builder.service \
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
systemctl --user stop meld7t-health.timer "${app_units[@]}" 2>/dev/null || true
if [[ -n $previous_release && -n $previous_config && -n $previous_quadlets ]]; then
  # Disable new-only units while their current fragments still exist; otherwise systemd may leave
  # dangling wants links after the destination's older unit set is restored.
  disable_user_units_absent_from "$previous_config"
  atomic_link "$previous_release" "$release_root/current"
  atomic_link "$previous_config" "$config_root/current"
  atomic_link "$previous_quadlets" "$quadlet_root/meld7t-current"
  link_user_units "$previous_config"
  systemctl --user daemon-reload
  enable_user_units_present_in "$previous_config"
  mapfile -t previous_app_units < <(units_for_config "$previous_config")
  if ! systemctl --user start "${previous_app_units[@]}" 2>/dev/null; then
    printf 'prior application unit set did not restart cleanly\n' >&2
  fi
  if [[ -f $previous_config/systemd/meld7t-health.timer ]]; then
    systemctl --user start meld7t-health.timer 2>/dev/null || true
  fi
else
  disable_user_units_absent_from ""
  rm -f -- "$release_root/current" "$config_root/current" "$quadlet_root/meld7t-current"
  for unit in "${managed_user_units[@]}"; do
    rm -f -- "$user_unit_root/$unit"
  done
  systemctl --user daemon-reload
fi
printf 'database migration was not reversed; verify expand/contract compatibility before retrying\n' >&2
exit 1
