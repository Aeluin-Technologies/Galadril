#!/bin/sh
# Development-only capabilities. Runtime services never receive admin credentials.
set -eu
: "${TERMINUSDB_ADMIN_PASS:?Set TERMINUSDB_ADMIN_PASS}"
endpoint=http://terminusdb:6363/api
admin_request() {
    curl --fail --silent --show-error --connect-timeout 5 --max-time 30 \
        --user "admin:${TERMINUSDB_ADMIN_PASS}" \
        --header 'Content-Type: application/json' "$@"
}
ensure_resource() {
    resource=$1
    create_path=$2
    payload=$3
    status=$(curl --silent --show-error --connect-timeout 5 --max-time 30 \
        --output /dev/null --write-out '%{http_code}' \
        --user "admin:${TERMINUSDB_ADMIN_PASS}" "$endpoint/$resource")
    case "$status" in
        200) return ;;
        404) admin_request --request POST "$endpoint/$create_path" --data "$payload" >/dev/null ;;
        *) echo "TerminusDB provisioning failed: HTTP $status" >&2; exit 1 ;;
    esac
}
admin_request --retry 3 --retry-connrefused --retry-delay 1 "$endpoint/info" >/dev/null
ensure_resource roles/galadril-writer roles '{"name":"galadril-writer","action":["branch","instance_read_access","instance_write_access","schema_read_access","commit_read_access","commit_write_access","meta_read_access","meta_write_access"]}'
for scope in tenant_a tenant_b bases; do
    ensure_resource "db/admin/$scope" "db/admin/$scope" "{\"label\":\"$scope\",\"schema\":false,\"prefixes\":{\"@base\":\"terminusdb:///data/\",\"@schema\":\"terminusdb:///schema#\"}}"
    ensure_resource "users/galadril_$scope" users "{\"name\":\"galadril_$scope\",\"password\":\"development_$scope\"}"
    admin_request --request POST "$endpoint/capabilities" --data "{\"operation\":\"grant\",\"scope_type\":\"database\",\"scope\":\"admin/$scope\",\"user\":\"galadril_$scope\",\"roles\":[\"galadril-writer\"]}" >/dev/null
    curl --fail --silent --show-error --connect-timeout 5 --max-time 30 \
        --user "galadril_$scope:development_$scope" "$endpoint/document/admin/$scope?as_list=true" >/dev/null
done
for pair in tenant_a:tenant_b tenant_b:tenant_a; do
    source=${pair%:*}
    target=${pair#*:}
    status=$(curl --silent --show-error --connect-timeout 5 --max-time 30 \
        --output /dev/null --write-out '%{http_code}' \
        --user "galadril_$source:development_$source" "$endpoint/document/admin/$target?as_list=true")
    case "$status" in
        403) ;;
        *) echo "TerminusDB isolation probe failed: HTTP $status" >&2; exit 1 ;;
    esac
done
