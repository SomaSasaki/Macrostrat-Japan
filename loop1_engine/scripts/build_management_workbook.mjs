#!/usr/bin/env node
/**
 * 全国50k管理表を @oai/artifact-tool で生成する。
 *
 * 再生成時は既存の「50k管理表」から手動4列を map_id で引き継ぐ。
 * 使い方:
 *   node scripts/build_management_workbook.mjs [inventory.json] [output.xlsx] [preview-dir]
 */

import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";


async function loadArtifactTool() {
  try {
    return await import("@oai/artifact-tool");
  } catch (firstError) {
    const modulesRoot = process.env.ARTIFACT_TOOL_NODE_MODULES;
    if (!modulesRoot) throw firstError;
    const packageRoot = path.join(modulesRoot, "@oai", "artifact-tool");
    const packageJson = JSON.parse(await fs.readFile(path.join(packageRoot, "package.json"), "utf8"));
    const entry = typeof packageJson.exports?.["."] === "string"
      ? packageJson.exports["."]
      : packageJson.module || packageJson.main;
    return await import(pathToFileURL(path.join(packageRoot, entry)).href);
  }
}


const { FileBlob, SpreadsheetFile, Workbook } = await loadArtifactTool();

const root = process.cwd();
const inventoryPath = path.resolve(process.argv[2] || path.join(root, "data", "00_management", "gsj_50k_inventory.json"));
const outputPath = path.resolve(process.argv[3] || path.join(root, "data", "00_management", "GSJ_50k_全国管理表.xlsx"));
const previewDir = path.resolve(process.argv[4] || path.join(root, "data", "00_management", "previews"));
const inventory = JSON.parse(await fs.readFile(inventoryPath, "utf8"));
const rows = inventory.maps || [];

const colors = {
  navy: "#12324A",
  teal: "#0D7377",
  green: "#2A9D8F",
  paleGreen: "#DFF3EA",
  blue: "#2878B5",
  paleBlue: "#E7F1F8",
  gold: "#E9B949",
  paleGold: "#FFF4D6",
  red: "#C94C4C",
  paleRed: "#FBE4E4",
  ink: "#24333D",
  gray: "#65757F",
  paleGray: "#F3F6F7",
  line: "#D5DEE3",
  white: "#FFFFFF",
};

const manualHeaders = ["優先度(手動)", "担当", "手動状態", "メモ"];


async function readExistingManualValues(xlsxPath) {
  const valuesById = new Map();
  try {
    await fs.access(xlsxPath);
    const oldBook = await SpreadsheetFile.importXlsx(await FileBlob.load(xlsxPath));
    const oldSheet = oldBook.worksheets.getItem("50k管理表");
    const values = oldSheet.getUsedRange(true).values || [];
    const headerRowIndex = values.findIndex((row) => row?.some((value) => String(value ?? "") === "map_id"));
    if (headerRowIndex < 0) return valuesById;
    const header = values[headerRowIndex].map((value) => String(value ?? ""));
    const idIndex = header.indexOf("map_id");
    const manualIndexes = manualHeaders.map((name) => header.indexOf(name));
    for (const row of values.slice(headerRowIndex + 1)) {
      const id = String(row?.[idIndex] ?? "").trim();
      if (!id) continue;
      const manual = manualIndexes.map((index) => index >= 0 ? row?.[index] ?? "" : "");
      if (manual.some((value) => value !== "" && value != null)) valuesById.set(id, manual);
    }
  } catch (error) {
    if (error?.code !== "ENOENT") console.warn(`既存手動列の読取りをスキップ: ${error.message}`);
  }
  return valuesById;
}


function colLetter(index1) {
  let value = index1;
  let out = "";
  while (value > 0) {
    value -= 1;
    out = String.fromCharCode(65 + (value % 26)) + out;
    value = Math.floor(value / 26);
  }
  return out;
}


function truth(value) {
  return value ? "あり" : "なし";
}


const previousManual = await readExistingManualValues(outputPath);
const workbook = Workbook.create();
const dashboard = workbook.worksheets.add("ダッシュボード");
const management = workbook.worksheets.add("50k管理表");
const raw = workbook.worksheets.add("raw_inventory");
const rules = workbook.worksheets.add("出典ルール");
const history = workbook.worksheets.add("更新履歴");

for (const sheet of [dashboard, management, raw, rules, history]) sheet.showGridLines = false;


