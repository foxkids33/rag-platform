let accessToken: string | null = null;

export function setApiAccessToken(token: string | null): void {
  accessToken = token;
}

export const apiFetch: typeof window.fetch = (input, init = {}) => {
  const headers = new Headers(init.headers);
  if (accessToken && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }
  return window.fetch(input, { ...init, headers }).then((response) => {
    if (response.status === 401 && accessToken) {
      window.dispatchEvent(new Event("rag:authentication-required"));
    }
    return response;
  });
};
