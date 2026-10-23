-- The page is compiled once per worker and remains available without application upstreams.
local PAGE = [==[<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="robots" content="noindex, nofollow" />
    <link rel="icon" href="data:," />
    <title>Service unavailable · Galadril</title>
    <style>
      :root {
  color-scheme: light;
  --ui-canvas: 248 250 252;
  --ui-surface: 255 255 255;
  --ui-surface-muted: 241 245 249;
  --ui-surface-hover: 226 232 240;
  --ui-border: 226 232 240;
  --ui-border-control: 100 116 139;
  --ui-foreground: 23 33 47;
  --ui-muted: 82 97 115;
  --ui-primary: 245 158 11;
  --ui-primary-hover: 217 119 6;
  --ui-primary-active: 180 83 9;
  --ui-on-primary: 23 33 47;
  --ui-on-primary-active: 255 255 255;
  --ui-primary-subtle: 255 251 235;
  --ui-primary-foreground: 146 64 14;
  --ui-focus: 146 64 14;
  --ui-info: 29 78 216;
  --ui-info-subtle: 239 246 255;
  --ui-success: 4 120 87;
  --ui-success-subtle: 236 253 245;
  --ui-warning: 146 64 14;
  --ui-warning-subtle: 255 251 235;
  --ui-danger: 185 28 28;
  --ui-danger-subtle: 254 242 242;
  --ui-radius-control: 8px;
  --ui-radius-panel: 12px;
  --ui-radius-dialog: 16px;
  --ui-shadow-panel: 0 1px 2px rgb(23 33 47 / 0.06);
  --ui-shadow-popover: 0 4px 12px rgb(23 33 47 / 0.1);
  --ui-shadow-dialog: 0 16px 48px rgb(23 33 47 / 0.16);
  --ui-duration-fast: 120ms;
  --ui-duration: 180ms;
}
      * {
        box-sizing: border-box;
      }
      body {
        margin: 0;
        background: rgb(var(--ui-canvas));
        color: rgb(var(--ui-foreground));
        font-family:
          Inter,
          -apple-system,
          BlinkMacSystemFont,
          "Segoe UI",
          sans-serif;
        -webkit-font-smoothing: antialiased;
      }
      .halos {
        position: fixed;
        inset: 0;
        pointer-events: none;
        background:
          radial-gradient(
            ellipse 24rem 22rem at 15% 22%,
            rgb(var(--ui-primary) / 0.1),
            transparent 75%
          ),
          radial-gradient(
            ellipse 26rem 24rem at 88% 78%,
            rgb(var(--ui-primary) / 0.07),
            transparent 75%
          );
      }
      main {
        position: relative;
        display: grid;
        grid-template-columns: minmax(0, 1.4fr) minmax(14rem, 0.8fr);
        gap: clamp(3rem, 7vw, 7rem);
        align-items: center;
        max-width: 76rem;
        min-height: 100svh;
        margin: 0 auto;
        padding: clamp(1.5rem, 5vw, 5rem);
      }
      .eyebrow {
        margin: 0 0 1.25rem;
        font-size: 0.75rem;
        font-weight: 600;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: rgb(var(--ui-muted));
      }
      h1 {
        margin: 0;
        font-size: clamp(2.25rem, 4vw, 3.5rem);
        line-height: 1.1;
        letter-spacing: -0.045em;
        font-weight: 650;
      }
      .intro {
        margin: 1.5rem 0 0;
        max-width: 32rem;
        font-size: 1rem;
        line-height: 1.75;
        color: rgb(var(--ui-muted));
      }
      .status {
        display: inline-flex;
        align-items: center;
        gap: 0.5rem;
        margin: 1.25rem 0 0;
        color: rgb(var(--ui-primary-foreground));
        font-size: 0.8125rem;
        font-weight: 600;
      }
      .status::before {
        content: "";
        width: 0.375rem;
        height: 0.375rem;
        border-radius: 50%;
        background: currentColor;
      }
      .diagnostic {
        margin-top: 2rem;
        padding: 1rem 1.125rem;
        border: 1px solid rgb(var(--ui-danger) / 0.3);
        border-radius: var(--ui-radius-panel);
        background: rgb(var(--ui-danger-subtle));
      }
      .diagnostic h2 {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        margin: 0 0 0.625rem;
        font-size: 0.8125rem;
        font-weight: 600;
        color: rgb(var(--ui-danger));
      }
      .diagnostic svg {
        width: 1.125rem;
        height: 1.125rem;
        flex: none;
      }
      pre {
        margin: 0;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        font:
          0.75rem/1.7 ui-monospace,
          SFMono-Regular,
          Consolas,
          monospace;
        color: rgb(var(--ui-foreground));
      }
      .support {
        margin: 1.5rem 0 0;
        font-size: 0.8125rem;
        line-height: 1.7;
        color: rgb(var(--ui-muted));
      }
      dl {
        display: grid;
        grid-template-columns: auto minmax(0, 1fr);
        gap: 0.625rem 1rem;
        margin: 1.25rem 0 0;
        padding-top: 1.25rem;
        border-top: 1px solid rgb(var(--ui-border));
        font-size: 0.75rem;
        line-height: 1.6;
      }
      dt {
        color: rgb(var(--ui-muted));
      }
      dd {
        margin: 0;
        overflow-wrap: anywhere;
        font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      }
      .brand {
        text-align: center;
      }
      .brand img {
        display: block;
        width: clamp(10rem, 18vw, 16rem);
        height: auto;
        margin: 0 auto;
      }
      .brand p {
        margin: 1.25rem 0 0;
        font-size: 1.75rem;
        font-weight: 600;
        letter-spacing: -0.04em;
      }
      @media (max-width: 700px) {
        main {
          grid-template-columns: minmax(0, 1fr);
          gap: 2.5rem;
          padding-block: 2.5rem;
        }
        .brand {
          order: -1;
          display: flex;
          align-items: center;
          gap: 0.625rem;
          text-align: start;
        }
        .brand img {
          width: 2rem;
          margin: 0;
        }
        .brand p {
          margin: 0;
          font-size: 1.125rem;
        }
        dl {
          grid-template-columns: minmax(0, 1fr);
          gap: 0.25rem;
        }
        dd + dt {
          margin-top: 0.5rem;
        }
      }
    </style>
  </head>
  <body>
    <div class="halos" aria-hidden="true"></div>
    <main>
      <section aria-labelledby="error-title">
        <p class="eyebrow">Galadril Platform</p>
        <h1 id="error-title">Service unavailable.</h1>
        <p class="status">Error @@STATUS@@</p>
        <p class="intro">
          We couldn’t reach the service behind this page. Please try again in a
          moment.
        </p>
        <section class="diagnostic" aria-labelledby="diagnostic-title">
          <h2 id="diagnostic-title">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.5"
              aria-hidden="true"
            >
              <path
                stroke-linecap="round"
                stroke-linejoin="round"
                d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"
              /></svg
            >Proxy diagnostic
          </h2>
          <pre>@@DETAILS@@</pre>
        </section>
        <p class="support">
          If the problem persists, share the reference below with your
          administrator so they can find the request in the logs.
        </p>
        <dl>
          <dt>Trace reference</dt>
          <dd>@@REQUEST_ID@@</dd>
          @@TRACE_ROW@@
        </dl>
      </section>
      <aside class="brand" aria-label="Galadril">
        <img src="data:image/webp;base64,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" alt="" width="256" height="256" />
        <p>Galadril</p>
      </aside>
    </main>
  </body>
</html>
]==]
local CSP = "default-src 'none'; script-src 'none'; style-src 'sha256-s6ejcUtH7Kx3L7hMTi+tWDWop4tkjjpMw+LJsX9e2dE='; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"