// ---------------------------------------------------------------------------
// 50k管理表
// ---------------------------------------------------------------------------
const managementHeaders = [
  "map_id", "図幅名(和)", "図幅名(英)", "図幅コード", "地域", "刊行年",
  "ZFK", "Shape", "PDF", "Viewer画像", "属性表", "情報源組合せ", "推奨経路", "確信度上限",
  "LLM要否", "作業状態", "レビュー層数", "unit_name入力", "lithology入力",
  "sort入力", "競合件数", "次アクション", "review_path", "submission_path",
  "shape_url", "pdf_url", ...manualHeaders,
];
const managementRows = rows.map((row) => [
  String(row.map_id), row.title_ja || "", row.title_en || "", row.sheet_code || "",
  row.region_folder || "", row.pub_year || "", truth(row.zfk_available),
  truth(row.shape_available), truth(row.pdf_available),
  truth(row.viewer_available || row.legend_image_available || row.map_image_available),
  truth(row.attribute_available),
  row.source_combination || "", row.recommended_route || "", row.confidence_ceiling || "",
  row.requires_llm ? "必要" : "不要", row.work_status || "", Number(row.review_units || 0),
  Number(row.unit_name_filled || 0), Number(row.lithology_filled || 0),
  Number(row.sort_order_filled || 0), Number(row.conflict_count || 0), row.next_action || "",
  row.review_path || "", row.submission_path || "", row.shape_url || "", row.pdf_url || "",
  ...(previousManual.get(String(row.map_id)) || ["", "", "", ""]),
]);
const managementEndRow = 5 + managementRows.length;
const managementEndCol = colLetter(managementHeaders.length);

management.mergeCells(`A1:${managementEndCol}2`);
management.getRange("A1").values = [["GSJ 1:50,000 全国管理表"]];
management.getRange("A1").format = {
  fill: colors.navy, font: { bold: true, color: colors.white, size: 20 },
  horizontalAlignment: "left", verticalAlignment: "center",
};
management.mergeCells(`A3:${managementEndCol}3`);
management.getRange("A3").values = [[
  "自動列は data/00_management/gsj_50k_inventory.json 由来。右端4列は手動編集でき、再生成時も map_id で保持されます。",
]];
management.getRange("A3").format = {
  fill: colors.paleBlue, font: { color: colors.ink, italic: true }, wrapText: true,
  verticalAlignment: "center",
};
management.getRange(`A5:${managementEndCol}5`).values = [managementHeaders];
management.getRange(`A6:${managementEndCol}${managementEndRow}`).values = managementRows;

management.getRange(`A5:${managementEndCol}${managementEndRow}`).format = {
  font: { name: "Aptos", size: 10, color: colors.ink },
  verticalAlignment: "center",
};
management.getRange(`A5:${managementEndCol}5`).format = {
  fill: colors.teal, font: { bold: true, color: colors.white, size: 10 },
  horizontalAlignment: "center", verticalAlignment: "center", wrapText: true,
  borders: { preset: "outside", style: "thin", color: colors.teal },
};
management.getRange(`AA5:${managementEndCol}${managementEndRow}`).format.fill = colors.paleGold;
management.getRange(`AA5:${managementEndCol}5`).format = {
  fill: colors.gold, font: { bold: true, color: colors.ink },
  horizontalAlignment: "center", verticalAlignment: "center", wrapText: true,
};
management.getRange(`A6:${managementEndCol}${managementEndRow}`).format.rowHeight = 21;
management.getRange("A1").format.rowHeight = 34;
management.getRange("A3").format.rowHeight = 30;
management.getRange(`A5:${managementEndCol}5`).format.rowHeight = 32;
management.freezePanes.freezeRows(5);
management.freezePanes.freezeColumns(1);

const widths = [10, 31, 20, 12, 16, 10, 9, 9, 9, 11, 9, 20, 27, 12, 10, 13, 12, 13, 13, 10, 10, 31, 20, 20, 18, 18, 14, 14, 14, 38];
widths.forEach((width, index) => {
  management.getRange(`${colLetter(index + 1)}5:${colLetter(index + 1)}${managementEndRow}`).format.columnWidth = width;
});
management.getRange(`B6:C${managementEndRow}`).format.wrapText = false;
management.getRange(`L6:P${managementEndRow}`).format.wrapText = true;
management.getRange(`V6:AD${managementEndRow}`).format.wrapText = false;
management.getRange(`Q6:U${managementEndRow}`).format.numberFormat = "0";

