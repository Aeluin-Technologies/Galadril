/** Protocol fields must reject control bytes without allocating a character array. */
export function hasAsciiControl(value: string, includeDelete = true): boolean {
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index);
    if (code < 32 || (includeDelete && code === 127)) return true;
  }
  return false;
}

/** Archive entries cannot inherit separators or control bytes from untrusted filenames. */
export function archiveFilename(value: string): string {
  let start = 0;
  let output = "";
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index);
    if (code < 32 || code === 47 || code === 92) {
      output += value.slice(start, index) + "_";
      start = index + 1;
    }
  }
  return start === 0 ? value : output + value.slice(start);
}
