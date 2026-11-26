import {
  ChangeEvent,
  DragEvent,
  KeyboardEvent,
  ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { readSseEvents } from "./sse";
import { KnowledgeBaseManager } from "./KnowledgeBaseManager";
import { ConversationGraph } from "./ConversationGraph";
import { ConfluenceImportPanel } from "./ConfluenceImportPanel";
import { RetrievalInspector } from "./RetrievalInspector";
import { CodeIntegrity, CodeIntegrityPanel } from "./CodeIntegrityPanel";
import { splitFencedCode } from "./fenced-code";
import { CleanStart } from "./CleanStart";
import { apiFetch as fetch } from "./api";
import { useCanUseConfluence } from "./auth-context";
import "./evidence-ui.css";
import { DocumentSummaryPicker } from "./DocumentSummaryPicker";

type ChatMode = "auto" | "general" | "sources";

type WorkspaceSourceMode = "USER_DOCUMENTS" | "KNOWLEDGE_BASE" | "HYBRID";

type KnowledgeBase = {
  id: string;
  tenant_id: string;
  slug: string;
  name: string;
  description?: string | null;
  active_version_id: string | null;
  active_version: number | null;
  active_version_status: string | null;
  active_document_count: number;
};

type Workspace = {
  id: string;
  tenant_id: string;
  name: string;
  base_knowledge_base_id: string | null;
  base_knowledge_base_name: string | null;
  active_knowledge_base_version_id: string | null;
  active_knowledge_base_version: number | null;
  source_mode: WorkspaceSourceMode;
  user_id: string;
  document_count: number;
  searchable_document_count: number;
  conversation_count: number;
  created_at: string;
  updated_at: string;
};

type ConversationSummary = {
  id: string;
  workspace_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  message_count: number;
};

type StoredMessageMetadata = {
  finish_reason?: string | null;
  status?: "complete" | "streaming" | "stopped" | "error";
  model?: string;
  generation_temperature?: number;
  query_selection_weight?: number;
  retrieval_query?: string;
  retrieval_mode?: string;
  workspace_source_mode?: WorkspaceSourceMode;
  knowledge_base_id?: string | null;
  knowledge_base_version_id?: string | null;
  rerank_applied?: boolean;
  context_chars?: number;
  abstained?: boolean;
  abstention_reason?: AbstentionReason | null;
  evidence_status?: "strong" | "limited" | "insufficient" | "not_applicable" | "selected_documents";
  evidence_score?: number | null;
  candidate_count?: number;
  selected_source_count?: number;
  required_anchor_count?: number;
  matched_anchor_count?: number;
  citation_valid?: boolean | null;
  cited_source_indices?: number[];
  invalid_citations?: number[];
  code_integrity?: CodeIntegrity | null;
  sources?: AnswerSource[];
};

type StoredMessage = {
  id: string;
  session_id: string;
  parent_message_id: string | null;
  role: "user" | "assistant";
  content: string;
  metadata: StoredMessageMetadata;
  created_at: string;
};

type ConversationDetail = ConversationSummary & {
  messages: StoredMessage[];
};

type DocumentRecord = {
  id: string;
  workspace_id: string | null;
  filename: string;
  object_key: string;
  mime_type: string | null;
  sha256: string;
  status: string;
  search_enabled: boolean;
  title: string | null;
  source_url: string | null;
  source_type: "UPLOAD" | "CONFLUENCE";
  external_id: string | null;
  external_version: string | null;
  synced_at: string | null;
  created_at: string;
};

type AnswerSource = {
  index: number;
  citation: string;
  chunk_id: string;
  document_id: string;
  filename: string;
  chunk_index: number;
  heading: string | null;
  page_start: number | null;
  page_end: number | null;
  excerpt: string;
  retrieval_rank: number;
  rerank_score: number | null;
  rerank_fusion_score: number | null;
  quality_score: number;
  query_relevance_score: number;
  selection_score: number;
  source_scope: "workspace" | "knowledge_base";
  knowledge_base_name: string | null;
  knowledge_base_version: number | null;
  source_url?: string | null;
  source_page_id?: string | null;
  source_version?: string | null;
};

type StreamMetadata = {
  finish_reason?: string | null;
  question: string;
  retrieval_query: string;
  conversation_id: string | null;
  user_message_id: string | null;
  parent_message_id: string | null;
  model: string;
  generation_temperature: number;
  query_selection_weight: number;
  retrieval_mode: string;
  workspace_source_mode: WorkspaceSourceMode;
  knowledge_base_id: string | null;
  knowledge_base_version_id: string | null;
  rerank_applied: boolean;
  context_chars: number;
  abstained: boolean;
  abstention_reason: AbstentionReason | null;
  evidence_status: "strong" | "limited" | "insufficient" | "not_applicable" | "selected_documents";
  evidence_score: number | null;
  candidate_count: number;
  selected_source_count: number;
  required_anchor_count: number;
  matched_anchor_count: number;
  citation_valid: boolean | null;
  cited_source_indices: number[];
  invalid_citations: number[];
  code_integrity?: CodeIntegrity | null;
  sources: AnswerSource[];
};

type StreamDone = {
  finish_reason?: string | null;
  assistant_message_id?: string | null;
  citation_valid?: boolean;
  cited_source_indices?: number[];
  invalid_citations?: number[];
  abstained?: boolean;
  abstention_reason?: AbstentionReason | null;
  evidence_status?: "strong" | "limited" | "insufficient" | "not_applicable" | "selected_documents";
  evidence_score?: number | null;
  query_selection_weight?: number;
  candidate_count?: number;
  selected_source_count?: number;
  required_anchor_count?: number;
  matched_anchor_count?: number;
  code_integrity?: CodeIntegrity | null;
};

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: "complete" | "streaming" | "stopped" | "error";
  parentMessageId?: string | null;
  sources?: AnswerSource[];
  metadata?: StreamMetadata;
};

type StreamStage = "idle" | "retrieving" | "generating";
type ViewMode = "chat" | "documents" | "confluence" | "knowledge" | "graph" | "retrieval";
type AbstentionReason =
  | "insufficient_retrieval_evidence"
  | "model_reported_insufficient_context"
  | "citation_validation_failed"
  | "code_integrity_validation_failed";

const API = import.meta.env.VITE_API_BASE_URL || "";
const CLEAN_INSTALL = import.meta.env.VITE_CLEAN_INSTALL === "true";
const ACCEPTED_FILES = ".txt,.md,.csv,.json,.html,.htm,.xml,.pdf,.docx,.png,.jpg,.jpeg,.webp,.tif,.tiff";
const SUGGESTED_QUESTIONS = [
  "Кратко опиши назначение продукта",
  "Какие ключевые компоненты входят в решение?",
  "Как обеспечивается отказоустойчивость?",
];

const COLLAPSED_CONVERSATION_COUNT = 6;
const VIEW_MODES = new Set<ViewMode>(["chat", "documents", "confluence", "knowledge", "graph", "retrieval"]);

type DemoDeepLink = { workspaceId: string; conversationId: string | null };

export function readDemoDeepLink(search: string): DemoDeepLink | null {
  const params = new URLSearchParams(search);
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const workspace = params.get("workspace") || "";
  if (params.getAll("workspace").length !== 1 || !uuid.test(workspace)) return null;
  const conversation = params.get("conversation") || "";
  return {
    workspaceId: workspace.toLowerCase(),
    conversationId: params.getAll("conversation").length === 1 && uuid.test(conversation)
      ? conversation.toLowerCase()
      : null,
  };
}

export function selectInitialConversation(
  items: Pick<ConversationSummary, "id" | "workspace_id">[],
  workspaceId: string,
  link: DemoDeepLink | null,
): string {
  const scoped = items.filter((item) => item.workspace_id === workspaceId);
  const requested = link?.workspaceId === workspaceId
    ? scoped.find((item) => item.id === link.conversationId)
    : undefined;
  return requested?.id || scoped[0]?.id || "";
}

