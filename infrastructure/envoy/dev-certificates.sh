#!/usr/bin/env bash
# Development identities outlive short-lived leaves and can be renewed without changing keys.
set -euo pipefail
umask 077

identity_dir="${1:-.local/identity}"
mode="${2:-}"
if [[ -n "$mode" && "$mode" != --renew ]]; then
  printf 'Usage: %s [identity-directory] [--renew]\n' "$0" >&2
  exit 1
fi
if [[ -e "$identity_dir" && "$mode" != --renew ]]; then
  printf 'Refusing to overwrite existing identity directory: %s\n' "$identity_dir" >&2
  exit 1
fi
if [[ "$mode" == --renew && ! -f "$identity_dir/ca-key.pem" ]]; then
  printf 'Renewal requires the existing development CA key.\n' >&2
  exit 1
fi
mkdir -p "$(dirname "$identity_dir")"
staging="$(mktemp -d "${identity_dir}.renew.XXXXXXXX")"
trap 'rm -rf "$staging"' EXIT
if [[ "$mode" == --renew ]]; then
  cp "$identity_dir/ca-key.pem" "$staging/ca-key.pem"
else
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$staging/ca-key.pem"
fi
openssl req -x509 -sha256 -key "$staging/ca-key.pem" -out "$staging/ca.pem" -days 365 \
  -subj '/CN=Galadril development workload CA' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign'

for workload in gateway registry intake vision scribe hydra document-compiler; do
  mkdir -p "$staging/$workload"
  if [[ "$mode" == --renew && -f "$identity_dir/$workload/key.pem" ]]; then
    cp "$identity_dir/$workload/key.pem" "$staging/$workload/key.pem"
  else
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$staging/$workload/key.pem"
  fi
  openssl req -new -key "$staging/$workload/key.pem" -out "$staging/$workload/request.pem" \
    -subj "/CN=$workload" \
    -addext "subjectAltName=URI:spiffe://galadril/$workload,DNS:$workload-proxy,DNS:localhost,IP:127.0.0.1" \
    -addext 'basicConstraints=critical,CA:FALSE' \
    -addext 'extendedKeyUsage=serverAuth,clientAuth' \
    -addext 'keyUsage=critical,digitalSignature'
  openssl x509 -req -sha256 -in "$staging/$workload/request.pem" \
    -CA "$staging/ca.pem" -CAkey "$staging/ca-key.pem" \
    -set_serial "0x$(openssl rand -hex 16)" -days 30 -copy_extensions copy \
    -out "$staging/$workload/cert.pem"
  cp "$staging/ca.pem" "$staging/$workload/ca.pem"
  rm "$staging/$workload/request.pem"
  openssl verify -CAfile "$staging/ca.pem" "$staging/$workload/cert.pem"
done
if [[ "$mode" == --renew ]]; then
  # Validate every identity before replacing any bind-mounted certificate.
  backup="$(mktemp -d "${identity_dir}.backup.XXXXXXXX")"
  cp -R "$identity_dir/." "$backup/"
  cp "$staging/ca.pem" "$identity_dir/ca.pem"
  for workload in gateway registry intake vision scribe hydra document-compiler; do
    mkdir -p "$identity_dir/$workload"
    if [[ ! -f "$identity_dir/$workload/key.pem" ]]; then
      cp "$staging/$workload/key.pem" "$identity_dir/$workload/key.pem"
    fi
    cp "$staging/$workload/cert.pem" "$identity_dir/$workload/cert.pem"
    cp "$staging/$workload/ca.pem" "$identity_dir/$workload/ca.pem"
  done
  printf 'Previous certificates backed up in %s.\n' "$backup"
else
  mv "$staging" "$identity_dir"
fi
printf 'Development CA valid for 365 days; leaf certificates valid for 30 days. Restart TLS consumers after renewal.\n'
