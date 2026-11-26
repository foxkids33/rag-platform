import { useState } from "react";

type SummarySource = { id: string; title: string | null; filename: string };

type Props = {
  documents: SummarySource[];
  disabled: boolean;
  onSubmit: (question: string, documentIds: string[]) => void;
};

export function DocumentSummaryPicker({ documents, disabled, onSubmit }: Props) {
  const [open, setOpen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const selected = documents.filter((document) => selectedIds.includes(document.id));

  function submit() {
    if (disabled || !selected.length) return;
    const titles = selected.map((doc) => `«${doc.title || doc.filename}»`).join(", ");
    onSubmit(`Сделай краткую выжимку по ${selected.length === 1 ? "документу" : "документам"} ${titles}.`, selected.map((doc) => doc.id));
    setOpen(false);
    setSelectedIds([]);
  }

  return (
    <div className="summary-picker">
      <button type="button" className="summary-picker-toggle" disabled={disabled}
        aria-expanded={open} aria-controls="summary-picker-sources" onClick={() => setOpen(!open)}>
        Выжимка по документам
      </button>
      {open && <div id="summary-picker-sources" className="summary-picker-panel">
        <fieldset disabled={disabled}>
          <legend>Выберите от одного до трёх документов</legend>
          <div className="summary-picker-list">
            {documents.map((doc) => {
              const checked = selectedIds.includes(doc.id);
              return <label key={doc.id}>
                <input type="checkbox" checked={checked} disabled={!checked && selected.length >= 3}
                  onChange={() => setSelectedIds((ids) => checked ? ids.filter((id) => id !== doc.id) : [...ids, doc.id])} />
                <span>{doc.title || doc.filename}</span>
              </label>;
            })}
          </div>
        </fieldset>
        <button type="button" className="primary-button" disabled={disabled || !selected.length} onClick={submit}>
          Сделать выжимку
        </button>
      </div>}
    </div>
  );
}
