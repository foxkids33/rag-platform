import { ChangeEvent, useEffect, useMemo, useRef, useState } from "react";

type KnowledgeBaseSummary = {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  active_version_id: string | null;
  active_version: number | null;
  active_version_status: string | null;
  active_document_count: number;
};

type KnowledgeBaseVersion = {
  id: string;
  knowledge_base_id: string;
  version: number;
  status: "DRAFT" | "INDEXING" | "READY" | "ACTIVE" | "ARCHIVED" | "FAILED";
  embedding_model: string;
  embedding_dimension: number;
  chunker_version: string;
  document_count: number;
  ready_document_count: number;
  searchable_document_count: number;
  failed_document_count: number;
  processing_document_count: number;
  created_at: string;
  activated_at: string | null;
};

type KnowledgeBaseDocument = {
  id: string;
  knowledge_base_version_id: string | null;
  filename: string;
  object_key: string;
  mime_type: string | null;
  sha256: string;
  status: string;
  search_enabled: boolean;
  created_at: string;
};

type Props = {
  apiBaseUrl: string;
  knowledgeBases: KnowledgeBaseSummary[];
  refreshKnowledgeBases: () => Promise<unknown>;
  refreshWorkspaces: () => Promise<unknown>;
  onError: (message: string) => void;
};

const ACCEPTED_FILES = ".txt,.md,.csv,.json,.html,.htm,.xml,.pdf,.docx";
const EDITABLE_STATUSES = new Set(["DRAFT", "INDEXING", "READY", "FAILED"]);

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(payload?.detail || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

async function readError(response: Response): Promise<string> {
  const payload = await response.json().catch(() => null) as { detail?: string } | null;
  return payload?.detail || `HTTP ${response.status}`;
}

function versionStatusLabel(status: KnowledgeBaseVersion["status"]): string {
  const labels: Record<KnowledgeBaseVersion["status"], string> = {
    DRAFT: "Черновик",
    INDEXING: "Индексация",
    READY: "Готова к публикации",
    ACTIVE: "Активна",
    ARCHIVED: "Архив",
    FAILED: "Ошибка",
  };
  return labels[status];
}

function documentStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    QUEUED: "В очереди",
    PROCESSING: "Обрабатывается",
    READY: "Готов",
    FAILED: "Ошибка",
  };
  return labels[status] || status;
}

