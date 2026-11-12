import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { copyFileSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { PDF } from "@libpdf/core";

export const signingTrace = [
  [
    [100, 500],
    [500, 100],
    [900, 650],
  ],
];

export function compileSigningPdf(dir: string, visible: boolean) {
  const path = join(dir, visible ? "visible.tex" : "invisible.tex");
  copyFileSync(
    process.env.GALADRIL_SIGNING_CLASS ??
      new URL(
        "../../../plugins/src/galadril_plugins/law/latex/recoursadmin.cls",
        import.meta.url,
      ),
    join(dir, "recoursadmin.cls"),
  );
  const fixture = String.raw`
\documentclass{recoursadmin}
${visible ? String.raw`\GaladrilEnableSignatureFields` : ""}
\begin{document}
Galadril signing test.
\newpage
Second page.
${visible ? String.raw`\GaladrilSignatureSlot{claimant}{136bp}{34bp}` : ""}
\end{document}
`;
  writeFileSync(path, fixture);
  execFileSync(
    process.env.GALADRIL_TECTONIC ?? "tectonic",
    ["--only-cached", "--untrusted", path],
    { cwd: dir, timeout: 60_000 },
  );
  return readFileSync(path.replace(/\.tex$/, ".pdf"));
}

export async function assertSignedPdf(
  path: string,
  original: Buffer,
  visible: boolean,
  placement?: { page: number; rect?: readonly number[] },
  signerName?: string,
) {
  const signed = readFileSync(path);
  assert.deepEqual(signed.subarray(0, original.length), original);
  const result = await PDF.load(signed);
  const [field] = result.getForm()?.getSignatureFields() ?? [];
  assert.ok(field);
  assert.ok(field.isSigned());
  const [widget] = field.getWidgets();
  assert.ok(widget);
  assert.equal(widget.width > 0 && widget.height > 0, visible);
  assert.equal(widget.hasNormalAppearance(), visible);
  if (visible) {
    if (!placement) assert.equal(field.name, "galadril:claimant");
    if (placement?.rect) assert.deepEqual(widget.rect, placement.rect);
    assert.equal(
      widget.pageRef?.objectNumber,
      result.getPages()[placement?.page ?? 1]?.ref.objectNumber,
    );
    const appearance = widget.getNormalAppearance();
    assert.ok(appearance);
    const content = new TextDecoder().decode(appearance.getDecodedData());
    assert.match(content, /\d l\n[\s\S]*S/);
    if (signerName) {
      assert.ok(
        content.includes(Buffer.from(signerName, "latin1").toString("hex")),
      );
      const lineWidth = content.match(/^([\d.]+) w$/m)?.[1];
      assert.ok(lineWidth && Number(lineWidth) <= 0.65);
    }
  }
  const validation = execFileSync(
    process.env.GALADRIL_PDFSIG ?? "pdfsig",
    ["-nocert", path],
    { encoding: "utf8", timeout: 30_000 },
  );
  assert.match(validation, /Signature Validation: Signature is Valid/);
  assert.match(validation, /Total document signed/);
  const certificate = validation.match(
    /Signer Certificate Common Name: (.+)/,
  )?.[1];
  assert.ok(certificate, "The signed PDF must identify its certificate");
  return certificate;
}
