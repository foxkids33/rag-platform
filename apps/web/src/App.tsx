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

type WorkspaceSourceMode = "USER_DOCUMENTS" | "KNOWLEDGE_BASE" | "HYBRID";

type KnowledgeBase = {
  id: string;
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
  name: string;
  base_knowledge_base_id: string | null;
  base_knowledge_base_name: string | null;
  active_knowledge_base_version_id: string | null;
  active_knowledge_base_version: number | null;
  source_mode: WorkspaceSourceMode;
  user_id: string | null;
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
  status?: "complete" | "streaming" | "stopped" | "error";
  model?: string;
  retrieval_query?: string;
  retrieval_mode?: string;
  workspace_source_mode?: WorkspaceSourceMode;
  knowledge_base_id?: string | null;
  knowledge_base_version_id?: string | null;
  rerank_applied?: boolean;
  context_chars?: number;
  abstained?: boolean;
  evidence_status?: "strong" | "limited" | "insufficient";
  evidence_score?: number | null;
  candidate_count?: number;
  selected_source_count?: number;
  citation_valid?: boolean | null;
  cited_source_indices?: number[];
  invalid_citations?: number[];
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
  source_scope: "workspace" | "knowledge_base";
  knowledge_base_name: string | null;
  knowledge_base_version: number | null;
};

type StreamMetadata = {
  question: string;
  retrieval_query: string;
  conversation_id: string | null;
  user_message_id: string | null;
  parent_message_id: string | null;
  model: string;
  retrieval_mode: string;
  workspace_source_mode: WorkspaceSourceMode;
  knowledge_base_id: string | null;
  knowledge_base_version_id: string | null;
  rerank_applied: boolean;
  context_chars: number;
  abstained: boolean;
  evidence_status: "strong" | "limited" | "insufficient";
  evidence_score: number | null;
  candidate_count: number;
  selected_source_count: number;
  citation_valid: boolean | null;
  cited_source_indices: number[];
  invalid_citations: number[];
  sources: AnswerSource[];
};

