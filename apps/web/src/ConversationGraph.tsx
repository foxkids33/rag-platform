import { MouseEvent, PointerEvent, WheelEvent, useMemo, useRef, useState } from "react";

type GraphMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  parentMessageId?: string | null;
  status: "complete" | "streaming" | "stopped" | "error";
  sourceCount: number;
};

type ConversationGraphProps = {
  messages: GraphMessage[];
  activeAssistantId: string | null;
  onSelectAssistant: (assistantId: string) => void;
};

type ExchangeNode = {
  id: string;
  parentId: string | null;
  question: string;
  answer: string;
  status: GraphMessage["status"];
  sourceCount: number;
  depth: number;
  order: number;
  x: number;
  y: number;
};

const CARD_WIDTH = 320;
const CARD_HEIGHT = 218;
const GAP_X = 100;
const GAP_Y = 34;
const PADDING = 80;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function excerpt(value: string, max = 360): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length <= max ? normalized : `${normalized.slice(0, max - 1).trim()}…`;
}

function buildNodes(messages: GraphMessage[]): ExchangeNode[] {
  const byId = new Map(messages.map((message) => [message.id, message]));
  const raw = messages
    .filter((message) => message.role === "assistant")
    .map((assistant) => {
      const user = assistant.parentMessageId ? byId.get(assistant.parentMessageId) : undefined;
      return {
        id: assistant.id,
        parentId: user?.parentMessageId || null,
        question: user?.content || "Вопрос не найден",
        answer: assistant.content,
        status: assistant.status,
        sourceCount: assistant.sourceCount,
      };
    });

  const nodeIds = new Set(raw.map((node) => node.id));
  const children = new Map<string | null, string[]>();
  for (const node of raw) {
    const parentId = node.parentId && nodeIds.has(node.parentId) ? node.parentId : null;
    const siblings = children.get(parentId) || [];
    siblings.push(node.id);
    children.set(parentId, siblings);
  }

  const rawById = new Map(raw.map((node) => [node.id, node]));
  const positioned: ExchangeNode[] = [];
  const visited = new Set<string>();
  let order = 0;

  const visit = (id: string, depth: number) => {
    if (visited.has(id)) return;
    visited.add(id);
    const node = rawById.get(id);
    if (!node) return;
    positioned.push({
      ...node,
      depth,
      order,
      x: PADDING + depth * (CARD_WIDTH + GAP_X),
      y: PADDING + order * (CARD_HEIGHT + GAP_Y),
    });
    order += 1;
    for (const childId of children.get(id) || []) visit(childId, depth + 1);
  };

  for (const rootId of children.get(null) || []) visit(rootId, 0);
  for (const node of raw) visit(node.id, 0);
  return positioned;
}

