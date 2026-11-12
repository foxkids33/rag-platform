import { Fragment, FormEvent, ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, setApiAccessToken, setApiCsrfToken } from "./api";
import { AuthUserProvider } from "./auth-context";
import "./password-auth.css";

type Session = {
  subject: string;
  tenant_id: string;
  roles: string[];
  auth_enabled: boolean;
  username: string;
  display_name: string | null;
  csrf_token: string;
  is_admin?: boolean;
};

type AuthState = "loading" | "authenticated" | "unauthenticated" | "error";

const API = import.meta.env.VITE_API_BASE_URL || "";
const SESSION_CHANNEL = "rag-platform-session";
const SESSION_EVENT_KEY = "rag-platform:session-changed";

function isSession(value: unknown): value is Session {
  if (!value || typeof value !== "object") return false;
  const session = value as Partial<Session>;
  return typeof session.subject === "string"
    && typeof session.tenant_id === "string"
    && typeof session.username === "string"
    && typeof session.csrf_token === "string"
    && session.csrf_token.length > 0
    && Array.isArray(session.roles)
    && session.roles.every((role) => typeof role === "string");
}

function isSecureLogin(): boolean {
  return window.location.protocol === "https:"
    || ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
}

function authError(response: Response, registration: boolean): string {
  if (response.status === 429) return "Слишком много попыток. Подождите немного и попробуйте снова.";
  if (response.status === 401) return "Неверное имя пользователя или пароль.";
  if (response.status === 409 && registration) return "Не удалось зарегистрировать это имя пользователя. Попробуйте другое.";
  if (response.status === 403) return "Запрос отклонён. Обновите страницу и попробуйте снова.";
  if (response.status === 422 || response.status === 400) {
    return registration
      ? "Проверьте имя пользователя и пароль: имя — от 3 до 64 символов, пароль — от 12 до 128."
      : "Проверьте имя пользователя и пароль.";
  }
  if (response.status >= 500) return "Сервис входа временно недоступен. Попробуйте позже.";
  return registration ? "Не удалось создать учётную запись." : "Не удалось войти в систему.";
}

function PasswordVisibilityIcon({ visible }: { visible: boolean }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" />
      <circle cx="12" cy="12" r="3" />
      {!visible && <path d="m3 3 18 18" />}
    </svg>
  );
}

