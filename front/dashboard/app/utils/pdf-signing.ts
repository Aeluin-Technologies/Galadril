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
import { signaturePadAspectRatio, type SignatureInk } from "./signature-ink.ts";
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
  baseline: number,
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
    const left = (width - font.widthOfTextAtSize(line, size)) / 2;
    return `BT /Caption ${size.toFixed(3)} Tf 1 0 0 1 ${left.toFixed(3)} ${(baseline + size * index * 1.15).toFixed(3)} Tm <${hex}> Tj ET`;
  });
}

export async function signPdf(
  bytes: Uint8Array,
  vault: SigningVault,
  privateKey: CryptoKey,
  visual:
    | (SignatureInk & {
        field: string;
        placement?: SigningPlacement;
        caption?: readonly string[];
      })
    | null,
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
    const pad = Math.min(3, Math.min(width, height) * 0.05);
    const captionHeight = visual.caption?.length
      ? Math.min(15, height * 0.32)
      : 0;
    const maxWidth = width - 2 * pad;
    const maxHeight = height - 2 * pad - captionHeight;
    const centerX = width / 2;
    const centerY = (height + captionHeight) / 2;
    const commands = ["q"];
    let inkHeight: number;
    const resources = PdfDict.of({
      Font: PdfDict.of({
        Caption: PdfDict.of({
          Type: PdfName.of("Font"),
          Subtype: PdfName.of("Type1"),
          BaseFont: PdfName.of("Helvetica"),
          Encoding: PdfName.of("WinAnsiEncoding"),
        }),
      }),
    });
    if ("image" in visual) {
      const image = pdf.embedImage(visual.image);
      const scale = Math.min(maxWidth / image.width, maxHeight / image.height);
      const w = image.width * scale;
      const h = image.height * scale;
      inkHeight = h;
      resources.set("XObject", PdfDict.of({ Mark: image.ref }));
      commands.push(
        `q ${w.toFixed(3)} 0 0 ${h.toFixed(3)} ${(centerX - w / 2).toFixed(3)} ${(centerY - h / 2).toFixed(3)} cm /Mark Do Q`,
      );
    } else {
      let minX = Infinity,
        minY = Infinity,
        maxX = -Infinity,
        maxY = -Infinity;
      for (const stroke of visual.trace) {
        for (const [x, y] of stroke) {
          minX = Math.min(minX, x);
          minY = Math.min(minY, y);
          maxX = Math.max(maxX, x);
          maxY = Math.max(maxY, y);
        }
      }
      if (!Number.isFinite(minX)) throw new Error("Empty signature trace");
      const scale = Math.min(
        maxWidth / Math.max(1, (maxX - minX) * signaturePadAspectRatio),
        maxHeight / Math.max(1, maxY - minY),
      );
      inkHeight = (maxY - minY) * scale;
      const midX = (minX + maxX) / 2;
      const midY = (minY + maxY) / 2;
      commands.push(
        "0 G",
        "1 J",
        "1 j",
        `${Math.max(0.1, Math.min(0.65, maxHeight * 0.045)).toFixed(3)} w`,
      );
      for (const stroke of visual.trace) {
        for (const [i, [x, y]] of stroke.entries()) {
          const px = centerX + (x - midX) * scale * signaturePadAspectRatio;
          const py = centerY - (y - midY) * scale;
          commands.push(
            `${px.toFixed(3)} ${py.toFixed(3)} ${i === 0 ? "m" : "l"}`,
          );
        }
        commands.push("S");
      }
    }
    if (visual.caption?.length) {
      const baseline = Math.max(pad, centerY - inkHeight / 2 - captionHeight);
      commands.push(
        ...captionCommands(visual.caption, width, height, pad, baseline),
      );
    }
    commands.push("Q");
    widget.setNormalAppearance(
      PdfStream.fromDict(
        {
          Type: PdfName.of("XObject"),
          Subtype: PdfName.of("Form"),
          BBox: new PdfArray([0, 0, width, height].map(PdfNumber.of)),
          Resources: resources,
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