export function ConversationGraph({
  messages,
  activeAssistantId,
  onSelectAssistant,
}: ConversationGraphProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ pointerId: number; x: number; y: number; panX: number; panY: number } | null>(null);
  const nodes = useMemo(() => buildNodes(messages), [messages]);
  const [scale, setScale] = useState(0.82);
  const [pan, setPan] = useState({ x: 24, y: 24 });

  const width = Math.max(
    900,
    ...nodes.map((node) => node.x + CARD_WIDTH + PADDING),
  );
  const height = Math.max(
    620,
    ...nodes.map((node) => node.y + CARD_HEIGHT + PADDING),
  );
  const byId = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);

  const fitAll = () => {
    const wrap = wrapRef.current;
    if (!wrap || nodes.length === 0) return;
    const nextScale = clamp(Math.min((wrap.clientWidth - 48) / width, (wrap.clientHeight - 48) / height), 0.28, 1);
    setScale(nextScale);
    setPan({
      x: Math.max(24, (wrap.clientWidth - width * nextScale) / 2),
      y: Math.max(24, (wrap.clientHeight - height * nextScale) / 2),
    });
  };

  const zoom = (factor: number) => setScale((current) => clamp(current * factor, 0.28, 1.5));

  const handleWheel = (event: WheelEvent<HTMLDivElement>) => {
    event.preventDefault();
    const wrap = wrapRef.current;
    if (!wrap) return;
    const rect = wrap.getBoundingClientRect();
    const cursorX = event.clientX - rect.left;
    const cursorY = event.clientY - rect.top;
    const nextScale = clamp(scale * (event.deltaY > 0 ? 0.9 : 1.1), 0.28, 1.5);
    const worldX = (cursorX - pan.x) / scale;
    const worldY = (cursorY - pan.y) / scale;
    setScale(nextScale);
    setPan({ x: cursorX - worldX * nextScale, y: cursorY - worldY * nextScale });
  };

  const handlePointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest(".graph-card")) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      panX: pan.x,
      panY: pan.y,
    };
  };

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    setPan({
      x: drag.panX + event.clientX - drag.x,
      y: drag.panY + event.clientY - drag.y,
    });
  };

  const stopDragging = (event: PointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
  };

  if (nodes.length === 0) {
    return (
      <div className="graph-empty">
        <div>⌘</div>
        <strong>Граф появится после первого ответа</strong>
        <span>Каждая новая ветка будет связана с выбранной карточкой.</span>
      </div>
    );
  }

  return (
    <div className="graph-shell">
      <div className="graph-toolbar">
        <span>{nodes.length} узлов</span>
        <button type="button" onClick={() => zoom(1.15)} title="Приблизить">+</button>
        <button type="button" onClick={() => zoom(0.87)} title="Отдалить">−</button>
        <button type="button" onClick={fitAll}>Показать всё</button>
      </div>
      <div
        className="graph-viewport"
        ref={wrapRef}
        onWheel={handleWheel}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={stopDragging}
        onPointerCancel={stopDragging}
      >
        <div
          className="graph-canvas"
          style={{
            width,
            height,
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})`,
          }}
        >
          <svg className="graph-edges" width={width} height={height} aria-hidden="true">
            {nodes.map((node) => {
              if (!node.parentId) return null;
              const parent = byId.get(node.parentId);
              if (!parent) return null;
              const startX = parent.x + CARD_WIDTH;
              const startY = parent.y + CARD_HEIGHT / 2;
              const endX = node.x;
              const endY = node.y + CARD_HEIGHT / 2;
              const curve = Math.max(50, (endX - startX) / 2);
              return (
                <path
                  key={`${parent.id}-${node.id}`}
                  d={`M ${startX} ${startY} C ${startX + curve} ${startY}, ${endX - curve} ${endY}, ${endX} ${endY}`}
                />
              );
            })}
          </svg>

          {nodes.map((node) => (
            <article
              className={`graph-card ${activeAssistantId === node.id ? "active" : ""}`}
              key={node.id}
              style={{ left: node.x, top: node.y, width: CARD_WIDTH, height: CARD_HEIGHT }}
              onClick={() => onSelectAssistant(node.id)}
            >
              <header>
                <span className="graph-depth">ветка {node.depth + 1}</span>
                <span>{node.sourceCount} источн.</span>
              </header>
              <strong>{excerpt(node.question, 110)}</strong>
              <p>{excerpt(node.answer)}</p>
              <footer>
                <span className={`graph-node-status ${node.status}`}>{node.status}</span>
                <button type="button" onClick={(event: MouseEvent<HTMLButtonElement>) => {
                  event.stopPropagation();
                  onSelectAssistant(node.id);
                }}>
                  {activeAssistantId === node.id ? "Контекст выбран" : "Продолжить отсюда"}
                </button>
              </footer>
            </article>
          ))}
        </div>

        <div className="graph-minimap" aria-label="Миникарта графа">
          <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet">
            {nodes.map((node) => {
              if (!node.parentId) return null;
              const parent = byId.get(node.parentId);
              if (!parent) return null;
              return (
                <line
                  key={`mini-${parent.id}-${node.id}`}
                  x1={parent.x + CARD_WIDTH / 2}
                  y1={parent.y + CARD_HEIGHT / 2}
                  x2={node.x + CARD_WIDTH / 2}
                  y2={node.y + CARD_HEIGHT / 2}
                />
              );
            })}
            {nodes.map((node) => (
              <rect
                key={`mini-${node.id}`}
                x={node.x}
                y={node.y}
                width={CARD_WIDTH}
                height={CARD_HEIGHT}
                className={activeAssistantId === node.id ? "active" : ""}
              />
            ))}
          </svg>
        </div>
      </div>
    </div>
  );
}
