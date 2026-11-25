export type CodeIntegrity = {
  status: "no_code" | "exact_match" | "unverified";
  total_block_count: number;
  verified_block_count: number;
  blocks: {
    index: number;
    status: "exact_match" | "unverified";
    sha256: string;
    witnesses: {
      source_index: number;
      source_page_id: string | null;
      source_version: string | null;
      source_url: string | null;
    }[];
  }[];
};

type Source = { index: number; heading: string | null; filename: string; excerpt: string };

export function CodeIntegrityPanel({ audit, sources, withheld = false }: {
  audit: CodeIntegrity | null | undefined;
  sources: Source[];
  withheld?: boolean;
}) {
  if (!audit || audit.status === "no_code" || audit.total_block_count === 0) return null;
  const exact = audit.status === "exact_match";

  return (
    <details className={`code-integrity ${exact ? "verified" : "unverified"}`}>
      <summary>
        <span className="integrity-symbol" aria-hidden="true">{exact ? "✓" : "!"}</span>
        <span>
          <strong>{exact ? "Код совпадает с источниками дословно" : withheld ? "Дословность кода не подтверждена · ответ остановлен" : "Не все блоки кода совпадают с источниками"}</strong>
          <small>{audit.verified_block_count} из {audit.total_block_count} блоков проверено по полному тексту</small>
        </span>
        <span className="integrity-expand" aria-hidden="true">⌄</span>
      </summary>
      <div className="integrity-body">
        <p className="integrity-scope">Сравнение проверяет текст и порядок строк внутри каждого блока. Оно не проверяет полноту инструкции, порядок между блоками, безопасность, исполнимость или применимость команд к вашей системе.</p>
        {audit.blocks.map((block) => (
          <section className={`integrity-block ${block.status}`} key={block.index}>
            <header><strong>Блок {block.index}</strong><span>{block.status === "exact_match" ? "Полное совпадение" : "Полное совпадение не найдено"}</span></header>
            <div className="integrity-witnesses">
              {block.witnesses.map((witness, index) => {
                const source = sources.find((item) => item.index === witness.source_index);
                return (
                  <details key={`${witness.source_index}-${index}`}>
                    <summary>
                      <span className="source-index">{witness.source_index}</span>
                      <span>{source?.heading || source?.filename || `Источник ${witness.source_index}`}{witness.source_version ? ` · Confluence v${witness.source_version}` : ""}</span>
                    </summary>
                    {source && <pre>{source.excerpt}</pre>}
                    {witness.source_url && /^https?:\/\//i.test(witness.source_url) && <a href={witness.source_url} target="_blank" rel="noopener noreferrer">Открыть оригинал в Confluence ↗</a>}
                  </details>
                );
              })}
            </div>
            <details className="integrity-hash"><summary>SHA-256 текста блока</summary><code>{block.sha256}</code></details>
          </section>
        ))}
      </div>
    </details>
  );
}
