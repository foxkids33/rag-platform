import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "../../apps/web/node_modules/vite/dist/node/index.js";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const files = new Map();
let browser;

before(async () => {
  const bundle = await build({
    root: fileURLToPath(new URL("../../apps/web/", import.meta.url)),
    build: { write: false }, logLevel: "silent",
  });
  for (const entry of bundle.output) files.set("/" + entry.fileName, entry.code ?? entry.source);
  browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
});
after(async () => { await browser?.close(); });

for (const isAdmin of [false, true]) {
  test(`personal Confluence uses only the session token (admin=${isAdmin})`, async () => {
    const page = await browser.newPage();
    page.setDefaultTimeout(5000);
    const unexpected = [];
    let connected = false;
    let offline = false;
    const confluence = "/api/v1/integrations/confluence/personal/connection";
    const workspace = "/api/v1/workspaces/mine";
    const personal = workspace + "/integrations/confluence/personal/";
    const pageInfo = { id: "101", title: "Моя страница", space_key: "~my", version: "1", has_children: false };
    const token = "test-user-token";
    const tokenUrl = `https://wiki.example${isAdmin ? "/wiki" : ""}/plugins/personalaccesstokens/usertokens.action`;
    let imported = false;
    await page.route("**/*", async (route) => {
      const req = route.request();
      const path = new URL(req.url()).pathname;
      const method = req.method();
      const reply = (data, status = 200) => route.fulfill({ status, json: data });
      if (path === "/runtime-config.js") return route.fulfill({
        contentType: "application/javascript",
        body: 'window.__RAG_RUNTIME_CONFIG__ = {authEnabled:true,authMode:"password"};',
      });
      if (!path.startsWith("/api/")) {
        const key = path === "/" ? "/index.html" : path;
        if (!files.has(key)) return route.fulfill({ status: 404 });
        return route.fulfill({ body: files.get(key), contentType: key.endsWith(".css") ? "text/css" :
          key.endsWith(".html") ? "text/html" : "application/javascript" });
      }
      if (path === confluence) {
        if (method === "POST") {
          assert.deepEqual(req.postDataJSON(), { token });
          assert.equal(req.headers()["x-csrf-token"], "test-csrf");
          connected = true;
        }
        if (method === "DELETE") {
          connected = false;
          return route.fulfill({ status: 204 });
        }
        return reply({ mode: "personal", enabled: true, ready: connected, host: "wiki.example", personal_token_url: tokenUrl,
          allowed_spaces: [], capabilities: ["LIST_SPACE_PAGES"], max_pages_per_job: 50 });
      }
      assert.ok(!req.url().includes(token) && !req.postData()?.includes(token));
      if (path === "/api/v1/auth/me") return reply({ subject: "me", tenant_id: "team", username: "test",
        roles: [], csrf_token: "test-csrf", is_admin: isAdmin });
      if (path === "/api/v1/health") return reply({ status: "ok" });
      if (path === "/api/v1/workspaces") return reply([{ id: "mine", name: "Моя область", user_id: "me",
        tenant_id: "team", source_mode: "USER_DOCUMENTS", document_count: 0, searchable_document_count: 0 }]);
      if (["/api/v1/knowledge-bases", workspace + "/documents", workspace + "/conversations"].includes(path)) return reply([]);
      if (connected && method === "POST" && path === personal + "spaces") return offline
        ? reply({ detail: "Не удалось прочитать Confluence через серверный VPN." }, 502)
        : reply({ spaces: [{ key: "~my", name: "Моё пространство" }] });
      if (connected && method === "POST" && path === personal + "children") return reply({ pages: [pageInfo] });
      if (connected && method === "POST" && path === personal + "imports") {
        assert.deepEqual(req.postDataJSON().page_ids, ["101"]);
        imported = true;
        return reply({ requested: 1, queued: 1, skipped: 0, failed: 0, results: [{ page_id: "101", status: "queued" }] }, 202);
      }
      unexpected.push(method + " " + path);
      return reply({}, 404);
    });
    try {
      await page.goto("https://rag.test/#confluence");
      const tokenLink = page.getByRole("link", { name: "Откройте личные токены в Confluence" });
      await tokenLink.waitFor();
      assert.equal(await tokenLink.getAttribute("href"), tokenUrl);
      assert.equal(await tokenLink.getAttribute("target"), "_blank");
      assert.equal(await tokenLink.getAttribute("rel"), "noopener noreferrer");
      assert.equal(await page.locator("#personal-token-steps > li").count(), 3);
      await page.getByLabel("Личный токен доступа Confluence").fill(token);
      assert.equal(await page.getByRole("option", { name: "Общий коннектор администратора" }).count(), isAdmin ? 1 : 0);
      await page.getByRole("button", { name: "Подключить", exact: true }).click();
      offline = true;
      await page.getByRole("button", { name: "Показать пространства" }).click();
      await page.getByText("Не удалось прочитать Confluence через серверный VPN.").waitFor();
      offline = false;
      await page.getByRole("button", { name: "Показать пространства" }).click();
      await page.getByRole("button", { name: /Моё пространство/ }).click();
      await page.getByRole("checkbox", { name: /Моя страница/ }).check();
      await page.getByRole("button", { name: "Добавить в workspace" }).click();
      await page.getByText("в очереди", { exact: false }).first().waitFor();
      assert.ok(imported);
      await page.reload();
      await page.getByRole("button", { name: "Отключить" }).click();
      await page.getByLabel("Личный токен доступа Confluence").waitFor();
      assert.equal(connected, false);
      assert.deepEqual(unexpected, []);
    } finally {
      await page.close();
    }
  });
}
