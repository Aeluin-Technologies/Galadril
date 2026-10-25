import type { FileRecord } from "~/utils/pengolo-api";
import { archiveFilename } from "../../shared/text.ts";
const MAX_ARCHIVE_BYTES = 100 * 1024 * 1024;
/** Bound aggregate memory and preserve filenames without archive path traversal. */
export async function downloadDocuments(
  files: readonly FileRecord[],
  resolve: (id: string) => Promise<{ url: string }>,
  signal: AbortSignal,
): Promise<Blob> {
  if (
    !files.length ||
    files.length > 100 ||
    files.reduce((total, file) => total + file.data.size, 0) > MAX_ARCHIVE_BYTES
  )
    throw new Error("ARCHIVE_LIMIT");
  const { default: JSZip } = await import("jszip");
  const archive = new JSZip();
  let total = 0;
  for (const [index, file] of files.entries()) {
    signal.throwIfAborted();
    const { url } = await resolve(file.id);
    const target = new URL(url, window.location.origin);
    if (
      !["https:", "http:"].includes(target.protocol) ||
      target.username ||
      target.password
    )
      throw new Error("DOWNLOAD_INVALID");
    const response = await fetch(target, {
      signal,
      credentials: "same-origin",
      redirect: "error",
    });
    if (!response.ok || !response.body) throw new Error("DOWNLOAD_UNAVAILABLE");
    const reader = response.body.getReader();
    const chunks: Uint8Array<ArrayBuffer>[] = [];
    let size = 0;
    try {
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        size += part.value.byteLength;
        total += part.value.byteLength;
        if (size > file.data.size || total > MAX_ARCHIVE_BYTES) {
          await reader.cancel();
          throw new Error("ARCHIVE_LIMIT");
        }
        chunks.push(part.value);
      }
    } finally {
      reader.releaseLock();
    }
    if (size !== file.data.size) throw new Error("DOWNLOAD_CHANGED");
    const name = `${index + 1}-${archiveFilename(file.data.name)}`;
    archive.file(name, new Blob(chunks));
  }
  signal.throwIfAborted();
  return archive.generateAsync({
    type: "blob",
    streamFiles: true,
    compression: "STORE",
  });
}
