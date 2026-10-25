import { BlockList, isIP } from "node:net";
import { lookup } from "node:dns/promises";
import { request } from "node:https";
import type { LookupAddress } from "node:dns";

export interface ImageResponse {
  status?: number;
  location?: string;
  mime?: string;
  body: AsyncIterable<Uint8Array | string> | Iterable<Uint8Array | string>;
  dispose?: () => void;
}

export interface ImageDependencies {
  lookup(host: string): Promise<LookupAddress[]>;
  open(
    url: URL,
    address: LookupAddress,
    signal: AbortSignal,
  ): Promise<ImageResponse>;
}

interface ImageResult {
  body: Buffer;
  mime: string;
}

const denied = new BlockList();
for (const [address, prefix] of [
  ["0.0.0.0", 8],
  ["10.0.0.0", 8],
  ["100.64.0.0", 10],
  ["127.0.0.0", 8],
  ["169.254.0.0", 16],
  ["172.16.0.0", 12],
  ["192.0.0.0", 24],
  ["192.0.2.0", 24],
  ["192.168.0.0", 16],
  ["198.18.0.0", 15],
  ["198.51.100.0", 24],
  ["203.0.113.0", 24],
  ["224.0.0.0", 4],
  ["240.0.0.0", 4],
] satisfies [string, number][])
  denied.addSubnet(address, prefix, "ipv4");
const globalV6 = new BlockList();
globalV6.addSubnet("2000::", 3, "ipv6");
for (const [address, prefix] of [
  ["2001::", 23],
  ["2001:db8::", 32],
  ["2002::", 16],
  ["3fff::", 20],
] satisfies [string, number][])
  denied.addSubnet(address, prefix, "ipv6");
const maximum = 5 * 1024 * 1024;

/** Reject special-use networks before opening a socket, including translated IPv6 ranges. */
export function publicImageAddress(address: string) {
  const family = isIP(address);
  return family === 4
    ? !denied.check(address, "ipv4")
    : family === 6 &&
        globalV6.check(address, "ipv6") &&
        !denied.check(address, "ipv6");
}

/** Every redirect must pass the same TLS and destination checks as the original URL. */
export function imageUrl(value: string) {
  const url = new URL(value);
  const host = url.hostname.replace(/^\[|\]$/g, "");
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    (url.port && url.port !== "443") ||
    (isIP(host) && !publicImageAddress(host))
  )
    throw new Error("Image destination refused");
  url.hash = "";
  return url;
}

function open(
  url: URL,
  address: LookupAddress,
  signal: AbortSignal,
): Promise<ImageResponse> {
  return new Promise((resolve, reject) => {
    const outgoing = request(
      url,
      {
        agent: false,
        signal,
        headers: {
          accept: "image/png,image/jpeg,image/webp,image/gif,image/avif",
          "user-agent": "Galadril-Image-Proxy",
        },
        // Pin the validated address while retaining the hostname for TLS verification.
        lookup: (_host, options, callback) =>
          options.all
            ? callback(null, [address])
            : callback(null, address.address, address.family),
      },
      (response) =>
        resolve({
          status: response.statusCode,
          location: response.headers.location,
          mime: response.headers["content-type"],
          body: response,
          dispose: () => response.destroy(),
        }),
    );
    outgoing.setTimeout(10_000, () =>
      outgoing.destroy(new Error("Image upstream timeout")),
    );
    outgoing.once("error", reject);
    outgoing.end();
  });
}

function rasterMime(bytes: Buffer) {
  if (bytes.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex")))
    return "image/png";
  if (bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255)
    return "image/jpeg";
  if (["GIF87a", "GIF89a"].includes(bytes.toString("ascii", 0, 6)))
    return "image/gif";
  if (
    bytes.toString("ascii", 0, 4) === "RIFF" &&
    bytes.toString("ascii", 8, 12) === "WEBP"
  )
    return "image/webp";
  if (
    bytes.toString("ascii", 4, 8) === "ftyp" &&
    ["avif", "avis"].includes(bytes.toString("ascii", 8, 12))
  )
    return "image/avif";
  throw new Error("Unsupported image");
}

async function readImage(
  value: string,
  dependencies: ImageDependencies,
  signal: AbortSignal,
): Promise<ImageResult> {
  let url = imageUrl(value);
  for (let redirects = 0; redirects <= 3; redirects++) {
    const addresses = await dependencies.lookup(
      url.hostname.replace(/^\[|\]$/g, ""),
    );
    signal.throwIfAborted();
    const address = addresses[0];
    if (
      !address ||
      addresses.some((address) => !publicImageAddress(address.address))
    )
      throw new Error("Image address refused");
    const response = await dependencies.open(url, address, signal);
    try {
      if (
        response.status !== undefined &&
        [301, 302, 303, 307, 308].includes(response.status)
      ) {
        if (!response.location || redirects === 3)
          throw new Error("Image redirect refused");
        url = imageUrl(new URL(response.location, url).href);
        continue;
      }
      if (response.status !== 200) throw new Error("Image unavailable");
      const chunks = [];
      let length = 0;
      for await (const chunk of response.body) {
        const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        length += bytes.length;
        if (length > maximum) throw new Error("Image too large");
        chunks.push(bytes);
      }
      const body = Buffer.concat(chunks, length);
      return { body, mime: rasterMime(body) };
    } finally {
      response.dispose?.();
    }
  }
  throw new Error("Image unavailable");
}

/**
 * Pin validated DNS answers while retaining hostname-based TLS verification.
 *
 * @param dependencies - Injectable transport for DNS, redirect and byte-budget regressions.
 * @param timeoutMilliseconds - Bounds the complete operation, including DNS resolution.
 */
export async function fetchImage(
  value: string,
  dependencies: ImageDependencies = {
    lookup: (host) => lookup(host, { all: true, verbatim: true }),
    open,
  },
  timeoutMilliseconds = 15_000,
) {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<ImageResult>((_resolve, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error("Image proxy timeout"));
    }, timeoutMilliseconds);
  });
  try {
    return await Promise.race([
      readImage(value, dependencies, controller.signal),
      timeout,
    ]);
  } finally {
    clearTimeout(timer);
  }
}