local function html_escape(value)
  return (tostring(value):gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;"):gsub('"', "&quot;"):gsub("'", "&#39;"))
end

local function accepts_html(accept)
  for item in accept:lower():gmatch("[^,]+") do
    if item:match("^%s*text/html%s*;") or item:match("^%s*text/html%s*$") then
      local quality = item:match(";%s*q%s*=%s*([%d.]+)")
      if not quality or (tonumber(quality) or 0) > 0 then return true end
    end
  end
  return false
end

function envoy_on_request(handle)
  local headers = handle:headers()
  local method = headers:get(":method")
  if (method == "GET" or method == "HEAD") and accepts_html(headers:get("accept") or "") then
    handle:streamInfo():dynamicMetadata():set("galadril.error_page", "html", true)
  end
end

function envoy_on_response(handle)
  local headers = handle:headers()
  if headers:get("x-galadril-error-page") ~= "1" then return end
  local trace = headers:get("x-galadril-error-trace")
  local request = headers:get("x-request-id") or "Unavailable"
  headers:remove("x-galadril-error-page")
  headers:remove("x-galadril-error-trace")
  local state = handle:streamInfo():dynamicMetadata():get("galadril.error_page")
  if not state or not state.html then return end
  local body = handle:body(true)
  -- Buffering may yield; Envoy invalidates header handles across that coroutine boundary.
  headers = handle:headers()
  local diagnostic = body:getBytes(0, math.min(body:length(), 4096))
  local values = { STATUS = headers:get(":status"), DETAILS = diagnostic, REQUEST_ID = request }
  -- Local replies do not always expose an active span; request.id remains searchable in OTLP.
  local trace_row = trace and #trace == 32 and trace:match("^[a-fA-F0-9]+$") and ("<dt>Trace ID</dt><dd>" .. html_escape(trace) .. "</dd>") or ""
  local page = PAGE:gsub("@@([A-Z_]+)@@", function(key)
    if key == "TRACE_ROW" then return trace_row end
    return html_escape(values[key] or "Unavailable")
  end)
  headers:replace("content-type", "text/html; charset=utf-8")
  headers:replace("content-length", tostring(#page))
  headers:replace("content-security-policy", CSP)
  headers:replace("x-content-type-options", "nosniff")
  headers:replace("referrer-policy", "no-referrer")
  headers:remove("content-encoding")
  body:setBytes(page)
end
