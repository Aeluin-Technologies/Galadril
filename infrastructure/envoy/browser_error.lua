-- The page is compiled once per worker and remains available without application upstreams.
local PAGE = [==[@@HTML@@]==]
local CSP = "@@CSP@@"

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
