export type RuntimeConfig = {
  authEnabled: boolean;
  authMode: "oidc" | "password";
  oidcAuthority: string;
  oidcClientId: string;
  oidcScopes: string;
  oidcRedirectUri: string;
  oidcPostLogoutRedirectUri: string;
};

declare global {
  interface Window {
    __RAG_RUNTIME_CONFIG__?: Partial<RuntimeConfig>;
  }
}

const raw = window.__RAG_RUNTIME_CONFIG__ ?? {};
const enabled = raw.authEnabled === true || String(raw.authEnabled).toLowerCase() === "true";

export const runtimeConfig: RuntimeConfig = Object.freeze({
  authEnabled: enabled,
  authMode: raw.authMode === "password" ? "password" : "oidc",
  oidcAuthority: String(raw.oidcAuthority ?? "").replace(/\/$/, ""),
  oidcClientId: String(raw.oidcClientId ?? ""),
  oidcScopes: String(raw.oidcScopes || "openid profile email"),
  oidcRedirectUri: String(raw.oidcRedirectUri || `${window.location.origin}/auth/callback`),
  oidcPostLogoutRedirectUri: String(raw.oidcPostLogoutRedirectUri || window.location.origin),
});
