// Narrow, reproducible rubric revision. Never overwrite v2 or existing reports.
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";

const [input, output] = process.argv.slice(2);
if (!input || !output || process.argv.length !== 4) {
  throw new Error("Usage: node scripts/evaluation/prepare-kbr-v3.mjs INPUT.jsonl NEW-OUTPUT.jsonl");
}
const raw = await readFile(input);
const sha = createHash("sha256").update(raw).digest("hex");
if (sha !== "caae95822112223f2a0d32910c72a6175443b06711895274b020273357d05b3f") {
  throw new Error("Unexpected v2 dataset hash; review the rubric changes before applying them.");
}
const cases = raw.toString("utf8").trim().split(/\r?\n/).map(line => JSON.parse(line));
const changed = [];
for (const item of cases) {
  if (["kbr-017", "kbr-018"].includes(item.id)) {
    // Only the requested command excerpts were reviewed, not whole-page equivalence.
    item.accepted_citation_filenames = ["copy_ Диагностика TLS в ПАК МХД.О-189210517.confluence.json"];
    item.notes += " Rubric v3: reviewed equivalent command excerpt in TLS copy 189210517 v1; citation alternative only, retrieval gold unchanged.";
    changed.push(item.id);
  }
  if (item.id === "kbr-022") {
    item.required_facts = item.required_facts.map(fact => ({
      "5": "5||пять", "2": "2||два||две", "3": "3||три",
    })[fact] ?? fact);
    item.notes += " Rubric v3: explicit Russian cardinal aliases for node counts; versions, ports and commands remain exact.";
    changed.push(item.id);
  }
}
if (changed.join(",") !== "kbr-017,kbr-018,kbr-022") throw new Error("Unexpected case identities");
const result = cases.map(item => JSON.stringify(item)).join("\n") + "\n";
await writeFile(output, result, { flag: "wx", mode: 0o600 });
console.log(JSON.stringify({ output, cases: cases.length, changed,
  sha256: createHash("sha256").update(result).digest("hex") }, null, 2));
