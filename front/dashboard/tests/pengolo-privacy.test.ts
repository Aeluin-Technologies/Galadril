import assert from "node:assert/strict";
import { test } from "node:test";
import {
  publicImageAddress,
  imageUrl,
  fetchImage,
} from "../server/media/image.ts";
import type { ImageDependencies } from "../server/media/image.ts";

test("image proxy rejects private, translated, shared and non-HTTPS destinations", () => {
  for (const address of [
    "127.0.0.1",
    "10.1.2.3",
    "169.254.169.254",
    "100.64.0.1",
    "192.168.0.1",
    "::1",
    "fc00::1",
    "fe80::1",
    "::ffff:127.0.0.1",
    "64:ff9b::7f00:1",
    "2002:7f00:1::1",
  ])
    assert.equal(publicImageAddress(address), false, address);
  assert.equal(publicImageAddress("93.184.216.34"), true);
  assert.equal(publicImageAddress("2606:4700:4700::1111"), true);
  for (const url of [
    "http://example.com/a.png",
    "https://user:pass@example.com/a",
    "https://example.com:444/a",
    "https://127.0.0.1/a",
  ])
    assert.throws(() => imageUrl(url));
});

test("DNS is pinned and every redirected destination is revalidated", async () => {
  let opens = 0;
  await assert.rejects(
    fetchImage("https://images.example/a", {
      lookup: async (host) =>
        host === "images.example"
          ? [{ address: "93.184.216.34", family: 4 }]
          : [{ address: "127.0.0.1", family: 4 }],
      open: async (_url, address) => {
        assert.equal(address.address, "93.184.216.34");
        opens++;
        return {
          status: 302,
          location: "https://metadata.example/private",
          mime: "",
          body: [],
        };
      },
    }),
  );
  assert.equal(opens, 1);
});

test("the entire proxy operation has a deadline, including DNS", async () => {
  await assert.rejects(
    fetchImage(
      "https://images.example/a",
      {
        lookup: () => new Promise(() => {}),
        open: () => assert.fail("Timed-out DNS must not open a socket"),
      },
      5,
    ),
    /timeout/,
  );
});

test("only bounded raster images can be returned, independent of upstream MIME", async () => {
  const png = Buffer.from("89504e470d0a1a0a00000000", "hex");
  const options = (body: Buffer[]): ImageDependencies => ({
    lookup: async () => [{ address: "93.184.216.34", family: 4 }],
    open: async () => ({ status: 200, mime: "image/png", body }),
  });
  const image = await fetchImage("https://images.example/a", options([png]));
  assert.deepEqual(image.body, png);
  assert.equal(image.mime, "image/png");
  await assert.rejects(
    fetchImage(
      "https://images.example/a",
      options([Buffer.from("<svg onload='alert(1)'/>")]),
    ),
  );
  await assert.rejects(
    fetchImage(
      "https://images.example/a",
      options([Buffer.alloc(5 * 1024 * 1024 + 1)]),
    ),
  );
});
