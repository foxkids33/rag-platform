import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch as fetch } from "./api";

type ConnectorSummary = {
  id: string;
  name: string;
  enabled: boolean;
  enrolled: boolean;
  online: boolean;
  enrollment_pending: boolean;
  enrollment_expires_at: string | null;
  allowed_spaces: string[];
  capabilities: string[];
  version: string | null;
  last_seen_at: string | null;
};

type ConnectorStatus = {
  enabled: boolean;
  configured: boolean;
  online: boolean;
  can_manage: boolean;
  connectors: ConnectorSummary[];
  max_pages_per_job: number;
  max_job_seconds: number;
};

type ConfluenceConnection = {
  mode: "direct" | "connector";
  enabled: boolean;
  configured: boolean;
  ready: boolean;
  readiness_reason: string | null;
  missing_configuration: string[];
  host: string | null;
  allowed_spaces: string[];
  capabilities: string[];
  max_pages_per_job: number;
  max_job_seconds: number;
  connector: ConnectorStatus | null;
};

type Enrollment = {
  connector_id: string;
  enrollment_token: string;
  expires_at: string;
  allowed_spaces: string[];
};

type Operation = {
  operation_id: string;
  status: "QUEUED" | "LEASED" | "SUCCEEDED" | "FAILED" | "CANCELLED";
  type: string;
  result: Record<string, unknown> | null;
  error: string | null;
};

type Space = {
  key: string;
  name: string;
  homepage_id: string | null;
  web_url: string | null;
};

type PageNode = {
  id: string;
  title: string;
  space_key: string;
  version: string;
  parent_id: string | null;
  has_children: boolean;
  web_url: string | null;
};

type ImportResult = {
  requested: number;
  queued: number;
  skipped: number;
  failed: number;
  results: { page_id: string; status: string; detail?: string | null }[];
};

type Props = {
  apiBaseUrl: string;
  workspaceId: string;
  onImported: () => Promise<void>;
  onError: (message: string) => void;
};

type PageBranchProps = {
  nodes: PageNode[];
  childrenByParent: Record<string, PageNode[]>;
  expandedPages: Set<string>;
  loadingKeys: Set<string>;
  selected: Record<string, PageNode>;
  disabled: boolean;
  onExpand: (page: PageNode) => void;
  onSelect: (page: PageNode, checked: boolean) => void;
};

async function errorMessage(response: Response): Promise<string> {
  const payload = await response.json().catch(() => null) as { detail?: string } | null;
  return payload?.detail || `HTTP ${response.status}`;
}

function parentKey(spaceKey: string, parentId: string | null): string {
  return `${spaceKey}:${parentId || "__root__"}`;
}

export function describeAllowedSpaces(keys: string[]): string {
  const count = keys.length;
  const remainder = count % 100;
  const ending = count % 10;
  const noun = remainder >= 11 && remainder <= 14
    ? "пространств"
    : ending === 1 ? "пространство" : ending >= 2 && ending <= 4 ? "пространства" : "пространств";
  const corporateKeys = keys.filter((key) => !key.startsWith("~"));
  const preferred = ["KBR", "DEV", "SUPPORT"].filter((key) => corporateKeys.includes(key));
  const preview = [...preferred, ...corporateKeys.filter((key) => !preferred.includes(key))].slice(0, 3);
  return `${count} ${noun}${preview.length ? ` · ${preview.join(", ")}${count > preview.length ? " и другие" : ""}` : ""}`;
}

