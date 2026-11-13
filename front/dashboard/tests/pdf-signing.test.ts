import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { PDF, Standard14Font } from "@libpdf/core";
import {
  createSigningVault,
  unlockSigningKey,
} from "../app/utils/signing-vault.ts";
import { inspectSigningFields, signPdf } from "../app/utils/pdf-signing.ts";
import { parseSignatureInk } from "../app/utils/signature-ink.ts";
import {
  assertSignedPdf,
  compileSigningPdf,
  signingTrace,
} from "./signing-fixture.ts";

const scope = { tenant: "tenant-a", subject: "alice" };
const password = "a long unique signing password";

test("password and PRF unlock the same non-exportable signing key", async () => {
  const prf = crypto.getRandomValues(new Uint8Array(32));
  const vault = await createSigningVault(scope, password, {
    credential: "credential-fixture",
    prf,
    salt: crypto.getRandomValues(new Uint8Array(32)),
  });
  const stored: typeof vault = JSON.parse(JSON.stringify(vault));
  const byPassword = await unlockSigningKey(scope, stored, { password });
  const byPasskey = await unlockSigningKey(scope, stored, { prf });
  assert.equal(byPassword.extractable, false);
  assert.equal(byPasskey.extractable, false);
  const message = new TextEncoder().encode("signature fixture");
  assert.deepEqual(
    await crypto.subtle.sign("RSASSA-PKCS1-v1_5", byPassword, message),
    await crypto.subtle.sign("RSASSA-PKCS1-v1_5", byPasskey, message),
  );
  await assert.rejects(
    unlockSigningKey(scope, stored, { password: "incorrect password" }),
  );
  await assert.rejects(
    unlockSigningKey(scope, stored, { prf: new Uint8Array(32) }),
  );
  await assert.rejects(
    unlockSigningKey({ ...scope, subject: "bob" }, stored, { password }),
  );
  await assert.rejects(
    unlockSigningKey({ ...scope, tenant: "other" }, stored, { prf }),
  );
});

test("an imported PNG is included in the signed widget appearance", async () => {
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jh2sAAAAASUVORK5CYII=",
    "base64",
  );
  const pdf = PDF.create();
  pdf.addPage();
  const original = await pdf.save();
  const vault = await createSigningVault(scope, password);
  const key = await unlockSigningKey(scope, vault, { password });
  const signed = await signPdf(original, vault, key, {
    field: "",
    ...parseSignatureInk(JSON.stringify({ png: png.toString("base64url") })),
    placement: { page: 0, rect: [30, 30, 166, 64] },
  });
  assert.deepEqual(signed.subarray(0, original.length), original);
  const result = await PDF.load(signed);
  const field = result.getForm()?.getSignatureFields()[0];
  const appearance = field?.getWidgets()[0]?.getNormalAppearance();
  assert.ok(field?.isSigned() && appearance);
  const commands = new TextDecoder().decode(appearance.getDecodedData());
  assert.match(commands, /([\d.]+) 0 0 \1 [\d.]+ [\d.]+ cm \/Mark Do/);
});

test("signature ink keeps its shape and fills the available field", async () => {
  const pdf = PDF.create();
  pdf.addPage();
  const original = await pdf.save();
  const vault = await createSigningVault(scope, password);
  const key = await unlockSigningKey(scope, vault, { password });
  for (const { trace, rect, expectedRatio, minimumSpan } of [
    {
      trace: [
        [
          [480, 200],
          [520, 700],
        ],
      ],
      rect: [30, 30, 166, 64],
      expectedRatio: 0.16,
      minimumSpan: 18,
    },
    {
      trace: [
        [
          [480, 200],
          [520, 700],
        ],
      ],
      rect: [30, 30, 166, 156],
      expectedRatio: 0.16,
      minimumSpan: 95,
    },
    {
      trace: [
        [
          [300, 400],
          [650, 460],
        ],
      ],
      rect: [30, 30, 166, 64],
      expectedRatio: 700 / 60,
      minimumSpan: 130,
    },
  ] as const) {
    const signed = await signPdf(original, vault, key, {
      field: "",
      trace,
      placement: { page: 0, rect },
      caption: ["Electronically signed by Alice", "On 12 November"],
    });
    const result = await PDF.load(signed);
    const appearance = result
      .getForm()
      ?.getSignatureFields()[0]
      ?.getWidgets()[0]
      ?.getNormalAppearance();
    assert.ok(appearance);
    const path = new TextDecoder().decode(appearance.getDecodedData());
    const start = path.match(/^([\d.]+) ([\d.]+) m$/m);
    const end = path.match(/^([\d.]+) ([\d.]+) l$/m);
    assert.ok(start && end);
    const width = Math.abs(Number(end[1]) - Number(start[1]));
    const height = Math.abs(Number(end[2]) - Number(start[2]));
    assert.ok(Math.abs(width / height - expectedRatio) < 0.01, path);
    assert.ok(Math.max(width, height) >= minimumSpan, path);
    assert.ok(Number(path.match(/^([\d.]+) w$/m)?.[1]) >= 0.4, path);
  }
});

