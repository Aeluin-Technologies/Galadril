import {
  CryptoKeySigner,
  PDF,
  PdfArray,
  PdfName,
  PdfNumber,
  PdfStream,
} from "@libpdf/core";
import { decodeSigningBytes } from "./signing-vault.ts";
import type { SigningVault } from "./signing-vault.ts";

export const signingPdfLimit = 16 * 1024 * 1024;

function eligibleFields(pdf: PDF) {
  return (pdf.getForm()?.getSignatureFields() ?? []).filter((field) => {
    if (field.isSigned() || field.isReadOnly()) return false;
    const widgets = field.getWidgets();
    const widget = widgets[0];
    return (
      widgets.length === 1 &&
      !!widget &&
      !widget.isHidden() &&
      !(widget.flags & 32) &&
      Number.isFinite(widget.width) &&
      Number.isFinite(widget.height) &&
      widget.width > 0 &&
      widget.width <= 14_400 &&
      widget.height > 0 &&
      widget.height <= 14_400
    );
  });
}

async function loadPdf(bytes: Uint8Array) {
  if (bytes.length > signingPdfLimit)
    throw new Error("PDF exceeds signing limit");
  return PDF.load(bytes);
}

export async function inspectSigningFields(
  bytes: Uint8Array,
): Promise<string[]> {
  return eligibleFields(await loadPdf(bytes)).map((field) => field.name);
}

type Point = readonly [number, number];

export function parseSigningTrace(encoded: string): Point[][] {
  if (encoded.length > 16384) throw new Error("Invalid signing trace");
  const value: unknown = JSON.parse(encoded);
  if (!Array.isArray(value) || !value.length || value.length > 32)
    throw new Error("Invalid signing trace");
  let count = 0;
  return value.map((stroke: unknown) => {
    if (
      !Array.isArray(stroke) ||
      stroke.length < 2 ||
      (count += stroke.length) > 1024
    )
      throw new Error("Invalid signing trace");
    return stroke.map((point: unknown): Point => {
      if (!Array.isArray(point) || point.length !== 2)
        throw new Error("Invalid signing point");
      const [x, y] = point;
      if (
        typeof x !== "number" ||
        typeof y !== "number" ||
        !Number.isInteger(x) ||
        !Number.isInteger(y) ||
        x < 0 ||
        x > 1000 ||
        y < 0 ||
        y > 1000
      )
        throw new Error("Invalid signing point");
      return [x, y];
    });
  });
}

export async function signPdf(
  bytes: Uint8Array,
  vault: SigningVault,
  privateKey: CryptoKey,
  visual: { field: string; trace: readonly (readonly Point[])[] } | null,
): Promise<Uint8Array> {
  const pdf = await loadPdf(bytes);
  const fields = eligibleFields(pdf);
  let fieldName = `Galadril-${crypto.randomUUID()}`;
  if (fields.length) {
    const field = fields.find((item) => item.name === visual?.field);
    const widget = field?.getWidgets()[0];
    if (!visual || !field || !widget)
      throw new Error("Select a field and a handwritten mark");
    const { width, height } = widget;
    const pad = Math.min(width, height) * 0.05;
    const commands = [
      "q",
      "0 G",
      "1 J",
      "1 j",
      `${Math.max(0.1, pad * 0.65).toFixed(3)} w`,
    ];
    for (const stroke of visual.trace) {
      for (const [i, [x, y]] of stroke.entries()) {
        const px = pad + (x / 1000) * (width - 2 * pad);
        const py = pad + ((1000 - y) / 1000) * (height - 2 * pad);
        commands.push(
          `${px.toFixed(3)} ${py.toFixed(3)} ${i === 0 ? "m" : "l"}`,
        );
      }
      commands.push("S");
    }
    commands.push("Q");
    widget.setNormalAppearance(
      PdfStream.fromDict(
        {
          Type: PdfName.of("XObject"),
          Subtype: PdfName.of("Form"),
          BBox: new PdfArray([0, 0, width, height].map(PdfNumber.of)),
        },
        new TextEncoder().encode(commands.join("\n")),
      ),
    );
    fieldName = field.name;
  } else if (visual) {
    throw new Error("No visible signature field");
  }
  const signer = new CryptoKeySigner(
    privateKey,
    decodeSigningBytes(vault.certificate),
    "RSA",
    "RSASSA-PKCS1-v1_5",
  );
  return (await pdf.sign({ signer, fieldName, level: "B-B" })).bytes;
}
