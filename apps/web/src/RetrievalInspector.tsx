import { FormEvent, useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";

type SearchMode = "dense" | "lexical" | "hybrid";

type SearchResult = {
  chunk_id: string;
  document_id: string;
  filename: string;
  text: string;
  parent_text: string | null;
  parent_text_truncated: boolean;
  heading: string | null;
  heading_breadcrumb: string | null;
  page_start: number | null;
  page_end: number | null;
  dense_score: number | null;
  lexical_score: number | null;
  exact_score?: number | null;
  rrf_score: number;
  dense_rank: number | null;
  lexical_rank: number | null;
  exact_rank?: number | null;
  retrieval_rank: number;
  source_scope: "workspace" | "knowledge_base";
  knowledge_base_name: string | null;
  knowledge_base_version: number | null;
  source_url: string | null;
  source_page_id: string | null;
  source_version: string | null;
};

type SearchResponse = {
  query: string;
  mode: string;
  candidate_limit: number;
  exact_identifiers: string[];
  rerank_applied: boolean;
  results: SearchResult[];
};

type ColumnState = { loading: boolean; response?: SearchResponse; error?: string };
type Props = {
  apiBaseUrl: string;
  workspaceId: string;
  workspaceName: string;
  hasSearchableContent: boolean;
  initialQuery?: string;
  onAsk: (query: string) => void;
};

const MODES: { id: SearchMode; label: string; subtitle: string; symbol: string }[] = [
  { id: "dense", label: "По смыслу", subtitle: "Dense · векторная близость", symbol: "◎" },
  { id: "lexical", label: "По словам", subtitle: "Lexical · полнотекстовый поиск", symbol: "Aa" },
  { id: "hybrid", label: "Вместе", subtitle: "Hybrid · dense + lexical + exact", symbol: "⋈" },
];
const EXAMPLES = [
  "Для чего нужен файл ostor-agentd.env и где он используется?",
  "Какие ограничения у Round-Robin в Vstorage?",
];
const EMPTY_COLUMNS: Record<SearchMode, ColumnState> = {
  dense: { loading: false }, lexical: { loading: false }, hybrid: { loading: false },
};

function rank(value: number | null | undefined): string {
  return value == null ? "—" : `#${value}`;
}

function score(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(4);
}

function ResultCard({ result, position }: { result: SearchResult; position: number }) {
  const sourceLabel = result.source_scope === "knowledge_base"
    ? `${result.knowledge_base_name || "База знаний"}${result.knowledge_base_version == null ? "" : ` · v${result.knowledge_base_version}`}`
    : result.source_page_id ? "Confluence" : "Документ workspace";

  return (
    <article className="retrieval-result">
      <header>
        <span className="retrieval-position">{position}</span>
        <div>
          <h3>{result.heading || result.filename}</h3>
          <p>{sourceLabel}{result.source_version ? ` · версия ${result.source_version} при импорте` : ""}</p>
        </div>
      </header>
      {result.heading_breadcrumb && <p className="retrieval-breadcrumb">{result.heading_breadcrumb}</p>}
      <dl className="retrieval-ranks" aria-label="Позиции в поисковых каналах">
        <div><dt>Dense</dt><dd>{rank(result.dense_rank)}</dd></div>
        <div><dt>Lexical</dt><dd>{rank(result.lexical_rank)}</dd></div>
        <div className={result.exact_rank != null ? "has-exact" : ""}><dt>Exact</dt><dd>{rank(result.exact_rank)}</dd></div>
      </dl>
      <p className="retrieval-snippet">{result.text.length > 380 ? `${result.text.slice(0, 380)}…` : result.text}</p>
      <details className="retrieval-excerpt">
        <summary>Поисковый фрагмент целиком</summary>
        <pre>{result.text}</pre>
      </details>
      {result.parent_text && result.parent_text !== result.text && (
        <details className="retrieval-excerpt">
          <summary>Контекст раздела{result.parent_text_truncated ? " · сокращён" : ""}</summary>
          <pre>{result.parent_text}</pre>
        </details>
      )}
      <details className="retrieval-scores">
        <summary>Значения ранжирования</summary>
        <dl>
          <div><dt>Dense score</dt><dd>{score(result.dense_score)}</dd></div>
          <div><dt>Lexical score</dt><dd>{score(result.lexical_score)}</dd></div>
          <div><dt>Exact score</dt><dd>{score(result.exact_score)}</dd></div>
          <div><dt>RRF score</dt><dd>{score(result.rrf_score)}</dd></div>
        </dl>
        <p>У каналов разные шкалы. Эти значения используются для сортировки и не являются вероятностью правильного ответа.</p>
      </details>
      {result.source_url && /^https?:\/\//i.test(result.source_url) && (
        <a className="retrieval-source-link" href={result.source_url} target="_blank" rel="noopener noreferrer">
          Открыть оригинал <span aria-hidden="true">↗</span>
        </a>
      )}
    </article>
  );
}

