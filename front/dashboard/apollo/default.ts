import { defineApolloClient } from "@nuxtjs/apollo/config";

export default defineApolloClient({
  httpEndpoint: "",
  wsEndpoint: "",
  connectToDevTools: true,
  tokenStorage: "cookie",
  authType: "Bearer",
  authHeader: "Authorization",
  tokenName: "token",
});