for (const visible of [true, false]) {
  test(
    `Tectonic PDF is signed with ${visible ? "a visible" : "an invisible"} field`,
    {
      skip: process.env.GALADRIL_SIGNING_E2E !== "1",
    },
    async () => {
      const dir = mkdtempSync(join(tmpdir(), "galadril-pdf-signing-"));
      try {
        const original = compileSigningPdf(dir, visible);
        assert.deepEqual(
          await inspectSigningFields(original),
          visible ? ["galadril:claimant"] : [],
        );
        const vault = await createSigningVault(scope, password);
        const key = await unlockSigningKey(scope, vault, { password });
        const signed = await signPdf(
          original,
          vault,
          key,
          visible
            ? {
                field: "galadril:claimant",
                ...parseSignatureInk(JSON.stringify(signingTrace)),
              }
            : null,
        );
        const path = join(dir, "signed.pdf");
        writeFileSync(path, signed);
        await assertSignedPdf(path, original, visible);
        const placement = {
          page: 0,
          rect: [30, 30, 190, 85],
        } as const;
        const placed = await signPdf(original, vault, key, {
          field: visible ? "galadril:claimant" : "",
          ...parseSignatureInk(JSON.stringify(signingTrace)),
          placement,
          caption: ["Electronically signed by Alice", "On 12 November"],
        });
        const placedPath = join(dir, "placed.pdf");
        writeFileSync(placedPath, placed);
        await assertSignedPdf(placedPath, original, true, placement);
        const result = await PDF.load(placed);
        const widget = result
          .getForm()
          ?.getSignatureFields()[0]
          ?.getWidgets()[0];
        const appearance = widget?.getNormalAppearance();
        assert.ok(widget && appearance);
        const commands = new TextDecoder().decode(appearance.getDecodedData());
        const captions = [
          ...commands.matchAll(
            /BT \/Caption ([\d.]+) Tf 1 0 0 1 ([\d.]+) [\d.]+ Tm <([a-f\d]+)> Tj ET/g,
          ),
        ];
        assert.equal(captions.length, 2);
        const font = Standard14Font.of("Helvetica");
        for (const [, size, left, text] of captions) {
          assert.ok(text);
          const line = Buffer.from(text, "hex").toString("latin1");
          const center =
            Number(left) + font.widthOfTextAtSize(line, Number(size)) / 2;
          assert.ok(Math.abs(center - widget.width / 2) < 0.05, line);
        }
        if (visible) {
          const pdfsig = process.env.GALADRIL_PDFSIG ?? "pdfsig";
          const changed = Buffer.from(signed);
          assert.match(changed.toString("ascii", 0, 8), /^%PDF-1\.[0-7]$/);
          changed.writeUInt8(changed.at(7) === 55 ? 54 : 55, 7);
          const tampered = join(dir, "tampered.pdf");
          writeFileSync(tampered, changed);
          const invalid = spawnSync(pdfsig, ["-nocert", tampered], {
            encoding: "utf8",
            timeout: 30_000,
          });
          assert.match(invalid.stdout, /Signature Validation: Digest Mismatch/);

          const other = { ...scope, subject: "bob" };
          const secondVault = await createSigningVault(other, password);
          assert.notEqual(secondVault.certificate, vault.certificate);
          const secondKey = await unlockSigningKey(other, secondVault, {
            password,
          });
          const countersigned = await signPdf(
            signed,
            secondVault,
            secondKey,
            null,
          );
          assert.deepEqual(countersigned.subarray(0, signed.length), signed);
          const countersignedPath = join(dir, "countersigned.pdf");
          writeFileSync(countersignedPath, countersigned);
          const signatures = execFileSync(
            pdfsig,
            ["-nocert", countersignedPath],
            {
              encoding: "utf8",
              timeout: 30_000,
            },
          );
          assert.equal(
            signatures.match(/Signature Validation: Signature is Valid/g)
              ?.length,
            2,
          );
        }
      } finally {
        rmSync(dir, { recursive: true, force: true });
      }
    },
  );
}
