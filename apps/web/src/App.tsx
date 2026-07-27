import { ChangeEvent, DragEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

type KnowledgeBase = {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
};

type Workspace = {
  id: string;
  name: string;
  base_knowledge_base_id: string | null;
  user_id: string | null;
};

type DocumentRecord = {
  id: string;
  workspace_id: string | null;
  filename: string;
  object_key: string;
  mime_type: string | null;
  sha256: string;
  status: string;
  created_at: string;
};

const API = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
const ACCEPTED_FILES = ".txt,.md,.csv,.json,.html,.htm,.xml,.pdf,.docx";

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(payload?.detail || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    UPLOADED: "Загружен",
    QUEUED: "В очереди",
    PROCESSING: "Обрабатывается",
    READY: "Готов",
    FAILED: "Ошибка",
  };
  return labels[status] || status;
}

export function App() {
  const bootstrapped = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const [health, setHealth] = useState("Подключение…");
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState("");
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");

  const selectedWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.id === selectedWorkspaceId) ?? null,
    [selectedWorkspaceId, workspaces],
  );

  const loadDocuments = useCallback(async (workspaceId: string) => {
    if (!workspaceId) {
      setDocuments([]);
      return;
    }
    try {
      const response = await fetch(`${API}/api/v1/workspaces/${workspaceId}/documents`);
      setDocuments(await readJson<DocumentRecord[]>(response));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось получить документы");
    }
  }, []);

  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;

    const bootstrap = async () => {
      try {
        const healthResponse = await fetch(`${API}/api/v1/health`);
        if (!healthResponse.ok) throw new Error("API недоступен");
        setHealth("API готов");

        const [kbResponse, workspaceResponse] = await Promise.all([
          fetch(`${API}/api/v1/knowledge-bases`),
          fetch(`${API}/api/v1/workspaces`),
        ]);
        const kbItems = await readJson<KnowledgeBase[]>(kbResponse);
        let workspaceItems = await readJson<Workspace[]>(workspaceResponse);

        if (workspaceItems.length === 0) {
          const createResponse = await fetch(`${API}/api/v1/workspaces`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name: "Мои документы",
              base_knowledge_base_id: null,
              user_id: "local-user",
            }),
          });
          workspaceItems = [await readJson<Workspace>(createResponse)];
        }

        setKnowledgeBases(kbItems);
        setWorkspaces(workspaceItems);
        setSelectedWorkspaceId(workspaceItems[0].id);
      } catch (reason) {
        setHealth("API недоступен");
        setError(reason instanceof Error ? reason.message : "Ошибка запуска интерфейса");
      }
    };

    void bootstrap();
  }, []);

  useEffect(() => {
    if (!selectedWorkspaceId) return;
    void loadDocuments(selectedWorkspaceId);
    const timer = window.setInterval(() => void loadDocuments(selectedWorkspaceId), 2500);
    return () => window.clearInterval(timer);
  }, [loadDocuments, selectedWorkspaceId]);

  const createWorkspace = async () => {
    const name = window.prompt("Название рабочей области", "Новая рабочая область")?.trim();
    if (!name) return;

    setError("");
    try {
      const response = await fetch(`${API}/api/v1/workspaces`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, base_knowledge_base_id: null, user_id: "local-user" }),
      });
      const workspace = await readJson<Workspace>(response);
      setWorkspaces((current) => [workspace, ...current]);
      setSelectedWorkspaceId(workspace.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось создать workspace");
    }
  };

  const updateKnowledgeBase = async (event: ChangeEvent<HTMLSelectElement>) => {
    if (!selectedWorkspace) return;
    const knowledgeBaseId = event.target.value || null;
    setError("");

    try {
      const response = await fetch(`${API}/api/v1/workspaces/${selectedWorkspace.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_knowledge_base_id: knowledgeBaseId }),
      });
      const updated = await readJson<Workspace>(response);
      setWorkspaces((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось изменить базу знаний");
    }
  };

  const uploadFiles = async (files: FileList | File[]) => {
    if (!selectedWorkspaceId || files.length === 0) return;
    setUploading(true);
    setError("");

    try {
      for (const file of Array.from(files)) {
        const body = new FormData();
        body.append("file", file);
        const response = await fetch(`${API}/api/v1/workspaces/${selectedWorkspaceId}/documents`, {
          method: "POST",
          body,
        });
        await readJson<DocumentRecord>(response);
      }
      await loadDocuments(selectedWorkspaceId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось загрузить файл");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    void uploadFiles(event.dataTransfer.files);
  };

  const readyCount = documents.filter((document) => document.status === "READY").length;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">Скала<span>^р</span><small>RAG</small></div>
        <nav><button className="active">Чат</button><button>Граф</button></nav>
        <div className="status"><i className={health === "API готов" ? "online" : "offline"} />{health}</div>
      </header>

      <div className="workspace">
        <aside className="sidebar">
          <section>
            <div className="section-title">
              <h3>Рабочая область</h3>
              <button className="text-button" onClick={createWorkspace}>+ Новая</button>
            </div>
            <label>Workspace</label>
            <select value={selectedWorkspaceId} onChange={(event) => setSelectedWorkspaceId(event.target.value)}>
              {workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}
            </select>

            <label className="secondary-label">База знаний</label>
            <select
              value={selectedWorkspace?.base_knowledge_base_id ?? ""}
              onChange={updateKnowledgeBase}
              disabled={!selectedWorkspace}
            >
              <option value="">Только мои документы</option>
              {knowledgeBases.map((kb) => <option key={kb.id} value={kb.id}>{kb.name}</option>)}
            </select>
            <p className="hint">Файлы workspace будут искаться вместе с выбранной готовой базой.</p>
          </section>

          <section>
            <div className="section-title">
              <h3>Документы</h3>
              <span className="counter">{readyCount}/{documents.length}</span>
            </div>
            <input
              ref={fileInput}
              type="file"
              multiple
              accept={ACCEPTED_FILES}
              hidden
              onChange={(event) => event.target.files && void uploadFiles(event.target.files)}
            />
            <div
              className={`dropzone ${dragging ? "dragging" : ""} ${uploading ? "busy" : ""}`}
              onClick={() => !uploading && fileInput.current?.click()}
              onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={handleDrop}
              role="button"
              tabIndex={0}
            >
              <strong>{uploading ? "Загрузка…" : "Добавить документы"}</strong>
              <small>Перетащите или выберите PDF, DOCX, TXT, MD, CSV, JSON, HTML, XML</small>
            </div>

            <div className="document-list">
              {documents.length === 0 && <p className="empty-documents">Документов пока нет</p>}
              {documents.map((document) => (
                <div className="document-item" key={document.id} title={document.filename}>
                  <div className="document-icon">▤</div>
                  <div className="document-info">
                    <strong>{document.filename}</strong>
                    <span>{new Date(document.created_at).toLocaleString("ru-RU")}</span>
                  </div>
                  <span className={`badge status-${document.status.toLowerCase()}`}>
                    {statusLabel(document.status)}
                  </span>
                </div>
              ))}
            </div>
          </section>

          <section>
            <h3>Режим поиска</h3>
            <div className="mode active">Быстрый hybrid RAG</div>
            <div className="mode disabled">Расширенный поиск · скоро</div>
          </section>
        </aside>

        <main className="chat">
          <div className="chat-head">
            <div>
              <strong>{selectedWorkspace?.name || "Рабочая область"}</strong>
              <span>{selectedWorkspace?.base_knowledge_base_id ? "Готовая база + документы workspace" : "Только документы workspace"}</span>
            </div>
          </div>

          {error && <div className="error-banner">{error}</div>}

          <div className="empty-state">
            <div className="spark">✦</div>
            <h1>Документы подключены</h1>
            <p>Загруженные файлы автоматически проходят очередь, обработку и сохраняются как поисковые чанки. Следующий этап — embeddings и поиск по pgvector.</p>
            <div className="summary-card">
              <span><strong>{documents.length}</strong> документов</span>
              <span><strong>{readyCount}</strong> готовы к поиску</span>
            </div>
          </div>
          <div className="composer disabled-composer">
            <textarea disabled placeholder="Чат появится после подключения retrieval…" />
            <button disabled>➤</button>
          </div>
        </main>
      </div>
    </div>
  );
}