export function KnowledgeBaseManager({
  apiBaseUrl,
  knowledgeBases,
  refreshKnowledgeBases,
  refreshWorkspaces,
  onError,
}: Props) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [selectedKnowledgeBaseId, setSelectedKnowledgeBaseId] = useState("");
  const [versions, setVersions] = useState<KnowledgeBaseVersion[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [documents, setDocuments] = useState<KnowledgeBaseDocument[]>([]);
  const [action, setAction] = useState<string | null>(null);

  const selectedKnowledgeBase = useMemo(
    () => knowledgeBases.find((item) => item.id === selectedKnowledgeBaseId) ?? null,
    [knowledgeBases, selectedKnowledgeBaseId],
  );
  const selectedVersion = useMemo(
    () => versions.find((item) => item.id === selectedVersionId) ?? null,
    [versions, selectedVersionId],
  );
  const versionEditable = Boolean(selectedVersion && EDITABLE_STATUSES.has(selectedVersion.status));
  const versionBusy = Boolean(selectedVersion?.processing_document_count);

  const loadVersions = async (knowledgeBaseId: string, preserveVersionId?: string) => {
    if (!knowledgeBaseId) {
      setVersions([]);
      setSelectedVersionId("");
      return [] as KnowledgeBaseVersion[];
    }
    const response = await fetch(`${apiBaseUrl}/api/v1/knowledge-bases/${knowledgeBaseId}/versions`);
    const items = await readJson<KnowledgeBaseVersion[]>(response);
    setVersions(items);
    const target = preserveVersionId && items.some((item) => item.id === preserveVersionId)
      ? preserveVersionId
      : items[0]?.id || "";
    setSelectedVersionId(target);
    return items;
  };

  const loadDocuments = async (knowledgeBaseId: string, versionId: string) => {
    if (!knowledgeBaseId || !versionId) {
      setDocuments([]);
      return;
    }
    const response = await fetch(
      `${apiBaseUrl}/api/v1/knowledge-bases/${knowledgeBaseId}/versions/${versionId}/documents`,
    );
    setDocuments(await readJson<KnowledgeBaseDocument[]>(response));
  };

  useEffect(() => {
    if (knowledgeBases.length === 0) {
      setSelectedKnowledgeBaseId("");
      setVersions([]);
      setSelectedVersionId("");
      setDocuments([]);
      return;
    }
    if (!knowledgeBases.some((item) => item.id === selectedKnowledgeBaseId)) {
      setSelectedKnowledgeBaseId(knowledgeBases[0].id);
    }
  }, [knowledgeBases, selectedKnowledgeBaseId]);

  useEffect(() => {
    if (!selectedKnowledgeBaseId) return;
    void loadVersions(selectedKnowledgeBaseId).catch((reason) => {
      onError(reason instanceof Error ? reason.message : "Не удалось загрузить версии базы");
    });
  }, [selectedKnowledgeBaseId]);

  useEffect(() => {
    if (!selectedKnowledgeBaseId || !selectedVersionId) {
      setDocuments([]);
      return;
    }
    const refresh = async () => {
      try {
        await loadDocuments(selectedKnowledgeBaseId, selectedVersionId);
        await loadVersions(selectedKnowledgeBaseId, selectedVersionId);
      } catch (reason) {
        onError(reason instanceof Error ? reason.message : "Не удалось обновить версию базы");
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [selectedKnowledgeBaseId, selectedVersionId]);

  const createKnowledgeBase = async () => {
    const name = window.prompt("Название новой базы знаний", "Новая база знаний")?.trim();
    if (!name || action) return;
    setAction("create-kb");
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/knowledge-bases`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const created = await readJson<KnowledgeBaseSummary>(response);
      await refreshKnowledgeBases();
      setSelectedKnowledgeBaseId(created.id);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось создать базу знаний");
    } finally {
      setAction(null);
    }
  };

  const renameKnowledgeBase = async () => {
    if (!selectedKnowledgeBase || action) return;
    const name = window.prompt("Новое название базы", selectedKnowledgeBase.name)?.trim();
    if (!name || name === selectedKnowledgeBase.name) return;
    setAction("rename-kb");
    try {
      const response = await fetch(
        `${apiBaseUrl}/api/v1/knowledge-bases/${selectedKnowledgeBase.id}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        },
      );
      await readJson<KnowledgeBaseSummary>(response);
      await Promise.all([refreshKnowledgeBases(), refreshWorkspaces()]);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось переименовать базу");
    } finally {
      setAction(null);
    }
  };

  const deleteKnowledgeBase = async () => {
    if (!selectedKnowledgeBase || action) return;
    if (!window.confirm(`Удалить базу знаний «${selectedKnowledgeBase.name}» и все её версии?`)) return;
    setAction("delete-kb");
    try {
      const response = await fetch(
        `${apiBaseUrl}/api/v1/knowledge-bases/${selectedKnowledgeBase.id}`,
        { method: "DELETE" },
      );
      if (!response.ok) throw new Error(await readError(response));
      setSelectedKnowledgeBaseId("");
      setVersions([]);
      setDocuments([]);
      await refreshKnowledgeBases();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось удалить базу знаний");
    } finally {
      setAction(null);
    }
  };

  const createVersion = async () => {
    if (!selectedKnowledgeBase || action) return;
    setAction("create-version");
    try {
      const response = await fetch(
        `${apiBaseUrl}/api/v1/knowledge-bases/${selectedKnowledgeBase.id}/versions`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        },
      );
      const created = await readJson<KnowledgeBaseVersion>(response);
      await loadVersions(selectedKnowledgeBase.id, created.id);
      setSelectedVersionId(created.id);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось создать версию");
    } finally {
      setAction(null);
    }
  };

  const deleteVersion = async () => {
    if (!selectedKnowledgeBase || !selectedVersion || action) return;
    if (!window.confirm(`Удалить версию v${selectedVersion.version}?`)) return;
    setAction("delete-version");
    try {
      const response = await fetch(
        `${apiBaseUrl}/api/v1/knowledge-bases/${selectedKnowledgeBase.id}/versions/${selectedVersion.id}`,
        { method: "DELETE" },
      );
      if (!response.ok) throw new Error(await readError(response));
      setSelectedVersionId("");
      setDocuments([]);
      await Promise.all([
        loadVersions(selectedKnowledgeBase.id),
        refreshKnowledgeBases(),
      ]);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось удалить версию");
    } finally {
      setAction(null);
    }
  };

  const publishVersion = async () => {
    if (!selectedKnowledgeBase || !selectedVersion || action) return;
    const verb = selectedVersion.status === "ARCHIVED" ? "Вернуть" : "Опубликовать";
    if (!window.confirm(`${verb} версию v${selectedVersion.version} как активную?`)) return;
    setAction("publish-version");
    try {
      const response = await fetch(
        `${apiBaseUrl}/api/v1/knowledge-bases/${selectedKnowledgeBase.id}/versions/${selectedVersion.id}/publish`,
        { method: "POST" },
      );
      await readJson<KnowledgeBaseSummary>(response);
      await Promise.all([
        refreshKnowledgeBases(),
        refreshWorkspaces(),
        loadVersions(selectedKnowledgeBase.id, selectedVersion.id),
      ]);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось опубликовать версию");
    } finally {
      setAction(null);
    }
  };

  const uploadFiles = async (files: FileList) => {
    if (!selectedKnowledgeBase || !selectedVersion || !versionEditable || action) return;
    setAction("upload");
    try {
      for (const file of Array.from(files)) {
        const body = new FormData();
        body.append("file", file);
        const response = await fetch(
          `${apiBaseUrl}/api/v1/knowledge-bases/${selectedKnowledgeBase.id}/versions/${selectedVersion.id}/documents`,
          { method: "POST", body },
        );
        await readJson<KnowledgeBaseDocument>(response);
      }
      await Promise.all([
        loadDocuments(selectedKnowledgeBase.id, selectedVersion.id),
        loadVersions(selectedKnowledgeBase.id, selectedVersion.id),
      ]);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось загрузить документы версии");
    } finally {
      setAction(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const toggleDocument = async (document: KnowledgeBaseDocument) => {
    if (!selectedKnowledgeBase || !selectedVersion || action) return;
    setAction(document.id);
    try {
      const response = await fetch(
        `${apiBaseUrl}/api/v1/knowledge-bases/${selectedKnowledgeBase.id}/versions/${selectedVersion.id}/documents/${document.id}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ search_enabled: !document.search_enabled }),
        },
      );
      const updated = await readJson<KnowledgeBaseDocument>(response);
      setDocuments((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось изменить документ");
    } finally {
      setAction(null);
    }
  };

  const reindexDocument = async (document: KnowledgeBaseDocument) => {
    if (!selectedKnowledgeBase || !selectedVersion || action) return;
    setAction(document.id);
    try {
      const response = await fetch(
        `${apiBaseUrl}/api/v1/knowledge-bases/${selectedKnowledgeBase.id}/versions/${selectedVersion.id}/documents/${document.id}/reindex`,
        { method: "POST" },
      );
      await readJson<KnowledgeBaseDocument>(response);
      await loadDocuments(selectedKnowledgeBase.id, selectedVersion.id);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось переиндексировать документ");
    } finally {
      setAction(null);
    }
  };

  const deleteDocument = async (document: KnowledgeBaseDocument) => {
    if (!selectedKnowledgeBase || !selectedVersion || action) return;
    if (!window.confirm(`Удалить «${document.filename}» из версии v${selectedVersion.version}?`)) return;
    setAction(document.id);
    try {
      const response = await fetch(
        `${apiBaseUrl}/api/v1/knowledge-bases/${selectedKnowledgeBase.id}/versions/${selectedVersion.id}/documents/${document.id}`,
        { method: "DELETE" },
      );
      if (!response.ok) throw new Error(await readError(response));
      await Promise.all([
        loadDocuments(selectedKnowledgeBase.id, selectedVersion.id),
        loadVersions(selectedKnowledgeBase.id, selectedVersion.id),
      ]);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Не удалось удалить документ версии");
    } finally {
      setAction(null);
    }
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    if (event.target.files) void uploadFiles(event.target.files);
  };

  return (
    <section className="kb-admin-section">
      <div className="section-title">
        <h3>Базы знаний</h3>
        <button className="text-button" onClick={() => void createKnowledgeBase()} disabled={Boolean(action)}>
          + Новая
        </button>
      </div>

      {knowledgeBases.length === 0 ? (
        <p className="empty-documents">Готовых баз пока нет</p>
      ) : (
        <>
          <label>Управление базой</label>
          <div className="workspace-selector-row">
            <select
              value={selectedKnowledgeBaseId}
              onChange={(event) => setSelectedKnowledgeBaseId(event.target.value)}
              disabled={Boolean(action)}
            >
              {knowledgeBases.map((knowledgeBase) => (
                <option value={knowledgeBase.id} key={knowledgeBase.id}>{knowledgeBase.name}</option>
              ))}
            </select>
            <button
              className="workspace-icon-button"
              type="button"
              onClick={() => void renameKnowledgeBase()}
              disabled={!selectedKnowledgeBase || Boolean(action)}
              title="Переименовать базу знаний"
            >
              ✎
            </button>
            <button
              className="workspace-icon-button danger"
              type="button"
              onClick={() => void deleteKnowledgeBase()}
              disabled={!selectedKnowledgeBase || Boolean(action)}
              title="Удалить базу знаний"
            >
              ×
            </button>
          </div>

          <div className="section-title kb-version-title">
            <label>Версия</label>
            <button
              className="text-button"
              type="button"
              onClick={() => void createVersion()}
              disabled={!selectedKnowledgeBase || Boolean(action)}
            >
              + Черновик
            </button>
          </div>

          {versions.length === 0 ? (
            <p className="empty-documents">Создайте первую версию</p>
          ) : (
            <>
              <div className="workspace-selector-row">
                <select
                  value={selectedVersionId}
                  onChange={(event) => setSelectedVersionId(event.target.value)}
                  disabled={Boolean(action)}
                >
                  {versions.map((version) => (
                    <option value={version.id} key={version.id}>
                      v{version.version} · {versionStatusLabel(version.status)}
                    </option>
                  ))}
                </select>
                <button
                  className="workspace-icon-button"
                  type="button"
                  onClick={() => fileInput.current?.click()}
                  disabled={!versionEditable || Boolean(action)}
                  title="Добавить документы в версию"
                >
                  +
                </button>
                <button
                  className="workspace-icon-button danger"
                  type="button"
                  onClick={() => void deleteVersion()}
                  disabled={!selectedVersion || selectedVersion.status === "ACTIVE" || versionBusy || Boolean(action)}
                  title="Удалить версию"
                >
                  ×
                </button>
              </div>
              <input
                ref={fileInput}
                type="file"
                multiple
                accept={ACCEPTED_FILES}
                hidden
                onChange={handleFileChange}
              />

              {selectedVersion && (
                <div className="kb-version-summary">
                  <span className={`badge kb-status-${selectedVersion.status.toLowerCase()}`}>
                    {versionStatusLabel(selectedVersion.status)}
                  </span>
                  <small>
                    {selectedVersion.searchable_document_count}/{selectedVersion.document_count} документов в поиске
                  </small>
                  <button
                    type="button"
                    onClick={() => void publishVersion()}
                    disabled={
                      !["READY", "ARCHIVED"].includes(selectedVersion.status)
                      || Boolean(action)
                    }
                  >
                    {selectedVersion.status === "ARCHIVED" ? "Сделать активной" : "Опубликовать"}
                  </button>
                </div>
              )}

              <div className="kb-document-list">
                {documents.length === 0 && <p className="empty-documents">В версии нет документов</p>}
                {documents.map((document) => {
                  const busy = document.status === "QUEUED" || document.status === "PROCESSING";
                  const pending = action === document.id;
                  return (
                    <div className={`kb-document-item ${document.search_enabled ? "" : "excluded"}`} key={document.id}>
                      <div className="kb-document-copy">
                        <strong title={document.filename}>{document.filename}</strong>
                        <span>{documentStatusLabel(document.status)}</span>
                      </div>
                      <div className="document-actions">
                        {document.status === "FAILED" && (
                          <button
                            className="document-action"
                            type="button"
                            onClick={() => void reindexDocument(document)}
                            disabled={!versionEditable || Boolean(action)}
                            title="Повторить индексацию"
                          >
                            {pending ? "…" : "↻"}
                          </button>
                        )}
                        <button
                          className="document-action"
                          type="button"
                          onClick={() => void toggleDocument(document)}
                          disabled={!versionEditable || document.status !== "READY" || Boolean(action)}
                          title={document.search_enabled ? "Исключить из версии" : "Вернуть в версию"}
                        >
                          {pending ? "…" : document.search_enabled ? "⊘" : "↻"}
                        </button>
                        <button
                          className="document-action danger"
                          type="button"
                          onClick={() => void deleteDocument(document)}
                          disabled={!versionEditable || busy || Boolean(action)}
                          title="Удалить документ версии"
                        >
                          {pending ? "…" : "×"}
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </>
      )}
    </section>
  );
}