export function filterConfluenceSpaces(
  spaces: Space[], query: string, includePersonal: boolean,
): Space[] {
  const needle = query.trim().toLocaleLowerCase("ru-RU");
  return spaces.filter((space) => {
    // A deliberate search may find a personal space; the initial screen exposes no names.
    if (space.key.startsWith("~") && !includePersonal && !needle) return false;
    return !needle || `${space.key} ${space.name}`.toLocaleLowerCase("ru-RU").includes(needle);
  });
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function PageBranch({
  nodes,
  childrenByParent,
  expandedPages,
  loadingKeys,
  selected,
  disabled,
  onExpand,
  onSelect,
}: PageBranchProps) {
  return (
    <ul className="confluence-tree-branch">
      {nodes.map((page) => {
        const expanded = expandedPages.has(page.id);
        const key = parentKey(page.space_key, page.id);
        const children = childrenByParent[key] || [];
        return (
          <li key={page.id}>
            <div className={`confluence-tree-row ${selected[page.id] ? "selected" : ""}`}>
              <button
                type="button"
                className="tree-toggle"
                disabled={disabled || !page.has_children || loadingKeys.has(key)}
                onClick={() => onExpand(page)}
                aria-label={expanded ? "Свернуть страницу" : "Раскрыть страницу"}
                aria-expanded={page.has_children ? expanded : undefined}
              >
                {loadingKeys.has(key) ? "…" : page.has_children ? (expanded ? "▾" : "▸") : "·"}
              </button>
              <label>
                <input
                  type="checkbox"
                  checked={Boolean(selected[page.id])}
                  disabled={disabled}
                  onChange={(event) => onSelect(page, event.target.checked)}
                />
                <span>
                  <strong title={page.title}>{page.title}</strong>
                  <small>ID {page.id} · v{page.version}</small>
                </span>
              </label>
            </div>
            {expanded && children.length > 0 && (
              <PageBranch
                nodes={children}
                childrenByParent={childrenByParent}
                expandedPages={expandedPages}
                loadingKeys={loadingKeys}
                selected={selected}
                disabled={disabled}
                onExpand={onExpand}
                onSelect={onSelect}
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function ConfluenceImportPanel({ apiBaseUrl, workspaceId, onImported, onError }: Props) {
  const lifetime = useRef<AbortController | null>(null);
  const [connection, setConnection] = useState<ConfluenceConnection | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [enrollmentSpaces, setEnrollmentSpaces] = useState("");
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [spaceQuery, setSpaceQuery] = useState("");
  const [showPersonalSpaces, setShowPersonalSpaces] = useState(false);
  const [expandedSpaces, setExpandedSpaces] = useState<Set<string>>(new Set());
  const [expandedPages, setExpandedPages] = useState<Set<string>>(new Set());
  const [childrenByParent, setChildrenByParent] = useState<Record<string, PageNode[]>>({});
  const [loadingKeys, setLoadingKeys] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Record<string, PageNode>>({});
  const [manualPageId, setManualPageId] = useState("");
  const [manualSpace, setManualSpace] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [progress, setProgress] = useState<{ completed: number; total: number } | null>(null);
  const [statusError, setStatusError] = useState("");
  const [spaceScanCount, setSpaceScanCount] = useState(0);

  const direct = connection?.mode === "direct";
  const integration = connection?.connector || null;
  const activeSource = useMemo(() => {
    if (connection?.mode === "direct") {
      return connection.ready ? {
        name: connection.host || "Confluence",
        allowed_spaces: connection.allowed_spaces,
        capabilities: connection.capabilities,
        last_seen_at: null,
      } : null;
    }
    return connection?.connector?.connectors.find((item) => item.online) || null;
  }, [connection]);
  const selectedPages = useMemo(() => Object.values(selected), [selected]);
  const visibleSpaces = useMemo(
    () => filterConfluenceSpaces(spaces, spaceQuery, showPersonalSpaces),
    [spaces, spaceQuery, showPersonalSpaces],
  );
  const personalSpaceCount = spaces.filter((space) => space.key.startsWith("~")).length;

  useEffect(() => {
    const firstSpace = activeSource?.allowed_spaces[0];
    if (firstSpace) setManualSpace((current) => current || firstSpace);
  }, [activeSource]);

  const loadStatus = async (quiet = false, signal = lifetime.current?.signal) => {
    if (!quiet) setStatusLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/integrations/confluence/connection`, { signal });
      if (!response.ok) throw new Error(await errorMessage(response));
      setConnection(await response.json() as ConfluenceConnection);
      setStatusError("");
    } catch (reason) {
      if (signal?.aborted) return;
      setStatusError("Не удалось обновить статус подключения. Проверьте сеть; повторим автоматически.");
      if (!quiet) onError(reason instanceof Error ? reason.message : "Не удалось проверить подключение");
    } finally {
      if (!quiet && !signal?.aborted) setStatusLoading(false);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    const refresh = async () => {
      if (!controller.signal.aborted) await loadStatus(true, controller.signal);
    };
    void loadStatus(false, controller.signal);
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [apiBaseUrl]);

  useEffect(() => {
    setSelected({});
    setResult(null);
    setProgress(null);
  }, [workspaceId]);

  const waitForOperation = async (operationId: string, signal?: AbortSignal): Promise<Operation> => {
    const deadline = Date.now() + ((connection?.max_job_seconds || 1800) + 30) * 1000;
    while (Date.now() < deadline) {
      const response = await fetch(
        `${apiBaseUrl}/api/v1/workspaces/${workspaceId}/integrations/confluence/operations/${operationId}`,
        { signal },
      );
      if (!response.ok) throw new Error(await errorMessage(response));
      const operation = await response.json() as Operation;
      if (operation.status === "SUCCEEDED") return operation;
      if (operation.status === "FAILED" || operation.status === "CANCELLED") {
        throw new Error(operation.error || "Операция connector завершилась с ошибкой");
      }
      await delay(1000);
    }
    throw new Error("Превышено время ожидания ответа connector");
  };

  const performRequest = async (
    suffix: string,
    body?: Record<string, unknown>,
  ): Promise<Record<string, unknown>> => {
    const signal = lifetime.current?.signal;
    signal?.throwIfAborted();
    const response = await fetch(
      `${apiBaseUrl}/api/v1/workspaces/${workspaceId}/integrations/confluence/${direct ? "direct/" : ""}${suffix}`,
      {
        method: "POST",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal,
      },
    );
    if (!response.ok) throw new Error(await errorMessage(response));
    if (direct) return await response.json() as Record<string, unknown>;
    const created = await response.json() as { operation_id: string };
    return (await waitForOperation(created.operation_id, signal)).result || {};
  };

  const createEnrollment = async () => {
    const allowed = enrollmentSpaces.split(/[\s,;]+/).map((item) => item.trim()).filter(Boolean);
    if (!allowed.length || busy) return;
    setBusy("enrollment");
    onError("");
    try {
      const response = await fetch(
        `${apiBaseUrl}/api/v1/integrations/confluence/connectors/enrollments`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: "Confluence Connector", allowed_spaces: allowed }),
          signal: lifetime.current?.signal,
        },
      );
      if (!response.ok) throw new Error(await errorMessage(response));
      setEnrollment(await response.json() as Enrollment);
      await loadStatus(true);
    } catch (reason) {
      if (lifetime.current?.signal.aborted) return;
      onError(reason instanceof Error ? reason.message : "Не удалось создать код подключения");
    } finally {
      setBusy(null);
    }
  };

  const loadSpaces = async () => {
    if (!workspaceId || busy) return;
    setBusy("spaces");
    onError("");
    try {
      const response = await performRequest("spaces");
      const loaded = response.spaces;
      setSpaces(Array.isArray(loaded) ? loaded as Space[] : []);
      setChildrenByParent({});
      setExpandedSpaces(new Set());
      setExpandedPages(new Set());
    } catch (reason) {
      if (lifetime.current?.signal.aborted) return;
      onError(reason instanceof Error ? reason.message : "Не удалось загрузить пространства");
    } finally {
      setBusy(null);
    }
  };

  const loadChildren = async (spaceKey: string, parentId: string | null) => {
    const key = parentKey(spaceKey, parentId);
    if (childrenByParent[key] || loadingKeys.has(key)) return;
    setLoadingKeys((current) => new Set(current).add(key));
    onError("");
    try {
      const response = await performRequest("children", {
        space_key: spaceKey,
        parent_id: parentId,
      });
      const loaded = response.pages;
      setChildrenByParent((current) => ({
        ...current,
        [key]: Array.isArray(loaded) ? loaded as PageNode[] : [],
      }));
    } catch (reason) {
      if (lifetime.current?.signal.aborted) return;
      onError(reason instanceof Error ? reason.message : "Не удалось загрузить дерево страниц");
    } finally {
      setLoadingKeys((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
    }
  };

  const toggleSpace = (space: Space) => {
    const expanded = expandedSpaces.has(space.key);
    setExpandedSpaces((current) => {
      const next = new Set(current);
      if (expanded) next.delete(space.key);
      else next.add(space.key);
      return next;
    });
    if (!expanded) void loadChildren(space.key, null);
  };

  const togglePage = (page: PageNode) => {
    if (!page.has_children) return;
    const expanded = expandedPages.has(page.id);
    setExpandedPages((current) => {
      const next = new Set(current);
      if (expanded) next.delete(page.id);
      else next.add(page.id);
      return next;
    });
    if (!expanded) void loadChildren(page.space_key, page.id);
  };

  const selectPage = (page: PageNode, checked: boolean) => {
    setSelected((current) => {
      const next = { ...current };
      if (checked) next[page.id] = page;
      else delete next[page.id];
      return next;
    });
  };

  const addManualPage = (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    let pageId = manualPageId.trim();
    const spaceKey = manualSpace.trim();
    if (!pageId || !spaceKey) return;
    if (!/^\d+$/.test(pageId)) {
      try {
        const url = new URL(pageId);
        pageId = url.searchParams.get("pageId") || url.pathname.match(/\/pages\/(\d+)(?:\/|$)/)?.[1] || "";
      } catch { pageId = ""; }
    }
    if (!/^\d+$/.test(pageId)) {
      onError("Укажите числовой ID страницы или ссылку Confluence с pageId / pages/ID.");
      return;
    }
    if (!activeSource?.allowed_spaces.some((key) => key.toLowerCase() === spaceKey.toLowerCase())) {
      onError("Выберите пространство из списка разрешённых для этого подключения.");
      return;
    }
    onError("");
    selectPage({
      id: pageId,
      title: `Страница ${pageId}`,
      space_key: spaceKey,
      version: "unknown",
      parent_id: null,
      has_children: false,
      web_url: null,
    }, true);
    setManualPageId("");
  };

  const importSelected = async () => {
    if (!selectedPages.length || busy || !workspaceId) return;
    await importPages(selectedPages);
  };

  const importSpace = async (space: Space) => {
    if (busy || !workspaceId) return;
    setBusy("space-scan");
    setSpaceScanCount(0);
    setResult(null);
    setProgress(null);
    onError("");
    try {
      const pages = new Map<string, PageNode>();
      let start: number | null = 0;
      let cursor: string | null = null;
      while (start !== null) {
        const response = await performRequest("space-pages", {
          space_key: space.key, start, ...(direct && cursor ? { cursor } : {}),
        });
        const listing = response as { pages: PageNode[]; next_start: number | null; next_cursor?: string | null };
        if (!listing || !Array.isArray(listing.pages)) throw new Error("Некорректный список страниц space");
        const previousSize = pages.size;
        for (const page of listing.pages) pages.set(page.id, page);
        setSpaceScanCount(pages.size);
        if (listing.next_start !== null && (
          !Number.isSafeInteger(listing.next_start) || listing.next_start <= start || pages.size === previousSize
        )) throw new Error("Обход space остановлен: список страниц не продвигается");
        start = listing.next_start;
        cursor = listing.next_cursor || null;
      }
      lifetime.current?.signal.throwIfAborted();
      if (!pages.size) {
        onError("В этом space нет доступных страниц для импорта.");
        return;
      }
      if (!window.confirm(`Импортировать все ${pages.size} доступных страниц из «${space.name}» (${space.key}) в текущий workspace? Неизменённые страницы будут пропущены. Не закрывайте раздел до окончания передачи.`)) return;
      setSelected(Object.fromEntries(pages));
      await importPages([...pages.values()]);
    } catch (reason) {
      if (!lifetime.current?.signal.aborted) onError(reason instanceof Error ? reason.message : "Не удалось импортировать space");
    } finally {
      setBusy(null);
    }
  };

  const importPages = async (pages: PageNode[]) => {
    setBusy("import");
    setResult(null);
    setProgress({ completed: 0, total: pages.length });
    onError("");
    try {
      const batchSize = Math.max(1, Math.min(10, connection?.max_pages_per_job || 10));
      const total: ImportResult = { requested: 0, queued: 0, skipped: 0, failed: 0, results: [] };
      for (let offset = 0; offset < pages.length; offset += batchSize) {
        lifetime.current?.signal.throwIfAborted();
        const batch = pages.slice(offset, offset + batchSize);
        const response = await performRequest("imports", {
          page_ids: batch.map((page) => page.id),
          page_spaces: Object.fromEntries(batch.map((page) => [page.id, page.space_key])),
          expected_versions: Object.fromEntries(batch
            .filter((page) => page.version !== "unknown")
            .map((page) => [page.id, page.version])),
          force: false,
        });
        const imported = response as unknown as ImportResult;
        total.requested += imported.requested;
        total.queued += imported.queued;
        total.skipped += imported.skipped;
        total.failed += imported.failed;
        total.results.push(...imported.results);
        setResult({ ...total, results: [...total.results] });
        setProgress({ completed: Math.min(offset + batch.length, pages.length), total: pages.length });
        setSelected((current) => {
          const remaining = { ...current };
          for (const item of imported.results) {
            if (item.status !== "failed" && item.status !== "busy") delete remaining[item.page_id];
          }
          return remaining;
        });
        await onImported();
      }
    } catch (reason) {
      if (lifetime.current?.signal.aborted) return;
      onError(reason instanceof Error ? reason.message : "Не удалось импортировать страницы");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="resource-card confluence-import-card">
      <div className="integration-heading">
        <span className="integration-logo" aria-hidden="true">C</span>
        <div>
          <h2>Confluence</h2>
          <p>{direct ? "Импорт напрямую с сервера RAG" : "Импорт из корпоративной сети через коннектор"}</p>
        </div>
        <span className={`integration-status ${connection?.ready && !statusError ? "connected" : ""}`}>
          {statusLoading ? "Проверка…" : statusError ? "Нет связи с RAG" : direct ? (connection?.ready ? "Прямой доступ настроен" : "Нужна настройка") : integration?.online ? "Коннектор в сети" : "Коннектор не в сети"}
        </span>
      </div>
      {statusError && <p className="integration-hint" role="status">{statusError}</p>}

      {statusLoading ? (
        <div className="integration-loading"><span className="spinner" /> Проверяю подключение…</div>
      ) : direct && !connection?.ready ? (
        <div className="integration-empty">
          <strong>Настройте прямой доступ к Confluence</strong>
          <span>Администратору нужны адрес Confluence, данные учётной записи для чтения и список разрешённых пространств. Сервер RAG должен иметь доступ к Confluence по сети.</span>
          {Boolean(connection?.missing_configuration.length) && <small>Не заполнены: {connection?.missing_configuration.join(", ")}</small>}
        </div>
      ) : !connection?.enabled ? (
        <div className="integration-empty">
          <strong>Connector отключён</strong>
          <span>Администратор должен включить CONFLUENCE_CONNECTOR_ENABLED.</span>
        </div>
      ) : !activeSource && integration ? (
        <div className="connector-onboarding">
          <div className="connector-onboarding-copy">
            <strong>{integration.configured ? "Connector пока не в сети" : "Подключите Confluence Connector"}</strong>
            <span>Он запускается внутри корпоративной сети и сам подключается к RAG по исходящему HTTP(S).</span>
          </div>
          {integration.can_manage && (
            <>
              <label htmlFor="connector-spaces">Разрешённые пространства</label>
              <div className="connector-enrollment-row">
                <input
                  id="connector-spaces"
                  value={enrollmentSpaces}
                  onChange={(event) => setEnrollmentSpaces(event.target.value)}
                  placeholder="DOCS, TEAM"
                />
                <button
                  type="button"
                  className="primary-button"
                  disabled={busy === "enrollment" || !enrollmentSpaces.trim()}
                  onClick={() => void createEnrollment()}
                >
                  {busy === "enrollment" ? "Создаю…" : "Создать код"}
                </button>
              </div>
            </>
          )}
          {enrollment && (
            <div className="connector-secret">
              <strong>Одноразовый код подключения</strong>
              <code>{enrollment.enrollment_token}</code>
              <small>
                Действует до {new Date(enrollment.expires_at).toLocaleString("ru-RU")}.
                Укажите его как RAG_ENROLLMENT_TOKEN на машине connector.
              </small>
            </div>
          )}
          {integration.connectors.length > 0 && (
            <div className="connector-list">
              {integration.connectors.map((connector) => (
                <div key={connector.id}>
                  <span className={connector.online ? "online" : "offline"} />
                  <strong>{connector.name}</strong>
                  <small>{describeAllowedSpaces(connector.allowed_spaces)} · {connector.version || "не зарегистрирован"}</small>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : activeSource ? (
        <div className="connector-browser">
          <div className="connector-toolbar">
            <div>
              <strong>{activeSource.name}</strong>
              <small>
                {describeAllowedSpaces(activeSource.allowed_spaces)} · {direct ? "учётная запись сервера" : `активность ${activeSource.last_seen_at
                  ? new Date(activeSource.last_seen_at).toLocaleTimeString("ru-RU")
                  : "только что"}`}
              </small>
            </div>
            <button
              type="button"
              className="connection-check-button"
              disabled={Boolean(busy) || loadingKeys.size > 0 || Boolean(statusError)}
              onClick={() => void loadSpaces()}
            >
              {busy === "spaces" ? "Загружаю…" : spaces.length ? "Обновить" : "Показать пространства"}
            </button>
          </div>

          {spaces.length > 0 && (
            <>
              <label htmlFor="confluence-space-search">Найти пространство</label>
              <div className="connector-enrollment-row">
                <input
                  id="confluence-space-search"
                  type="search"
                  value={spaceQuery}
                  onChange={(event) => setSpaceQuery(event.target.value)}
                  placeholder="Название или ключ пространства"
                />
                {personalSpaceCount > 0 && (
                  <button
                    type="button"
                    className="connection-check-button"
                    aria-pressed={showPersonalSpaces}
                    onClick={() => setShowPersonalSpaces((value) => !value)}
                  >
                    {showPersonalSpaces ? "Скрыть" : "Показать"} личные ({personalSpaceCount})
                  </button>
                )}
              </div>
              <p className="integration-hint">
                Показано {visibleSpaces.length} из {spaces.length}. Личные пространства доступны
                через поиск или кнопку; права доступа и выбор страниц не меняются.
              </p>
            </>
          )}

          {spaces.length > 0 ? (
            <div className="confluence-tree">
              {visibleSpaces.length === 0 && <p className="integration-hint">Нет пространств по этому запросу.</p>}
              {visibleSpaces.map((space) => {
                const expanded = expandedSpaces.has(space.key);
                const key = parentKey(space.key, null);
                return (
                  <div className="confluence-space" key={space.key}>
                    <div className="confluence-space-heading">
                    <button type="button" className="confluence-space-row" disabled={Boolean(busy)} aria-expanded={expanded} onClick={() => toggleSpace(space)}>
                      <span>{loadingKeys.has(key) ? "…" : expanded ? "▾" : "▸"}</span>
                      <strong>{space.name}</strong>
                      <small>{space.key}</small>
                    </button>
                    <button
                      type="button"
                      className="connection-check-button"
                      disabled={Boolean(busy) || Boolean(statusError) || !activeSource.capabilities.includes("LIST_SPACE_PAGES")}
                      onClick={() => void importSpace(space)}
                      title={activeSource.capabilities.includes("LIST_SPACE_PAGES") ? "Все доступные страницы, включая вложенные" : "Перезапустите обновлённый коннектор на компьютере"}
                    >Импортировать весь space</button>
                    </div>
                    {expanded && (
                      <PageBranch
                        nodes={childrenByParent[key] || []}
                        childrenByParent={childrenByParent}
                        expandedPages={expandedPages}
                        loadingKeys={loadingKeys}
                        selected={selected}
                        disabled={Boolean(busy)}
                        onExpand={togglePage}
                        onSelect={selectPage}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="connector-browser-empty">
              <span>⌘</span>
              <strong>Пространства ещё не загружены</strong>
              <small>{direct ? "Нажмите «Показать пространства», чтобы проверить доступ и получить дерево из Confluence." : "Дерево запрашивается лениво через подключённый коннектор."}</small>
            </div>
          )}

          {busy === "space-scan" && (
            <div className="import-progress" role="status">
              <span>Собираю страницы space… Найдено: {spaceScanCount}</span>
              <small>После обхода появится подтверждение с общим числом страниц.</small>
            </div>
          )}

          <form className="manual-page-row" onSubmit={addManualPage}>
            <input
              value={manualSpace}
              onChange={(event) => setManualSpace(event.target.value)}
              aria-label="Ключ пространства"
              placeholder="Ключ пространства"
              disabled={Boolean(busy)}
            />
            <input
              value={manualPageId}
              onChange={(event) => setManualPageId(event.target.value)}
              aria-label="ID или ссылка страницы"
              placeholder="ID или ссылка Confluence"
              disabled={Boolean(busy)}
            />
            <button type="submit" disabled={Boolean(busy) || !manualPageId.trim() || !manualSpace.trim()}>Добавить</button>
          </form>

          <p className="integration-hint">Можно выбрать несколько страниц. Выбор родительской страницы не включает вложенные.</p>

          {selectedPages.length > 0 && (
            <div className="selected-pages" aria-label="Выбранные страницы">
              <div className="selected-pages-heading">
                <strong>К импорту: {selectedPages.length}</strong>
                <button type="button" className="text-button" disabled={Boolean(busy)} onClick={() => setSelected({})}>Снять выбор</button>
              </div>
              <ul>
                {selectedPages.map((page) => (
                  <li key={page.id}>
                    <span title={page.title}>{page.title}<small>{page.space_key} · ID {page.id}</small></span>
                    <button type="button" disabled={Boolean(busy)} aria-label={`Убрать страницу ${page.id} из выбора`} onClick={() => selectPage(page, false)}>×</button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="connector-selection">
            <div>
              <strong>Выбрано страниц: {selectedPages.length}</strong>
              <small>
                {selectedPages.length
                  ? `${selectedPages.slice(0, 2).map((page) => page.title).join(" · ")}${selectedPages.length > 2 ? ` · ещё ${selectedPages.length - 2}` : ""}`
                  : "Отметьте страницы в дереве или добавьте ID вручную"}
              </small>
            </div>
            <button
              type="button"
              className="primary-button"
              disabled={!selectedPages.length || Boolean(busy) || Boolean(statusError)}
              onClick={() => void importSelected()}
            >
              {busy === "import" ? "Импортирую…" : "Добавить в workspace"}
            </button>
          </div>

          {progress && (
            <div className="import-progress" role="status">
              <span>{busy === "import" ? "Передача страниц" : "Передано на проверку"}: {progress.completed} из {progress.total}</span>
              <progress aria-label="Прогресс передачи страниц" max={progress.total} value={progress.completed} />
              <small>Индексация выполняется отдельно. Готовность проверяйте в списке импортированных страниц.</small>
              {busy === "import" && <small>Не закрывайте этот раздел до завершения передачи всех пакетов.</small>}
            </div>
          )}

          {result && (
            <div className={`import-result ${result.failed ? "has-errors" : ""}`} role="status">
              <span><strong>{result.queued}</strong> в очереди</span>
              <span><strong>{result.skipped}</strong> без изменений</span>
              <span><strong>{result.failed}</strong> ошибок</span>
              {result.results.filter((item) => item.status === "failed").map((item) => (
                <p key={item.page_id}>Страница {item.page_id}: {item.detail || "Ошибка импорта"}</p>
              ))}
            </div>
          )}
        </div>
      ) : null}

      <div className="integration-notice">
        <span aria-hidden="true">i</span>
        <p>
          <strong>Область доступа</strong> {direct ? "Сервер импортирует страницы только из разрешённых пространств от имени своей учётной записи." : "Коннектор принимает только команды Confluence и ограничен указанными пространствами."} Права отдельных страниц пока не переносятся в RAG: импортированные материалы доступны участникам выбранного workspace.
        </p>
      </div>
    </section>
  );
}
