// Run: PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node scripts/ui/chat-navigation.test.mjs
// Optional PLAYWRIGHT_CHANNEL=chrome uses installed Chrome (no browser download).
// All API calls are mocked; no documents, dialogs or connector jobs are created.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { mkdir } from "node:fs/promises";
import { after, before, test } from "node:test";
import { createServer } from "../../apps/web/node_modules/vite/dist/node/index.js";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
let server;
let browser;
let baseUrl;
const now = "2026-09-16T12:00:00Z";
const conversation = (id, title) => ({
  id, title, workspace_id: "workspace-test", message_count: 0,
  created_at: now, updated_at: now,
});

before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL("../../apps/web/", import.meta.url)),
    configFile: false,
    server: { host: "127.0.0.1", port: 0 },
  });
  await server.listen();
  baseUrl = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}),
  });
});

after(async () => {
  await browser?.close();
  await server?.close();
});

const documentRecord = (id, changes = {}) => ({
  id, workspace_id: "workspace-test", filename: `File ${id}.pdf`, title: `Документ ${id}`,
  source_type: "UPLOAD", source_url: null, status: "READY", search_enabled: true,
  created_at: now, synced_at: null, external_id: null, external_version: null, ...changes,
});

test("Confluence source footer separates the link and imported version", async () => scenario(async (page) => {
  await page.locator(".source-card summary").click();
  const link = page.getByRole("link", { name: "Открыть в Confluence", exact: true });
  assert.equal(await link.getAttribute("href"), "https://confluence.example/pages/viewpage.action?pageId=123");
  assert.equal(await link.getAttribute("target"), "_blank");
  assert.match(await link.getAttribute("rel"), /noopener/);
  await page.locator(".source-card-footer").getByText("Версия 1", { exact: true }).waitFor();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const layout = await page.locator(".source-card").evaluate(card => {
      const excerpt = card.querySelector("p").getBoundingClientRect();
      const footer = card.querySelector(".source-card-footer").getBoundingClientRect();
      const link = card.querySelector("a");
      return { excerptBottom: excerpt.bottom, footerTop: footer.top,
        cardRight: card.getBoundingClientRect().right, linkRight: link.getBoundingClientRect().right,
        underline: getComputedStyle(link).textDecorationLine };
    });
    assert.ok(layout.footerTop >= layout.excerptBottom - 1);
    assert.ok(layout.linkRight <= layout.cardRight);
    assert.equal(layout.underline, "none");
  }
}, { messages: [{
  id: "answer-test", role: "assistant", content: "Ответ с источником [1]", metadata: {
    model: "test-model", cited_source_indices: [1], sources: [{
      index: 1, filename: "Инструкция.confluence.json", heading: "Работа с источником", chunk_index: 5,
      excerpt: "Фрагмент документа с инструкцией.\n".repeat(30), source_scope: "workspace",
      source_url: "https://confluence.example/pages/viewpage.action?pageId=123", source_version: "1",
      rerank_score: null,
    }],
  },
}] }));

const knowledgeBases = ["alpha", "beta"].map(id => ({
  id, slug: id, name: `База ${id}`, active_version_id: null, active_version: null,
  active_document_count: 0, active_version_status: null,
}));
const kbVersions = (id) => [{
  id: `${id}-v1`, knowledge_base_id: id, version: 1, status: "READY",
  document_count: 1, searchable_document_count: 1, processing_document_count: 0,
  ready_document_count: 1, failed_document_count: 0, embedding_dimension: 384,
  embedding_model: "test", chunker_version: "test", created_at: now, activated_at: null,
}];

