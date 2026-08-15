#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -eq 1 ]] || { echo "Usage: rollback-release.sh <release>" >&2; exit 64; }
readonly release="$1"
readonly release_root="/opt/actiontag/releases"
readonly target="${release_root}/${release}"
readonly current_link="/opt/actiontag/current"
readonly previous_link="/opt/actiontag/previous"
readonly env_file="/etc/actiontag/actiontag.env"

[[ "${EUID}" -eq 0 ]] || { echo "Rollback must run as root." >&2; exit 1; }
[[ "${release}" =~ ^[0-9a-f]{12}$ ]] || { echo "Invalid release identity." >&2; exit 1; }
[[ "$(readlink -m "${target}")" == "${release_root}/${release}" ]] || { echo "Unsafe release path." >&2; exit 1; }
[[ -f "${target}/RELEASE-MANIFEST" ]] || { echo "Target release manifest is missing." >&2; exit 1; }
grep -qx "release=${release}" "${target}/RELEASE-MANIFEST" || { echo "Target release manifest identity is wrong." >&2; exit 1; }

exec 9>/run/lock/actiontag-release.lock
flock -n 9 || { echo "Another ActionTag release operation is active." >&2; exit 1; }
readonly old="$(readlink -f "${current_link}")"
[[ -n "${old}" && "${old}" != "${target}" ]] || { echo "Rollback target is already active or current is missing." >&2; exit 1; }
readonly old_release="$(basename "${old}")"
[[ "${old}" == "${release_root}/${old_release}" && "${old_release}" =~ ^[0-9a-f]{12}$ ]] || { echo "Current release path is unsafe." >&2; exit 1; }
[[ "$(grep -c '^ACTIONTAG_RELEASE=' "${env_file}")" == 1 ]] || { echo "ActionTag release environment is missing or duplicated." >&2; exit 1; }

ln -sfn "${old}" "${previous_link}"
ln -sfn "${target}" "${current_link}"
sed -i "s/^ACTIONTAG_RELEASE=.*/ACTIONTAG_RELEASE=${release}/" "${env_file}"
systemctl restart actiontag.service
nginx -t
systemctl reload nginx

for _ in $(seq 1 30); do
  response="$(curl --fail --silent http://127.0.0.1:3021/healthz 2>/dev/null || true)"
  if [[ "${response}" == *"\"status\":\"healthy\""* && "${response}" == *"\"release\":\"${release}\""* ]]; then
    printf '%s\n' "Rollback activated ${release}: ${response}"
    exit 0
  fi
  sleep 1
done
echo "Rollback target failed health; restoring ${old_release}." >&2
ln -sfn "${old}" "${current_link}"
sed -i "s/^ACTIONTAG_RELEASE=.*/ACTIONTAG_RELEASE=${old_release}/" "${env_file}"
systemctl restart actiontag.service
for _ in $(seq 1 30); do
  response="$(curl --fail --silent http://127.0.0.1:3021/healthz 2>/dev/null || true)"
  if [[ "${response}" == *"\"status\":\"healthy\""* && "${response}" == *"\"release\":\"${old_release}\""* ]]; then
    echo "Original release ${old_release} restored after failed rollback." >&2
    exit 1
  fi
  sleep 1
done
echo "Rollback target and automatic reversion both failed; operator intervention is required." >&2
exit 1
