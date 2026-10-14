export {};

declare global {
  interface WindowEventMap {
    "pengolo-access-invalidated": Event;
    "pengolo-identity-verified": Event;
  }
}
