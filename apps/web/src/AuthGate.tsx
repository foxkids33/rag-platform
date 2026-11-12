import { Fragment, ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { User, UserManager, WebStorageStateStore } from "oidc-client-ts";
import { setApiAccessToken } from "./api";
import { runtimeConfig } from "./runtime-config";
import { PasswordAuthGate } from "./PasswordAuthGate";
import { AuthUserProvider } from "./auth-context";

type AuthState = "loading" | "authenticated" | "unauthenticated" | "error";

function callbackInProgress(): boolean {
  const params = new URLSearchParams(window.location.search);
  return params.has("state") && (params.has("code") || params.has("error"));
}

function returnPath(user: User): string {
  const requested = typeof user.state === "string" ? user.state : "/";
  return requested.startsWith("/") && !requested.startsWith("//") ? requested : "/";
}

function OidcAuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(runtimeConfig.authEnabled ? "loading" : "authenticated");
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState("");

  const manager = useMemo(() => {
    if (!runtimeConfig.authEnabled) return null;
    if (!runtimeConfig.oidcAuthority || !runtimeConfig.oidcClientId) return null;
    return new UserManager({
      authority: runtimeConfig.oidcAuthority,
      client_id: runtimeConfig.oidcClientId,
      redirect_uri: runtimeConfig.oidcRedirectUri,
      post_logout_redirect_uri: runtimeConfig.oidcPostLogoutRedirectUri,
      response_type: "code",
      scope: runtimeConfig.oidcScopes,
      userStore: new WebStorageStateStore({ store: window.sessionStorage }),
      stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
      automaticSilentRenew: false,
      monitorSession: false,
    });
  }, []);

  const applyUser = useCallback((nextUser: User | null) => {
    const active = nextUser && !nextUser.expired ? nextUser : null;
    setUser(active);
    setApiAccessToken(active?.access_token ?? null);
    setState(active ? "authenticated" : "unauthenticated");
  }, []);

  useEffect(() => {
    if (!runtimeConfig.authEnabled) return;
    if (!manager) {
      setError("OIDC включён, но issuer или client ID не настроены.");
      setState("error");
      return;
    }
    let cancelled = false;
    const handleLoaded = (nextUser: User) => applyUser(nextUser);
    const handleUnloaded = () => applyUser(null);
    manager.events.addUserLoaded(handleLoaded);
    manager.events.addUserUnloaded(handleUnloaded);
    manager.events.addAccessTokenExpired(handleUnloaded);
    window.addEventListener("rag:authentication-required", handleUnloaded);

    const initialize = async () => {
      try {
        if (callbackInProgress()) {
          const callbackUser = await manager.signinRedirectCallback();
          if (cancelled) return;
          applyUser(callbackUser);
          window.history.replaceState({}, document.title, returnPath(callbackUser));
          return;
        }
        const storedUser = await manager.getUser();
        if (!cancelled) applyUser(storedUser);
      } catch (reason) {
        if (cancelled) return;
        setApiAccessToken(null);
        setError(reason instanceof Error ? reason.message : "Не удалось завершить корпоративный вход.");
        setState("error");
      }
    };
    void initialize();
    return () => {
      cancelled = true;
      manager.events.removeUserLoaded(handleLoaded);
      manager.events.removeUserUnloaded(handleUnloaded);
      manager.events.removeAccessTokenExpired(handleUnloaded);
      window.removeEventListener("rag:authentication-required", handleUnloaded);
    };
  }, [applyUser, manager]);

  const signIn = async () => {
    if (!manager) return;
    setError("");
    setState("loading");
    try {
      const requested = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      await manager.signinRedirect({ state: requested });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось начать корпоративный вход.");
      setState("error");
    }
  };

  const signOut = async () => {
    if (!manager) return;
    setApiAccessToken(null);
    try {
      await manager.signoutRedirect();
    } catch (reason) {
      await manager.removeUser();
      applyUser(null);
      setError(reason instanceof Error ? reason.message : "Не удалось завершить сессию в провайдере.");
    }
  };

  if (!runtimeConfig.authEnabled) return <>{children}</>;
  if (state !== "authenticated") {
    return (
      <main className="auth-page">
        <section className="auth-card" aria-live="polite">
          <div className="auth-brand">Скала<span>^р</span></div>
          <span className="auth-kicker">Enterprise Knowledge Assistant</span>
          <h1>{state === "loading" ? "Проверяем сессию…" : "Корпоративный вход"}</h1>
          <p>Используйте рабочую учётную запись. Доступ к пространствам и базе знаний определяется tenant и ролью.</p>
          {error && <div className="auth-error">{error}</div>}
          {state !== "loading" && <button type="button" onClick={() => void signIn()}>Войти через SSO</button>}
          <small>Токен хранится только в текущей вкладке браузера.</small>
        </section>
      </main>
    );
  }

  const displayName = String(user?.profile.name || user?.profile.preferred_username || user?.profile.email || "Пользователь");
  return (
    <>
      <AuthUserProvider user={{ subject: user?.profile.sub || "", roles: Array.isArray(user?.profile.roles) ? user.profile.roles.filter((role): role is string => typeof role === "string") : [] }}>
        <Fragment key={user?.profile.sub}>{children}</Fragment>
      </AuthUserProvider>
      <aside className="auth-session" title={displayName}>
        <span>{displayName}</span>
        <button type="button" onClick={() => void signOut()}>Выйти</button>
      </aside>
    </>
  );
}

export function AuthGate({ children }: { children: ReactNode }) {
  if (!runtimeConfig.authEnabled) return <>{children}</>;
  return runtimeConfig.authMode === "password"
    ? <PasswordAuthGate>{children}</PasswordAuthGate>
    : <OidcAuthGate>{children}</OidcAuthGate>;
}
