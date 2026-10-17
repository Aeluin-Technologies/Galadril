export {};

declare global {
  interface WindowEventMap {
    "pengolo-access-invalidated": Event;
    "pengolo-identity-verified": Event;
    "galadril-session-refreshed": Event;
  }
}
