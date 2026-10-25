import { metrics } from "@opentelemetry/api";
import { fetchImage } from "../../media/image.ts";

const requests = metrics.getMeter("galadril.studio.media").createCounter("galadril.media.requests");

export default defineEventHandler(async event => {
  setResponseHeaders(event, {
    "Cache-Control":"private, no-store, max-age=0",
    "Vary":"Cookie, Authorization",
    "Referrer-Policy":"no-referrer",
    "X-Content-Type-Options":"nosniff",
    "Content-Security-Policy":"default-src 'none'; sandbox",
  });
  const url = getQuery(event).url;
  if (typeof url !== "string" || url.length > 4096) throw createError({statusCode:400,statusMessage:"Invalid image"});
  try {
    const image = await fetchImage(url);
    requests.add(1,{outcome:"success"});
    setResponseHeader(event,"Content-Type",image.mime);
    return image.body;
  } catch {
    requests.add(1,{outcome:"refused"});
    throw createError({statusCode:502,statusMessage:"Image unavailable"});
  }
});
