import {
  CryptoKeySigner,
  PDF,
  PdfArray,
  PdfDict,
  PdfName,
  PdfNumber,
  PdfRef,
  PdfStream,
  Standard14Font,
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

export type SigningPlacement = {
  page: number;
  rect: readonly [number, number, number, number];
};

export async function inspectSigningLayout(bytes: Uint8Array) {
  const pdf = await loadPdf(bytes);
  const pages = pdf.getPages();
  return eligibleFields(pdf).map((field) => {
    const widget = field.getWidgets()[0];
    const page = pages.findIndex(
      (item) => item.ref.toString() === widget?.pageRef?.toString(),
    );
    return {
      name: field.name,
      placement: widget && page >= 0 ? { page, rect: widget.rect } : undefined,
    };
  });
}

function placeWidget(
  pdf: PDF,
  placement: SigningPlacement,
  ref: PdfRef,
  dict: PdfDict,
) {
  const page = pdf.getPages().at(placement.page);
  const [left, bottom, right, top] = placement.rect;
  if (
    !page ||
    placement.page < 0 ||
    !Number.isInteger(placement.page) ||
    !placement.rect.every(Number.isFinite) ||
    right - left < 8 ||
    top - bottom < 8
  )
    throw new Error("Invalid signature rectangle");
  const box = page.getCropBox();
  if (
    left < box.x - 0.01 ||
    bottom < box.y - 0.01 ||
    right > box.x + box.width + 0.01 ||
    top > box.y + box.height + 0.01
  )
    throw new Error("Signature rectangle outside the page");
  const oldRef = dict.get("P");
  const samePage =
    oldRef instanceof PdfRef && oldRef.toString() === page.ref.toString();
  const annotations = (target: PdfDict) =>
    target.getArray("Annots", (id) => pdf.getObject(id));
  if (!samePage) {
    const oldPage = oldRef instanceof PdfRef ? pdf.getObject(oldRef) : null;
    const oldAnnotations =
      oldPage instanceof PdfDict ? annotations(oldPage) : undefined;
    if (oldAnnotations && oldPage instanceof PdfDict)
      oldPage.set(
        "Annots",
        new PdfArray(
          [...oldAnnotations].filter(
            (item) =>
              !(item instanceof PdfRef && item.toString() === ref.toString()),
          ),
        ),
      );
    page.dict.set(
      "Annots",
      new PdfArray([...(annotations(page.dict) ?? []), ref]),
    );
  }
  dict.set("P", page.ref);
  dict.set("Rect", new PdfArray(placement.rect.map(PdfNumber.of)));
}

function captionCommands(
  lines: readonly string[],
  width: number,
  height: number,
  pad: number,
) {
  const font = Standard14Font.of("Helvetica");
  const encoded = lines
    .slice(0, 2)
    .map((line) => line.slice(0, 180).replace(/[^\x20-\x7e\xa0-\xff]/gu, " "));
  const size = Math.min(
    6,
    height / 8,
    ...encoded.map(
      (line) =>
        (width - 2 * pad) / Math.max(1, font.widthOfTextAtSize(line, 1)),
    ),
  );
  return encoded.reverse().map((line, index) => {
    const hex = Array.from(line, (char) =>
      char.charCodeAt(0).toString(16).padStart(2, "0"),
    ).join("");
    return `BT /Caption ${size.toFixed(3)} Tf 1 0 0 1 ${pad.toFixed(3)} ${(pad + size * index * 1.15).toFixed(3)} Tm <${hex}> Tj ET`;
  });
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
  visual: {
    field: string;
    trace: readonly (readonly Point[])[];
    placement?: SigningPlacement;
    caption?: readonly string[];
  } | null,
  signingTime?: Date,
): Promise<Uint8Array> {
  const pdf = await loadPdf(bytes);
  const fields = eligibleFields(pdf);
  let fieldName = `Galadril-${crypto.randomUUID()}`;
  if (visual) {
    const field = visual.field
      ? fields.find((item) => item.name === visual.field)
      : !fields.length && visual.placement
        ? pdf.getOrCreateForm().createSignatureField(fieldName)
        : undefined;
    if (!field) throw new Error("Select a signature field");
    const parent = field.getRef();
    const widget =
      field.getWidgets()[0] ??
      (parent && visual.placement
        ? field.addWidget(
            PdfDict.of({
              Type: PdfName.of("Annot"),
              Subtype: PdfName.of("Widget"),
              Parent: parent,
              F: PdfNumber.of(4),
            }),
          )
        : undefined);
    if (!widget) throw new Error("Missing signature widget");
    if (visual.placement) {
      if (!widget.ref) throw new Error("Signature widget cannot be moved");
      placeWidget(pdf, visual.placement, widget.ref, widget.dict);
    }
    const { width, height } = widget;
    const pad = Math.min(width, height) * 0.05;
    const captionHeight = visual.caption?.length ? height * 0.32 : 0;
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
        const py =
          pad +
          captionHeight +
          ((1000 - y) / 1000) * (height - 2 * pad - captionHeight);
        commands.push(
          `${px.toFixed(3)} ${py.toFixed(3)} ${i === 0 ? "m" : "l"}`,
        );
      }
      commands.push("S");
    }
    if (visual.caption?.length)
      commands.push(...captionCommands(visual.caption, width, height, pad));
    commands.push("Q");
    widget.setNormalAppearance(
      PdfStream.fromDict(
        {
          Type: PdfName.of("XObject"),
          Subtype: PdfName.of("Form"),
          BBox: new PdfArray([0, 0, width, height].map(PdfNumber.of)),
          Resources: PdfDict.of({
            Font: PdfDict.of({
              Caption: PdfDict.of({
                Type: PdfName.of("Font"),
                Subtype: PdfName.of("Type1"),
                BaseFont: PdfName.of("Helvetica"),
                Encoding: PdfName.of("WinAnsiEncoding"),
              }),
            }),
          }),
        },
        new TextEncoder().encode(commands.join("\n")),
      ),
    );
    fieldName = field.name;
  } else if (fields.length) {
    throw new Error("Select a field and a handwritten mark");
  }
  const signer = new CryptoKeySigner(
    privateKey,
    decodeSigningBytes(vault.certificate),
    "RSA",
    "RSASSA-PKCS1-v1_5",
  );
  return (await pdf.sign({ signer, fieldName, level: "B-B", signingTime }))
    .bytes;
}
