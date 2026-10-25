import { readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const root = resolve(fileURLToPath(new URL("../..", import.meta.url)));
/** Embed canonical assets so upstream outages never require a second network request. */
export function renderPage() {
  const tokens = readFileSync(
    resolve(root, "front/ui/src/tokens.css"),
    "utf8",
  ).match(/^:root \{[\s\S]*?\n\}/)?.[0];
  if (!tokens) throw new Error("Missing shared UI tokens");
  const logo = readFileSync(
    resolve(root, "front/dashboard/public/favicon.webp"),
  ).toString("base64");
  return readFileSync(
    resolve(root, "front/ui/src/static/service-unavailable.html"),
    "utf8",
  )
    .replace("/* @@TOKENS@@ */", () => tokens)
    .replace("@@LOGO@@", () => `data:image/webp;base64,${logo}`);
}
/** Hash the generated stylesheet instead of granting inline styles general authority. */
export function contentSecurityPolicy(page: string) {
  const css = page.match(/<style>([\s\S]*?)<\/style>/)?.[1];
  if (!css) throw new Error("Missing error page styles");
  const hash = createHash("sha256").update(css).digest("base64");
  return `default-src 'none'; script-src 'none'; style-src 'sha256-${hash}'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`;
}
/** Keep the isolated proxy filter identical to its reviewed HTML and CSP sources. */
export function renderLua() {
  const page = renderPage();
  const policy = contentSecurityPolicy(page);
  return readFileSync(
    resolve(root, "infrastructure/envoy/browser_error.lua"),
    "utf8",
  )
    .replace("@@HTML@@", () => page)
    .replace("@@CSP@@", () => policy);
}
/** Check mode must report stale generated artifacts without rewriting deployment files. */
export function updateConfigurations(check = false) {
  const lua = renderLua();
  const artifact = resolve(root, "infrastructure/envoy/error-page.lua");
  if (check && readFileSync(artifact, "utf8") !== lua)
    throw new Error("Regenerate infrastructure/envoy/error-page.lua");
  if (!check) writeFileSync(artifact, lua);
  for (const file of ["infrastructure/kubernetes/browser-error-page.yaml"]) {
    const path = resolve(root, file);
    const source = readFileSync(path, "utf8");
    const pattern =
      /^( *)-- BEGIN GENERATED ERROR PAGE\n[\s\S]*?^\1-- END GENERATED ERROR PAGE/gm;
    let matches = 0;
    const updated = source.replace(
      pattern,
      (_match: string, indent: string) => {
        matches++;
        return `${indent}-- BEGIN GENERATED ERROR PAGE\n${lua
          .trimEnd()
          .split("\n")
          .map((line) => (line ? `${indent}${line}` : ""))
          .join("\n")}\n${indent}-- END GENERATED ERROR PAGE`;
      },
    );
    if (matches !== 1) throw new Error(`Expected one error page in ${file}`);
    if (check && updated !== source)
      throw new Error(`Regenerate error page in ${file}`);
    if (!check) writeFileSync(path, updated);
  }
}
if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
)
  updateConfigurations(process.argv.includes("--check"));