const managementTable = management.tables.add(`A5:${managementEndCol}${managementEndRow}`, true, "National50kTable");
managementTable.style = "TableStyleMedium2";
managementTable.showFilterButton = true;
management.getRange(`AA6:AA${managementEndRow}`).dataValidation = {
  rule: { type: "list", values: ["高", "中", "低", "保留"] },
};
management.getRange(`AC6:AC${managementEndRow}`).dataValidation = {
  rule: { type: "list", values: ["未着手", "作業中", "確認待ち", "完了", "保留"] },
};

management.getRange(`N6:N${managementEndRow}`).conditionalFormats.add("containsText", {
  text: "A", format: { fill: colors.paleGreen, font: { color: "#176044", bold: true } },
});
management.getRange(`N6:N${managementEndRow}`).conditionalFormats.add("containsText", {
  text: "C", format: { fill: colors.paleGold, font: { color: "#795600", bold: true } },
});
management.getRange(`N6:N${managementEndRow}`).conditionalFormats.add("containsText", {
  text: "D", format: { fill: colors.paleRed, font: { color: "#8F2525", bold: true } },
});
management.getRange(`O6:O${managementEndRow}`).conditionalFormats.add("containsText", {
  text: "必要", format: { fill: colors.paleGold, font: { color: "#795600", bold: true } },
});
management.getRange(`P6:P${managementEndRow}`).conditionalFormats.add("containsText", {
  text: "提出済み", format: { fill: colors.paleGreen, font: { color: "#176044", bold: true } },
});
management.getRange(`P6:P${managementEndRow}`).conditionalFormats.add("containsText", {
  text: "レビュー中", format: { fill: colors.paleBlue, font: { color: colors.blue, bold: true } },
});
management.getRange(`U6:U${managementEndRow}`).conditionalFormats.add("cellIs", {
  operator: "greaterThan", formula: 0,
  format: { fill: colors.paleRed, font: { color: "#8F2525", bold: true } },
});


// ---------------------------------------------------------------------------
// ダッシュボード（管理表を参照するライブ数式）
// ---------------------------------------------------------------------------
dashboard.mergeCells("A1:H2");
dashboard.getRange("A1").values = [["全国50kデータ整備ダッシュボード"]];
dashboard.getRange("A1").format = {
  fill: colors.navy, font: { bold: true, color: colors.white, size: 21 },
  horizontalAlignment: "left", verticalAlignment: "center",
};
dashboard.mergeCells("A3:H3");
dashboard.getRange("A3").values = [[`更新: ${inventory.generated_at}　｜　優先順: ZFK → Shape → PDF → Viewer画像（OCR/Vision+目視）`]];
dashboard.getRange("A3").format = { fill: colors.paleBlue, font: { color: colors.gray }, verticalAlignment: "center" };

const dataFirst = 6;
const dataLast = managementEndRow;
const kpis = [
  ["刊行図幅", `=COUNTA('50k管理表'!$A$${dataFirst}:$A$${dataLast})`, colors.paleBlue, colors.blue],
  ["ZFKあり", `=COUNTIF('50k管理表'!$G$${dataFirst}:$G$${dataLast},"あり")`, colors.paleGreen, colors.green],
  ["Shapeあり", `=COUNTIF('50k管理表'!$H$${dataFirst}:$H$${dataLast},"あり")`, colors.paleGreen, colors.green],
  ["PDFあり", `=COUNTIF('50k管理表'!$I$${dataFirst}:$I$${dataLast},"あり")`, colors.paleBlue, colors.blue],
  ["PDFのみ / LLM", `=COUNTIF('50k管理表'!$L$${dataFirst}:$L$${dataLast},"PDF")`, colors.paleGold, colors.gold],
  ["Viewer画像のみ", `=COUNTIF('50k管理表'!$L$${dataFirst}:$L$${dataLast},"ViewerImage")`, colors.paleRed, colors.red],
  ["レビュー中", `=COUNTIF('50k管理表'!$P$${dataFirst}:$P$${dataLast},"レビュー中")`, colors.paleBlue, colors.blue],
  ["提出済み", `=COUNTIF('50k管理表'!$P$${dataFirst}:$P$${dataLast},"提出済み")`, colors.paleGreen, colors.green],
];
for (let i = 0; i < kpis.length; i += 1) {
  const row = 5 + Math.floor(i / 4) * 3;
  const col = 1 + (i % 4) * 2;
  const from = `${colLetter(col)}${row}`;
  const to = `${colLetter(col + 1)}${row}`;
  const valueFrom = `${colLetter(col)}${row + 1}`;
  const valueTo = `${colLetter(col + 1)}${row + 2}`;
  dashboard.mergeCells(`${from}:${to}`);
  dashboard.mergeCells(`${valueFrom}:${valueTo}`);
  dashboard.getRange(from).values = [[kpis[i][0]]];
  dashboard.getRange(valueFrom).formulas = [[kpis[i][1]]];
  dashboard.getRange(from).format = {
    fill: kpis[i][2], font: { bold: true, color: colors.ink },
    horizontalAlignment: "center", verticalAlignment: "center",
    borders: { preset: "outside", style: "thin", color: kpis[i][3] },
  };
  dashboard.getRange(valueFrom).format = {
    fill: colors.white, font: { bold: true, color: kpis[i][3], size: 22 },
    horizontalAlignment: "center", verticalAlignment: "center", numberFormat: "0",
    borders: { preset: "outside", style: "thin", color: kpis[i][3] },
  };
}

