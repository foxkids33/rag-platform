import { FormEvent, useEffect, useState } from "react";
import { apiFetch as fetch } from "./api";

type ConfluenceStatus = {
  enabled: boolean;
  can_import: boolean;
  deployment: "cloud" | "data_center";
  auth_mode: "basic" | "bearer";
  host: string | null;
  max_pages_per_import: number;
};

type ImportResult = {
  requested: number;
  queued: number;
  skipped: number;
  failed: number;
};

type Props = {
  apiBaseUrl: string;
  workspaceId: string;
  onImported: () => Promise<void>;
  onError: (message: string) => void;
};

async function errorMessage(response: Response): Promise<string> {
  const payload = await response.json().catch(() => null) as { detail?: string } | null;
  return payload?.detail || `HTTP ${response.status}`;
}

export function ConfluenceImportPanel({ apiBaseUrl, workspaceId, onImported, onError }: Props) {
  const [integration, setIntegration] = useState<ConfluenceStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [statusUnavailable, setStatusUnavailable] = useState(false);
  const [mode, setMode] = useState<"pages" | "space">("space");
  const [value, setValue] = useState("KBR");
  const [email, setEmail] = useState("");
  const [token, setToken] = useState("");
  const [checking, setChecking] = useState(false);
  const [connectionVerified, setConnectionVerified] = useState(false);
  const [importing, setImporting] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetch(`${apiBaseUrl}/api/v1/integrations/confluence/status`)
      .then(async (response) => {
        if (!response.ok) throw new Error(await errorMessage(response));
        return response.json() as Promise<ConfluenceStatus>;
      })
      .then((status) => {
        if (!cancelled) {
          setIntegration(status);
          setStatusUnavailable(false);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setIntegration(null);
          setStatusUnavailable(true);
        }
      })
      .finally(() => { if (!cancelled) setStatusLoading(false); });
    return () => { cancelled = true; };
  }, [apiBaseUrl]);

  const credentialsReady = Boolean(
    token.trim()
    && (integration?.auth_mode !== "basic" || email.trim()),
  );

  const connectionPayload = () => ({
    email: integration?.auth_mode === "basic" ? email.trim() : null,
    token: token.trim(),
  });

  const checkConnection = async () => {
    if (!credentialsReady || checking) return;
    setChecking(true);
    setConnectionVerified(false);
    onError("");
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/integrations/confluence/check`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(connectionPayload()),
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      setConnectionVerified(true);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось проверить подключение");
    } finally {
      setChecking(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const cleaned = value.trim();
    if (!workspaceId || !cleaned || importing) return;
    setImporting(true);
    setResult(null);
    onError("");
    try {
      const body = mode === "space"
        ? { space_key: cleaned, credentials: connectionPayload() }
        : {
          page_ids: cleaned.split(/[\s,;]+/).filter(Boolean),
          credentials: connectionPayload(),
        };
      const response = await fetch(
        `${apiBaseUrl}/api/v1/workspaces/${workspaceId}/imports/confluence`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (!response.ok) throw new Error(await errorMessage(response));
      const importResult = await response.json() as ImportResult;
      setResult(importResult);
      await onImported();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось импортировать Confluence");
    } finally {
      setImporting(false);
    }
  };

  return (
    <section className="resource-card confluence-import-card">
      <div className="integration-heading">
        <span className="integration-logo" aria-hidden="true">C</span>
        <div>
          <h2>Импорт из Confluence</h2>
          <p>{integration?.host || "Подключение к корпоративной базе знаний"}</p>
        </div>
        <span className={`integration-status ${connectionVerified ? "connected" : ""}`}>
          {statusLoading
            ? "Проверка…"
            : connectionVerified
              ? "Подключение проверено"
              : integration?.enabled
                ? "Нужен токен"
                : "Не настроено"}
        </span>
      </div>

      {statusLoading ? (
        <div className="integration-loading"><span className="spinner" /> Проверяю настройки интеграции…</div>
      ) : statusUnavailable ? (
        <div className="integration-empty">
          <strong>Не удалось проверить интеграцию</strong>
          <span>Проверьте доступность API и повторите попытку после восстановления соединения.</span>
        </div>
      ) : !integration?.enabled ? (
        <div className="integration-empty">
          <strong>Confluence не настроен</strong>
          <span>Администратор должен добавить адрес и учётные данные Confluence в серверную конфигурацию.</span>
        </div>
      ) : !integration.can_import ? (
        <div className="integration-empty">
          <strong>Недостаточно прав</strong>
          <span>Импорт доступен пользователям с ролью tenant-admin.</span>
        </div>
      ) : (
        <form className="confluence-import" onSubmit={(event) => void submit(event)}>
          <div className="confluence-credentials">
            <div className="credentials-heading">
              <div>
                <strong>Подключение</strong>
                <small>Учётные данные действуют только для текущей вкладки и не сохраняются.</small>
              </div>
              <span>{integration.deployment === "cloud" ? "Confluence Cloud" : "Data Center"}</span>
            </div>

            {integration.auth_mode === "basic" && (
              <label htmlFor="confluence-email">
                E-mail аккаунта Atlassian
                <input
                  id="confluence-email"
                  type="email"
                  value={email}
                  onChange={(event) => { setEmail(event.target.value); setConnectionVerified(false); }}
                  placeholder="name@company.ru"
                  autoComplete="username"
                />
              </label>
            )}

            <label htmlFor="confluence-token">
              {integration.auth_mode === "basic" ? "API-токен Atlassian" : "Personal access token"}
              <div className="credential-token-row">
                <input
                  id="confluence-token"
                  type="password"
                  value={token}
                  onChange={(event) => { setToken(event.target.value); setConnectionVerified(false); }}
                  placeholder="Вставьте токен"
                  autoComplete="off"
                  spellCheck={false}
                />
                <button
                  className="connection-check-button"
                  type="button"
                  disabled={!credentialsReady || checking || importing}
                  onClick={() => void checkConnection()}
                >
                  {checking ? "Проверяю…" : "Проверить"}
                </button>
              </div>
            </label>

            <small className="credential-security-note">
              🔒 Токен передаётся backend по текущему HTTPS-соединению, используется для запросов к Confluence и не записывается в базу.
            </small>
          </div>

          <div className="segmented-control" aria-label="Режим импорта">
            <button type="button" className={mode === "space" ? "active" : ""} onClick={() => setMode("space")}>
              Пространство
            </button>
            <button type="button" className={mode === "pages" ? "active" : ""} onClick={() => setMode("pages")}>
              Отдельные страницы
            </button>
          </div>

          <label htmlFor="confluence-source-value">
            {mode === "space" ? "Ключ пространства" : "ID страниц"}
          </label>
          <div className="confluence-import-row">
            <input
              id="confluence-source-value"
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder={mode === "space" ? "Например, KBR" : "123456, 234567, 345678"}
            />
            <button
              className="primary-button"
              type="submit"
              disabled={!value.trim() || !credentialsReady || importing || checking || !workspaceId}
            >
              {importing ? "Синхронизация…" : "Синхронизировать"}
            </button>
          </div>
          <small className="field-hint">
            {mode === "space"
              ? `Будет обработано не более ${integration.max_pages_per_import} страниц за один запуск.`
              : "Несколько ID можно разделить пробелами, запятыми или точкой с запятой."}
          </small>

          {result && (
            <div className={`import-result ${result.failed ? "has-errors" : ""}`} role="status">
              <span><strong>{result.queued}</strong> в очереди</span>
              <span><strong>{result.skipped}</strong> без изменений</span>
              <span><strong>{result.failed}</strong> ошибок</span>
            </div>
          )}
        </form>
      )}

      <div className="integration-notice">
        <span aria-hidden="true">i</span>
        <p><strong>Область доступа</strong> Права отдельных страниц Confluence не переносятся. Используйте workspace только для одной доверенной аудитории.</p>
      </div>
    </section>
  );
}