async function scenario(run, { failCreate = false, documents = [], messages = [], failImportBatch = 0, importPending = false, withKnowledgeBases = false, spacePageCount = 11, spacePageTitle = "Page", answerFinishReason, answerMode = "hybrid" } = {}) {
  const context = await browser.newContext({ serviceWorkers: "block", viewport: { width: 1440, height: 1000 } });
  const unexpected = [];
  const pageErrors = [];
  const imports = [];
  const answerRequests = [];
  const operations = new Map();
  let importPolls = 0;
  let creates = 0;
  await context.route("**/runtime-config.js", route => route.fulfill({
    contentType: "application/javascript",
    body: "window.__RAG_RUNTIME_CONFIG__ = {authEnabled: false};",
  }));
  await context.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    let data;
    let status = 200;
    if (path.endsWith("/answer/stream") && method === "POST" && answerFinishReason) {
      answerRequests.push(route.request().postDataJSON());
      const metadata = {
        model: "test-model", retrieval_mode: answerMode, context_chars: 120,
        workspace_source_mode: "USER_DOCUMENTS", evidence_status: "strong",
        sources: [], cited_source_indices: [], finish_reason: answerFinishReason,
      };
      messages = [{ id: "generated", role: "assistant", content: "Тестовый ответ", metadata }];
      const event = (name, payload) => `event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`;
      return route.fulfill({ contentType: "text/event-stream", body:
        event("metadata", metadata) + event("token", { text: "Тестовый ответ" }) +
        event("done", { assistant_message_id: "generated", finish_reason: answerFinishReason }),
      });
    } else if (path.endsWith("/conversations") && method === "POST") {
      creates++;
      status = failCreate ? 500 : 200;
      data = failCreate ? { detail: "Тестовая ошибка создания" } : conversation("new", null);
    } else if (method === "POST" && path.includes("/integrations/confluence/")) {
      const id = `operation-${operations.size + 1}`;
      let result;
      if (path.endsWith("/imports")) {
        const body = route.request().postDataJSON();
        imports.push(body);
        if (imports.length === failImportBatch) {
          return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Тестовый сбой пакета" }) });
        }
        result = {
          requested: body.page_ids.length, queued: body.page_ids.length, skipped: 0, failed: 0,
          results: body.page_ids.map(page_id => ({ page_id, status: "queued" })),
        };
      } else if (path.endsWith("/spaces")) {
        result = { spaces: [{ key: "KBR", name: "Тестовое пространство", homepage_id: "10" }] };
      } else if (path.endsWith("/space-pages")) {
        const start = route.request().postDataJSON().start;
        result = {
          pages: Array.from({ length: Math.min(100, spacePageCount - start) }, (_, i) => start + i + 1).map(id => ({
            id: String(id), title: `${spacePageTitle} ${id}`, space_key: "KBR", version: "1", has_children: false,
          })),
          next_start: start + 100 < spacePageCount ? start + 100 : null,
        };
      } else if (path.endsWith("/children")) {
        result = { pages: [{ id: "10", title: "Родительская страница", space_key: "KBR", version: "1", has_children: true }] };
      } else {
        unexpected.push(`${method} ${path}`);
        return route.abort();
      }
      operations.set(id, { operation_id: id, status: path.endsWith("/imports") && importPending ? "LEASED" : "SUCCEEDED", result });
      data = { operation_id: id };
    } else if (method !== "GET") {
      unexpected.push(`${method} ${path}`);
      return route.abort();
    } else if (path === "/api/v1/health") {
      data = { status: "ok" };
    } else if (path === "/api/v1/knowledge-bases") {
      data = withKnowledgeBases ? knowledgeBases : [];
    } else if (path.startsWith("/api/v1/knowledge-bases/")) {
      const parts = path.split("/");
      const id = parts[4];
      if (path.endsWith("/versions")) data = kbVersions(id);
      else if (path.endsWith("/documents") && parts[6] === `${id}-v1`) {
        data = [documentRecord(`kb-${id}`, { filename: `Файл базы ${id}.pdf` })];
      } else {
        unexpected.push(`${method} ${path}`);
        return route.abort();
      }
    } else if (path.endsWith("/documents")) {
      data = path.includes("/workspace-second/") ? [documentRecord("second", { title: "Документ второй области" })] : documents;
    } else if (path === "/api/v1/workspaces") {
      data = ["workspace-test", "workspace-second"].map(id => ({
        id, name: id === "workspace-test" ? "Test workspace" : "Second workspace", tenant_id: "local", user_id: "local-user",
        source_mode: "USER_DOCUMENTS", base_knowledge_base_id: null,
        document_count: documents.length, searchable_document_count: documents.length, conversation_count: 2,
        created_at: now, updated_at: now,
      }));
    } else if (path.endsWith("/conversations")) {
      data = [conversation("existing", "Первый диалог"), conversation("other", "Второй диалог")];
    } else if (path.includes("/conversations/")) {
      const id = path.split("/").at(-1);
      data = { ...conversation(id, id === "existing" ? "Первый диалог" : "Второй диалог"), messages };
    } else if (path.includes("/integrations/confluence/operations/")) {
      data = operations.get(path.split("/").at(-1));
      if (data.status === "LEASED") importPolls++;
    } else if (path.endsWith("/integrations/confluence/connection")) {
      const connector = { enabled: true, configured: true, online: true, can_manage: false,
        max_pages_per_job: 50, max_job_seconds: 1800,
        connectors: [{ id: "connector-test", name: "Тестовый коннектор", online: true, allowed_spaces: ["KBR"], capabilities: ["LIST_SPACE_PAGES"] }],
      };
      data = { mode: "connector", enabled: true, configured: true, ready: true,
        connector, max_pages_per_job: 50, max_job_seconds: 1800 };
    } else {
      unexpected.push(`${method} ${path}`);
      return route.abort();
    }
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
  });
  try {
    const page = await context.newPage();
    page.on("pageerror", error => pageErrors.push(error.message));
    page.setDefaultTimeout(5000);
    await page.goto(baseUrl);
    await page.locator(".chat-head").getByText("Первый диалог", { exact: true }).waitFor();
    await run(page, () => creates, { imports, importPolls: () => importPolls, answerRequests });
    assert.deepEqual(unexpected, [], "Unexpected API calls must never reach a real backend");
    assert.deepEqual(pageErrors, [], "No browser JavaScript errors");
  } finally {
    await context.close();
  }
}

