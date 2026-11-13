export const signaturePadAspectRatio = 2;
const maxPayloadBytes = 16_384;
const maxImageSide = 640;

export type Point = readonly [number, number];

function parseTrace(value: unknown): Point[][] {
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

export type SignatureInk =
  { trace: readonly (readonly Point[])[] } | { image: Uint8Array };

export function parseSignatureInk(payload: string): SignatureInk {
  if (payload.length > maxPayloadBytes)
    throw new Error("Invalid signature mark");
  const value: unknown = JSON.parse(payload);
  if (Array.isArray(value)) return { trace: parseTrace(value) };
  if (
    !value ||
    typeof value !== "object" ||
    !("png" in value) ||
    Object.keys(value).length !== 1 ||
    typeof value.png !== "string" ||
    !/^[A-Za-z0-9_-]+$/.test(value.png)
  )
    throw new Error("Invalid signature image");
  const image = Uint8Array.from(
    atob(value.png.replaceAll("-", "+").replaceAll("_", "/")),
    (c) => c.charCodeAt(0),
  );
  const header = new DataView(image.buffer);
  if (
    image.length < 33 ||
    header.getUint32(0) !== 0x89504e47 ||
    header.getUint32(4) !== 0x0d0a1a0a ||
    header.getUint32(8) !== 13 ||
    header.getUint32(12) !== 0x49484452 ||
    !header.getUint32(16) ||
    header.getUint32(16) > maxImageSide ||
    !header.getUint32(20) ||
    header.getUint32(20) > maxImageSide
  )
    throw new Error("Invalid signature image");
  return { image };
}

export function signatureImageUrl(bytes: Uint8Array): string {
  return "data:image/png;base64," + btoa(String.fromCharCode(...bytes));
}

export async function prepareSignatureImage(
  file: File,
): Promise<{ payload: string; preview: string }> {
  if (
    !["image/png", "image/jpeg"].includes(file.type) ||
    file.size > 5 * 1024 * 1024
  )
    throw new Error("Unsupported signature image");
  const bitmap = await createImageBitmap(file);
  const source = document.createElement("canvas");
  try {
    if (
      !bitmap.width ||
      !bitmap.height ||
      bitmap.width * bitmap.height > 16_000_000
    )
      throw new Error("Signature image exceeds pixel limit");
    const scale = Math.min(
      1,
      maxImageSide / Math.max(bitmap.width, bitmap.height),
    );
    source.width = Math.max(1, Math.round(bitmap.width * scale));
    source.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = source.getContext("2d", { willReadFrequently: true });
    if (!context) throw new Error("Signature image canvas unavailable");
    context.drawImage(bitmap, 0, 0, source.width, source.height);
    const { data } = context.getImageData(0, 0, source.width, source.height);
    let left = source.width,
      right = -1,
      top = source.height,
      bottom = -1;
    // Trim empty paper while retaining the colour and proportions of the mark.
    for (let y = 0; y < source.height; y++) {
      for (let x = 0; x < source.width; x++) {
        const offset = (y * source.width + x) * 4;
        if (
          (data[offset + 3] ?? 0) < 16 ||
          Math.min(
            data[offset] ?? 255,
            data[offset + 1] ?? 255,
            data[offset + 2] ?? 255,
          ) >= 248
        )
          continue;
        left = Math.min(left, x);
        top = Math.min(top, y);
        right = Math.max(right, x);
        bottom = Math.max(bottom, y);
      }
    }
    if (right < left || bottom < top) throw new Error("Empty signature image");
    const width = right - left + 1;
    const height = bottom - top + 1;
    const output = document.createElement("canvas");
    const target = output.getContext("2d");
    if (!target) throw new Error("Signature image canvas unavailable");
    for (let ratio = 1; ratio >= 0.15; ratio *= 0.75) {
      output.width = Math.max(1, Math.round(width * ratio));
      output.height = Math.max(1, Math.round(height * ratio));
      target.drawImage(
        source,
        left,
        top,
        width,
        height,
        0,
        0,
        output.width,
        output.height,
      );
      const preview = output.toDataURL("image/png");
      const png = preview
        .split(",")[1]
        ?.replaceAll("+", "-")
        .replaceAll("/", "_")
        .replace(/=+$/, "");
      const payload = JSON.stringify({ png });
      if (png && payload.length <= maxPayloadBytes) return { payload, preview };
    }
    throw new Error("Signature image exceeds payload limit");
  } finally {
    bitmap.close();
  }
}
