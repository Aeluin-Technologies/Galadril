#!/usr/bin/env bash
# Keeps local identity secrets stable across restarts and out of version control.
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
environment_file="${1:-$script_dir/.env}"
temporary_file="$(mktemp "${environment_file}.XXXXXX")"
trap 'rm -f -- "$temporary_file"' EXIT
if [[ -e "$environment_file" ]]; then
  cat -- "$environment_file" > "$temporary_file"
else
  cat -- "$script_dir/.env.example" > "$temporary_file"
fi

# Do not evaluate dotenv as shell code or rotate credentials for existing volumes.
for name in AUTH_PUBLIC_ORIGIN AUTH_LOCAL_LOGIN_ENABLED \
  AUTH_CLIENT_SECRET AUTH_SESSION_SECRET AUTH_GATEWAY_KEY SEARXNG_SECRET \
  GALADRIL_AUTH_PASSWORD HYDRA_DATABASE_PASSWORD HYDRA_SYSTEM_SECRET \
  POLIS_DATABASE_PASSWORD POLIS_ENCRYPTION_KEY POLIS_API_KEY \
  POLIS_CLIENT_SECRET_VERIFIER SSO_PROVISIONING_KEY SCRIBE_CHATGPT_ENCRYPTION_KEY \
  KRATOS_DATABASE_PASSWORD KRATOS_COOKIE_SECRET KRATOS_CIPHER_SECRET; do
  if awk -v name="$name" '
    $0 ~ "^[[:space:]]*" name "[[:space:]]*=" {
      value = substr($0, index($0, "=") + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if (value != "" && value != "\"\"" && value != "\047\047") found = 1
    }
    END { exit !found }
  ' "$temporary_file"; then
    continue
  fi
  case "$name" in
    AUTH_PUBLIC_ORIGIN) value=https://localhost:8080 ;;
    AUTH_LOCAL_LOGIN_ENABLED) value=true ;;
    SCRIBE_CHATGPT_ENCRYPTION_KEY) value="$(openssl rand -base64 32)" ;;
    POLIS_ENCRYPTION_KEY|KRATOS_CIPHER_SECRET) value="$(openssl rand -hex 16)" ;;
    *) value="$(openssl rand -hex 32)" ;;
  esac
  replacement="$(mktemp "${environment_file}.XXXXXX")"
  if ! awk -v name="$name" '$0 !~ "^[[:space:]]*" name "[[:space:]]*="' \
    "$temporary_file" > "$replacement"; then
    rm -f -- "$replacement"
    exit 1
  fi
  printf '\n%s=%s\n' "$name" "$value" >> "$replacement"
  mv -- "$replacement" "$temporary_file"
done
# Provision the bundled worker beside local state, never inside dotenv values.
if [[ "$environment_file" == "$script_dir/.env" ]]; then
  utils_root="$(cd -- "$script_dir/../.." && pwd)/.local/utils-email"
else
  utils_root="$(dirname -- "$environment_file")/utils-email"
fi
mkdir -p -- "$utils_root/private" "$utils_root/data"
chmod 700 "$utils_root" "$utils_root/private" "$utils_root/data"
if [[ ! -e "$utils_root/private/service-key" ]]; then
  ( set -o noclobber; openssl rand -hex 32 > "$utils_root/private/service-key" )
fi
if [[ ! -e "$utils_root/private/keyring.json" ]]; then
  utils_encryption_key="$(openssl rand -base64 32)"
  ( set -o noclobber; printf '{"active":"v1","keys":{"v1":"%s"}}\n' "$utils_encryption_key" > "$utils_root/private/keyring.json" )
fi
chmod 600 "$utils_root/private/service-key" "$utils_root/private/keyring.json"
utils_uid="$(id -u)"
utils_gid="$(id -g)"
if [[ "$utils_uid" == 0 ]]; then
  utils_uid=1000
  utils_gid=1000
  chown -R "$utils_uid:$utils_gid" "$utils_root/private" "$utils_root/data"
fi
for assignment in "GALADRIL_UTILS_PRIVATE_DIR=$utils_root/private" "GALADRIL_UTILS_DATA_DIR=$utils_root/data" "GALADRIL_UTILS_UID=$utils_uid" "GALADRIL_UTILS_GID=$utils_gid"; do
  utils_name="${assignment%%=*}"
  if ! awk -v name="$utils_name" '$0 ~ "^" name "=" { found=1 } END {exit !found}' "$temporary_file"; then
    printf '\n%s\n' "$assignment" >> "$temporary_file"
  fi
done
chmod 600 "$temporary_file"
mv -- "$temporary_file" "$environment_file"
printf 'Local Compose environment ready: %s (existing values preserved).\n' "$environment_file"
