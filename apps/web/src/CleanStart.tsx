import { useState, type FormEvent } from "react";
import "./clean-start.css";

type Props = {
  loading: boolean;
  error: string;
  busy: boolean;
  onCreate: (name: string) => Promise<void>;
};

export function CleanStart({ loading, error, busy, onCreate }: Props) {
  const [name, setName] = useState("Confluence команды");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (name.trim() && !loading && !busy) void onCreate(name.trim());
  };
  return (
    <main className="clean-start">
      <section className="clean-start-card" aria-labelledby="clean-start-title">
        <span className="clean-start-mark" aria-hidden="true">C</span>
        <span className="clean-start-kicker">База знаний вашей команды</span>
        <h1 id="clean-start-title">Начните с Confluence</h1>
        <p>Создайте рабочую область и выберите страницы Confluence для поиска и ответов с источниками.</p>
        <ol className="clean-start-steps">
          <li><span>01</span> Создайте свою рабочую область</li>
          <li><span>02</span> Импортируйте выбранные страницы</li>
          <li><span>03</span> Задайте вопрос и проверьте источники</li>
        </ol>
        {loading ? <p role="status">Подключаемся к вашей установке…</p> : (
          <form onSubmit={submit}>
            <label htmlFor="clean-workspace-name">Название рабочей области</label>
            <input id="clean-workspace-name" value={name} onChange={(event) => setName(event.target.value)} maxLength={120} required autoComplete="off" disabled={busy} />
            <button type="submit" disabled={busy || !name.trim()}>{busy ? "Создаём…" : "Создать область и выбрать страницы"}</button>
          </form>
        )}
        {error && <div className="clean-start-error" role="alert"><p>{error}</p><button type="button" onClick={() => window.location.reload()}>Повторить загрузку</button></div>}
        <p className="clean-start-note">Установка начинается без прежних документов и диалогов. Настройки подключения Confluence и моделей задаёт администратор.</p>
      </section>
    </main>
  );
}