test("greeting works in an empty workspace without retrieval diagnostics", async () => scenario(async (page, _, state) => {
  const input = page.getByRole("textbox", { name: "Сообщение", exact: true });
  await input.fill("Привет");
  await input.press("Enter");
  await page.getByText("Тестовый ответ", { exact: true }).waitFor();
  assert.equal(state.answerRequests[0].question, "Привет");
  assert.equal(Object.hasOwn(state.answerRequests[0], "summary_document_ids"), false);
  assert.equal(state.answerRequests[0].chat_mode, "auto");
  assert.equal(await page.locator(".answer-evidence").count(), 0);
  await page.getByText("Без поиска по источникам", { exact: true }).waitFor();
}, { answerMode: "conversation", answerFinishReason: "stop" }));

test("general mode passes follow-ups in the same conversation and displays history without source claims", async () => scenario(async (page, _, state) => {
  const mode = page.getByRole("combobox", { name: "Режим ответа", exact: true });
  const input = page.getByRole("textbox", { name: "Сообщение", exact: true });
  await mode.selectOption("general");
  for (const text of ["Как подготовиться к собеседованию?", "А если опыта пока нет?"]) {
    await input.fill(text);
    const saved = page.waitForResponse(response => response.url().endsWith("/conversations/existing"));
    await input.press("Enter");
    await saved;
    await page.getByText("Тестовый ответ", { exact: true }).last().waitFor();
  }
  assert.equal(state.answerRequests.length, 2);
  assert.deepEqual(state.answerRequests.map(req => req.chat_mode), ["general", "general"]);
  assert.equal(state.answerRequests[0].conversation_id, state.answerRequests[1].conversation_id);
  assert.equal(state.answerRequests[1].parent_message_id, "generated");
  await page.reload();
  await page.getByText("Без поиска по источникам", { exact: true }).waitFor();
  assert.equal(await page.locator(".answer-evidence").count(), 0);
}, { documents: [documentRecord("ready")], answerMode: "conversation", answerFinishReason: "stop" }));