dashboard.getRange("A12:B12").values = [["情報源組合せ", "図幅数"]];
const combos = ["ZFK+Shape+PDF", "Shape+PDF", "PDF", "Shape", "ViewerImage"];
dashboard.getRange("A13:A17").values = combos.map((name) => [name]);
dashboard.getRange("B13").formulas = [[`=COUNTIF('50k管理表'!$L$${dataFirst}:$L$${dataLast},A13)`]];
dashboard.getRange("B13:B17").fillDown();

dashboard.getRange("D12:E12").values = [["推奨処理経路", "図幅数"]];
const routes = ["ZFK→Shape検算→PDF補完", "Shape→PDF補完", "PDF→LLM→引用検証", "図幅画像→OCR/Vision→人確認", "50k資料を追加探索"];
dashboard.getRange("D13:D17").values = routes.map((name) => [name]);
dashboard.getRange("E13").formulas = [[`=COUNTIF('50k管理表'!$M$${dataFirst}:$M$${dataLast},D13)`]];
dashboard.getRange("E13:E17").fillDown();

for (const range of ["A12:B12", "D12:E12"]) {
  dashboard.getRange(range).format = { fill: colors.teal, font: { bold: true, color: colors.white }, horizontalAlignment: "center" };
}
dashboard.getRange("A13:B17").format.borders = { preset: "inside", style: "thin", color: colors.line };
dashboard.getRange("D13:E17").format.borders = { preset: "inside", style: "thin", color: colors.line };
dashboard.getRange("B13:B17").format.numberFormat = "0";
dashboard.getRange("E13:E17").format.numberFormat = "0";

dashboard.mergeCells("A20:H20");
dashboard.getRange("A20").values = [["運用判断"]];
dashboard.getRange("A20").format = { fill: colors.navy, font: { bold: true, color: colors.white } };
dashboard.getRange("A21:H25").values = [
  ["1", "ZFK", "名称・年代・本文の第一候補", "A", "機械採用", "Shapeで照合", "PDFは不足項目だけ", ""],
  ["2", "Shape", "geo_A.dbfの地層名・時代・岩相／正確な形状", "A", "機械採用", "ZFKがあれば競合検出", "PDFは不足項目だけ", ""],
  ["3", "PDF", "層厚・堆積環境・基底関係・詳細記載", "C", "LLM候補", "原文引用・ページ必須", "数値・引用を機械検証", ""],
  ["4", "推定", "資料のないセル", "D", "自動採用しない", "人の判断", "空欄を許容", ""],
  ["範囲", "200k", "50kが刊行されていない地理的空白だけを補完", "別系統", "scaleを保持", "刊行済み50kの不足セルには混ぜない", "coverage_tierを記録", ""],
];
dashboard.getRange("A21:H25").format = { wrapText: true, verticalAlignment: "center", font: { size: 10, color: colors.ink } };
dashboard.getRange("A21:A25").format.fill = colors.paleGray;
dashboard.getRange("A21:H25").format.borders = { preset: "inside", style: "thin", color: colors.line };
dashboard.freezePanes.freezeRows(3);
[13, 19, 26, 32, 12, 18, 20, 10].forEach((width, index) => dashboard.getRange(`${colLetter(index + 1)}1:${colLetter(index + 1)}25`).format.columnWidth = width);
dashboard.getRange("A1").format.rowHeight = 34;
dashboard.getRange("A3").format.rowHeight = 26;
dashboard.getRange("A21:H25").format.rowHeight = 33;