export function PasswordAuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>("loading");
  const [session, setSession] = useState<Session | null>(null);
  const [registration, setRegistration] = useState(false);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [rememberMe, setRememberMe] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  const requestSequence = useRef(0);
  const channel = useRef<BroadcastChannel | null>(null);

  const applySession = useCallback((next: Session | null) => {
    requestSequence.current += 1;
    setApiAccessToken(null);
    setApiCsrfToken(next?.csrf_token ?? null);
    setSession(next);
    setState(next ? "authenticated" : "unauthenticated");
    setPassword("");
    setConfirmation("");
    setShowPassword(false);
  }, []);

  const broadcastSessionChange = useCallback(() => {
    // Only a non-sensitive notification is shared. Cookies and CSRF/session
    // tokens are never stored in localStorage or sent through this channel.
    const marker = `${Date.now()}-${Math.random()}`;
    if (channel.current) channel.current.postMessage(marker);
    else {
      try { window.localStorage.setItem(SESSION_EVENT_KEY, marker); }
      catch { /* Storage can be disabled; focus validation still works. */ }
    }
  }, []);

  const restoreSession = useCallback(async (signal?: AbortSignal) => {
    const sequence = ++requestSequence.current;
    try {
      const response = await apiFetch(`${API}/api/v1/auth/me`, { cache: "no-store", signal });
      if (!mounted.current || sequence !== requestSequence.current) return;
      if (response.status === 401) {
        applySession(null);
        return;
      }
      if (!response.ok) throw new Error("Не удалось проверить сессию. Попробуйте ещё раз.");
      const next: unknown = await response.json();
      if (!mounted.current || sequence !== requestSequence.current) return;
      if (!isSession(next)) throw new Error("Сервис входа вернул некорректный ответ.");
      applySession(next);
      setError("");
    } catch (reason) {
      if (!mounted.current || signal?.aborted || sequence !== requestSequence.current) return;
      // Hide private content while the current identity cannot be established.
      applySession(null);
      setState("error");
      setError(reason instanceof Error ? reason.message : "Не удалось проверить сессию.");
    }
  }, [applySession]);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    const recheck = () => {
      if (document.visibilityState === "visible") void restoreSession(controller.signal);
    };
    const sessionChanged = () => {
      applySession(null);
      setState("loading");
      void restoreSession(controller.signal);
    };
    const storageChanged = (event: StorageEvent) => {
      if (event.key === SESSION_EVENT_KEY) sessionChanged();
    };
    const authenticationRequired = () => {
      applySession(null);
      setError("Сессия завершена. Войдите снова.");
      broadcastSessionChange();
    };
    if (typeof BroadcastChannel !== "undefined") {
      channel.current = new BroadcastChannel(SESSION_CHANNEL);
      channel.current.addEventListener("message", sessionChanged);
    }
    window.addEventListener("storage", storageChanged);
    window.addEventListener("focus", recheck);
    window.addEventListener("rag:authentication-required", authenticationRequired);
    document.addEventListener("visibilitychange", recheck);
    void restoreSession(controller.signal);
    return () => {
      mounted.current = false;
      controller.abort();
      requestSequence.current += 1;
      channel.current?.close();
      channel.current = null;
      window.removeEventListener("storage", storageChanged);
      window.removeEventListener("focus", recheck);
      window.removeEventListener("rag:authentication-required", authenticationRequired);
      document.removeEventListener("visibilitychange", recheck);
    };
  }, [applySession, broadcastSessionChange, restoreSession]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || !isSecureLogin()) return;
    setError("");
    if (registration && password !== confirmation) {
      setError("Пароли не совпадают.");
      return;
    }
    const sequence = ++requestSequence.current;
    setBusy(true);
    try {
      const response = await apiFetch(`${API}/api/v1/auth/${registration ? "register" : "login"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(registration
          ? { username: username.trim(), password, display_name: displayName.trim() || undefined }
          : { username: username.trim(), password, remember_me: rememberMe }),
      });
      if (!mounted.current || sequence !== requestSequence.current) return;
      if (!response.ok) throw new Error(authError(response, registration));
      const next: unknown = await response.json();
      if (!mounted.current || sequence !== requestSequence.current) return;
      if (!isSession(next)) throw new Error("Сервис входа вернул некорректный ответ.");
      applySession(next);
      setDisplayName("");
      broadcastSessionChange();
    } catch (reason) {
      if (!mounted.current || sequence !== requestSequence.current) return;
      setError(reason instanceof Error ? reason.message : "Не удалось соединиться с сервисом входа.");
    } finally {
      if (mounted.current) {
        setBusy(false);
        setPassword("");
        setConfirmation("");
      }
    }
  };

  const signOut = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const response = await apiFetch(`${API}/api/v1/auth/logout`, { method: "POST" });
      if (!response.ok && response.status !== 401) {
        throw new Error("Не удалось завершить серверную сессию. Попробуйте выйти ещё раз.");
      }
      if (!mounted.current) return;
      applySession(null);
      setRegistration(false);
      setUsername("");
      setDisplayName("");
      setRememberMe(false);
      broadcastSessionChange();
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : "Не удалось выйти из системы.");
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  if (state === "authenticated" && session) {
    return (
      <>
        <AuthUserProvider user={{ subject: session.subject, roles: session.roles, username: session.username, display_name: session.display_name, is_admin: session.is_admin }}>
          <Fragment key={session.subject}>{children}</Fragment>
        </AuthUserProvider>
        <aside className="auth-session password-session" aria-label="Текущая учётная запись">
          {error && <span className="password-session-error" role="alert">{error}</span>}
          <span title={session.username}>{session.display_name || session.username}</span>
          <button type="button" onClick={() => void signOut()} disabled={busy}>{busy ? "Выходим…" : "Выйти"}</button>
        </aside>
      </>
    );
  }

  const secure = isSecureLogin();
  return (
    <main className="password-auth-page">
      <div className="password-auth-shell">
        <header className="password-auth-brand" aria-label="Скала РАГ">
          Скала<span>^р</span><small>RAG</small>
        </header>
        <section className="password-auth-card" aria-labelledby="password-auth-title" aria-busy={busy || state === "loading"}>
          <h1 id="password-auth-title">{state === "loading" ? "Проверяем сессию…" : registration ? "Создать учётную запись" : "Вход в систему"}</h1>
          {state === "loading" ? <p className="password-auth-loading" role="status">Подключаемся к сервису…</p> : (
            <>
              <p className="password-auth-description">Рабочие пространства и история запросов привязаны к вашей учётной записи.</p>
              {!secure && <div className="password-auth-error" role="alert">Для безопасного входа откройте HTTPS-адрес сервиса. Пароль по незашифрованному HTTP не отправляется.</div>}
              {error && <div className="password-auth-error" role="alert">{error}</div>}
              <form onSubmit={(event) => void submit(event)}>
                <label htmlFor="rag-username">Имя пользователя</label>
                <input id="rag-username" name="username" type="text" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} minLength={3} maxLength={64} pattern="[a-zA-Z0-9][a-zA-Z0-9_.\-]{2,63}" title="От 3 до 64 символов: латинские буквы, цифры, точка, дефис и подчёркивание. Первый символ — буква или цифра." required disabled={busy} autoFocus />
                {registration && <>
                  <small className="password-auth-field-hint">Латинские буквы, цифры, точка, дефис и подчёркивание.</small>
                  <label htmlFor="rag-display-name">Ваше имя <span className="password-auth-optional">необязательно</span></label>
                  <input id="rag-display-name" name="display_name" type="text" value={displayName} onChange={(event) => setDisplayName(event.target.value)} autoComplete="name" maxLength={120} disabled={busy} />
                </>}
                <label htmlFor="rag-password">Пароль</label>
                <div className="password-auth-password">
                  <input id="rag-password" name="password" type={showPassword ? "text" : "password"} value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={registration ? "new-password" : "current-password"} minLength={12} maxLength={128} required disabled={busy} aria-describedby={registration ? "password-policy" : undefined} />
                  <button type="button" className="password-auth-visibility" onClick={() => setShowPassword((visible) => !visible)} aria-label={showPassword ? "Скрыть пароль" : "Показать пароль"} aria-pressed={showPassword} disabled={busy}><PasswordVisibilityIcon visible={showPassword} /></button>
                </div>
                {registration ? <>
                  <small id="password-policy" className="password-auth-field-hint">От 12 до 128 символов. Используйте уникальный пароль.</small>
                  <label htmlFor="rag-password-confirmation">Повторите пароль</label>
                  <input id="rag-password-confirmation" name="password_confirmation" type={showPassword ? "text" : "password"} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="new-password" minLength={12} maxLength={128} required disabled={busy} />
                </> : <label className="password-auth-remember"><input name="remember_me" type="checkbox" checked={rememberMe} onChange={(event) => setRememberMe(event.target.checked)} disabled={busy} />Запомнить меня</label>}
                <button className="password-auth-submit" type="submit" disabled={busy || !secure}>{busy ? "Подождите…" : registration ? "Зарегистрироваться" : "Войти"}</button>
              </form>
              <p className="password-auth-switch">{registration ? "Уже есть учётная запись?" : "Нет учётной записи?"}{" "}<button type="button" disabled={busy} onClick={() => { setRegistration((value) => !value); setPassword(""); setConfirmation(""); setShowPassword(false); setError(""); }}>{registration ? "Войти" : "Зарегистрироваться"}</button></p>
              {state === "error" && <button type="button" className="password-auth-retry" disabled={busy} onClick={() => { setState("loading"); void restoreSession(); }}>Проверить подключение</button>}
            </>
          )}
        </section>
        <p className="password-auth-footer">Корпоративная платформа знаний</p>
      </div>
    </main>
  );
}
