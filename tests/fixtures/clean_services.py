"""Synthetic Confluence, embeddings and chat contracts for disposable local QA.

This fixture contains no customer documents and is excluded from the release.
It does not measure model quality. Run only with RAG_TEST_FIXTURES=1.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import re
from urllib.parse import parse_qs, urlsplit

DIMENSION = 384
COUNTS = {"confluence": 0, "embedding": 0, "chat": 0}
BODY = (
    "<h1>Восстановление тестового сервиса</h1>"
    "<p>Инструкция предназначена только для проверки изолированного стенда. "
    "Для восстановления тестового сервиса сначала проверьте его состояние. "
    "Затем перезапустите тестовый сервис и проверьте журнал. "
    "После перезапуска убедитесь, что проверка доступности завершается успешно.</p>"
    "<h2>Проверка результата</h2><p>В журнале не должно быть новых ошибок. "
    "Если сервис остаётся недоступным, сохраните журнал и обратитесь к ответственной команде. "
    "Это синтетический документ: он не описывает реальные рабочие системы.</p>"
)


def page(page_id="101", space="TEAM"):
    return {"id": page_id, "type": "page", "status": "current",
            "title": "Восстановление тестового сервиса", "space": {"key": space},
            "version": {"number": 1}, "body": {"storage": {"value": BODY}},
            "ancestors": [], "metadata": {"labels": {"results": []}},
            "children": {"page": {"size": 0}},
            "_links": {"webui": f"/display/{space}/Recovery"}}


def vector(text):
    result = [0.0] * DIMENSION
    for word in re.findall(r"\w+", text.lower()):
        index = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % DIMENSION
        result[index] += 1.0
    norm = math.sqrt(sum(value * value for value in result)) or 1.0
    return [value / norm for value in result]


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send(self, payload, status=200, content_type="application/json"):
        body = json.dumps(payload, ensure_ascii=False).encode() if content_type == "application/json" else payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/__fixture__/stats":
            return self.send({"synthetic": True, "counts": COUNTS})
        if path == "/health":
            return self.send({"synthetic": True, "status": "ok"})
        if not path.startswith("/rest/api/"):
            return self.send({"detail": "Unknown fixture path"}, 404)
        COUNTS["confluence"] += 1
        if path == "/rest/api/space/TEAM":
            return self.send({"id": 1, "key": "TEAM", "name": "Synthetic QA team",
                              "homepage": {"id": "101"}, "_links": {"webui": "/display/TEAM"}})
        if path == "/rest/api/content/101":
            return self.send(page())
        if path == "/rest/api/content/999":
            return self.send(page("999", "PRIVATE"))
        if path == "/rest/api/content/101/child/page":
            return self.send({"results": [], "size": 0, "_links": {}})
        if path == "/rest/api/content":
            query = parse_qs(urlsplit(self.path).query)
            items = [page()] if query.get("spaceKey") == ["TEAM"] else []
            return self.send({"results": items, "size": len(items), "start": 0, "limit": 100, "_links": {}})
        return self.send({"detail": "Unknown Confluence fixture"}, 404)

    def do_POST(self):
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (ValueError, TypeError):
            return self.send({"detail": "Invalid JSON"}, 400)
        if self.path == "/embed":
            COUNTS["embedding"] += 1
            return self.send([vector(text) for text in payload["inputs"]])
        if self.path == "/v1/chat/completions":
            COUNTS["chat"] += 1
            answer = "Для восстановления тестового сервиса сначала проверьте его состояние, затем перезапустите тестовый сервис и проверьте журнал. [1]"
            if payload.get("stream"):
                chunks = [{"choices": [{"delta": {"content": answer}, "finish_reason": None}]},
                          {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
                body = "".join("data: " + json.dumps(item, ensure_ascii=False) + "\n\n" for item in chunks)
                return self.send(body + "data: [DONE]\n\n", content_type="text/event-stream")
            return self.send({"model": "clean-test-model", "choices": [{"message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]})
        return self.send({"detail": "Unknown fixture path"}, 404)


if __name__ == "__main__":
    if os.environ.get("RAG_TEST_FIXTURES") != "1":
        raise SystemExit("Set RAG_TEST_FIXTURES=1 for this synthetic QA server")
    ThreadingHTTPServer(("0.0.0.0", 8060), FixtureHandler).serve_forever()