// ---------------------------------------------------------------------------
// raw_inventory
// ---------------------------------------------------------------------------
const rawHeaders = inventory.columns || Object.keys(rows[0] || {});
const rawRows = rows.map((row) => rawHeaders.map((header) => row[header] ?? ""));
const rawEndRow = rawRows.length + 1;
const rawEndCol = colLetter(rawHeaders.length);
raw.getRange(`A1:${rawEndCol}1`).values = [rawHeaders];
raw.getRange(`A2:${rawEndCol}${rawEndRow}`).values = rawRows;
raw.getRange(`A1:${rawEndCol}1`).format = {
  fill: colors.navy, font: { bold: true, color: colors.white, size: 9 },
  horizontalAlignment: "center", wrapText: true,
};
raw.getRange(`A1:${rawEndCol}${rawEndRow}`).format.font = { name: "Aptos", size: 9, color: colors.ink };
raw.getRange(`A1:${rawEndCol}1`).format.font = { name: "Aptos", size: 9, bold: true, color: colors.white };
raw.getRange(`A1:${rawEndCol}${rawEndRow}`).format.rowHeight = 18;
raw.getRange(`A1:${rawEndCol}1`).format.rowHeight = 34;
raw.getRange(`A1:${rawEndCol}${rawEndRow}`).format.columnWidth = 15;
for (const index of [2, 3, 6, 20, 21, 29, 30, 31, 32, 33, 34, 35]) {
  if (index <= rawHeaders.length) raw.getRange(`${colLetter(index)}1:${colLetter(index)}${rawEndRow}`).format.columnWidth = 25;
}
raw.freezePanes.freezeRows(1);
raw.freezePanes.freezeColumns(1);
const rawTable = raw.tables.add(`A1:${rawEndCol}${rawEndRow}`, true, "RawInventoryTable");
rawTable.style = "TableStyleMedium2";


// ---------------------------------------------------------------------------
// 出典ルール
// ---------------------------------------------------------------------------
rules.mergeCells("A1:H2");
rules.getRange("A1").values = [["データ採用ルールと確信度"]];
rules.getRange("A1").format = { fill: colors.navy, font: { bold: true, color: colors.white, size: 19 }, verticalAlignment: "center" };
rules.getRange("A4:H4").values = [["対象フィールド", "第一候補", "第二候補", "第三候補", "確信度", "自動採用", "検証", "備考"]];
rules.getRange("A5:H10").values = [
  ["地層名・凡例年代", "ZFK", "geo_A.dbf", "PDF", "A/A/C", "Aは可", "major_code・原文", "情報源の優先順位を固定"],
  ["岩相", "ZFK構造化", "geo_A.dbf", "PDF", "A/A/C", "候補語彙照合後", "Macrostrat語彙", "主/副岩相は別判定"],
  ["地理形状・代表位置", "Shapefile", "ZFK GeoJSON", "PDF図版", "A/A/C", "Shape可", "CRS・bbox", "ZFK GeoJSONは代表形状"],
  ["層厚", "構造化本文規則", "PDF", "LLM", "B/B/C", "数値規則のみ", "単位・場所・引用", "場所差を1値に潰さない"],
  ["堆積環境・基底関係", "構造化本文規則", "PDF", "LLM", "B/B/C", "語彙一致のみ", "引用・ページ", "不明は空欄"],
  ["層序順", "凡例順/major_code", "PDF凡例", "LLM", "B/B/D", "初期候補", "上下関係を検査", "同時異相を許容"],
];
rules.getRange("A13:H13").values = [["クラス", "定義", "例", "自動採用", "人の確認", "必要な証拠", "出力", ""]];
rules.getRange("A14:H17").values = [
  ["A", "GSJネイティブ構造化値", "ZFK / DBF / SHP", "可", "競合時のみ", "URL・レコード・コード", "採用値+出典", ""],
  ["B", "構造化本文の決定的規則抽出", "数値+単位の抽出", "検証通過時", "例外時", "原文・ページ", "採用値+引用", ""],
  ["C", "LLM候補+引用照合", "PDF説明文", "条件付き", "要確認", "原文引用・ページ・数値", "候補値+引用", ""],
  ["D", "推定・解釈", "資料外の補完", "不可", "必須", "判断理由", "空欄または保留", ""],
];
for (const range of ["A4:H4", "A13:H13"]) rules.getRange(range).format = { fill: colors.teal, font: { bold: true, color: colors.white }, wrapText: true };
rules.getRange("A5:H10").format = { wrapText: true, verticalAlignment: "center", borders: { preset: "inside", style: "thin", color: colors.line } };
rules.getRange("A14:H17").format = { wrapText: true, verticalAlignment: "center", borders: { preset: "inside", style: "thin", color: colors.line } };
rules.getRange("A14:A17").format = { font: { bold: true }, horizontalAlignment: "center" };
rules.getRange("A14").format.fill = colors.paleGreen;
rules.getRange("A15").format.fill = colors.paleBlue;
rules.getRange("A16").format.fill = colors.paleGold;
rules.getRange("A17").format.fill = colors.paleRed;
[23, 25, 21, 18, 13, 20, 28, 18].forEach((width, index) => rules.getRange(`${colLetter(index + 1)}1:${colLetter(index + 1)}17`).format.columnWidth = width);
rules.getRange("A5:H10").format.rowHeight = 38;
rules.getRange("A14:H17").format.rowHeight = 35;
rules.freezePanes.freezeRows(4);