test("explicit source mode reaches the answer API", async () => scenario(async (page, _, state) => {
  await page.getByRole("combobox", { name: "Режим ответа", exact: true }).selectOption("sources");
  await page.getByRole("textbox", { name: "Сообщение", exact: true }).fill("Какие сроки указаны в документе?");
  await page.getByRole("button", { name: "Отправить вопрос" }).click();
  await page.getByText("Тестовый ответ", { exact: true }).waitFor();
  assert.equal(state.answerRequests[0].chat_mode, "sources");
}, { documents: [documentRecord("ready")], answerFinishReason: "stop" }));

for (const count of [1, 3]) {
  test(`summary sends ${count} selected ready articles and preserves the ordinary chat draft`, async () => scenario(async (page, _, state) => {
    const draft = page.getByRole("textbox", { name: "Сообщение", exact: true });
    await draft.fill("Следующий вопрос");
    await page.getByRole("button", { name: "Выжимка по документам", exact: true }).click();
    const choices = page.locator(".summary-picker").getByRole("checkbox");
    assert.equal(await choices.count(), 4);
    for (let i = 0; i < count; i++) await choices.nth(i).check();
    if (count === 3) assert.equal(await choices.last().isDisabled(), true);
    await page.getByRole("button", { name: "Сделать выжимку", exact: true }).click();
    await page.getByText("Тестовый ответ", { exact: true }).waitFor();
    assert.deepEqual(state.answerRequests[0].summary_document_ids, ["a", "b", "c"].slice(0, count));
    assert.equal(await draft.inputValue(), "Следующий вопрос");
    assert.equal(await page.locator(".answer-evidence").count(), 0);
  }, { documents: [
    ...["a", "b", "c", "d"].map(id => documentRecord(id, { source_type: "CONFLUENCE" })),
    documentRecord("disabled", { search_enabled: false }), documentRecord("pending", { status: "PROCESSING" }),
  ], answerMode: "document_summary", answerFinishReason: "stop" }));
}

for (const limit of [2048, 800, 4096]) {
  test(`answer token limit ${limit} reaches API and truncation survives history reload`, async () => scenario(async (page, _, state) => {
    const selector = page.getByRole("combobox", { name: "Лимит ответа", exact: true });
    assert.equal(await selector.inputValue(), "2048");
    await selector.selectOption(String(limit));
    await page.getByRole("textbox", { name: "Сообщение", exact: true }).fill("Тестовый вопрос");
    await page.getByRole("button", { name: "Отправить вопрос", exact: true }).click();
    await page.getByText("Тестовый ответ", { exact: true }).waitFor();
    assert.equal(state.answerRequests[0].max_tokens, limit);
    await page.getByText(/Ответ достиг лимита токенов/).waitFor();
    await page.reload();
    await page.getByText(/Ответ достиг лимита токенов/).waitFor();
    await page.setViewportSize({ width: 390, height: 900 });
    const bounds = await selector.boundingBox();
    assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= 390);
  }, { documents: [documentRecord("ready")], answerFinishReason: "length" }));
}

test("normal answer finish has no truncation warning", async () => scenario(async page => {
  await page.getByRole("textbox", { name: "Сообщение", exact: true }).fill("Тестовый вопрос");
  await page.getByRole("button", { name: "Отправить вопрос", exact: true }).click();
  await page.getByText("Тестовый ответ", { exact: true }).waitFor();
  assert.equal(await page.getByText(/Ответ достиг лимита токенов/).count(), 0);
}, { documents: [documentRecord("ready")], answerFinishReason: "stop" }));

for (const section of ["Confluence", "Документы", "Базы знаний", "Граф"]) {
  test(`new dialog opens Chat from ${section}`, async () => scenario(async (page, creates) => {
    await page.getByRole("navigation").getByRole("button", { name: section, exact: true }).click();
    await page.getByRole("button", { name: "+ Новый", exact: true }).click();
    await page.waitForURL("**/#chat");
    await page.locator(".chat-head").getByText("Новый диалог", { exact: true }).waitFor();
    assert.equal(creates(), 1);
    assert.equal(await page.getByRole("navigation").getByRole("button", { name: "Чат", exact: true }).getAttribute("class"), "active");
  }));
}

