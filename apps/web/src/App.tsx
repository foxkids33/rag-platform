import { useEffect, useState } from "react";

type KnowledgeBase = {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
};

const API = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

export function App() {
  const [health, setHealth] = useState("Подключение…");
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKb, setSelectedKb] = useState("");

  useEffect(() => {
    fetch(`${API}/api/v1/health`)
      .then((r) => r.json())
      .then(() => setHealth("API готов"))
      .catch(() => setHealth("API недоступен"));
    fetch(`${API}/api/v1/knowledge-bases`)
      .then((r) => (r.ok ? r.json() : []))
      .then(setKnowledgeBases)
      .catch(() => setKnowledgeBases([]));
  }, []);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">Скала<span>^р</span><small>RAG</small></div>
        <nav><button className="active">Чат</button><button>Граф</button></nav>
        <div className="status"><i />{health}</div>
      </header>

      <div className="workspace">
        <aside className="sidebar">
          <section>
            <h3>Рабочая область</h3>
            <label>База знаний</label>
            <select value={selectedKb} onChange={(e) => setSelectedKb(e.target.value)}>
              <option value="">Только мои документы</option>
              {knowledgeBases.map((kb) => <option key={kb.id} value={kb.id}>{kb.name}</option>)}
            </select>
            <p className="hint">Документы workspace будут искаться вместе с выбранной базой знаний.</p>
          </section>
          <section>
            <h3>Документы</h3>
            <div className="dropzone">Перетащите файлы сюда<br/><small>Upload появится на следующем этапе</small></div>
          </section>
          <section>
            <h3>Режим поиска</h3>
            <div className="mode active">Быстрый hybrid RAG</div>
            <div className="mode disabled">Расширенный поиск · скоро</div>
          </section>
        </aside>

        <main className="chat">
          <div className="chat-head">
            <div><strong>Новый запрос</strong><span>{selectedKb ? "Готовая база + документы workspace" : "Только документы workspace"}</span></div>
          </div>
          <div className="empty-state">
            <div className="spark">✦</div>
            <h1>Спросите базу знаний</h1>
            <p>Первый этап готовит фундамент проекта. Следующим шагом подключаем загрузку, ingestion и настоящий hybrid retrieval.</p>
          </div>
          <div className="composer"><textarea placeholder="Спросите что-нибудь по базе знаний…"/><button>➤</button></div>
        </main>
      </div>
    </div>
  );
}