// ---------------------------------------------------------------------------
// 更新履歴
// ---------------------------------------------------------------------------
history.mergeCells("A1:D2");
history.getRange("A1").values = [["生成情報・再現方法"]];
history.getRange("A1").format = { fill: colors.navy, font: { bold: true, color: colors.white, size: 18 }, verticalAlignment: "center" };
const cachedCount = rows.filter((row) => row.publication_status === "cached").length;
history.getRange("A4:D12").values = [
  ["項目", "値", "意味", "更新方法"],
  ["generated_at", inventory.generated_at || "", "全国インベントリ生成日時", "python run.py inventory"],
  ["total_maps", Number(inventory.total_maps || rows.length), "50k図幅索引の総数", "python run.py index / inventory"],
  ["publication_cached", cachedCount, "GSJ出版物APIを取得済みの図幅", "python run.py inventory --refresh"],
  ["publication_api", inventory.publication_api || "", "PDF/Shape/Viewer画像の公開状況", "未取得分のみ再開可能"],
  ["zfk_source", inventory.zfk_source || "", "ZFK索引の取得元", "python run.py index"],
  ["inventory_json", path.relative(root, inventoryPath), "Excelの自動列の原データ", "管理表生成前に更新"],
  ["manual_columns", manualHeaders.join(" / "), "Excelで編集してよい列", "再生成時もmap_idで保持"],
  ["usage_policy", "Codexは開発・例外調査のみ", "全国バッチはローカルPython", "PDF/Viewer画像経路だけLLM/Vision"],
];
history.getRange("A4:D4").format = { fill: colors.teal, font: { bold: true, color: colors.white }, horizontalAlignment: "center" };
history.getRange("A5:D12").format = { wrapText: true, verticalAlignment: "center", borders: { preset: "inside", style: "thin", color: colors.line } };
history.getRange("B5").format.numberFormat = "yyyy-mm-dd hh:mm:ss";
[22, 48, 36, 34].forEach((width, index) => history.getRange(`${colLetter(index + 1)}1:${colLetter(index + 1)}12`).format.columnWidth = width);
history.getRange("A5:D12").format.rowHeight = 32;


// ---------------------------------------------------------------------------
// QA, preview, export
// ---------------------------------------------------------------------------
const summary = await workbook.inspect({
  kind: "sheet", include: "id,name", maxChars: 3000,
});
const formulaErrors = await workbook.inspect({
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, matchCase: false, matchEntireCell: false, maxResults: 50 },
  maxChars: 4000,
});
console.log(summary.ndjson);
console.log(`FORMULA_ERROR_SCAN\n${formulaErrors.ndjson || "(none)"}`);

await fs.mkdir(previewDir, { recursive: true });
const previews = [
  ["ダッシュボード", "A1:H25", "dashboard.png"],
  ["50k管理表", `A1:${managementEndCol}25`, "management_top.png"],
  ["raw_inventory", `A1:${rawEndCol}15`, "raw_top.png"],
  ["出典ルール", "A1:H17", "source_rules.png"],
  ["更新履歴", "A1:D12", "history.png"],
];
for (const [sheetName, range, filename] of previews) {
  const preview = await workbook.render({ sheetName, range, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(path.join(previewDir, filename), new Uint8Array(await preview.arrayBuffer()));
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);
console.log(`SAVED ${outputPath}`);
console.log(`PREVIEWS ${previewDir}`);
