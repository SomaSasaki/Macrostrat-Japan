import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [inputArg, outputDirArg] = process.argv.slice(2);
if (!inputArg || !outputDirArg) {
  throw new Error("Usage: qa_review_v2.mjs <review-v2.xlsx> <output-dir>");
}

const inputPath = path.resolve(inputArg);
const outputDir = path.resolve(outputDirArg);
await fs.mkdir(outputDir, { recursive: true });

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const renderSpecs = [
  ["Review", "A1:AA24", 0.72, "Review.png"],
  ["Columns", "A1:J52", 1.0, "Columns.png"],
  ["Evidence", "A1:E80", 0.9, "Evidence_first.png"],
  ["Evidence", "A321:E440", 0.9, "Evidence_last.png"],
  ["Project", "A1:J80", 1.0, "Project.png"],
];

const rendered = [];
for (const [sheetName, range, scale, fileName] of renderSpecs) {
  try {
    const image = await workbook.render({ sheetName, range, scale, format: "png" });
    const outputPath = path.join(outputDir, fileName);
    await fs.writeFile(outputPath, new Uint8Array(await image.arrayBuffer()));
    rendered.push({ sheetName, range, outputPath });
  } catch (error) {
    rendered.push({ sheetName, range, error: error.message });
  }
}

const topology = await workbook.inspect({
  kind: "workbook,sheet,table,drawing",
  maxChars: 16000,
  tableMaxRows: 5,
  tableMaxCols: 15,
  tableMaxCellChars: 120,
});
const formulas = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "Review v2 formula error scan",
});
const review = await workbook.inspect({
  kind: "table",
  range: "Review!A1:AA24",
  include: "values,formulas",
  tableMaxRows: 24,
  tableMaxCols: 27,
  tableMaxCellChars: 100,
  maxChars: 26000,
});

const inspectionPath = path.join(outputDir, "inspection.ndjson");
await fs.writeFile(
  inspectionPath,
  [topology.ndjson, formulas.ndjson, review.ndjson].filter(Boolean).join("\n") + "\n",
  "utf8",
);
console.log(JSON.stringify({ inputPath, outputDir, rendered, inspectionPath }));
