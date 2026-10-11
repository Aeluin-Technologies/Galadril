import { defineApolloClient } from "@nuxtjs/apollo/config";

export default defineApolloClient({
  httpEndpoint: "/graphql",
  wsEndpoint: "/graphql",

  connectToDevTools: process.env.NODE_ENV === "development",
  httpLinkOptions: { credentials: "same-origin" },
  tokenStorage: "localStorage",
  tokenName: "unused",
});