function initialViewMode(): ViewMode {
  const candidate = window.location.hash.replace(/^#/, "") as ViewMode;
  return VIEW_MODES.has(candidate) ? candidate : CLEAN_INSTALL ? "confluence" : "chat";
}

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


function workspaceModeLabel(mode: WorkspaceSourceMode): string {
  const labels: Record<WorkspaceSourceMode, string> = {
    USER_DOCUMENTS: "Только документы workspace",
    KNOWLEDGE_BASE: "Только готовая база знаний",
    HYBRID: "База знаний + документы",
  };
  return labels[mode];
}

function sourceProvenance(source: AnswerSource): string {
  if (source.source_scope === "knowledge_base") {
    const name = source.knowledge_base_name || "База знаний";
    return source.knowledge_base_version == null
      ? name
      : `${name} · версия ${source.knowledge_base_version}`;
  }
  return "Документ workspace";
}

function pageLabel(source: AnswerSource): string | null {
  if (source.page_start == null) return null;
  if (source.page_end == null || source.page_end === source.page_start) {
    return `стр. ${source.page_start}`;
  }
  return `стр. ${source.page_start}–${source.page_end}`;
}

type WorkspaceDocumentListProps = {
  documents: DocumentRecord[];
  emptyText: string;
  actionId: string | null;
  onToggle: (document: DocumentRecord) => void;
  onDelete: (document: DocumentRecord) => void;
};

function WorkspaceDocumentList({
  documents,
  emptyText,
  actionId,
  onToggle,
  onDelete,
}: WorkspaceDocumentListProps) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [limit, setLimit] = useState(50);
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("ru");
    return documents.filter((document) => {
      const matchesText = `${document.title || ""} ${document.filename} ${document.external_id || ""}`
        .toLocaleLowerCase("ru").includes(needle);
      const matchesStatus = filter === "all"
        || (filter === "ready" && document.status === "READY" && document.search_enabled)
        || (filter === "processing" && ["QUEUED", "PROCESSING"].includes(document.status))
        || (filter === "failed" && document.status === "FAILED")
        || (filter === "excluded" && document.status === "READY" && !document.search_enabled);
      return matchesText && matchesStatus;
    });
  }, [documents, query, filter]);

  if (documents.length === 0) {
    return (
      <div className="resource-empty">
        <span aria-hidden="true">◇</span>
        <strong>{emptyText}</strong>
        <small>Новые источники появятся здесь после отправки в обработку.</small>
      </div>
    );
  }

  return (
    <>
    <div className="document-filters">
      <input
        type="search"
        aria-label="Поиск документов"
        placeholder="Название, файл или ID страницы…"
        value={query}
        onChange={(event) => { setQuery(event.target.value); setLimit(50); }}
      />
      <select aria-label="Статус документов" value={filter} onChange={(event) => { setFilter(event.target.value); setLimit(50); }}>
        <option value="all">Все статусы</option>
        <option value="ready">В поиске</option>
        <option value="processing">В обработке</option>
        <option value="failed">С ошибкой</option>
        <option value="excluded">Исключены</option>
      </select>
    </div>
    <div className="document-filter-summary" role="status">Найдено: {filtered.length} из {documents.length}</div>
    <div className="resource-document-list">
      {filtered.slice(0, limit).map((document) => {
        const busy = document.status === "QUEUED" || document.status === "PROCESSING";
        const actionPending = actionId === document.id;
        const excluded = document.status === "READY" && !document.search_enabled;
        const title = document.title || document.filename;

        return (
          <article className={`resource-document ${excluded ? "excluded" : ""}`} key={document.id}>
            <div className={`resource-document-icon ${document.source_type.toLowerCase()}`} aria-hidden="true">
              {document.source_type === "CONFLUENCE" ? "C" : "▤"}
            </div>
            <div className="resource-document-copy">
              <strong title={title}>
                {document.source_url && /^https?:\/\//i.test(document.source_url) ? (
                  <a className="document-source-link" href={document.source_url} target="_blank" rel="noreferrer">
                    {title}
                  </a>
                ) : title}
              </strong>
              <span>
                {document.source_type === "CONFLUENCE" ? "Confluence" : document.filename}
                {document.external_version ? ` · версия ${document.external_version}` : ""}
                {` · ${new Date(document.synced_at || document.created_at).toLocaleString("ru-RU")}`}
              </span>
            </div>
            <span className={`badge ${excluded ? "status-excluded" : `status-${document.status.toLowerCase()}`}`}>
              {excluded ? "Не в поиске" : statusLabel(document.status)}
            </span>
            <div className="resource-document-actions">
              <button
                className="document-action"
                type="button"
                disabled={document.status !== "READY" || Boolean(actionId)}
                onClick={() => onToggle(document)}
                title={document.search_enabled ? "Исключить из поиска" : "Вернуть в поиск"}
                aria-label={document.search_enabled ? "Исключить документ из поиска" : "Вернуть документ в поиск"}
              >
                {actionPending ? "…" : document.search_enabled ? "⊘" : "↻"}
              </button>
              <button
                className="document-action danger"
                type="button"
                disabled={busy || Boolean(actionId)}
                onClick={() => onDelete(document)}
                title={busy ? "Дождитесь завершения обработки" : "Удалить документ навсегда"}
                aria-label="Удалить документ навсегда"
              >
                {actionPending ? "…" : "×"}
              </button>
            </div>
          </article>
        );
      })}
      {filtered.length === 0 && (
        <div className="resource-empty">
          <strong>Ничего не найдено</strong>
          <small>Попробуйте другое название или статус.</small>
          <button type="button" className="text-button" onClick={() => { setQuery(""); setFilter("all"); setLimit(50); }}>Сбросить фильтры</button>
        </div>
      )}
    </div>
    {filtered.length > limit && (
      <button type="button" className="chat-history-more" onClick={() => setLimit((current) => current + 50)}>
        Показать ещё {Math.min(50, filtered.length - limit)} из {filtered.length - limit}
      </button>
    )}
    </>
  );
}

function sourceTitle(source: AnswerSource): string {
  return source.heading || source.filename;
}

function citedSources(message: ChatMessage): AnswerSource[] {
  const citedIndices = new Set(message.metadata?.cited_source_indices ?? []);
  return (message.sources ?? []).filter((source) => citedIndices.has(source.index));
}

function renderCitations(message: ChatMessage, content: string, prefix: string): ReactNode[] {
  return content.split(/(\[\d+\])/g).map((part, index) => {
    const match = part.match(/^\[(\d+)\]$/);
    if (!match) return <span key={`${message.id}-${prefix}-text-${index}`}>{part}</span>;

    const sourceIndex = Number(match[1]);
    const exists = message.sources?.some((source) => source.index === sourceIndex);
    if (!exists) return <span key={`${message.id}-${prefix}-citation-${index}`}>{part}</span>;

    return (
      <a
        key={`${message.id}-${prefix}-citation-${index}`}
        className="inline-citation"
        href={`#${message.id}-source-${sourceIndex}`}
        aria-label={`Перейти к источнику ${sourceIndex}`}
      >
        {part}
      </a>
    );
  });
}

function renderAnswerText(message: ChatMessage): ReactNode[] {
  const nodes: ReactNode[] = [];
  // Render fenced code as literal text: citations inside commands are never turned into links.
  splitFencedCode(message.content).forEach((segment, index) => {
    if (segment.type === "text") {
      nodes.push(...renderCitations(message, segment.content, `text-${index}`));
      return;
    }
    const audit = message.metadata?.code_integrity?.blocks.find((block) => block.index === segment.index);
    nodes.push(
      <div className="answer-code" key={`${message.id}-code-${segment.index}`}>
        <div className="answer-code-heading">
          <span>{segment.language || "Код"} <small>· блок {segment.index}</small></span>
          {audit && <span className={`answer-code-audit ${audit.status}`}>{audit.status === "exact_match" ? "✓ Совпадает с источником" : "Дословность не подтверждена"}</span>}
        </div>
        <pre><code>{segment.content}</code></pre>
      </div>,
    );
  });
  return nodes;
}

