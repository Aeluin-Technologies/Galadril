# Managed production credentials and certificates

This overlay composes the existing Istio Ambient deployment, the document
compiler, Vault Secrets Operator resources and cert-manager public certificates.
It does not consume `.local/` or the development CLI's signing keys.

Install the Gateway API, Istio Ambient, Vault Secrets Operator and cert-manager
CRDs/controllers first. Enable cert-manager's Gateway API support. Configure DNS
and route public ports 80 and 443 to `public-gateway`. Set the public hostname
and ACME contact email in `kustomization.yaml` through your GitOps overlay before
applying; the shipped `example.invalid` values intentionally cannot issue a
certificate. Test ACME against the staging directory before production issuance.
The HTTP listener serves the restricted HTTP-01 challenge; normal APIs retain
their HTTPS/JWT boundary. This overlay removes the base Vault PKI public-TLS
resource so that only cert-manager writes `galadril-public-tls`.

```sh
kubectl kustomize infrastructure/kubernetes-overlays/production
kubectl apply -k infrastructure/kubernetes-overlays/production
```

Provision Vault separately, with verified TLS and Kubernetes authentication.
Place its public CA in `vault-ca` in both `galadril` and `document-compilation`.
Bind the existing Galadril Vault role to the `galadril/vault-secrets` service
account, and the `galadril-document-compiler` role exclusively to
`document-compilation/vault-secrets`. Both roles use audience `vault`. Do not
grant compiler workers access to Vault, Kubernetes tokens or storage secrets.

Extend the existing Galadril role's KV v2 read policy for
`kv/data/galadril/hydra-runtime` and `kv/data/galadril/polis-runtime`. Their keys
must match the environment variables consumed by the existing auth Deployment.
Retain the existing policies for Registry, Gateway, Scribe and plugin credentials.
The compiler role needs only:

```hcl
path "kv/data/galadril/document-compiler-storage" {
  capabilities = ["read"]
}
```

Populate this KV record through your approved secret-management workflow with
`DOCUMENT_COMPILER_S3_ENDPOINT`, `DOCUMENT_COMPILER_S3_REGION`,
`DOCUMENT_COMPILER_S3_BUCKET`, `DOCUMENT_COMPILER_S3_ACCESS_KEY` and
`DOCUMENT_COMPILER_S3_SECRET_KEY`. Scope that identity to the compiler's private
bucket; install versioning and the seven-day output lifecycle policy at storage
provisioning. Vault syncs these values into the API Deployment's existing Secret
and rolls it when credentials change. No storage credential reaches Pengolo or
an isolated compilation process. Hydra continues issuing user tokens and
publishing JWKS; Vault does not manufacture per-user JWTs.

The compiler's public bundle init container downloads the pinned official TeX
archive, verifies its SHA-256 and publishes a complete offline TTB on its PVC.
It has no secrets or service-account token. Permit its HTTPS egress only during
initialization according to cluster policy; the existing worker remains without
network access. The worker-rootfs init container then verifies the bundle digest
before installing public resources. Pin application images in your release
overlay and provide the documented persistent volumes and mandatory Landlock
Linux kernel support.

Rendering verifies resource composition only. Certificate issuance, Vault login,
secret rotation, Istio policies and compilation must be exercised on a configured
cluster before production rollout.
