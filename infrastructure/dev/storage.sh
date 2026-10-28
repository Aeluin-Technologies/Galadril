#!/bin/sh
# Root credentials exist only in this short-lived trusted storage initializer.
set -eu
umask 077
export MC_CONFIG_DIR=/tmp/mc
mc alias set bootstrap http://rustfs:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc ready bootstrap >/dev/null
mc mb --ignore-existing "bootstrap/$DOCUMENT_COMPILER_S3_BUCKET" >/dev/null
mc version enable "bootstrap/$DOCUMENT_COMPILER_S3_BUCKET" >/dev/null
mc admin policy create bootstrap galadril-document-compiler /bootstrap/policy.json >/dev/null
mc admin user add bootstrap "$DOCUMENT_COMPILER_S3_ACCESS_KEY" "$DOCUMENT_COMPILER_S3_SECRET_KEY" >/dev/null
mc admin policy attach bootstrap galadril-document-compiler --user "$DOCUMENT_COMPILER_S3_ACCESS_KEY" >/dev/null
mc ilm import "bootstrap/$DOCUMENT_COMPILER_S3_BUCKET" </bootstrap/lifecycle.json >/dev/null
printf '%s\n' 'Document compiler storage ready; access is limited to its private bucket.'