function newMessageId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function storedMessageToChat(message: StoredMessage): ChatMessage {
  const storedStatus = message.metadata?.status;
  const status = storedStatus === "stopped" || storedStatus === "error"
    ? storedStatus
    : "complete";
  const sources = message.role === "assistant" ? message.metadata?.sources ?? [] : undefined;
  const metadata = message.role === "assistant" && message.metadata?.model
    ? {
        question: "",
        retrieval_query: message.metadata.retrieval_query || "",
        conversation_id: message.session_id,
        user_message_id: null,
        parent_message_id: message.parent_message_id,
        model: message.metadata.model,
        finish_reason: message.metadata.finish_reason,
        generation_temperature: message.metadata.generation_temperature ?? 0,
        query_selection_weight: message.metadata.query_selection_weight ?? 0.3,
        retrieval_mode: message.metadata.retrieval_mode || "hybrid",
        workspace_source_mode: message.metadata.workspace_source_mode || "USER_DOCUMENTS",
        knowledge_base_id: message.metadata.knowledge_base_id ?? null,
        knowledge_base_version_id: message.metadata.knowledge_base_version_id ?? null,
        rerank_applied: Boolean(message.metadata.rerank_applied),
        context_chars: message.metadata.context_chars || 0,
        abstained: Boolean(message.metadata.abstained),
        abstention_reason: message.metadata.abstention_reason ?? null,
        evidence_status: message.metadata.evidence_status || "limited",
        evidence_score: message.metadata.evidence_score ?? null,
        candidate_count: message.metadata.candidate_count || 0,
        selected_source_count: message.metadata.selected_source_count || (sources?.length ?? 0),
        required_anchor_count: message.metadata.required_anchor_count || 0,
        matched_anchor_count: message.metadata.matched_anchor_count || 0,
        citation_valid: message.metadata.citation_valid ?? null,
        cited_source_indices: message.metadata.cited_source_indices || [],
        invalid_citations: message.metadata.invalid_citations || [],
        code_integrity: message.metadata.code_integrity,
        sources: sources || [],
      } satisfies StreamMetadata
    : undefined;

  return {
    id: message.id,
    role: message.role,
    content: message.content,
    status,
    parentMessageId: message.parent_message_id,
    sources,
    metadata,
  };
}


