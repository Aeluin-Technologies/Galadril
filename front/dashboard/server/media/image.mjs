import { BlockList, isIP } from "node:net";
import { lookup } from "node:dns/promises";
import { request } from "node:https";

const denied = new BlockList();
for (const [address, prefix] of [["0.0.0.0",8],["10.0.0.0",8],["100.64.0.0",10],["127.0.0.0",8],["169.254.0.0",16],["172.16.0.0",12],["192.0.0.0",24],["192.0.2.0",24],["192.168.0.0",16],["198.18.0.0",15],["198.51.100.0",24],["203.0.113.0",24],["224.0.0.0",4],["240.0.0.0",4]]) denied.addSubnet(address,prefix,"ipv4");
const globalV6 = new BlockList();
globalV6.addSubnet("2000::",3,"ipv6");
for (const [address,prefix] of [["2001::",23],["2001:db8::",32],["2002::",16],["3fff::",20]]) denied.addSubnet(address,prefix,"ipv6");
const maximum = 5 * 1024 * 1024;

export function publicImageAddress(address) {
  const family = isIP(address);
  return family === 4 ? !denied.check(address,"ipv4") : family === 6 && globalV6.check(address,"ipv6") && !denied.check(address,"ipv6");
}

export function imageUrl(value) {
  const url = new URL(value);
  const host = url.hostname.replace(/^\[|\]$/g, "");
  if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443") || (isIP(host) && !publicImageAddress(host))) throw new Error("Image destination refused");
  url.hash = "";
  return url;
}

function open(url,address,signal) {
  return new Promise((resolve,reject) => {
    const outgoing = request(url, {
      agent:false,
      signal,
      headers:{accept:"image/png,image/jpeg,image/webp,image/gif,image/avif", "user-agent":"Galadril-Image-Proxy"},
      // Pin the validated address while retaining the hostname for TLS verification.
      lookup:(_host,options,callback) => options.all ? callback(null,[address]) : callback(null,address.address,address.family),
    }, response => resolve({status:response.statusCode,location:response.headers.location,mime:response.headers["content-type"],body:response,dispose:() => response.destroy()}));
    outgoing.setTimeout(10_000,() => outgoing.destroy(new Error("Image upstream timeout")));
    outgoing.once("error",reject);
    outgoing.end();
  });
}

function rasterMime(bytes) {
  if (bytes.subarray(0,8).equals(Buffer.from("89504e470d0a1a0a","hex"))) return "image/png";
  if (bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) return "image/jpeg";
  if (["GIF87a","GIF89a"].includes(bytes.toString("ascii",0,6))) return "image/gif";
  if (bytes.toString("ascii",0,4) === "RIFF" && bytes.toString("ascii",8,12) === "WEBP") return "image/webp";
  if (bytes.toString("ascii",4,8) === "ftyp" && ["avif","avis"].includes(bytes.toString("ascii",8,12))) return "image/avif";
  throw new Error("Unsupported image");
}

async function readImage(value, dependencies, signal) {
  let url = imageUrl(value);
  for (let redirects = 0; redirects <= 3; redirects++) {
    const addresses = await dependencies.lookup(url.hostname.replace(/^\[|\]$/g,""));
    signal.throwIfAborted();
    if (!addresses.length || addresses.some(address => !publicImageAddress(address.address))) throw new Error("Image address refused");
    const response = await dependencies.open(url,addresses[0],signal);
    try {
      if ([301,302,303,307,308].includes(response.status)) {
        if (!response.location || redirects === 3) throw new Error("Image redirect refused");
        url = imageUrl(new URL(response.location,url).href);
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
      const body = Buffer.concat(chunks,length);
      return {body,mime:rasterMime(body)};
    } finally { response.dispose?.(); }
  }
  throw new Error("Image unavailable");
}

export async function fetchImage(value, dependencies = {lookup:host => lookup(host,{all:true,verbatim:true}),open}, timeoutMilliseconds = 15_000) {
  const controller = new AbortController();
  let timer;
  const timeout = new Promise((_resolve,reject) => {
    timer = setTimeout(() => { controller.abort(); reject(new Error("Image proxy timeout")); },timeoutMilliseconds);
  });
  try { return await Promise.race([readImage(value,dependencies,controller.signal),timeout]); }
  finally { clearTimeout(timer); }
}