type StreamDone = {
  assistant_message_id?: string | null;
  citation_valid?: boolean;
  cited_source_indices?: number[];
  invalid_citations?: number[];
  abstained?: boolean;
  evidence_status?: "strong" | "limited" | "insufficient";
  evidence_score?: number | null;
  candidate_count?: number;
  selected_source_count?: number;
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

const API = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
const ACCEPTED_FILES = ".txt,.md,.csv,.json,.html,.htm,.xml,.pdf,.docx";
const SUGGESTED_QUESTIONS = [
  "Кратко опиши назначение продукта",
  "Какие ключевые компоненты входят в решение?",
  "Как обеспечивается отказоустойчивость?",
];

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

function sourceTitle(source: AnswerSource): string {
  return source.heading || source.filename;
}

function renderAnswerText(message: ChatMessage): ReactNode[] {
  return message.content.split(/(\[\d+\])/g).map((part, index) => {
    const match = part.match(/^\[(\d+)\]$/);
    if (!match) return <span key={`${message.id}-text-${index}`}>{part}</span>;

    const sourceIndex = Number(match[1]);
    const exists = message.sources?.some((source) => source.index === sourceIndex);
    if (!exists) return <span key={`${message.id}-citation-${index}`}>{part}</span>;

    return (
      <a
        key={`${message.id}-citation-${index}`}
        className="inline-citation"
        href={`#${message.id}-source-${sourceIndex}`}
        aria-label={`Перейти к источнику ${sourceIndex}`}
      >
        {part}
      </a>
    );
  });
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
        retrieval_mode: message.metadata.retrieval_mode || "hybrid",
        workspace_source_mode: message.metadata.workspace_source_mode || "USER_DOCUMENTS",
        knowledge_base_id: message.metadata.knowledge_base_id ?? null,
        knowledge_base_version_id: message.metadata.knowledge_base_version_id ?? null,
        rerank_applied: Boolean(message.metadata.rerank_applied),
        context_chars: message.metadata.context_chars || 0,
        abstained: Boolean(message.metadata.abstained),
        evidence_status: message.metadata.evidence_status || "limited",
        evidence_score: message.metadata.evidence_score ?? null,
        candidate_count: message.metadata.candidate_count || 0,
        selected_source_count: message.metadata.selected_source_count || (sources?.length ?? 0),
        citation_valid: message.metadata.citation_valid ?? null,
        cited_source_indices: message.metadata.cited_source_indices || [],
        invalid_citations: message.metadata.invalid_citations || [],
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
  const bootstrapped = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);
  const activeRequest = useRef<AbortController | null>(null);

  const [health, setHealth] = useState("Подключение…");
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState("");
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState("");
  const [conversationLoading, setConversationLoading] = useState(false);
  const [conversationActionId, setConversationActionId] = useState<string | null>(null);
  const [workspaceActionId, setWorkspaceActionId] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [documentActionId, setDocumentActionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [viewMode, setViewMode] = useState<"chat" | "graph">("chat");
  const [branchParentId, setBranchParentId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [streamStage, setStreamStage] = useState<StreamStage>("idle");
  const [error, setError] = useState("");

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
  const hasActiveKnowledgeBase = Boolean(
    selectedKnowledgeBase?.active_version_id
    && selectedKnowledgeBase.active_document_count > 0
  );
  const hasSearchableContent = selectedWorkspace?.source_mode === "USER_DOCUMENTS"
    ? searchableCount > 0
    : selectedWorkspace?.source_mode === "KNOWLEDGE_BASE"
      ? hasActiveKnowledgeBase
      : searchableCount > 0 || hasActiveKnowledgeBase;
  const isStreaming = streamStage !== "idle";
  const canSubmit = Boolean(
    selectedWorkspaceId
    && hasSearchableContent
    && question.trim()
    && !isStreaming
    && !conversationLoading
  );

  const loadConversation = useCallback(async (
    workspaceId: string,
    conversationId: string,
    focusAssistantId?: string | null,
  ) => {
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
      const mapped = detail.messages.map(storedMessageToChat);
      setMessages(mapped);
      const focused = focusAssistantId
        ? mapped.find((message) => message.id === focusAssistantId && message.role === "assistant")
        : null;
      const latestAssistant = [...mapped].reverse().find((message) => message.role === "assistant");
      setBranchParentId(focused?.id || latestAssistant?.id || null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось загрузить диалог");
    } finally {
      setConversationLoading(false);
    }
  }, []);

  const refreshConversations = useCallback(async (workspaceId: string) => {
    if (!workspaceId) {
      setConversations([]);
      return [] as ConversationSummary[];
    }
    const response = await fetch(`${API}/api/v1/workspaces/${workspaceId}/conversations`);
    const items = await readJson<ConversationSummary[]>(response);
    setConversations(items);
    return items;
  }, []);

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

        if (workspaceItems.length === 0) {
          const createResponse = await fetch(`${API}/api/v1/workspaces`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name: "Мои документы",
              base_knowledge_base_id: null,
              source_mode: "USER_DOCUMENTS",
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

  useEffect(() => {
    if (!selectedWorkspaceId) return;
    let cancelled = false;

    const loadHistory = async () => {
      try {
        const items = await refreshConversations(selectedWorkspaceId);
        if (cancelled) return;
        const targetId = items[0]?.id || "";
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
    activeRequest.current?.abort();
    activeRequest.current = null;
    setStreamStage("idle");
    setMessages([]);
    setBranchParentId(null);
    setConversations([]);
    setSelectedConversationId("");
    setQuestion("");
    setError("");
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
      setConversations((current) => [conversation, ...current]);
      setSelectedConversationId(conversation.id);
      setMessages([]);
      setBranchParentId(null);
      return conversation;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось создать диалог");
      return null;
    } finally {
      setConversationActionId(null);
    }
  };

  const switchConversation = async (conversationId: string) => {
    if (!selectedWorkspaceId || conversationId === selectedConversationId || isStreaming) return;
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
        user_id: "local-user",
      }),
    });
    return readJson<Workspace>(response);
  };

  const createWorkspace = async () => {
    const name = window.prompt("Название рабочей области", "Новая рабочая область")?.trim();
    if (!name || workspaceActionId) return;

    setWorkspaceActionId("new");
    setError("");
    try {
      const workspace = await createWorkspaceRecord(name);
      setWorkspaces((current) => [workspace, ...current]);
      switchWorkspace(workspace.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось создать workspace");
    } finally {
      setWorkspaceActionId(null);
    }
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
      if (remaining.length === 0) {
        const fallback = await createWorkspaceRecord("Мои документы");
        remaining = [fallback];
      }
      setWorkspaces(remaining);
      switchWorkspace(remaining[0].id);
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

  const submitQuestion = async (rawQuestion?: string) => {
    const text = (rawQuestion ?? question).trim();
    if (!selectedWorkspaceId || !hasSearchableContent || !text || isStreaming) return;

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
    setQuestion("");
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
          conversation_id: conversationId,
          parent_message_id: userMessage.parentMessageId || null,
          mode: "hybrid",
          retrieval_limit: 8,
          source_limit: 5,
          rerank: true,
          max_tokens: 800,
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
          const payload = JSON.parse(event.data) as { detail?: string; assistant_message_id?: string | null };
          persistedAssistantId = payload.assistant_message_id || null;
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
      if (activeRequest.current === controller) activeRequest.current = null;
      setStreamStage("idle");
      try {
        await refreshConversations(selectedWorkspaceId);
        if (conversationId) {
          await loadConversation(selectedWorkspaceId, conversationId, persistedAssistantId);
        }
      } catch {
        // The completed answer remains visible even if the sidebar refresh fails.
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
      : !hasSearchableContent
        ? "Добавьте документ и дождитесь статуса «Готов»"
        : "Enter — отправить, Shift+Enter — новая строка";

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">Скала<span>^р</span><small>RAG</small></div>
        <nav>
          <button className={viewMode === "chat" ? "active" : ""} onClick={() => setViewMode("chat")}>Чат</button>
          <button
            className={viewMode === "graph" ? "active" : ""}
            onClick={() => setViewMode("graph")}
            disabled={!selectedConversationId}
          >
            Граф
          </button>
        </nav>
        <div className="status"><i className={health === "API готов" ? "online" : "offline"} />{health}</div>
      </header>

      <div className="workspace">
        <aside className="sidebar">
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

          <KnowledgeBaseManager
            apiBaseUrl={API}
            knowledgeBases={knowledgeBases}
            refreshKnowledgeBases={refreshKnowledgeBases}
            refreshWorkspaces={refreshWorkspaces}
            onError={setError}
          />

          <section className="chat-history-section">
            <div className="section-title">
              <h3>Диалоги</h3>
              <button
                className="text-button"
                onClick={() => void createConversation()}
                disabled={!selectedWorkspaceId || Boolean(conversationActionId) || isStreaming}
              >
                + Новый
              </button>
            </div>
            <div className="chat-history-list">
              {conversationLoading && conversations.length === 0 && (
                <p className="empty-documents">Загрузка истории…</p>
              )}
              {!conversationLoading && conversations.length === 0 && (
                <p className="empty-documents">Диалогов пока нет</p>
              )}
              {conversations.map((conversation) => (
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
          </section>

          <section>
            <div className="section-title">
              <h3>Документы</h3>
              <span className="counter">{searchableCount}/{documents.length} локальных включено</span>
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
              <small>PDF, DOCX, TXT, MD, CSV, JSON, HTML, XML</small>
            </div>

            <div className="document-list">
              {documents.length === 0 && <p className="empty-documents">Документов пока нет</p>}
              {documents.map((document) => {
                const busy = document.status === "QUEUED" || document.status === "PROCESSING";
                const actionPending = documentActionId === document.id;
                const excluded = document.status === "READY" && !document.search_enabled;

                return (
                  <div
                    className={`document-item ${excluded ? "excluded" : ""}`}
                    key={document.id}
                    title={document.filename}
                  >
                    <div className="document-icon">▤</div>
                    <div className="document-info">
                      <strong>{document.filename}</strong>
                      <span>{new Date(document.created_at).toLocaleString("ru-RU")}</span>
                    </div>
                    <span className={`badge ${excluded ? "status-excluded" : `status-${document.status.toLowerCase()}`}`}>
                      {excluded ? "Не в поиске" : statusLabel(document.status)}
                    </span>
                    <div className="document-actions">
                      <button
                        className="document-action"
                        type="button"
                        disabled={document.status !== "READY" || Boolean(documentActionId)}
                        onClick={() => void toggleDocumentSearch(document)}
                        title={document.search_enabled ? "Исключить из поиска" : "Вернуть в поиск"}
                        aria-label={document.search_enabled ? "Исключить документ из поиска" : "Вернуть документ в поиск"}
                      >
                        {actionPending ? "…" : document.search_enabled ? "⊘" : "↻"}
                      </button>
                      <button
                        className="document-action danger"
                        type="button"
                        disabled={busy || Boolean(documentActionId)}
                        onClick={() => void deleteDocument(document)}
                        title={busy ? "Дождитесь завершения обработки" : "Удалить документ навсегда"}
                        aria-label="Удалить документ навсегда"
                      >
                        {actionPending ? "…" : "×"}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          <section>
            <h3>Режим ответа</h3>
            <div className="mode active"><span>Быстрый hybrid RAG</span><small>RRF + reranker</small></div>
            <div className="mode disabled">Расширенный поиск · скоро</div>
          </section>
        </aside>

        <main className="chat">
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
              <span>{searchableCount} локальных включено</span>
              {selectedKnowledgeBase?.active_version != null && (
                <span>KB v{selectedKnowledgeBase.active_version}</span>
              )}
              <span>Hybrid retrieval</span>
            </div>
          </div>

          {error && <div className="error-banner"><span>{error}</span><button onClick={() => setError("")}>×</button></div>}

          {viewMode === "graph" ? (
            <ConversationGraph
              messages={messages.map((message) => ({
                id: message.id,
                role: message.role,
                content: message.content,
                parentMessageId: message.parentMessageId,
                status: message.status,
                sourceCount: message.sources?.length || 0,
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
                <h1>Задайте вопрос по документам</h1>
                <p>Ответ строится по hybrid retrieval, reranker и выбранным источникам. Проверяйте утверждения по ссылкам под ответом.</p>
                <div className="summary-card">
                  <span><strong>{documents.length}</strong> документов workspace</span>
                  <span><strong>{searchableCount}</strong> локальных включено</span>
                  {selectedKnowledgeBase?.active_version != null && (
                    <span><strong>{selectedKnowledgeBase.active_document_count}</strong> в базе знаний</span>
                  )}
                </div>
                {hasSearchableContent && (
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

                  {message.role === "assistant" && message.metadata && (
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
                      {message.metadata.citation_valid != null && (
                        <span>
                          {message.metadata.citation_valid
                            ? "ссылки проверены"
                            : "есть проблема со ссылками"}
                        </span>
                      )}
                    </div>
                  )}

                  {message.role === "assistant" && message.sources && message.sources.length > 0 && (
                    <div className="sources-panel">
                      <h4>Источники</h4>
                      <div className="source-list">
                        {message.sources.map((source) => (
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
                            <p>{source.excerpt}</p>
                          </details>
                        ))}
                      </div>
                    </div>
                  )}

                  {message.status === "stopped" && <div className="message-note">Генерация остановлена</div>}
                  {message.status === "error" && <div className="message-note error-note">Ответ завершился с ошибкой</div>}
                </div>
              </article>
            ))}

            {isStreaming && (
              <div className="stream-status">
                <span className="spinner" />
                {streamStage === "retrieving" ? "Ищу и ранжирую источники…" : "Формирую ответ…"}
              </div>
            )}
            <div ref={chatEnd} />
          </div>
          )}

          <div className="composer-area">
            {branchParentId && selectedConversationId && (
              <div className="branch-context">
                <span>↗ Новый вопрос продолжит выбранную ветку</span>
                <button type="button" onClick={() => setViewMode("graph")}>Выбрать другую карточку</button>
              </div>
            )}
            <div className={`composer ${!hasSearchableContent ? "disabled-composer" : ""}`}>
              <textarea
                value={question}
                disabled={!selectedWorkspaceId || !hasSearchableContent || conversationLoading}
                placeholder={hasSearchableContent ? "Спросите по выбранным источникам…" : "Подключите источник с готовыми документами"}
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
            <div className="composer-hint">{composerHint}</div>
          </div>
        </main>
      </div>
    </div>
  );
}