export function App() {
  const canUseConfluence = useCanUseConfluence();
  const bootstrapped = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const workspaceIdRef = useRef("");
  const conversationLoadId = useRef(0);
  const pendingDemoLink = useRef(CLEAN_INSTALL ? null : readDemoDeepLink(window.location.search));

  const [health, setHealth] = useState("Подключение…");
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState("");
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [showAllConversations, setShowAllConversations] = useState(false);
  const [selectedConversationId, setSelectedConversationId] = useState("");
  const [conversationLoading, setConversationLoading] = useState(false);
  const [conversationActionId, setConversationActionId] = useState<string | null>(null);
  const [workspaceActionId, setWorkspaceActionId] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [documentActionId, setDocumentActionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [viewMode, setViewMode] = useState<ViewMode>(() => {
    const initial = initialViewMode();
    return initial === "confluence" && !canUseConfluence ? "documents" : initial;
  });
  const [branchParentId, setBranchParentId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [chatMode, setChatMode] = useState<ChatMode>("auto");
  const [answerMaxTokens, setAnswerMaxTokens] = useState(2048);
  const [streamStage, setStreamStage] = useState<StreamStage>("idle");
  const [error, setError] = useState("");
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  useEffect(() => {
    workspaceIdRef.current = selectedWorkspaceId;
  }, [selectedWorkspaceId]);

  const selectViewMode = (mode: ViewMode) => {
    if (mode === "confluence" && !canUseConfluence) mode = "documents";
    setViewMode(mode);
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${mode}`);
  };

  const selectedWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.id === selectedWorkspaceId) ?? null,
    [selectedWorkspaceId, workspaces],
  );
  const selectedConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === selectedConversationId) ?? null,
    [conversations, selectedConversationId],
  );
  const selectedKnowledgeBase = useMemo(
    () => knowledgeBases.find((item) => item.id === selectedWorkspace?.base_knowledge_base_id) ?? null,
    [knowledgeBases, selectedWorkspace?.base_knowledge_base_id],
  );

  const activeBranchMessages = useMemo(() => {
    if (!branchParentId) return messages;
    const byId = new Map(messages.map((message) => [message.id, message]));
    const branchIds = new Set<string>();
    let currentId: string | null | undefined = branchParentId;
    while (currentId && !branchIds.has(currentId)) {
      branchIds.add(currentId);
      currentId = byId.get(currentId)?.parentMessageId;
    }
    return messages.filter((message) => branchIds.has(message.id));
  }, [branchParentId, messages]);

  const searchableCount = documents.filter((document) => (
    document.status === "READY" && document.search_enabled
  )).length;
  const uploadedDocuments = documents.filter((document) => document.source_type === "UPLOAD");
  const confluenceDocuments = documents.filter((document) => document.source_type === "CONFLUENCE");
  const confluenceSearchableCount = confluenceDocuments.filter((document) => (
    document.status === "READY" && document.search_enabled
  )).length;
  const hasActiveKnowledgeBase = Boolean(
    selectedKnowledgeBase?.active_version_id
    && selectedKnowledgeBase.active_document_count > 0
  );
  const hasSearchableContent = selectedWorkspace?.source_mode === "USER_DOCUMENTS"
    ? searchableCount > 0
    : selectedWorkspace?.source_mode === "KNOWLEDGE_BASE"
      ? hasActiveKnowledgeBase
      : searchableCount > 0 || hasActiveKnowledgeBase;
  const summaryDocuments = selectedWorkspace?.source_mode === "KNOWLEDGE_BASE" ? [] : documents.filter(
    (doc) => doc.workspace_id === selectedWorkspaceId && doc.status === "READY" && doc.search_enabled,
  );
  const isStreaming = streamStage !== "idle";
  const visibleConversations = showAllConversations
    ? conversations
    : conversations.slice(0, COLLAPSED_CONVERSATION_COUNT);
  const canSubmit = Boolean(
    selectedWorkspaceId
    && !conversationActionId
    && question.trim().length >= 2
    && !isStreaming
    && !conversationLoading
  );

  const loadConversation = useCallback(async (
    workspaceId: string,
    conversationId: string,
    focusAssistantId?: string | null,
  ) => {
    const loadId = ++conversationLoadId.current;
    if (!workspaceId || !conversationId) {
      setMessages([]);
      setBranchParentId(null);
      return;
    }
    setConversationLoading(true);
    try {
      const response = await fetch(
        `${API}/api/v1/workspaces/${workspaceId}/conversations/${conversationId}`,
      );
      const detail = await readJson<ConversationDetail>(response);
      if (loadId !== conversationLoadId.current || workspaceIdRef.current !== workspaceId) return;
      if (detail.id !== conversationId || detail.workspace_id !== workspaceId) {
        throw new Error("Диалог не относится к выбранной рабочей области");
      }
      const mapped = detail.messages.map(storedMessageToChat);
      setMessages(mapped);
      const focused = focusAssistantId
        ? mapped.find((message) => message.id === focusAssistantId && message.role === "assistant")
        : null;
      const latestAssistant = [...mapped].reverse().find((message) => message.role === "assistant");
      setBranchParentId(focused?.id || latestAssistant?.id || null);
    } catch (reason) {
      if (loadId !== conversationLoadId.current || workspaceIdRef.current !== workspaceId) return;
      setError(reason instanceof Error ? reason.message : "Не удалось загрузить диалог");
    } finally {
      if (loadId === conversationLoadId.current) setConversationLoading(false);
    }
  }, []);

  const refreshConversations = useCallback(async (workspaceId: string) => {
    if (!workspaceId) {
      setConversations([]);
      return [] as ConversationSummary[];
    }
    const response = await fetch(`${API}/api/v1/workspaces/${workspaceId}/conversations`);
    const items = (await readJson<ConversationSummary[]>(response))
      .filter((item) => item.workspace_id === workspaceId);
    if (workspaceIdRef.current === workspaceId) setConversations(items);
    return items;
  }, []);

  const loadDocuments = useCallback(async (workspaceId: string) => {
    if (!workspaceId) {
      setDocuments([]);
      return;
    }
    try {
      const response = await fetch(`${API}/api/v1/workspaces/${workspaceId}/documents`);
      const items = await readJson<DocumentRecord[]>(response);
      if (workspaceIdRef.current === workspaceId) setDocuments(items);
    } catch (reason) {
      if (workspaceIdRef.current !== workspaceId) return;
      setError(reason instanceof Error ? reason.message : "Не удалось получить документы");
    }
  }, []);

  const refreshKnowledgeBases = useCallback(async () => {
    const response = await fetch(`${API}/api/v1/knowledge-bases`);
    const items = await readJson<KnowledgeBase[]>(response);
    setKnowledgeBases(items);
    return items;
  }, []);

  const refreshWorkspaces = useCallback(async () => {
    const response = await fetch(`${API}/api/v1/workspaces`);
    const items = await readJson<Workspace[]>(response);
    setWorkspaces(items);
    return items;
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

        if (workspaceItems.length === 0 && !CLEAN_INSTALL) {
          const createResponse = await fetch(`${API}/api/v1/workspaces`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name: "Мои документы",
              base_knowledge_base_id: null,
              source_mode: "USER_DOCUMENTS",
            }),
          });
          workspaceItems = [await readJson<Workspace>(createResponse)];
        }

        setKnowledgeBases(kbItems);
        setWorkspaces(workspaceItems);
        const linkedWorkspace = workspaceItems.find(
          (item) => item.id === pendingDemoLink.current?.workspaceId,
        );
        if (!linkedWorkspace) pendingDemoLink.current = null;
        const initialWorkspace = linkedWorkspace || workspaceItems[0];
        workspaceIdRef.current = initialWorkspace?.id || "";
        setSelectedWorkspaceId(initialWorkspace?.id || "");
      } catch (reason) {
        setHealth("API недоступен");
        setError(reason instanceof Error ? reason.message : "Ошибка запуска интерфейса");
      } finally {
        setWorkspaceLoading(false);
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

  useEffect(() => {
    if (!selectedWorkspaceId) return;
    let cancelled = false;

    const loadHistory = async () => {
      try {
        const items = await refreshConversations(selectedWorkspaceId);
        if (cancelled) return;
        const targetId = selectInitialConversation(items, selectedWorkspaceId, pendingDemoLink.current);
        if (pendingDemoLink.current?.workspaceId === selectedWorkspaceId) {
          pendingDemoLink.current = null;
        }
        setSelectedConversationId(targetId);
        if (targetId) {
          await loadConversation(selectedWorkspaceId, targetId);
        } else {
          setMessages([]);
          setBranchParentId(null);
        }
      } catch (reason) {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : "Не удалось загрузить историю");
        }
      }
    };

    void loadHistory();
    return () => { cancelled = true; };
  }, [loadConversation, refreshConversations, selectedWorkspaceId]);

  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: isStreaming ? "auto" : "smooth", block: "end" });
  }, [isStreaming, messages]);

  useEffect(() => () => activeRequest.current?.abort(), []);

  const switchWorkspace = (workspaceId: string) => {
    // A demonstration link is a one-time initial selection, not a navigation override.
    pendingDemoLink.current = null;
    setMobileSidebarOpen(false);
    workspaceIdRef.current = workspaceId;
    conversationLoadId.current++;
    setConversationLoading(false);
    setDocuments([]);
    activeRequest.current?.abort();
    activeRequest.current = null;
    setStreamStage("idle");
    setMessages([]);
    setBranchParentId(null);
    setConversations([]);
    setShowAllConversations(false);
    setSelectedConversationId("");
    setQuestion("");
    setError("");
    setChatMode("auto");
    setSelectedWorkspaceId(workspaceId);
  };

  const createConversation = async (): Promise<ConversationSummary | null> => {
    if (!selectedWorkspaceId || conversationActionId) return null;
    setConversationActionId("new");
    setError("");
    try {
      const response = await fetch(
        `${API}/api/v1/workspaces/${selectedWorkspaceId}/conversations`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: null }),
        },
      );
      const conversation = await readJson<ConversationSummary>(response);
      if (workspaceIdRef.current !== selectedWorkspaceId) return null;
      conversationLoadId.current++;
      setConversationLoading(false);
      setConversations((current) => [conversation, ...current]);
      setSelectedConversationId(conversation.id);
      setMessages([]);
      setBranchParentId(null);
      selectViewMode("chat");
      setMobileSidebarOpen(false);
      return conversation;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось создать диалог");
      return null;
    } finally {
      setConversationActionId(null);
    }
  };

  const switchConversation = async (conversationId: string) => {
    if (!selectedWorkspaceId || isStreaming) return;
    selectViewMode("chat");
    setMobileSidebarOpen(false);
    if (conversationId === selectedConversationId) return;
    activeRequest.current?.abort();
    activeRequest.current = null;
    setStreamStage("idle");
    setSelectedConversationId(conversationId);
    setMessages([]);
    setBranchParentId(null);
    setError("");
    await loadConversation(selectedWorkspaceId, conversationId);
  };

  const deleteConversation = async (conversation: ConversationSummary) => {
    if (!selectedWorkspaceId || conversationActionId || isStreaming) return;
    if (!window.confirm(`Удалить диалог «${conversation.title || "Новый диалог"}»?`)) return;
    setConversationActionId(conversation.id);
    setError("");
    try {
      const response = await fetch(
        `${API}/api/v1/workspaces/${selectedWorkspaceId}/conversations/${conversation.id}`,
        { method: "DELETE" },
      );
      if (!response.ok) throw new Error(await readError(response));
      const remaining = conversations.filter((item) => item.id !== conversation.id);
      setConversations(remaining);
      if (selectedConversationId === conversation.id) {
        const nextId = remaining[0]?.id || "";
        setSelectedConversationId(nextId);
        if (nextId) await loadConversation(selectedWorkspaceId, nextId);
        else {
          setMessages([]);
          setBranchParentId(null);
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось удалить диалог");
    } finally {
      setConversationActionId(null);
    }
  };

  const createWorkspaceRecord = async (name: string): Promise<Workspace> => {
    const response = await fetch(`${API}/api/v1/workspaces`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        base_knowledge_base_id: null,
        source_mode: "USER_DOCUMENTS",
      }),
    });
    return readJson<Workspace>(response);
  };

  const createNamedWorkspace = async (name: string) => {
    if (!name || workspaceActionId) return;

    setWorkspaceActionId("new");
    setError("");
    try {
      const workspace = await createWorkspaceRecord(name);
      setWorkspaces((current) => [workspace, ...current]);
      switchWorkspace(workspace.id);
      if (CLEAN_INSTALL && canUseConfluence) selectViewMode("confluence");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось создать workspace");
    } finally {
      setWorkspaceActionId(null);
    }
  };

  const createWorkspace = async () => {
    const name = window.prompt("Название рабочей области", "Новая рабочая область")?.trim();
    if (name) await createNamedWorkspace(name);
  };

  const patchWorkspace = async (changes: Record<string, unknown>): Promise<Workspace | null> => {
    if (!selectedWorkspace || workspaceActionId) return null;
    setWorkspaceActionId(selectedWorkspace.id);
    setError("");
    try {
      const response = await fetch(`${API}/api/v1/workspaces/${selectedWorkspace.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      });
      const updated = await readJson<Workspace>(response);
      setWorkspaces((current) => current.map((item) => item.id === updated.id ? updated : item));
      return updated;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось изменить workspace");
      return null;
    } finally {
      setWorkspaceActionId(null);
    }
  };

  const renameWorkspace = async () => {
    if (!selectedWorkspace) return;
    const name = window.prompt("Новое название workspace", selectedWorkspace.name)?.trim();
    if (!name || name === selectedWorkspace.name) return;
    await patchWorkspace({ name });
  };

  const deleteWorkspace = async () => {
    if (!selectedWorkspace || workspaceActionId || isStreaming) return;
    const confirmed = window.confirm(
      `Удалить workspace «${selectedWorkspace.name}»? Документы, embeddings и диалоги будут удалены.`,
    );
    if (!confirmed) return;

    const deletingId = selectedWorkspace.id;
    setWorkspaceActionId(deletingId);
    setError("");
    try {
      const response = await fetch(`${API}/api/v1/workspaces/${deletingId}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await readError(response));
      let remaining = workspaces.filter((item) => item.id !== deletingId);
      if (remaining.length === 0 && !CLEAN_INSTALL) {
        const fallback = await createWorkspaceRecord("Мои документы");
        remaining = [fallback];
      }
      setWorkspaces(remaining);
      switchWorkspace(remaining[0]?.id || "");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось удалить workspace");
    } finally {
      setWorkspaceActionId(null);
    }
  };

  const updateSourceMode = async (event: ChangeEvent<HTMLSelectElement>) => {
    const sourceMode = event.target.value as WorkspaceSourceMode;
    if (!selectedWorkspace) return;
    if (sourceMode !== "USER_DOCUMENTS" && !selectedWorkspace.base_knowledge_base_id) {
      setError("Сначала выберите готовую базу знаний");
      return;
    }
    await patchWorkspace({ source_mode: sourceMode });
  };

  const updateKnowledgeBase = async (event: ChangeEvent<HTMLSelectElement>) => {
    if (!selectedWorkspace) return;
    const knowledgeBaseId = event.target.value || null;
    const changes: Record<string, unknown> = {
      base_knowledge_base_id: knowledgeBaseId,
      source_mode: selectedWorkspace.source_mode,
    };
    if (!knowledgeBaseId && selectedWorkspace.source_mode !== "USER_DOCUMENTS") {
      changes.source_mode = "USER_DOCUMENTS";
    }
    await patchWorkspace(changes);
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
      await refreshWorkspaces();
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

  const toggleDocumentSearch = async (document: DocumentRecord) => {
    if (!selectedWorkspaceId || documentActionId) return;
    const nextEnabled = !document.search_enabled;
    setDocumentActionId(document.id);
    setError("");

    try {
      const response = await fetch(
        `${API}/api/v1/workspaces/${selectedWorkspaceId}/documents/${document.id}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ search_enabled: nextEnabled }),
        },
      );
      const updated = await readJson<DocumentRecord>(response);
      setDocuments((current) => current.map((item) => item.id === updated.id ? updated : item));
      await refreshWorkspaces();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось изменить участие документа в поиске");
    } finally {
      setDocumentActionId(null);
    }
  };

  const deleteDocument = async (document: DocumentRecord) => {
    if (!selectedWorkspaceId || documentActionId) return;
    const confirmed = window.confirm(
      `Удалить «${document.filename}» навсегда? Исходный файл, чанки и embeddings будут удалены.`,
    );
    if (!confirmed) return;

    setDocumentActionId(document.id);
    setError("");

    try {
      const response = await fetch(
        `${API}/api/v1/workspaces/${selectedWorkspaceId}/documents/${document.id}`,
        { method: "DELETE" },
      );
      if (!response.ok) throw new Error(await readError(response));
      setDocuments((current) => current.filter((item) => item.id !== document.id));
      await refreshWorkspaces();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось удалить документ");
    } finally {
      setDocumentActionId(null);
    }
  };

  const stopGeneration = () => {
    activeRequest.current?.abort();
    activeRequest.current = null;
    setStreamStage("idle");
    setMessages((current) => current.map((message) => (
      message.status === "streaming" ? { ...message, status: "stopped" } : message
    )));
  };

  const submitQuestion = async (rawQuestion?: string, summaryDocumentIds?: string[]) => {
    const text = (rawQuestion ?? question).trim();
    if (!selectedWorkspaceId || text.length < 2 || isStreaming || conversationLoading || conversationActionId) return;

    let conversationId = selectedConversationId;
    if (!conversationId) {
      const created = await createConversation();
      if (!created) return;
      conversationId = created.id;
    }

    const userMessage: ChatMessage = {
      id: newMessageId("user"),
      role: "user",
      content: text,
      status: "complete",
      parentMessageId: branchParentId,
    };
    const assistantId = newMessageId("assistant");
    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      status: "streaming",
      parentMessageId: userMessage.id,
    };

    setMessages((current) => [...current, userMessage, assistantMessage]);
    setBranchParentId(assistantId);
    if (!summaryDocumentIds) setQuestion("");
    setError("");
    setStreamStage("retrieving");

    const controller = new AbortController();
    activeRequest.current = controller;
    let persistedAssistantId: string | null = null;

    try {
      const response = await fetch(`${API}/api/v1/workspaces/${selectedWorkspaceId}/answer/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          question: text,
          ...(summaryDocumentIds ? { summary_document_ids: summaryDocumentIds } : { chat_mode: chatMode }),
          conversation_id: conversationId,
          parent_message_id: userMessage.parentMessageId || null,
          mode: "hybrid",
          retrieval_limit: 8,
          source_limit: 5,
          rerank: true,
          max_tokens: answerMaxTokens,
        }),
      });

      if (!response.ok) throw new Error(await readError(response));

      for await (const event of readSseEvents(response)) {
        if (event.event === "metadata") {
          const metadata = JSON.parse(event.data) as StreamMetadata;
          if (metadata.conversation_id) setSelectedConversationId(metadata.conversation_id);
          setMessages((current) => current.map((message) => (
            message.id === assistantId
              ? { ...message, metadata, sources: metadata.sources }
              : message
          )));
          setStreamStage("generating");
          continue;
        }

        if (event.event === "token") {
          const payload = JSON.parse(event.data) as { text?: string };
          if (!payload.text) continue;
          setMessages((current) => current.map((message) => (
            message.id === assistantId
              ? { ...message, content: message.content + payload.text }
              : message
          )));
          continue;
        }

        if (event.event === "error") {
          const payload = JSON.parse(event.data) as StreamDone & { detail?: string };
          persistedAssistantId = payload.assistant_message_id || null;
          setMessages((current) => current.map((message) => message.id === assistantId && message.metadata
            ? { ...message, metadata: { ...message.metadata, ...payload } }
            : message));
          throw new Error(payload.detail || "Ошибка генерации ответа");
        }

        if (event.event === "done") {
          const payload = JSON.parse(event.data) as StreamDone;
          persistedAssistantId = payload.assistant_message_id || null;
          setMessages((current) => current.map((message) => (
            message.id === assistantId
              ? {
                  ...message,
                  status: "complete",
                  metadata: message.metadata
                    ? { ...message.metadata, ...payload }
                    : message.metadata,
                }
              : message
          )));
        }
      }
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      const detail = reason instanceof Error ? reason.message : "Не удалось получить ответ";
      setError(detail);
      setMessages((current) => current.map((message) => (
        message.id === assistantId
          ? { ...message, status: "error", content: message.content || detail }
          : message
      )));
    } finally {
      try {
        await refreshConversations(selectedWorkspaceId);
        if (conversationId && workspaceIdRef.current === selectedWorkspaceId &&
            (!activeRequest.current || activeRequest.current === controller)) {
          await loadConversation(selectedWorkspaceId, conversationId, persistedAssistantId);
        }
      } catch {
        // The completed answer remains visible even if the sidebar refresh fails.
      } finally {
        if (activeRequest.current === controller) {
          activeRequest.current = null;
          setStreamStage("idle");
        }
      }
    }
  };

  const handleComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (canSubmit) void submitQuestion();
    }
  };

  const composerHint = !selectedWorkspaceId
    ? "Выберите рабочую область"
    : conversationLoading
      ? "Загружается история диалога"
      : "Enter — отправить, Shift+Enter — новая строка";

  if (CLEAN_INSTALL && !selectedWorkspace) {
    return (
      <div className="app-shell">
        <header className="topbar">
          <div className="brand">Скала<span>^р</span><small>RAG</small></div>
          <div className="status"><i className={health === "API готов" ? "online" : "offline"} />{health}</div>
        </header>
        <CleanStart loading={workspaceLoading} error={error} busy={Boolean(workspaceActionId)} onCreate={createNamedWorkspace} />
      </div>
    );
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">Скала<span>^р</span><small>RAG</small></div>
        <nav aria-label="Основная навигация">
          <button className={viewMode === "chat" ? "active" : ""} onClick={() => selectViewMode("chat")}>Чат</button>
          <button className={viewMode === "documents" ? "active" : ""} onClick={() => selectViewMode("documents")}>Документы</button>
          {canUseConfluence && <button className={viewMode === "confluence" ? "active" : ""} onClick={() => selectViewMode("confluence")}>Confluence</button>}
          <button className={viewMode === "knowledge" ? "active" : ""} onClick={() => selectViewMode("knowledge")}>Базы знаний</button>
          <button className={viewMode === "retrieval" ? "active" : ""} onClick={() => selectViewMode("retrieval")}>Разбор поиска</button>
          <button
            className={viewMode === "graph" ? "active" : ""}
            onClick={() => selectViewMode("graph")}
            disabled={!selectedConversationId}
          >
            Граф
          </button>
        </nav>
        <div className="status"><i className={health === "API готов" ? "online" : "offline"} />{health}</div>
      </header>

      <button
        type="button"
        className="mobile-workspace-toggle"
        aria-expanded={mobileSidebarOpen}
        aria-controls="workspace-sidebar"
        onClick={() => setMobileSidebarOpen((open) => !open)}
      >
        <span>{selectedWorkspace?.name || "Рабочая область"}</span>
        <span>{mobileSidebarOpen ? "Скрыть ↑" : "Область и диалоги ↓"}</span>
      </button>
      <div className={`workspace ${mobileSidebarOpen ? "sidebar-open" : ""}`}>
        <aside className="sidebar" id="workspace-sidebar">
          <section>
            <div className="section-title">
              <h3>Рабочая область</h3>
              <button
                className="text-button"
                onClick={createWorkspace}
                disabled={Boolean(workspaceActionId) || isStreaming}
              >
                + Новая
              </button>
            </div>
            <label>Workspace</label>
            <div className="workspace-selector-row">
              <select
                aria-label="Рабочая область"
                value={selectedWorkspaceId}
                onChange={(event) => switchWorkspace(event.target.value)}
                disabled={Boolean(workspaceActionId) || isStreaming}
              >
                {workspaces.map((workspace) => (
                  <option key={workspace.id} value={workspace.id}>{workspace.name}</option>
                ))}
              </select>
              <button
                type="button"
                className="workspace-icon-button"
                onClick={() => void renameWorkspace()}
                disabled={!selectedWorkspace || Boolean(workspaceActionId) || isStreaming}
                title="Переименовать workspace"
                aria-label="Переименовать workspace"
              >
                ✎
              </button>
              <button
                type="button"
                className="workspace-icon-button danger"
                onClick={() => void deleteWorkspace()}
                disabled={!selectedWorkspace || Boolean(workspaceActionId) || isStreaming}
                title="Удалить workspace"
                aria-label="Удалить workspace"
              >
                ×
              </button>
            </div>

            <label className="secondary-label">Готовая база знаний</label>
            <select
              aria-label="Готовая база знаний"
              value={selectedWorkspace?.base_knowledge_base_id ?? ""}
              onChange={updateKnowledgeBase}
              disabled={!selectedWorkspace || Boolean(workspaceActionId)}
            >
              <option value="">Не выбрана</option>
              {knowledgeBases.map((kb) => (
                <option key={kb.id} value={kb.id}>
                  {kb.name}{kb.active_version == null ? " · нет активной версии" : ` · v${kb.active_version}`}
                </option>
              ))}
            </select>

            <label className="secondary-label">Источники поиска</label>
            <select
              aria-label="Источники поиска"
              value={selectedWorkspace?.source_mode ?? "USER_DOCUMENTS"}
              onChange={updateSourceMode}
              disabled={!selectedWorkspace || Boolean(workspaceActionId)}
            >
              <option value="USER_DOCUMENTS">Только документы workspace</option>
              <option value="KNOWLEDGE_BASE" disabled={!selectedWorkspace?.base_knowledge_base_id}>
                Только готовая база знаний
              </option>
              <option value="HYBRID" disabled={!selectedWorkspace?.base_knowledge_base_id}>
                База знаний + документы
              </option>
            </select>
            <p className="hint workspace-mode-hint">
              {selectedWorkspace ? workspaceModeLabel(selectedWorkspace.source_mode) : "Выберите workspace"}
              {selectedKnowledgeBase?.active_version != null
                ? ` · ${selectedKnowledgeBase.name}, v${selectedKnowledgeBase.active_version}, ${selectedKnowledgeBase.active_document_count} документов`
                : selectedWorkspace?.base_knowledge_base_id
                  ? " · у базы нет активной версии"
                  : ""}
            </p>
          </section>

          <section className="source-shortcuts">
            <div className="section-title">
              <h3>Источники</h3>
              <span className="counter">{searchableCount} готово</span>
            </div>
            <button type="button" onClick={() => selectViewMode("documents")}>
              <span className="source-shortcut-icon">▤</span>
              <span><strong>Документы</strong><small>{uploadedDocuments.length} файлов workspace</small></span>
              <i>›</i>
            </button>
            {canUseConfluence && <button type="button" onClick={() => selectViewMode("confluence")}>
              <span className="source-shortcut-icon confluence">C</span>
              <span><strong>Confluence</strong><small>{confluenceDocuments.length} страниц</small></span>
              <i>›</i>
            </button>}
            <button type="button" onClick={() => selectViewMode("knowledge")}>
              <span className="source-shortcut-icon knowledge">◆</span>
              <span><strong>Базы знаний</strong><small>{knowledgeBases.length} баз</small></span>
              <i>›</i>
            </button>
          </section>

          <section className="chat-history-section">
            <div className="section-title">
              <h3>Диалоги</h3>
              <button
                className="text-button"
                onClick={() => void createConversation()}
                disabled={!selectedWorkspaceId || Boolean(conversationActionId) || isStreaming}
              >
                {conversationActionId === "new" ? "Создаю…" : "+ Новый"}
              </button>
            </div>
            <div className="chat-history-list">
              {conversationLoading && conversations.length === 0 && (
                <p className="empty-documents">Загрузка истории…</p>
              )}
              {!conversationLoading && conversations.length === 0 && (
                <p className="empty-documents">Диалогов пока нет</p>
              )}
              {visibleConversations.map((conversation) => (
                <div
                  className={`chat-history-item ${selectedConversationId === conversation.id ? "active" : ""}`}
                  key={conversation.id}
                >
                  <button
                    className="chat-history-select"
                    type="button"
                    disabled={isStreaming || conversationActionId === conversation.id}
                    onClick={() => void switchConversation(conversation.id)}
                  >
                    <strong>{conversation.title || "Новый диалог"}</strong>
                    <span>
                      {conversation.message_count} сообщ. · {new Date(conversation.updated_at).toLocaleDateString("ru-RU")}
                    </span>
                  </button>
                  <button
                    className="chat-history-delete"
                    type="button"
                    disabled={isStreaming || Boolean(conversationActionId)}
                    onClick={() => void deleteConversation(conversation)}
                    title="Удалить диалог"
                    aria-label="Удалить диалог"
                  >
                    {conversationActionId === conversation.id ? "…" : "×"}
                  </button>
                </div>
              ))}
            </div>
            {conversations.length > COLLAPSED_CONVERSATION_COUNT && (
              <button
                type="button"
                className="chat-history-more"
                onClick={() => setShowAllConversations((current) => !current)}
                aria-expanded={showAllConversations}
              >
                {showAllConversations
                  ? "Свернуть"
                  : `Показать ещё ${conversations.length - COLLAPSED_CONVERSATION_COUNT}`}
                <span aria-hidden="true">{showAllConversations ? "↑" : "↓"}</span>
              </button>
            )}
          </section>

          <section className="answer-mode-section">
            <h3>Режим ответа</h3>
            <div className="mode active">
              <span><i aria-hidden="true" /> Hybrid RAG</span>
              <small>Dense + полнотекстовый поиск · RRF</small>
            </div>
            <p className="mode-note">Reranker и расширенный режим подключим после серверного A/B-теста.</p>
          </section>
        </aside>

        <main className={`chat ${["documents", "confluence", "knowledge", "retrieval"].includes(viewMode) ? "management-surface" : ""}`}>
          {error && <div className="error-banner" role="alert"><span>{error}</span><button aria-label="Закрыть сообщение об ошибке" onClick={() => setError("")}>×</button></div>}

          {viewMode === "retrieval" ? (
            <RetrievalInspector
              key={`${selectedWorkspaceId}-${selectedWorkspace?.source_mode}-${selectedKnowledgeBase?.active_version_id || ""}`}
              apiBaseUrl={API}
              workspaceId={selectedWorkspaceId}
              workspaceName={selectedWorkspace?.name || "Рабочая область"}
              hasSearchableContent={hasSearchableContent}
              initialQuery={question || [...activeBranchMessages].reverse().find((message) => message.role === "user")?.content || ""}
              onAsk={(query) => { setQuestion(query); selectViewMode("chat"); }}
            />
          ) : viewMode === "documents" ? (
            <div className="management-page">
              <header className="management-hero">
                <div>
                  <span className="eyebrow">Источники workspace</span>
                  <h1>Документы</h1>
                  <p>Загружайте рабочие файлы и управляйте тем, что участвует в поиске.</p>
                </div>
                <div className="management-metrics">
                  <span><strong>{uploadedDocuments.length}</strong> файлов</span>
                  <span><strong>{uploadedDocuments.filter((item) => item.status === "READY" && item.search_enabled).length}</strong> в поиске</span>
                </div>
              </header>

              <div className="resource-layout">
                <section className="resource-card upload-card">
                  <div className="resource-card-heading">
                    <span className="resource-card-icon">＋</span>
                    <div><h2>Добавить документы</h2><p>Файлы будут обработаны в фоне и автоматически появятся в списке.</p></div>
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
                    className={`dropzone large ${dragging ? "dragging" : ""} ${uploading ? "busy" : ""}`}
                    onClick={() => !uploading && fileInput.current?.click()}
                    onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
                    onDragLeave={() => setDragging(false)}
                    onDrop={handleDrop}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        if (!uploading) fileInput.current?.click();
                      }
                    }}
                    role="button"
                    aria-label="Выбрать файлы для загрузки"
                    aria-disabled={uploading}
                    tabIndex={0}
                  >
                    <span className="upload-glyph" aria-hidden="true">⇧</span>
                    <strong>{uploading ? "Загружаю документы…" : "Перетащите файлы или выберите на компьютере"}</strong>
                    <small>PDF, DOCX, TXT, MD, CSV, JSON, HTML, XML<br />Изображения: PNG, JPG, WEBP, TIFF</small>
                  </div>
                </section>

                <section className="resource-card resource-list-card">
                  <div className="resource-card-heading compact">
                    <div><h2>Файлы workspace</h2><p>Готовые документы можно временно исключить из поиска.</p></div>
                    <span className="counter">{uploadedDocuments.length}</span>
                  </div>
                  <WorkspaceDocumentList
                    key={`${selectedWorkspaceId}:uploads`}
                    documents={uploadedDocuments}
                    emptyText="Файлов пока нет"
                    actionId={documentActionId}
                    onToggle={(document) => void toggleDocumentSearch(document)}
                    onDelete={(document) => void deleteDocument(document)}
                  />
                </section>
              </div>
            </div>
          ) : viewMode === "confluence" && canUseConfluence ? (
            <div className="management-page confluence-page">
              <header className="management-hero confluence-hero">
                <div>
                  <span className="eyebrow">Подключённый источник</span>
                  <h1>Confluence</h1>
                  <p>Выбирайте страницы из корпоративного Confluence и добавляйте их в поиск workspace.</p>
                </div>
                <div className="management-metrics">
                  <span><strong>{confluenceDocuments.length}</strong> страниц</span>
                  <span><strong>{confluenceSearchableCount}</strong> в поиске</span>
                </div>
              </header>

              <div className="resource-layout confluence-layout">
                <ConfluenceImportPanel
                  key={selectedWorkspaceId}
                  apiBaseUrl={API}
                  workspaceId={selectedWorkspaceId}
                  onImported={async () => {
                    await loadDocuments(selectedWorkspaceId);
                    await refreshWorkspaces();
                  }}
                  onError={setError}
                />
                <section className="resource-card resource-list-card">
                  <div className="resource-card-heading compact">
                    <div><h2>Импортированные страницы</h2><p>Ссылка открывает исходную страницу в Confluence.</p></div>
                    <span className="counter">{confluenceDocuments.length}</span>
                  </div>
                  <WorkspaceDocumentList
                    key={`${selectedWorkspaceId}:confluence`}
                    documents={confluenceDocuments}
                    emptyText="Страницы ещё не импортированы"
                    actionId={documentActionId}
                    onToggle={(document) => void toggleDocumentSearch(document)}
                    onDelete={(document) => void deleteDocument(document)}
                  />
                </section>
              </div>
            </div>
          ) : viewMode === "knowledge" ? (
            <div className="management-page knowledge-page">
              <header className="management-hero knowledge-hero">
                <div>
                  <span className="eyebrow">Управляемый контент</span>
                  <h1>Базы знаний</h1>
                  <p>Собирайте неизменяемые версии, проверяйте индексацию и публикуйте их атомарно.</p>
                </div>
                <div className="management-metrics">
                  <span><strong>{knowledgeBases.length}</strong> баз</span>
                  <span><strong>{knowledgeBases.filter((item) => item.active_version_id).length}</strong> активных</span>
                </div>
              </header>
              <KnowledgeBaseManager
                apiBaseUrl={API}
                knowledgeBases={knowledgeBases}
                refreshKnowledgeBases={refreshKnowledgeBases}
                refreshWorkspaces={refreshWorkspaces}
                onError={setError}
              />
            </div>
          ) : (
          <>
          <div className="chat-head">
            <div>
              <strong>{selectedConversation?.title || "Новый диалог"}</strong>
              <span>
                {selectedWorkspace?.name || "Рабочая область"}
                {selectedWorkspace ? ` · ${workspaceModeLabel(selectedWorkspace.source_mode)}` : ""}
              </span>
            </div>
            <div className="chat-head-stats">
              <span>{conversations.length} диалогов</span>
              <span>{searchableCount} документов включено</span>
              {selectedKnowledgeBase?.active_version != null && (
                <span>KB v{selectedKnowledgeBase.active_version}</span>
              )}
              <span>Hybrid retrieval</span>
            </div>
          </div>

          {viewMode === "graph" ? (
            <ConversationGraph
              messages={messages.map((message) => ({
                id: message.id,
                role: message.role,
                content: message.content,
                parentMessageId: message.parentMessageId,
                status: message.status,
                sourceCount: citedSources(message).length,
              }))}
              activeAssistantId={branchParentId}
              onSelectAssistant={(assistantId) => setBranchParentId(assistantId)}
            />
          ) : (
          <div className="conversation" aria-live="polite">
            {conversationLoading && messages.length === 0 && (
              <div className="empty-state compact-empty">
                <span className="spinner" />
                <p>Загружаю историю диалога…</p>
              </div>
            )}
            {!conversationLoading && messages.length === 0 && (
              <div className="empty-state">
                <div className="spark">✦</div>
                <h1>Чем помочь?</h1>
                <p>{hasSearchableContent ? "Задайте вопрос по материалам пространства или попросите выжимку." : "Начните диалог. Для вопросов по источникам добавьте документы."}</p>
                <div className="summary-card">
                  <span><strong>{documents.length}</strong> документов workspace</span>
                  <span><strong>{searchableCount}</strong> включено в поиск</span>
                  {selectedKnowledgeBase?.active_version != null && (
                    <span><strong>{selectedKnowledgeBase.active_document_count}</strong> в базе знаний</span>
                  )}
                </div>
                {hasSearchableContent && chatMode !== "general" && (
                  <div className="suggestions">
                    {SUGGESTED_QUESTIONS.map((item) => (
                      <button key={item} onClick={() => void submitQuestion(item)}>{item}</button>
                    ))}
                  </div>
                )}
              </div>
            )}

            {activeBranchMessages.map((message) => (
              <article className={`message-row ${message.role}`} key={message.id}>
                <div className="message-avatar" aria-hidden="true">{message.role === "user" ? "В" : "✦"}</div>
                <div className="message-content">
                  <div className="message-label">{message.role === "user" ? "Вы" : "Скала RAG"}</div>
                  <div className={`message-bubble ${message.status}`}>
                    {message.role === "assistant" && !message.content && message.status === "streaming" ? (
                      <span className="typing"><i /><i /><i /></span>
                    ) : (
                      <div className="message-text">{message.role === "assistant" ? renderAnswerText(message) : message.content}</div>
                    )}
                  </div>

                  {message.role === "assistant" && (
                    <CodeIntegrityPanel
                      audit={message.metadata?.code_integrity}
                      sources={message.sources || []}
                      withheld={message.metadata?.abstention_reason === "code_integrity_validation_failed"}
                    />
                  )}

                  {message.metadata?.retrieval_mode === "conversation" && (
                    <div className="answer-meta"><span>Без поиска по источникам</span></div>
                  )}
                  {message.metadata?.retrieval_mode === "document_summary" && (
                    <div className="answer-meta"><span>Выжимка по документам</span></div>
                  )}
                  {message.role === "assistant" && message.metadata && !["conversation", "document_summary"].includes(message.metadata.retrieval_mode) && (
                    <div className="answer-meta">
                      <span>{message.metadata.retrieval_mode}</span>
                      <span>{workspaceModeLabel(message.metadata.workspace_source_mode)}</span>
                      <span>{message.metadata.rerank_applied ? "reranker применён" : "без reranker"}</span>
                      <span>{message.metadata.context_chars.toLocaleString("ru-RU")} символов контекста</span>
                      <span>
                        {message.metadata.evidence_status === "strong"
                          ? "доказательства: сильные"
                          : message.metadata.evidence_status === "limited"
                            ? "доказательства: ограниченные"
                            : "недостаточно доказательств"}
                      </span>
                      {message.metadata.abstained && (
                        <span>
                          {message.metadata.abstention_reason === "code_integrity_validation_failed"
                            ? "отказ: дословность кода не подтверждена"
                            : message.metadata.abstention_reason === "insufficient_retrieval_evidence"
                            ? "безопасный отказ: слабые retrieval-доказательства"
                            : "безопасный отказ: контекст не подтверждает ответ"}
                        </span>
                      )}
                      {!message.metadata.abstained && message.metadata.citation_valid != null && (
                        <span>
                          {message.metadata.citation_valid
                            ? "ссылки проверены"
                            : "есть проблема со ссылками"}
                        </span>
                      )}
                    </div>
                  )}

                  {message.role === "assistant" && message.metadata && !["conversation", "document_summary"].includes(message.metadata.retrieval_mode) && (
                    <details className="answer-evidence">
                      <summary>Как выбран ответ <span aria-hidden="true">↗</span></summary>
                      <div>
                        <p className="evidence-query-label">Запрос к индексу</p>
                        <p className="evidence-query">{message.metadata.retrieval_query || "Не сохранён для этого ответа"}</p>
                        <dl>
                          <div><dt>Кандидаты</dt><dd>{message.metadata.candidate_count}</dd></div>
                          <div><dt>В контексте</dt><dd>{message.metadata.selected_source_count}</dd></div>
                          <div><dt>Использовано ссылок</dt><dd>{(message.metadata.cited_source_indices || []).length}</dd></div>
                          <div><dt>Точные идентификаторы</dt><dd>{message.metadata.matched_anchor_count} / {message.metadata.required_anchor_count}</dd></div>
                        </dl>
                        <p className="evidence-scope">Проверка ссылок подтверждает их наличие в выбранных источниках. Проверяйте сами утверждения по приведённым фрагментам.</p>
                        <button type="button" onClick={() => { setQuestion(message.metadata?.retrieval_query || ""); selectViewMode("retrieval"); }}>Сравнить способы поиска</button>
                      </div>
                    </details>
                  )}

                  {message.role === "assistant"
                    && citedSources(message).length > 0 && (
                    <div className="sources-panel">
                      <h4>Использованные источники</h4>
                      <div className="source-list">
                        {citedSources(message).map((source) => (
                          <details id={`${message.id}-source-${source.index}`} className="source-card" key={source.index}>
                            <summary>
                              <span className="source-index">{source.index}</span>
                              <span className="source-summary">
                                <strong>{sourceTitle(source)}</strong>
                                <small>
                                  {sourceProvenance(source)} · {source.filename}
                                  {pageLabel(source) ? ` · ${pageLabel(source)}` : ""}
                                  {` · чанк ${source.chunk_index}`}
                                </small>
                              </span>
                              <span className="source-score">
                                {source.rerank_score == null ? "—" : source.rerank_score.toFixed(3)}
                              </span>
                            </summary>
                            <p style={{ whiteSpace: "pre-wrap" }}>{source.excerpt}</p>
                            {source.source_url && /^https?:\/\//i.test(source.source_url) && (
                              <div className="source-card-footer">
                                <a className="source-external-link" href={source.source_url} target="_blank" rel="noopener noreferrer">
                                  <span>Открыть в Confluence</span>
                                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                                    <path d="M14 3h7v7M21 3l-11 11M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5" />
                                  </svg>
                                </a>
                                {source.source_version && (
                                  <span className="source-version" title="Версия страницы при импорте; ссылка открывает оригинал в Confluence">
                                    Версия {source.source_version}
                                  </span>
                                )}
                              </div>
                            )}
                          </details>
                        ))}
                      </div>
                    </div>
                  )}

                  {message.status === "stopped" && <div className="message-note">Генерация остановлена</div>}
                  {message.metadata?.finish_reason === "length" && (
                    <div className="message-note" role="status">
                      Ответ достиг лимита токенов и может быть неполным. Попросите продолжить или разбейте вопрос на части.
                    </div>
                  )}
                  {message.status === "error" && <div className="message-note error-note">Ответ завершился с ошибкой</div>}
                </div>
              </article>
            ))}

            {isStreaming && (
              <div className="stream-status">
                <span className="spinner" />
                {streamStage === "retrieving" ? "Обрабатываю запрос…" : "Формирую ответ…"}
              </div>
            )}
            <div ref={chatEnd} />
          </div>
          )}

          <div className="composer-area">
            {branchParentId && selectedConversationId && (
              <div className="branch-context">
                <span>↗ Новый вопрос продолжит выбранную ветку</span>
                <button type="button" onClick={() => selectViewMode("graph")}>Выбрать другую карточку</button>
              </div>
            )}
            {summaryDocuments.length > 0 && (
              <DocumentSummaryPicker key={`${selectedWorkspaceId}/${selectedConversationId}`}
                documents={summaryDocuments}
                disabled={isStreaming || conversationLoading || Boolean(conversationActionId)}
                onSubmit={(text, ids) => void submitQuestion(text, ids)}
              />
            )}
            <div className="composer">
              <input
                ref={attachmentInput}
                type="file"
                accept={ACCEPTED_FILES}
                multiple
                hidden
                onChange={(event) => {
                  const files = Array.from(event.currentTarget.files ?? []);
                  event.currentTarget.value = "";
                  if (files.length) void uploadFiles(files);
                }}
              />
              <button
                className="composer-attachment"
                type="button"
                disabled={!selectedWorkspaceId || uploading || isStreaming || conversationLoading || selectedWorkspace?.source_mode === "KNOWLEDGE_BASE"}
                onClick={() => attachmentInput.current?.click()}
                title={selectedWorkspace?.source_mode === "KNOWLEDGE_BASE" ? "Для вложений выберите режим с документами рабочего пространства" : "Добавить документ или изображение в пространство"}
                aria-label="Прикрепить документ или изображение"
              >
                <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="m8 12 6-6a3 3 0 0 1 4.2 4.2L9.5 19a5 5 0 0 1-7.1-7.1l10-10" transform="translate(2 1) scale(.9)" /><path d="m8.5 12.5 5-5a1 1 0 0 1 1.4 1.4l-6 6" /></svg>
              </button>
              <textarea
                aria-label="Вопрос по источникам"
                value={question}
                disabled={!selectedWorkspaceId || conversationLoading}
                placeholder={chatMode === "sources" ? "Задайте вопрос по источникам…" : "Напишите сообщение…"}
                rows={1}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={handleComposerKeyDown}
              />
              {isStreaming ? (
                <button className="stop-button" onClick={stopGeneration} title="Остановить генерацию" aria-label="Остановить генерацию">■</button>
              ) : (
                <button onClick={() => void submitQuestion()} disabled={!canSubmit} title="Отправить" aria-label="Отправить вопрос">➤</button>
              )}
            </div>
            <div className="composer-options">
              <label>
                Режим ответа
                <select value={chatMode} disabled={isStreaming} onChange={(event) => setChatMode(event.target.value as ChatMode)}>
                  <option value="auto">Авто</option>
                  <option value="general">Общий чат</option>
                  <option value="sources">Только по источникам</option>
                </select>
              </label>
              <label>
                Лимит ответа
                <select value={answerMaxTokens} disabled={isStreaming} onChange={(event) => setAnswerMaxTokens(Number(event.target.value))}>
                  <option value={800}>800 токенов</option>
                  <option value={2048}>2048 токенов</option>
                  <option value={4096}>4096 токенов</option>
                </select>
              </label>
              <span className="composer-hint">Верхняя граница длины, не требование к объёму ответа</span>
            </div>
            <div className="composer-hint">{composerHint}</div>
            <div className="composer-hint composer-attachment-hint">{uploading ? "Загружаем вложения…" : "Вложения сохраняются в документах пространства. Для вопросов по ним дождитесь статуса «Готов»."}</div>
          </div>
          </>
          )}
        </main>
      </div>
    </div>
  );
}
