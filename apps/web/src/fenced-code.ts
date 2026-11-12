export type AnswerSegment =
  | { type: "text"; content: string }
  | { type: "code"; content: string; language: string; index: number; closed: boolean };

/** Keep fence parsing aligned with the backend code-integrity audit. */
export function splitFencedCode(value: string): AnswerSegment[] {
  const text = value.replace(/\r\n/g, "\n");
  const segments: AnswerSegment[] = [];
  let opening: { marker: string; language: string; contentStart: number } | null = null;
  let textStart = 0;
  let blockIndex = 0;

  for (const match of text.matchAll(/[^\n]*(?:\n|$)/g)) {
    if (!match[0]) continue;
    const lineStart = match.index ?? 0;
    const lineEnd = lineStart + match[0].length;
    const line = match[0].replace(/\n$/, "");
    const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (!opening) {
      if (!marker) continue;
      if (lineStart > textStart) segments.push({ type: "text", content: text.slice(textStart, lineStart) });
      opening = { marker: marker[1], language: marker[2].trim(), contentStart: lineEnd };
      continue;
    }
    if (marker && marker[1][0] === opening.marker[0] && marker[1].length >= opening.marker.length && !marker[2].trim()) {
      segments.push({ type: "code", content: text.slice(opening.contentStart, lineStart).replace(/\n$/, ""),
        language: opening.language, index: ++blockIndex, closed: true });
      opening = null;
      textStart = lineEnd;
    }
  }
  if (opening) {
    segments.push({ type: "code", content: text.slice(opening.contentStart), language: opening.language,
      index: ++blockIndex, closed: false });
  } else if (textStart < text.length) {
    segments.push({ type: "text", content: text.slice(textStart) });
  }
  return segments;
}
