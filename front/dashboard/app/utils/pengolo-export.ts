export function tableClipboard(rows: readonly (readonly string[])[]): string {
  return rows
    .map((row) =>
      row
        .map((value) => {
          const literal = /^[=+@-]/.test(value.trimStart())
            ? "'" + value
            : value;
          return /[\t\n\r"]/.test(literal)
            ? `"${literal.replaceAll('"', '""')}"`
            : literal;
        })
        .join("\t"),
    )
    .join("\n");
}

export async function tableWorkbook(
  rows: readonly (readonly string[])[],
): Promise<Uint8Array<ArrayBuffer>> {
  const { default: ExcelJS } = await import("exceljs");
  const workbook = new ExcelJS.Workbook();
  const sheet = workbook.addWorksheet("Pengolo");
  for (const row of rows) sheet.addRow([...row]);
  const bytes: unknown = await workbook.xlsx.writeBuffer();
  if (bytes instanceof ArrayBuffer) return new Uint8Array(bytes);
  if (ArrayBuffer.isView(bytes))
    return Uint8Array.from(
      new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength),
    );
  throw new Error("Invalid spreadsheet output");
}

export function downloadBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
