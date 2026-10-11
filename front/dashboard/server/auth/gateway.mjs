import { requestJson } from "./http.mjs";

const operations = {
  loginProvider:
    "query($email:String!,$localEnabled:Boolean!){loginProvider(email:$email,localEnabled:$localEnabled)}",
  activeUser:
    "query($tenant:String!,$user:String!){activeUser(tenant:$tenant,user:$user)}",
  passkeyContext:
    "query($tenant:String!,$user:String!){passkeyContext(tenant:$tenant,user:$user)}",
  resolveIdentity:
    "mutation($domain:String!,$connection:String!,$external:String!,$profile:String!){resolveIdentity(domain:$domain,connection:$connection,external:$external,profile:$profile)}",
  bindLocal:
    "mutation($tenant:String!,$user:String!,$external:String!,$profile:String!){bindLocal(tenant:$tenant,user:$user,external:$external,profile:$profile)}",
  challenge:
    "mutation($tenant:String!,$user:String!,$flow:String!,$value:String!){challenge(tenant:$tenant,user:$user,flow:$flow,value:$value)}",
  consume:
    "mutation($tenant:String!,$user:String!,$flow:String!){consume(tenant:$tenant,user:$user,flow:$flow)}",
  saveCredential:
    "mutation($tenant:String!,$user:String!,$generation:String!,$credential:String!,$previousCounter:String){saveCredential(tenant:$tenant,user:$user,generation:$generation,credential:$credential,previousCounter:$previousCounter)}",
};

/** Only the private Gateway schema can persist or resolve canonical identities. */
export class GatewayAuthClient {
  constructor(endpoint, key) {
    this.endpoint = new URL(endpoint);
    if (
      !["http:", "https:"].includes(this.endpoint.protocol) ||
      this.endpoint.username ||
      this.endpoint.password ||
      this.endpoint.search ||
      this.endpoint.hash ||
      this.endpoint.pathname !== "/internal/auth/graphql" ||
      typeof key !== "string" ||
      key.length < 32
    )
      throw new Error("Invalid private Gateway configuration");
    this.key = key;
  }

  async request(operation, variables) {
    if (!Object.hasOwn(operations, operation))
      throw new Error("Invalid authentication operation");
    const response = await requestJson(this.endpoint, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-galadril-auth-key": this.key,
      },
      body: JSON.stringify({ query: operations[operation], variables }),
    });
    if (
      response.errors?.length ||
      response.data?.[operation] === undefined ||
      response.data[operation] === null
    )
      throw new Error("Authentication unavailable or denied");
    return response.data[operation];
  }
}
