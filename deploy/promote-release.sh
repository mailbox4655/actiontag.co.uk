#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 5 ]]; then
  echo "Usage: promote-release.sh <release> <artifact.tar.gz> <sha256> <public-url> <verification-url>" >&2
  exit 64
fi

readonly release="$1"
readonly artifact="$2"
readonly expected_sha256="$3"
readonly public_url="$4"
readonly verification_url="$5"
readonly release_root="/opt/actiontag/releases"
readonly current_link="/opt/actiontag/current"
readonly previous_link="/opt/actiontag/previous"
readonly new_release="${release_root}/${release}"
readonly incoming_release="${new_release}.incoming"
readonly env_file="/etc/actiontag/actiontag.env"
readonly nginx_snippet="/etc/nginx/snippets/actiontag-managed.conf"
readonly nginx_site="/etc/nginx/sites-available/actiontag"
readonly nginx_enabled="/etc/nginx/sites-enabled/actiontag"
readonly backup_root="/var/backups/actiontag"

keep_new=0
rollback_needed=0
old_release=""
backup_dir=""

fail() { echo "ActionTag release promotion failed: $*" >&2; exit 1; }

validated_release_path() {
  local candidate="$1" base
  base="$(basename "${candidate}")"
  [[ "${base}" =~ ^[0-9a-f]{12}$ ]] || return 1
  [[ "$(dirname "${candidate}")" == "${release_root}" ]] || return 1
  [[ "$(readlink -m "${candidate}")" == "${release_root}/${base}" ]]
}

save_file() {
  local source="$1" key="$2"
  if [[ -e "${source}" ]]; then
    cp -a -- "${source}" "${backup_dir}/${key}"
    : > "${backup_dir}/${key}.present"
  else
    : > "${backup_dir}/${key}.absent"
  fi
}

restore_file() {
  local target="$1" key="$2"
  if [[ -f "${backup_dir}/${key}.present" ]]; then
    cp -a -- "${backup_dir}/${key}" "${target}"
  elif [[ -f "${backup_dir}/${key}.absent" ]]; then
    rm -f -- "${target}"
  else
    return 1
  fi
}

rollback_install() {
  local previous_before
  set +e
  echo "Promotion failed after host mutation; restoring the recorded ActionTag state." >&2
  systemctl disable --now actiontag-health-watch.timer actiontag-health-watch.service actiontag.service cloudflared-actiontag.service >/dev/null 2>&1
  restore_file "${env_file}" env
  restore_file "${nginx_snippet}" nginx-snippet
  restore_file "${nginx_site}" nginx-site
  restore_file /etc/systemd/system/actiontag.service unit-actiontag
  restore_file /etc/systemd/system/actiontag-health-watch.service unit-health-service
  restore_file /etc/systemd/system/actiontag-health-watch.timer unit-health-timer
  restore_file /etc/systemd/system/cloudflared-actiontag.service unit-cloudflared
  if [[ -f "${backup_dir}/nginx-enabled.target" ]]; then
    ln -sfn "$(<"${backup_dir}/nginx-enabled.target")" "${nginx_enabled}"
  else
    rm -f -- "${nginx_enabled}"
  fi
  if [[ -n "${old_release}" ]]; then
    ln -sfn "${old_release}" "${current_link}"
  else
    rm -f -- "${current_link}"
  fi
  previous_before="$(sed -n 's/^previous=//p' "${backup_dir}/links-before.txt")"
  if [[ -n "${previous_before}" ]] && validated_release_path "${previous_before}"; then
    ln -sfn "${previous_before}" "${previous_link}"
  else
    rm -f -- "${previous_link}"
  fi
  systemctl daemon-reload
  nginx -t && systemctl reload nginx
  if [[ -n "${old_release}" ]]; then
    systemctl enable --now actiontag.service cloudflared-actiontag.service >/dev/null 2>&1
    systemctl enable --now actiontag-health-watch.timer >/dev/null 2>&1
  fi
  set -e
}

