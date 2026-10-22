# Browser outage responses

The public listeners in `local.yaml` and `gateway.yaml`, and the Kubernetes
`public-error-page` EnvoyFilter, render local 5xx errors using the shared
[static UI template](../../front/ui/src/static/service-unavailable.html).
Standalone proxies load `error-page.lua` through `default_source_code.filename`.
This generated file contains the page, logo and CSP and is compiled once per
worker; requests do not read files or contact application upstreams. Docker
Compose mounts it read-only alongside the bootstrap YAML; custom deployments
must provide it at `/etc/envoy/error-page.lua`.

The Kubernetes EnvoyFilter keeps its generated source inline because Istio
delivers it through xDS without mounting files into the managed gateway. Both
forms use the same template and generator. No browser JavaScript is needed.

Only GET/HEAD requests accepting `text/html` receive the page. JSON clients,
mutations, authentication failures and upstream application responses keep their
original bodies and status codes. Diagnostics are bounded and HTML-escaped.
The inline stylesheet is authorized by a generated CSP hash; scripts are blocked.

The displayed trace reference is the Envoy-generated `x-request-id`. Public proxy
logs and spans include this reference as `request.id` for correlation in OTLP.
A separate OpenTelemetry trace ID appears when it is available in the local reply.

Regenerate the standalone filter and Kubernetes embedded filter from the repository root:

```sh
node infrastructure/envoy/build-error-page.mjs
```

Targeted checks:

```sh
bazel test //infrastructure/envoy/tests:error_page_test
bazel test //infrastructure/envoy/tests:error_page_browser_test \
  --test_env=PLAYWRIGHT_BROWSERS_PATH=/path/to/browser-cache
bazel test //infrastructure/envoy/tests:proxy_test \
  --test_arg=ProxyIntegrationTest.test_browser_local_failures_have_an_independent_error_page \
  --test_arg=ProxyIntegrationTest.test_error_pages_escape_diagnostics_and_keep_api_failures_technical
```