export function RetrievalInspector({ apiBaseUrl, workspaceId, workspaceName, hasSearchableContent, initialQuery = "", onAsk }: Props) {
  const [query, setQuery] = useState(initialQuery);
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [columns, setColumns] = useState(EMPTY_COLUMNS);
  const request = useRef<AbortController | null>(null);
  const busy = MODES.some((mode) => columns[mode.id].loading);

  useEffect(() => () => request.current?.abort(), []);

  const compare = async (event: FormEvent) => {
    event.preventDefault();
    const submitted = query.trim();
    if (!workspaceId || !hasSearchableContent || submitted.length < 2 || busy) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setSubmittedQuery(submitted);
    setColumns({ dense: { loading: true }, lexical: { loading: true }, hybrid: { loading: true } });
    await Promise.allSettled(MODES.map(async ({ id }) => {
      try {
        const response = await apiFetch(`${apiBaseUrl}/api/v1/workspaces/${workspaceId}/search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: submitted, limit: 5, mode: id, rerank: false }),
          signal: controller.signal,
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => null) as { detail?: string } | null;
          throw new Error(payload?.detail || `HTTP ${response.status}`);
        }
        const payload = await response.json() as SearchResponse;
        if (controller.signal.aborted) return;
        setColumns((current) => ({ ...current, [id]: { loading: false, response: payload } }));
      } catch (reason) {
        if (controller.signal.aborted) return;
        setColumns((current) => ({
          ...current,
          [id]: { loading: false, error: reason instanceof Error ? reason.message : "Не удалось выполнить поиск" },
        }));
      }
    }));
  };

  const identifiers = [...new Set(MODES.flatMap(({ id }) => columns[id].response?.exact_identifiers || []))];
  const ready = MODES.every(({ id }) => Boolean(columns[id].response));
  const sharedCount = ready
    ? columns.dense.response!.results.filter((result) => MODES.every(({ id }) => columns[id].response!.results.some((item) => item.chunk_id === result.chunk_id))).length
    : null;

  return (
    <div className="management-page retrieval-page">
      <header className="management-hero">
        <div>
          <span className="eyebrow">Прозрачный поиск · {workspaceName}</span>
          <h1>Как находятся источники</h1>
          <p>Один вопрос, три способа поиска. Сравните, что находит смысловая близость, совпадение слов и их объединение с точными идентификаторами.</p>
        </div>
        <span className="retrieval-live-label"><i /> Запросы к текущему индексу</span>
      </header>
      <section className="retrieval-query-card" aria-label="Сравнение способов поиска">
        <form onSubmit={(event) => void compare(event)}>
          <label htmlFor="retrieval-query">Вопрос по документам</label>
          <div className="retrieval-query-row">
            <textarea id="retrieval-query" value={query} maxLength={2000} rows={2}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Имя файла, код ошибки или вопрос своими словами…"
              disabled={!workspaceId || !hasSearchableContent || busy} />
            <button type="submit" className="primary-button" disabled={!hasSearchableContent || !workspaceId || query.trim().length < 2 || busy}>
              {busy ? "Сравниваю…" : "Сравнить поиск"}
            </button>
          </div>
        </form>
        {hasSearchableContent ? (
          <div className="retrieval-examples" aria-label="Примеры вопросов">
            {EXAMPLES.map((example) => <button type="button" key={example} disabled={busy} onClick={() => setQuery(example)}>{example}</button>)}
          </div>
        ) : <p className="retrieval-empty-note">Подключите готовые документы или активную базу знаний в выбранной рабочей области.</p>}
        <p className="retrieval-query-note">Показываем первые 5 фрагментов каждого режима без reranker. Источники ограничены выбранной рабочей областью и режимом доступа.</p>
      </section>
      {submittedQuery && (
        <div className="retrieval-comparison-summary" role="status" aria-live="polite">
          <div>
            <strong>{submittedQuery}</strong>
            <span>{busy ? "Ожидаем результаты поиска…" : sharedCount == null ? "Сравнение выполнено частично" : `Общих фрагментов в трёх выдачах: ${sharedCount}`}</span>
            {identifiers.length > 0 && <p>Точные идентификаторы: {identifiers.map((identifier) => <code key={identifier}>{identifier}</code>)}</p>}
          </div>
          {!busy && columns.hybrid.response && <button type="button" className="retrieval-ask-button" onClick={() => onAsk(submittedQuery)}>Задать этот вопрос в чате <span aria-hidden="true">↗</span></button>}
        </div>
      )}
      <div className="retrieval-columns">
        {MODES.map((mode) => {
          const column = columns[mode.id];
          return (
            <section className={`retrieval-column ${mode.id}`} key={mode.id} aria-label={mode.label} aria-busy={column.loading}>
              <header className="retrieval-column-heading">
                <span className="retrieval-mode-icon" aria-hidden="true">{mode.symbol}</span>
                <div><h2>{mode.label}</h2><p>{mode.subtitle}</p></div>
                {column.response && <span className="retrieval-result-count">{column.response.results.length}</span>}
              </header>
              {column.loading && <div className="retrieval-placeholder" role="status"><span className="spinner" /> Ищем фрагменты…</div>}
              {column.error && <div className="retrieval-error" role="alert">{column.error}</div>}
              {!column.loading && !column.response && !column.error && <div className="retrieval-placeholder">Результаты появятся после сравнения</div>}
              {column.response && column.response.results.length === 0 && <div className="retrieval-placeholder">Подходящие фрагменты не найдены</div>}
              {column.response?.results.map((result, index) => <ResultCard result={result} position={index + 1} key={result.chunk_id} />)}
              {column.response && <p className="retrieval-column-note">Лимит кандидатов: {column.response.candidate_limit}{column.response.rerank_applied ? " · reranker применён" : " · без reranker"}</p>}
            </section>
          );
        })}
      </div>
    </div>
  );
}
