#!/usr/bin/env bash
# Generates a disposable local CA; deployed proxies receive only leaf keys.
set -euo pipefail
umask 077

identity_dir="${1:-.local/identity}"
if [[ -e "$identity_dir" ]]; then
  printf 'Refusing to overwrite existing identity directory: %s\n' "$identity_dir" >&2
  exit 1
fi
mkdir -p "$identity_dir"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -keyout "$identity_dir/ca-key.pem" -out "$identity_dir/ca.pem" -days 7 \
  -subj '/CN=Galadril development workload CA' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign'

for workload in gateway registry intake vision scribe; do
  mkdir -p "$identity_dir/$workload"
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
    -keyout "$identity_dir/$workload/key.pem" -out "$identity_dir/$workload/request.pem" \
    -subj "/CN=$workload" \
    -addext "subjectAltName=URI:spiffe://galadril/$workload,DNS:$workload-proxy,DNS:localhost,IP:127.0.0.1" \
    -addext 'basicConstraints=critical,CA:FALSE' \
    -addext 'extendedKeyUsage=serverAuth,clientAuth' \
    -addext 'keyUsage=critical,digitalSignature'
  openssl x509 -req -in "$identity_dir/$workload/request.pem" \
    -CA "$identity_dir/ca.pem" -CAkey "$identity_dir/ca-key.pem" \
    -set_serial "0x$(openssl rand -hex 16)" -days 7 -copy_extensions copy \
    -out "$identity_dir/$workload/cert.pem"
  cp "$identity_dir/ca.pem" "$identity_dir/$workload/ca.pem"
  rm "$identity_dir/$workload/request.pem"
done
printf 'Development certificates created in %s; they expire in seven days.\n' "$identity_dir"