cleanup() {
  rm -f -- "${artifact}"
  if [[ "${0}" =~ ^/var/tmp/actiontag-promote-${release}-[0-9a-f]{32}\.sh$ ]]; then
    rm -f -- "${0}"
  fi
  if [[ -d "${incoming_release}" ]]; then rm -rf -- "${incoming_release}"; fi
  if [[ "${keep_new}" -ne 1 && -d "${new_release}" ]]; then
    local active
    active="$(readlink -f "${current_link}" 2>/dev/null || true)"
    if [[ "${active}" != "${new_release}" ]]; then rm -rf -- "${new_release}"; fi
  fi
}

on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ "${rc}" -ne 0 && "${rollback_needed}" -eq 1 ]]; then
    rollback_install || echo "Automatic host-state rollback encountered an error." >&2
  fi
  cleanup
  exit "${rc}"
}
trap on_exit EXIT INT TERM

[[ "${EUID}" -eq 0 ]] || fail "must run as root"
command -v flock >/dev/null || fail "flock is unavailable"
exec 9>/run/lock/actiontag-release.lock
flock -n 9 || fail "another ActionTag release operation is active"
[[ "${release}" =~ ^[0-9a-f]{12}$ ]] || fail "invalid release identity"
[[ "${expected_sha256}" =~ ^[0-9a-f]{64}$ ]] || fail "invalid SHA-256"
[[ "${artifact}" =~ ^/var/tmp/actiontag-${release}-[0-9a-f]{32}-linux-x64\.tar\.gz$ ]] || fail "unexpected artifact path"
[[ "${public_url}" == "https://actiontag.co.uk" ]] || fail "unexpected public URL"
[[ "${verification_url}" =~ ^https://[A-Za-z0-9.-]+$ ]] || fail "invalid verification URL"
validated_release_path "${new_release}" || fail "unsafe new release path"
[[ ! -e "${new_release}" && ! -e "${incoming_release}" ]] || fail "release path already exists"
[[ -f "${artifact}" ]] || fail "uploaded artifact is missing"
[[ -x /opt/node24/bin/node ]] || fail "the pinned Node 24 runtime is missing"
[[ -x /usr/local/bin/cloudflared ]] || fail "cloudflared is missing"
[[ -s /etc/cloudflared/actiontag-token ]] || fail "the ActionTag tunnel token is missing"
[[ "$(stat -c '%a' /etc/cloudflared/actiontag-token)" =~ ^(400|600)$ ]] || fail "the ActionTag tunnel token must have mode 0400 or 0600"

getent group actiontag >/dev/null || groupadd --system actiontag
id actiontag >/dev/null 2>&1 || useradd --system --gid actiontag --home-dir /nonexistent --shell /usr/sbin/nologin actiontag
install -d -o actiontag -g www-data -m 0750 /opt/actiontag "${release_root}"
install -d -o root -g actiontag -m 0750 /etc/actiontag
install -d -o actiontag -g actiontag -m 0750 /var/lib/actiontag
install -d -o root -g root -m 0700 "${backup_root}"

old_release="$(readlink -f "${current_link}" 2>/dev/null || true)"
if [[ -n "${old_release}" ]]; then validated_release_path "${old_release}" || fail "active ActionTag release path is unsafe"; fi

readonly timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="${backup_root}/${timestamp}-pre-${release}"
install -d -o root -g root -m 0700 "${backup_dir}"
save_file "${env_file}" env
save_file "${nginx_snippet}" nginx-snippet
save_file "${nginx_site}" nginx-site
save_file /etc/systemd/system/actiontag.service unit-actiontag
save_file /etc/systemd/system/actiontag-health-watch.service unit-health-service
save_file /etc/systemd/system/actiontag-health-watch.timer unit-health-timer
save_file /etc/systemd/system/cloudflared-actiontag.service unit-cloudflared
if [[ -L "${nginx_enabled}" ]]; then readlink "${nginx_enabled}" > "${backup_dir}/nginx-enabled.target"; fi
printf 'current=%s\nprevious=%s\n' "${old_release}" "$(readlink -f "${previous_link}" 2>/dev/null || true)" > "${backup_dir}/links-before.txt"
nginx -T > "${backup_dir}/nginx-before.txt" 2>&1
(cd "${backup_dir}" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS && sha256sum -c SHA256SUMS)

printf '%s  %s\n' "${expected_sha256}" "${artifact}" | sha256sum -c -
if tar -tzf "${artifact}" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then fail "artifact contains an unsafe path"; fi
install -d -o actiontag -g www-data -m 0750 "${incoming_release}"
tar -xzf "${artifact}" -C "${incoming_release}"
readonly manifest="${incoming_release}/RELEASE-MANIFEST"
[[ -f "${manifest}" ]] || fail "release manifest is missing"
grep -qx 'schema=actiontag-linux-release-v1' "${manifest}" || fail "release manifest schema is wrong"
grep -qx "release=${release}" "${manifest}" || fail "release manifest identity is wrong"
grep -qx "commit=${release}[0-9a-f]\{28\}" "${manifest}" || fail "release manifest commit is wrong"
grep -qx 'platform=linux-x64' "${manifest}" || fail "release platform is wrong"
grep -qx "public_url=${public_url}" "${manifest}" || fail "release public URL is wrong"
[[ -f "${incoming_release}/site/dist/en/index.html" ]] || fail "English static page is missing"
[[ -f "${incoming_release}/site/server/contact-service.mjs" ]] || fail "contact service is missing"
[[ -f "${incoming_release}/deploy/actiontag-managed.nginx" ]] || fail "managed Nginx snippet is missing"
if find "${incoming_release}" -type f \( -name '.env' -o -name '.env.local' -o -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \) -print -quit | grep -q .; then
  fail "release contains an environment or database file"
fi
chown -R actiontag:www-data "${incoming_release}"
find "${incoming_release}" -type d -exec chmod 0750 {} +
find "${incoming_release}" -type f -exec chmod 0640 {} +
chmod 0750 "${incoming_release}/deploy/promote-release.sh" "${incoming_release}/deploy/rollback-release.sh"
chmod 0750 "${incoming_release}/deploy/health-watch.py"

rollback_needed=1
readonly lasertagsale_env="/etc/lasertagsale/lasertagsale.env"
[[ -r "${lasertagsale_env}" ]] || fail "LaserTagSale environment is unavailable for approved Postmark reuse"
for name in POSTMARK_SERVER_TOKEN POSTMARK_FROM_EMAIL POSTMARK_FROM_NAME POSTMARK_MESSAGE_STREAM; do
  [[ "$(grep -c "^${name}=" "${lasertagsale_env}")" == 1 ]] || fail "LaserTagSale environment is missing or duplicates ${name}"
done

if [[ ! -f "${env_file}" ]]; then
  env_tmp="$(mktemp /etc/actiontag/actiontag.env.XXXXXX)"
  chmod 0600 "${env_tmp}"
  {
    printf 'ACTIONTAG_RELEASE=%s\n' "${release}"
    printf 'ACTIONTAG_PORT=3021\n'
    printf 'ACTIONTAG_TRUSTED_HOSTS=actiontag.co.uk,www.actiontag.co.uk,actiontag.redevelopingdeveloper.uk\n'
    printf 'ACTIONTAG_LOCAL_HEALTH_URL=http://127.0.0.1:3021/healthz\n'
    printf 'CONTACT_TO=sales@actiontag.co.uk\n'
    printf 'CONTACT_CC=office@actiontag.co.uk\n'
    for name in POSTMARK_SERVER_TOKEN POSTMARK_FROM_EMAIL POSTMARK_FROM_NAME POSTMARK_MESSAGE_STREAM; do
      grep -m1 "^${name}=" "${lasertagsale_env}"
    done
  } > "${env_tmp}"
  install -o root -g actiontag -m 0640 "${env_tmp}" "${env_file}"
  rm -f -- "${env_tmp}"
else
  [[ "$(grep -c '^ACTIONTAG_RELEASE=' "${env_file}")" == 1 ]] || fail "ACTIONTAG_RELEASE is missing or duplicated"
  grep -qx 'CONTACT_TO=sales@actiontag.co.uk' "${env_file}" || fail "CONTACT_TO differs from owner-approved recipient"
  grep -qx 'CONTACT_CC=office@actiontag.co.uk' "${env_file}" || fail "CONTACT_CC differs from owner-approved copy recipient"
  for name in POSTMARK_SERVER_TOKEN POSTMARK_FROM_EMAIL POSTMARK_FROM_NAME POSTMARK_MESSAGE_STREAM; do
    [[ "$(grep -c "^${name}=" "${env_file}")" == 1 ]] || fail "${name} is missing or duplicated"
    [[ "$(grep -m1 "^${name}=" "${env_file}")" == "$(grep -m1 "^${name}=" "${lasertagsale_env}")" ]] || fail "${name} differs from the approved LaserTagSale configuration"
  done
  sed -i "s/^ACTIONTAG_RELEASE=.*/ACTIONTAG_RELEASE=${release}/" "${env_file}"
fi

install -o root -g root -m 0644 "${incoming_release}/deploy/actiontag-managed.nginx" "${nginx_snippet}"
if [[ ! -f "${nginx_site}" ]]; then
  install -o root -g root -m 0644 "${incoming_release}/deploy/actiontag-site.nginx" "${nginx_site}"
else
  grep -qF 'include /etc/nginx/snippets/actiontag-managed.conf;' "${nginx_site}" || fail "existing ActionTag Nginx site does not retain the managed include"
fi
ln -sfn "${nginx_site}" "${nginx_enabled}"
install -o root -g root -m 0644 "${incoming_release}/deploy/actiontag.service" /etc/systemd/system/actiontag.service
install -o root -g root -m 0644 "${incoming_release}/deploy/actiontag-health-watch.service" /etc/systemd/system/actiontag-health-watch.service
install -o root -g root -m 0644 "${incoming_release}/deploy/actiontag-health-watch.timer" /etc/systemd/system/actiontag-health-watch.timer
install -o root -g root -m 0644 "${incoming_release}/deploy/cloudflared-actiontag.service" /etc/systemd/system/cloudflared-actiontag.service

mv "${incoming_release}" "${new_release}"
if [[ -n "${old_release}" ]]; then ln -sfn "${old_release}" "${previous_link}"; fi
ln -sfn "${new_release}" "${current_link}"
systemctl daemon-reload
nginx -t
systemctl reload nginx
systemctl enable --now cloudflared-actiontag.service actiontag.service actiontag-health-watch.timer

activation_ok=0
for _ in $(seq 1 45); do
  response="$(curl --fail --silent http://127.0.0.1:3021/healthz 2>/dev/null || true)"
  if [[ "${response}" == *"\"status\":\"healthy\""* && "${response}" == *"\"release\":\"${release}\""* ]]; then
    activation_ok=1
    break
  fi
  sleep 1
done
[[ "${activation_ok}" -eq 1 ]] || fail "loopback contact-service health did not become ready"
local_proxy="$(curl --fail --silent -H 'Host: actiontag.redevelopingdeveloper.uk' http://127.0.0.1/healthz)"
[[ "${local_proxy}" == *"\"release\":\"${release}\""* ]] || fail "Nginx health did not report the release"
[[ "$(curl --silent --output /dev/null --write-out '%{http_code}' -H 'Host: www.actiontag.co.uk' http://127.0.0.1/en/)" == 301 ]] || fail "local www canonical redirect is missing"

public_ok=0
for _ in $(seq 1 60); do
  public_health="$(curl --fail --silent --show-error "${verification_url}/healthz" 2>/dev/null || true)"
  if [[ "${public_health}" == *"\"status\":\"healthy\""* && "${public_health}" == *"\"release\":\"${release}\""* ]]; then
    public_ok=1
    break
  fi
  sleep 2
done
[[ "${public_ok}" -eq 1 ]] || fail "public verification health did not become ready"
[[ "$(curl --location --silent --output /dev/null --write-out '%{http_code}' "${verification_url}/")" == 200 ]] || fail "public preview root did not reach a successful page"
curl --fail --silent "${verification_url}/en/" | grep -Fq 'https://actiontag.co.uk/en/' || fail "public preview canonical URL is wrong"
curl --fail --silent https://lasertagsale.com/api/health | grep -Fq '"status":"healthy"' || fail "neighbor LaserTagSale health failed after Nginx reload"

systemctl is-active --quiet actiontag.service cloudflared-actiontag.service nginx
systemctl is-enabled --quiet actiontag-health-watch.timer
[[ "$(readlink -f "${current_link}")" == "${new_release}" ]] || fail "current release link is wrong"
keep_new=1
rollback_needed=0

printf 'ActionTag release %s promoted successfully.\n' "${release}"
printf 'Backup: %s\n' "${backup_dir}"
printf 'Health: %s\n' "${public_health}"
df -h "${release_root}"
