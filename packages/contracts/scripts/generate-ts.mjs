// Генерация TypeScript-типов из JSON Schema контрактов (ADR-0005).
// Использование: node scripts/generate-ts.mjs <out_dir>
//
// Все схемы компилируются одним проходом через сборную корневую схему: кросс-файловые $ref
// (events → components → user_input и т.д.) разрешаются в один набор типов без дублей.
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { compile } from "json-schema-to-typescript";

const schemasDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../schemas");
const outDir = process.argv[2];
if (!outDir) {
  console.error("usage: generate-ts.mjs <out_dir>");
  process.exit(1);
}

const files = (await readdir(schemasDir)).filter((f) => f.endsWith(".schema.json")).sort();

// Каждая схема даёт свой корневой тип (если есть) и все $defs.
const defs = {};
for (const file of files) {
  const schema = JSON.parse(await readFile(path.join(schemasDir, file), "utf8"));
  if (schema.title) {
    defs[schema.title] = { $ref: file };
  }
  for (const name of Object.keys(schema.$defs ?? {})) {
    defs[name] = { $ref: `${file}#/$defs/${name}` };
  }
}

const root = { title: "Contracts", $defs: defs };
const ts = await compile(root, "Contracts", {
  cwd: schemasDir,
  bannerComment: [
    "/* eslint-disable */",
    "// Сгенерировано из packages/contracts/schemas — не редактировать, запустить `make contracts`.",
  ].join("\n"),
  unreachableDefinitions: true,
  additionalProperties: false,
  style: { singleQuote: false, semi: true, trailingComma: "all", printWidth: 100 },
});

await mkdir(outDir, { recursive: true });
await writeFile(path.join(outDir, "index.ts"), ts);
