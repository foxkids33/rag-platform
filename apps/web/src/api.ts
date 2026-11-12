let accessToken: string | null = null;
let csrfToken: string | null = null;
let authenticationRevision = 0;

export function setApiAccessToken(token: string | null): void {
  accessToken = token;
  authenticationRevision += 1;
}

export function setApiCsrfToken(token: string | null): void {
  csrfToken = token;
  authenticationRevision += 1;
}

function isApiRequest(input: RequestInfo | URL): boolean {
  try {
    const configuredBase = new URL(import.meta.env?.VITE_API_BASE_URL || "/", window.location.href);
    const target = new URL(input instanceof Request ? input.url : String(input), window.location.href);
    const prefix = `${configuredBase.pathname.replace(/\/$/, "")}/api/`;
    return target.origin === configuredBase.origin && target.pathname.startsWith(prefix);
  } catch {
    return false;
  }
}

export const apiFetch: typeof window.fetch = (input, init = {}) => {
  const apiRequest = isApiRequest(input);
  const headers = new Headers(init.headers ?? (input instanceof Request ? input.headers : undefined));
  const method = String(init.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
  const revision = authenticationRevision;
  const authenticated = Boolean(accessToken || csrfToken);
  if (apiRequest && accessToken && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }
  if (apiRequest && csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  return window.fetch(input, {
    ...init,
    headers,
    credentials: init.credentials ?? "same-origin",
    ...(apiRequest ? { redirect: "error" as const } : {}),
  }).then((response) => {
    // An old request must not sign out a different account that logged in while
    // the request was in flight. Never attach credentials to third-party URLs.
    if (apiRequest && response.status === 401 && authenticated && revision === authenticationRevision) {
      window.dispatchEvent(new Event("rag:authentication-required"));
    }
    return response;
  });
};
