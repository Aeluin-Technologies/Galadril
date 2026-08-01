import { defineApolloClient } from "@nuxtjs/apollo/config";

export default defineApolloClient({
  httpEndpoint:
    process.env.NUXT_PUBLIC_GRAPHQL_ENDPOINT ?? "http://localhost:8080/graphql",

  wsEndpoint:
    process.env.NUXT_PUBLIC_GRAPHQL_WS_ENDPOINT ??
    "ws://localhost:8080/graphql",

  connectToDevTools: process.env.NODE_ENV === "development",
  tokenStorage: "cookie",
  authType: "Bearer",
  authHeader: "Authorization",
  tokenName: "token",
});