for (const title of ["Первый диалог", "Второй диалог"]) {
  test(`selecting ${title} opens Chat from Confluence without creating a dialog`, async () => scenario(async (page, creates) => {
    await page.getByRole("navigation").getByRole("button", { name: "Confluence", exact: true }).click();
    await page.locator(".chat-history-select").filter({ hasText: title }).click();
    await page.waitForURL("**/#chat");
    await page.locator(".chat-head").getByText(title, { exact: true }).waitFor();
    assert.equal(creates(), 0);
  }));
}

test("failed creation keeps Confluence open and shows the error", async () => scenario(async (page) => {
  await page.getByRole("navigation").getByRole("button", { name: "Confluence", exact: true }).click();
  await page.getByRole("button", { name: "+ Новый", exact: true }).click();
  await page.getByText("Тестовая ошибка создания", { exact: true }).waitFor();
  assert.equal(new URL(page.url()).hash, "#confluence");
}, { failCreate: true }));

test("document search, status filters, empty result and pagination", async () => scenario(async (page) => {
  await page.getByRole("navigation").getByRole("button", { name: "Документы", exact: true }).click();
  assert.equal(await page.locator(".resource-document").count(), 50);
  await page.getByRole("button", { name: "Показать ещё 12 из 12", exact: true }).click();
  assert.equal(await page.locator(".resource-document").count(), 62);
  await page.getByRole("searchbox", { name: "Поиск документов" }).fill("ошибочный");
  await page.getByText("Найдено: 1 из 62", { exact: true }).waitFor();
  await page.getByRole("combobox", { name: "Статус документов" }).selectOption("ready");
  await page.getByText("Ничего не найдено", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Сбросить фильтры", exact: true }).click();
  assert.equal(await page.locator(".resource-document").count(), 50);
  await page.getByRole("combobox", { name: "Статус документов" }).selectOption("excluded");
  await page.getByText("Найдено: 1 из 62", { exact: true }).waitFor();
}, { documents: [
  ...Array.from({ length: 60 }, (_, i) => documentRecord(String(i))),
  documentRecord("bad", { title: "Ошибочный документ", status: "FAILED" }),
  documentRecord("off", { search_enabled: false }),
] }));

test("upload picker can be opened with the keyboard", async () => scenario(async (page) => {
  await page.getByRole("navigation").getByRole("button", { name: "Документы", exact: true }).click();
  const picker = page.getByRole("button", { name: "Выбрать файлы для загрузки", exact: true });
  await picker.focus();
  for (const key of ["Enter", "Space"]) {
    const chooser = page.waitForEvent("filechooser");
    await picker.press(key);
    assert.equal((await chooser).isMultiple(), true);
  }
}));

async function openConfluence(page) {
  await page.getByRole("navigation").getByRole("button", { name: "Confluence", exact: true }).click();
  await page.getByText("Коннектор в сети", { exact: true }).waitFor();
}
async function addPage(page, id) {
  await page.getByRole("textbox", { name: "ID или ссылка страницы", exact: true }).fill(id);
  await page.getByRole("button", { name: "Добавить", exact: true }).click();
}

test("whole space discovers all pages and imports batches after confirmation", async () => scenario(async (page, _, state) => {
  await openConfluence(page);
  await page.getByRole("button", { name: "Показать пространства", exact: true }).click();
  await page.getByRole("button", { name: "Импортировать весь space", exact: true }).waitFor();
  let confirmation = "";
  page.on("dialog", async dialog => { confirmation = dialog.message(); await dialog.accept(); });
  await page.getByRole("button", { name: "Импортировать весь space", exact: true }).click();
  await page.getByText("Передано на проверку: 11 из 11", { exact: true }).waitFor();
  assert.match(confirmation, /все 11/);
  assert.deepEqual(state.imports.map(item => item.page_ids.length), [10, 1]);
  assert.deepEqual(state.imports.flatMap(item => item.page_ids), Array.from({ length: 11 }, (_, i) => String(i + 1)));
}));

test("whole space confirmation cancellation creates no imports", async () => scenario(async (page, _, state) => {
  await openConfluence(page);
  await page.getByRole("button", { name: "Показать пространства", exact: true }).click();
  page.on("dialog", dialog => dialog.dismiss());
  await page.getByRole("button", { name: "Импортировать весь space", exact: true }).click();
  await page.locator(".import-progress").waitFor({ state: "hidden" });
  assert.equal(state.imports.length, 0);
}));

test("large space import keeps progress inside its card and imported list compact", async () => scenario(async (page) => {
  await openConfluence(page);
  await page.getByRole("button", { name: "Показать пространства", exact: true }).click();
  page.on("dialog", dialog => dialog.accept());
  await page.getByRole("button", { name: "Импортировать весь space", exact: true }).click();
  await page.getByText("Передача страниц: 0 из 311", { exact: true }).waitFor();
  for (const width of [1650, 1280, 1190, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const bounds = await page.evaluate(() => {
      const card = document.querySelector(".confluence-import-card").getBoundingClientRect();
      const progress = document.querySelector(".import-progress progress").getBoundingClientRect();
      const browser = document.querySelector(".connector-browser").getBoundingClientRect();
      const list = document.querySelector(".confluence-page .resource-document-list");
      return { cardRight: card.right, progressRight: progress.right, browserRight: browser.right,
        scrollWidth: document.documentElement.scrollWidth, viewport: innerWidth,
        listHeight: list.getBoundingClientRect().height, listScroll: list.scrollHeight, listClient: list.clientHeight };
    });
    assert.ok(bounds.progressRight <= bounds.cardRight, `Progress overflows at ${width}px: ${JSON.stringify(bounds)}`);
    assert.ok(bounds.browserRight <= bounds.cardRight, `Browser overflows at ${width}px`);
    assert.ok(bounds.scrollWidth <= bounds.viewport, `Page overflows at ${width}px`);
    assert.ok(bounds.listHeight <= 421, `Imported list too tall: ${bounds.listHeight}px`);
    assert.ok(bounds.listScroll > bounds.listClient, "Long imported list must scroll internally");
    const list = page.locator(".confluence-page .resource-document-list");
    await list.evaluate(element => { element.scrollTop = element.scrollHeight; });
    assert.ok(await list.evaluate(element => element.scrollTop > 0));
  }
}, {
  spacePageCount: 311,
  spacePageTitle: "Инструкция по настройке корпоративной инфраструктуры и рабочих сервисов ".repeat(5),
  importPending: true,
  documents: Array.from({ length: 151 }, (_, i) => documentRecord(String(i), {
    source_type: "CONFLUENCE", title: `Импортированная страница ${i} с длинным названием`,
  })),
}));

test("Confluence accepts page URLs, validates IDs and supports removing selection", async () => scenario(async (page) => {
  await openConfluence(page);
  assert.equal(await page.getByRole("textbox", { name: "ID или ссылка страницы" }).inputValue(), "");
  await addPage(page, "https://confluence.example/pages/viewpage.action?pageId=1234");
  await page.getByText("К импорту: 1", { exact: true }).waitFor();
  await addPage(page, "https://confluence.example/spaces/KBR/pages/5678/Page");
  await page.getByText("К импорту: 2", { exact: true }).waitFor();
  await addPage(page, "невалидный ID");
  await page.getByRole("alert").waitFor();
  assert.equal(await page.locator(".selected-pages li").count(), 2);
  await page.getByRole("button", { name: "Убрать страницу 1234 из выбора" }).click();
  assert.equal(await page.locator(".selected-pages li").count(), 1);
  await page.getByRole("button", { name: "Снять выбор", exact: true }).click();
  assert.equal(await page.locator(".selected-pages li").count(), 0);
}));

test("Confluence batches 11 selected pages into 10 + 1 and reports progress", async () => scenario(async (page, _, state) => {
  await openConfluence(page);
  for (let i = 1; i <= 11; i++) await addPage(page, String(i));
  await page.getByRole("button", { name: "Добавить в workspace" }).click();
  await page.getByText("Передано на проверку: 11 из 11", { exact: true }).waitFor();
  assert.deepEqual(state.imports.map(item => item.page_ids.length), [10, 1]);
  assert.equal(await page.locator(".selected-pages li").count(), 0);
}));

test("failed batch preserves remaining selections and successful batch progress", async () => scenario(async (page, _, state) => {
  await openConfluence(page);
  for (let i = 1; i <= 11; i++) await addPage(page, String(i));
  await page.getByRole("button", { name: "Добавить в workspace" }).click();
  await page.getByRole("alert").getByText("Тестовый сбой пакета", { exact: true }).waitFor();
  await page.getByText("Передано на проверку: 10 из 11", { exact: true }).waitFor();
  assert.equal(await page.locator(".selected-pages li").count(), 1);
  assert.equal(state.imports.length, 2);
}, { failImportBatch: 2 }));

test("leaving Confluence stops polling and does not submit subsequent batches", async () => scenario(async (page, _, state) => {
  await openConfluence(page);
  await addPage(page, "1234");
  const operationResponse = page.waitForResponse(response => response.url().includes("/operations/"));
  await page.getByRole("button", { name: "Добавить в workspace" }).click();
  await operationResponse;
  await page.getByRole("navigation").getByRole("button", { name: "Чат", exact: true }).click();
  const polls = state.importPolls();
  await new Promise(resolve => setTimeout(resolve, 1400));
  assert.equal(state.importPolls(), polls);
  assert.equal(state.imports.length, 1);
} , { importPending: true }));

test("mobile exposes workspaces/history, document status and has no page overflow", async () => scenario(async (page) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const toggle = page.getByRole("button", { name: /Область и диалоги/ });
  await toggle.click();
  await page.getByRole("combobox", { name: "Рабочая область", exact: true }).waitFor();
  await page.getByRole("button", { name: "+ Новый", exact: true }).click();
  await page.waitForURL("**/#chat");
  await page.locator("#workspace-sidebar").waitFor({ state: "hidden" });
  await page.getByRole("navigation").getByRole("button", { name: "Документы", exact: true }).click();
  assert.equal(await page.locator(".resource-document .badge").isVisible(), true);
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await mkdir(new URL("../../.storage/ui-review/", import.meta.url), { recursive: true });
  await page.locator(".resource-document").scrollIntoViewIfNeeded();
  await page.screenshot({ path: fileURLToPath(new URL("../../.storage/ui-review/documents-mobile.png", import.meta.url)) });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openConfluence(page);
  await addPage(page, "1234");
  await page.screenshot({ path: fileURLToPath(new URL("../../.storage/ui-review/confluence-desktop.png", import.meta.url)) });
  for (const width of [320, 390, 768, 1280]) {
    await page.setViewportSize({ width, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Confluence page overflow at ${width}px`);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: fileURLToPath(new URL("../../.storage/ui-review/confluence-mobile.png", import.meta.url)) });
}, { documents: [documentRecord("long", { title: "Очень длинное название документа для проверки переноса и доступности статуса на телефоне", status: "FAILED" })] }));

test("late document response cannot overwrite the newly selected workspace", async () => scenario(async (page) => {
  await page.getByRole("navigation").getByRole("button", { name: "Документы", exact: true }).click();
  let release;
  let arrived;
  const gate = new Promise(resolve => { release = resolve; });
  const requestStarted = new Promise(resolve => { arrived = resolve; });
  await page.route("**/workspaces/workspace-test/documents", async route => {
    arrived();
    await gate;
    await route.fulfill({ contentType: "application/json", body: JSON.stringify([documentRecord("stale", { title: "Устаревший документ" })]) });
  });
  try {
    await requestStarted; // Wait for the next document-list refresh.
    await page.getByRole("combobox", { name: "Рабочая область", exact: true }).selectOption("workspace-second");
    await page.getByText("Документ второй области", { exact: true }).waitFor();
    const response = page.waitForResponse(res => res.url().endsWith("/workspace-test/documents"));
    release();
    await response;
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(await page.getByText("Устаревший документ", { exact: true }).count(), 0);
    assert.equal(await page.getByText("Документ второй области", { exact: true }).count(), 1);
  } finally { release(); }
}));

test("late conversation response cannot populate a newly created dialog", async () => scenario(async (page) => {
  let release;
  let arrived;
  const gate = new Promise(resolve => { release = resolve; });
  const requestStarted = new Promise(resolve => { arrived = resolve; });
  await page.route("**/conversations/other", async route => {
    arrived();
    await gate;
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      ...conversation("other", "Второй диалог"),
      messages: [{ id: "stale", role: "user", content: "Устаревшее сообщение", metadata: {}, created_at: now }],
    }) });
  });
  try {
    await page.locator(".chat-history-select").filter({ hasText: "Второй диалог" }).click();
    await requestStarted;
    await page.getByRole("button", { name: "+ Новый", exact: true }).click();
    await page.locator(".chat-head").getByText("Новый диалог", { exact: true }).waitFor();
    const response = page.waitForResponse(res => res.url().endsWith("/conversations/other"));
    release();
    await response;
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(await page.getByText("Устаревшее сообщение", { exact: true }).count(), 0);
  } finally { release(); }
}));

test("changing workspace resets Confluence selection", async () => scenario(async (page) => {
  await openConfluence(page);
  await addPage(page, "1234");
  await page.getByText("К импорту: 1", { exact: true }).waitFor();
  await page.getByRole("combobox", { name: "Рабочая область", exact: true }).selectOption("workspace-second");
  await page.getByText("Коннектор в сети", { exact: true }).waitFor();
  assert.equal(await page.locator(".selected-pages li").count(), 0);
}));

test("late versions from the previous knowledge base do not replace current versions", async () => scenario(async (page) => {
  let release;
  let arrived;
  let finished;
  const gate = new Promise(resolve => { release = resolve; });
  const requestStarted = new Promise(resolve => { arrived = resolve; });
  const requestFinished = new Promise(resolve => { finished = resolve; });
  await page.route("**/knowledge-bases/alpha/versions", async route => {
    arrived();
    await gate;
    try { await route.fulfill({ contentType: "application/json", body: JSON.stringify(kbVersions("alpha")) }); }
    finally { finished(); }
  });
  try {
    await page.getByRole("navigation").getByRole("button", { name: "Базы знаний", exact: true }).click();
    await requestStarted;
    await page.getByRole("combobox", { name: "Управление базой знаний" }).selectOption("beta");
    await page.getByText("Файл базы beta.pdf", { exact: true }).waitFor();
    release();
    await requestFinished;
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(await page.getByRole("combobox", { name: "Версия базы знаний" }).inputValue(), "beta-v1");
    assert.equal(await page.getByText("Файл базы alpha.pdf", { exact: true }).count(), 0);
  } finally { release(); }
}, { withKnowledgeBases: true }));

test("late knowledge-base document refresh is cancelled on selection change", async () => scenario(async (page) => {
  await page.getByRole("navigation").getByRole("button", { name: "Базы знаний", exact: true }).click();
  await page.getByText("Файл базы alpha.pdf", { exact: true }).waitFor();
  let release;
  let arrived;
  let finished;
  const gate = new Promise(resolve => { release = resolve; });
  const requestStarted = new Promise(resolve => { arrived = resolve; });
  const requestFinished = new Promise(resolve => { finished = resolve; });
  await page.route("**/knowledge-bases/alpha/versions/alpha-v1/documents", async route => {
    arrived();
    await gate;
    try { await route.fulfill({ contentType: "application/json", body: JSON.stringify([documentRecord("stale", { filename: "Устаревший файл базы.pdf" })]) }); }
    finally { finished(); }
  });
  try {
    await requestStarted;
    await page.getByRole("combobox", { name: "Управление базой знаний" }).selectOption("beta");
    await page.getByText("Файл базы beta.pdf", { exact: true }).waitFor();
    release();
    await requestFinished;
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(await page.getByText("Устаревший файл базы.pdf", { exact: true }).count(), 0);
    assert.equal(await page.getByRole("combobox", { name: "Версия базы знаний" }).inputValue(), "beta-v1");
  } finally { release(); }
}, { withKnowledgeBases: true }));
