import fs from "node:fs/promises";
import path from "node:path";
import { createHash } from "node:crypto";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [inputArg, outputArg, mapArg, mapJsonArg, kmlArg, evidenceJsonArg] = process.argv.slice(2);
if (!inputArg || !outputArg) {
  throw new Error(
    "Usage: build_review_v2.mjs <review-input.xlsx|json> <output.xlsx> "
    + "[column-map.png] [column-map.json] [column-map.kml] [evidence.json]",
  );
}

const inputPath = path.resolve(inputArg);
const outputPath = path.resolve(outputArg);
const OPTIONAL_PATH_SENTINEL = "__MACROSTRAT_NONE__";
const optionalPath = (value) => (
  value && value !== OPTIONAL_PATH_SENTINEL ? path.resolve(value) : null
);
const mapPath = optionalPath(mapArg);
const mapJsonPath = optionalPath(mapJsonArg);
const kmlPath = optionalPath(kmlArg);
const evidenceJsonPath = evidenceJsonArg ? path.resolve(evidenceJsonArg) : null;
const inputSha256 = createHash("sha256").update(await fs.readFile(inputPath)).digest("hex");

const inputIsJson = path.extname(inputPath).toLowerCase() === ".json";
const inputDocument = inputIsJson
  ? JSON.parse(await fs.readFile(inputPath, "utf8"))
  : null;
const legacy = inputIsJson
  ? null
  : await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
let mapMetadata = null;
if (mapJsonPath) {
  try {
    mapMetadata = JSON.parse(await fs.readFile(mapJsonPath, "utf8"));
  } catch (error) {
    console.warn(`Column map metadata was not loaded: ${error.message}`);
  }
}
let evidenceDocument = null;
let compiledDocument = null;
if (evidenceJsonPath) {
  try {
    evidenceDocument = JSON.parse(await fs.readFile(evidenceJsonPath, "utf8"));
  } catch (error) {
    console.warn(`Canonical evidence was not loaded: ${error.message}`);
  }
  try {
    const compiledJsonPath = path.join(path.dirname(evidenceJsonPath), "compiled.json");
    compiledDocument = JSON.parse(await fs.readFile(compiledJsonPath, "utf8"));
  } catch (error) {
    console.warn(`Canonical compiled values were not loaded: ${error.message}`);
  }
}
const canonicalEvidence = Array.isArray(evidenceDocument)
  ? evidenceDocument
  : (evidenceDocument?.evidence ?? []);
const canonicalUnits = Array.isArray(compiledDocument?.units) ? compiledDocument.units : [];

function sheetMatrix(name) {
  if (!legacy) return [];
  try {
    const sheet = legacy.worksheets.getItem(name);
    const range = sheet.getUsedRange(true);
    return range ? range.values : [];
  } catch {
    return [];
  }
}

function inputRows(key, legacySheet) {
  if (inputDocument) {
    const rows = inputDocument[key];
    return Array.isArray(rows) ? rows : [];
  }
  return rowsFromMatrix(sheetMatrix(legacySheet));
}

function rowsFromMatrix(matrix) {
  if (!matrix?.length) return [];
  const headers = matrix[0].map((v) => String(v ?? "").trim());
  return matrix.slice(1)
    .filter((row) => row.some((v) => v !== null && v !== undefined && String(v).trim() !== ""))
    .map((row) => Object.fromEntries(headers.map((h, i) => [h, row[i] ?? null])));
}

const units = inputRows("units", "units_review");
const columns = inputRows("columns", "columns_review");
const refs = inputRows("refs", "refs_review");
const images = inputRows("images", "images_review");
const projectMeta = inputRows("project_meta", "project_meta");
const gsjMeta = inputRows("gsj_meta", "gsj_meta");
const legacyEvidence = inputRows("source_evidence", "source_evidence");

const isBlank = (v) => v === null || v === undefined || String(v).trim() === "";
const textOf = (v) => isBlank(v) ? "" : String(v).trim();
const numOf = (v) => {
  if (isBlank(v)) return null;
  if (typeof v === "number" && Number.isFinite(v)) return v;
  const n = Number(String(v ?? "").replace(/,/g, "").trim());
  return Number.isFinite(n) ? n : null;
};
const clampText = (v, n = 32000) => textOf(v).slice(0, n);
const short = (v, n = 240) => {
  const s = textOf(v).replace(/\s+/g, " ");
  return s.length <= n ? s : `${s.slice(0, n - 3)}...`;
};
const splitColumns = (v) => textOf(v).split(",").map((s) => s.trim()).filter(Boolean);
const splitAligned = (v, count) => {
  if (count <= 0) return [];
  const parts = Array.isArray(v)
    ? [...v]
    : (count > 1 && typeof v === "string" && v.includes(",")
      ? v.split(",").map((item) => item.trim())
      : [v]);
  if (parts.length === count) return parts;
  if (parts.length === 1) return Array(count).fill(parts[0]);
  return Array(count).fill(v);
};

// Blank review cells may receive only candidates that the canonical layer has
// already separated from provenance and marked for human confirmation.
const candidateFields = new Set([
  "unit_name", "strat_name", "environment", "unit_description", "lithology", "minor_lith",
  "min_thickness", "max_thickness", "basal_surface", "lateral_relationship",
  "t_int", "b_int", "t_age_ma", "b_age_ma",
]);
const numericCandidateFields = new Set([
  "min_thickness", "max_thickness", "t_age_ma", "b_age_ma",
]);
const promotedFieldsByRow = units.map(() => []);
for (let index = 0; index < units.length; index += 1) {
  const row = units[index];
  const compiled = canonicalUnits[index];
  if (!compiled || textOf(compiled.unit_id) !== textOf(row.unit_id)) continue;
  const origins = compiled.value_origins ?? {};
  const values = compiled.values ?? {};
  for (const field of candidateFields) {
    if (
      numericCandidateFields.has(field)
      && typeof row[field] === "string"
      && /^[\s,;|/]*$/.test(row[field])
    ) {
      row[field] = null;
    }
    if (!isBlank(row[field]) || origins[field] !== "evidence_candidate" || isBlank(values[field])) continue;
    row[field] = values[field];
    promotedFieldsByRow[index].push(field);
  }
}

function alignedPerColumn(items) {
  if (!items.length) return null;
  if (items.length === 1) return items[0].value ?? null;
  // Keep empty elements: "4, " means west=4 and east=blank.  The exporter
  // consumes these values in exactly the same order as column_id.
  return items.map((x) => x.value ?? "").join(", ");
}

function deriveAutoFields(rows) {
  const memberships = rows.map((r) => splitColumns(r.column_id));
  const allColumns = [...new Set(memberships.flat())];
  const positionByRow = rows.map(() => []);
  const tPosByRow = rows.map(() => []);
  const sectionByRow = rows.map(() => []);

  for (const column of allColumns) {
    const idxs = rows.map((_, i) => i).filter((i) => memberships[i].includes(column));
    const sorts = idxs.map((i) => {
      const aligned = splitAligned(rows[i].sort_order, memberships[i].length);
      return numOf(aligned[memberships[i].indexOf(column)]);
    });
    const validSorts = sorts.filter((v) => v !== null);
    const topSort = validSorts.length ? Math.max(...validSorts) : null;
    const positions = sorts.map((v) => v === null || topSort === null ? null : Math.trunc(topSort - v + 1));
    const validPositions = positions.filter((v) => v !== null);
    const highest = validPositions.length ? Math.max(...validPositions) : null;
    const tPositions = positions.map((p) => {
      if (p === null || highest === null) return null;
      if (p === highest) return highest + 1;
      if (positions.filter((q) => q === p).length >= 2) {
        const above = positions.filter((q) => q !== null && q > p);
        return above.length ? Math.min(...above) : p + 1;
      }
      return null;
    });

    const bounds = idxs.map((i) => [numOf(rows[i].b_age_ma), numOf(rows[i].t_age_ma)]);
    const ages = bounds.flat().filter((v) => v !== null);
    let sections = bounds.map(() => null);
    if (ages.length >= 2 && Math.max(...ages) > Math.min(...ages)) {
      const threshold = Math.max((Math.max(...ages) - Math.min(...ages)) * 0.15, 0.5);
      let current = 1;
      const tentative = bounds.map(([b, t], i) => {
        if (i > 0) {
          const previousBottom = bounds[i - 1][0];
          if (previousBottom !== null && t !== null && (t - previousBottom) > threshold) current += 1;
        }
        return current;
      });
      if (current > 1 && current <= Math.max(1, Math.floor(bounds.length / 2))) sections = tentative;
    }

    idxs.forEach((rowIndex, j) => {
      positionByRow[rowIndex].push({ column, value: positions[j] });
      tPosByRow[rowIndex].push({ column, value: tPositions[j] });
      sectionByRow[rowIndex].push({ column, value: sections[j] });
    });
  }

  return rows.map((_, i) => ({
    position: alignedPerColumn(positionByRow[i]),
    section_id: alignedPerColumn(sectionByRow[i]),
    t_pos: alignedPerColumn(tPosByRow[i]),
  }));
}

const auto = deriveAutoFields(units);

function sourcePrefix(row) {
  const confidence = textOf(row.REF_confidence_class) || (textOf(row.REF_conflict) ? "CHECK" : "B");
  const source = short(row.REF_source, 110) || "source not located";
  return `[${confidence} | ${source}]`;
}

function canonicalRank(record) {
  const selection = { selected: 4, validation: 3, candidate: 2, unselected: 0 }[textOf(record.selection)] ?? 1;
  const confidence = numOf(record.confidence?.score) ?? 0;
  const assertion = textOf(record.assertion) === "explicit" ? 1 : 0;
  const source = { ZFK: 4, Shapefile: 3, SHAPEFILE: 3, Shape: 3, PDF: 2, Vision: 2, LLM: 1 }[textOf(record.source?.type)] ?? 0;
  return source * 1000 + confidence * 100 + assertion * 10 + selection;
}

function canonicalSummary(row, group) {
  const rowColumns = new Set(splitColumns(row.column_id));
  const records = canonicalEvidence.filter((record) => {
    if (textOf(record.unit_id) !== textOf(row.unit_id) || textOf(record.group) !== group) return false;
    const scope = record.scope ?? {};
    const scopeType = textOf(scope.type) || "column_specific";
    if (scopeType === "map_global") return false;
    if (scopeType === "unit_global") return true;
    const evidenceColumns = Array.isArray(scope.column_ids)
      ? scope.column_ids.map(textOf).filter(Boolean)
      : (Array.isArray(record.column_ids) ? record.column_ids.map(textOf).filter(Boolean) : []);
    return !evidenceColumns.length || evidenceColumns.some((column) => rowColumns.has(column));
  })
    .sort((a, b) => canonicalRank(b) - canonicalRank(a));
  if (!records.length) return "";
  const selected = [];
  const fields = new Set();
  for (const record of records) {
    const field = textOf(record.field);
    if (!field || fields.has(field)) continue;
    fields.add(field);
    selected.push(record);
    if (selected.length >= 8) break;
  }
  const lines = selected.map((record) => {
    const source = record.source ?? {};
    const where = [source.type, source.locator].filter((v) => !isBlank(v)).map((v) => short(v, 100)).join(" | ");
    const confidence = textOf(record.confidence?.class) || "?";
    return `${record.conflict ? "CONFLICT " : ""}[${confidence} | ${where}] ${textOf(record.field)}: ${short(record.candidate ?? source.quote, 150)}\nEvidence: ${textOf(record.evidence_id)}`;
  });
  const shapeCount = records.filter((record) => /shape/i.test(textOf(record.source?.type))).length;
  if (shapeCount) lines.push(`Shape validation: ${shapeCount} record(s); see Evidence sheet.`);
  return lines.join("\n");
}

function ageEvidence(row) {
  const canonical = canonicalSummary(row, "age_evidence");
  if (canonical) return canonical;
  const pieces = [row.REF_age_text, row.REF_age_from_abstract].filter((v) => !isBlank(v)).map((v) => short(v, 170));
  return pieces.length ? `${sourcePrefix(row)}\n${pieces.join(" / ")}\nSee Evidence: ${textOf(row.unit_id)}` : "";
}

function contextEvidence(row) {
  const canonical = canonicalSummary(row, "context_evidence");
  if (canonical) return canonical;
  const pieces = [
    row.REF_strat_name,
    row.REF_environment,
    row.REF_lithology_gsj,
    row.REF_lithology,
    row.REF_shape_lith_text,
  ].filter((v) => !isBlank(v)).map((v) => short(v, 135));
  if (!isBlank(row.REF_conflict)) pieces.unshift(`CONFLICT: ${short(row.REF_conflict, 140)}`);
  return pieces.length ? `${sourcePrefix(row)}\n${pieces.slice(0, 3).join(" / ")}\nSee Evidence: ${textOf(row.unit_id)}` : "";
}

function physicalEvidence(row) {
  const canonical = canonicalSummary(row, "physical_evidence");
  if (canonical) return canonical;
  const pieces = [row.REF_thickness, row.REF_basal_surface].filter((v) => !isBlank(v)).map((v) => short(v, 220));
  return pieces.length ? `${sourcePrefix(row)}\n${pieces.join(" / ")}\nSee Evidence: ${textOf(row.unit_id)}` : "";
}

function reviewStatus(row) {
  const required = [row.column_id, row.sort_order, row.unit_name, row.t_int, row.b_int];
  if (required.some(isBlank)) return "MISSING";
  if (!isBlank(row.REF_conflict)) return "CHECK";
  if ([row.environment, row.unit_description, row.lithology].some(isBlank)) return "CHECK";
  const bp = numOf(row.b_prop);
  const tp = numOf(row.t_prop);
  if ((bp !== null || tp !== null) && !(bp !== null && tp !== null && bp >= 0 && tp <= 1 && bp < tp)) return "CHECK";
  const minT = numOf(row.min_thickness);
  const maxT = numOf(row.max_thickness);
  if (minT !== null && maxT !== null && minT > maxT) return "CHECK";
  return "OK";
}

const reviewHeaders = [
  "unit_id", "column_id", "sort_order", "position", "section_id", "t_pos",
  "unit_name", "t_int", "b_int", "t_age_ma", "b_age_ma", "t_prop", "b_prop", "age_evidence",
  "strat_name", "environment", "unit_description", "lithology", "minor_lith", "context_evidence",
  "min_thickness", "max_thickness", "basal_surface", "lateral_relationship", "physical_evidence",
  "status", "comments",
];

const reviewRows = units.map((row, i) => ({
  ...Object.fromEntries(reviewHeaders.map((h) => [h, row[h] ?? null])),
  position: auto[i].position,
  section_id: auto[i].section_id ?? row.section_id ?? null,
  t_pos: auto[i].t_pos ?? row.t_pos ?? null,
  age_evidence: [
    ageEvidence(row),
    promotedFieldsByRow[i].some((field) => ["t_int", "b_int", "t_age_ma", "b_age_ma"].includes(field))
      ? `AUTO CANDIDATE - verify Evidence: ${promotedFieldsByRow[i].filter((field) => ["t_int", "b_int", "t_age_ma", "b_age_ma"].includes(field)).join(", ")}`
      : "",
  ].filter(Boolean).join("\n"),
  context_evidence: [
    contextEvidence(row),
    promotedFieldsByRow[i].some((field) => ["unit_name", "strat_name", "environment", "unit_description", "lithology", "minor_lith"].includes(field))
      ? `AUTO CANDIDATE - verify Evidence: ${promotedFieldsByRow[i].filter((field) => ["unit_name", "strat_name", "environment", "unit_description", "lithology", "minor_lith"].includes(field)).join(", ")}`
      : "",
  ].filter(Boolean).join("\n"),
  physical_evidence: [
    physicalEvidence(row),
    promotedFieldsByRow[i].some((field) => ["min_thickness", "max_thickness", "basal_surface", "lateral_relationship"].includes(field))
      ? `AUTO CANDIDATE - verify Evidence: ${promotedFieldsByRow[i].filter((field) => ["min_thickness", "max_thickness", "basal_surface", "lateral_relationship"].includes(field)).join(", ")}`
      : "",
  ].filter(Boolean).join("\n"),
  status: reviewStatus(row),
}));

const evidenceRows = [];
function addEvidence(row, field, candidate, context, flag = "BEST") {
  if (isBlank(candidate) && isBlank(context)) return;
  evidenceRows.push({
    unit_id: textOf(row.unit_id),
    field,
    candidate: clampText(candidate, 1200),
    source_and_full_context: clampText(context),
    flag,
  });
}

for (const row of units) {
  addEvidence(
    row,
    "age",
    [row.t_int, row.b_int, row.t_age_ma, row.b_age_ma].filter((v) => !isBlank(v)).join(" | "),
    [row.REF_source, row.REF_age_text, row.REF_age_from_abstract].filter((v) => !isBlank(v)).join("\n\n"),
  );
  addEvidence(
    row,
    "context",
    [row.strat_name, row.environment, row.lithology, row.minor_lith].filter((v) => !isBlank(v)).join(" | "),
    [row.REF_source, row.REF_desc, row.REF_strat_name, row.REF_environment, row.REF_lithology_gsj,
      row.REF_minor_lith_gsj, row.REF_lithology, row.REF_minor_lith, row.REF_unit_description,
      row.REF_shape_unit_name, row.REF_shape_age_text, row.REF_shape_lith_text]
      .filter((v) => !isBlank(v)).join("\n\n"),
    isBlank(row.REF_conflict) ? "BEST" : "CONFLICT",
  );
  addEvidence(
    row,
    "physical",
    [row.min_thickness, row.max_thickness, row.basal_surface, row.lateral_relationship]
      .filter((v) => !isBlank(v)).join(" | "),
    [row.REF_source, row.REF_thickness, row.REF_basal_surface].filter((v) => !isBlank(v)).join("\n\n"),
  );
  if (!isBlank(row.REF_conflict)) {
    addEvidence(row, "conflict", "", row.REF_conflict, "CONFLICT");
  }
}

for (const row of legacyEvidence) {
  evidenceRows.push({
    unit_id: textOf(row.unit_id),
    field: textOf(row.field_name),
    candidate: clampText(row.candidate_value, 1200),
    source_and_full_context: [row.source_type, row.source_locator, row.conflict]
      .filter((v) => !isBlank(v)).join(" | "),
    flag: textOf(row.conflict) ? "CONFLICT" : (textOf(row.selected).toLowerCase() === "yes" ? "BEST" : "ALTERNATIVE"),
  });
}

if (canonicalEvidence.length) {
  evidenceRows.length = 0;
  const bestByField = new Map();
  for (const record of canonicalEvidence) {
    const key = `${textOf(record.unit_id)}|${textOf(record.field)}`;
    const previous = bestByField.get(key);
    if (!previous || canonicalRank(record) > canonicalRank(previous)) bestByField.set(key, record);
  }
  for (const record of canonicalEvidence) {
    const source = record.source ?? {};
    const confidence = record.confidence ?? {};
    const key = `${textOf(record.unit_id)}|${textOf(record.field)}`;
    let flag = bestByField.get(key) === record ? "BEST" : "ALTERNATIVE";
    if (record.conflict) flag = "CONFLICT";
    else if (textOf(record.assertion) === "inferred") flag = "INFERRED";
    const sourceContext = [
      `evidence_id: ${textOf(record.evidence_id)}`,
      `source_type: ${textOf(source.type)}`,
      !isBlank(source.file) ? `source_file: ${source.file}` : null,
      !isBlank(source.locator) ? `locator: ${source.locator}` : null,
      !isBlank(source.pdf_page) ? `PDF page: ${source.pdf_page}` : null,
      !isBlank(source.printed_page) ? `printed page: ${source.printed_page}` : null,
      !isBlank(source.section) ? `section/table: ${source.section}` : null,
      !isBlank(source.matched_sentence) ? `matched sentence: ${source.matched_sentence}` : null,
      !isBlank(source.quote) ? `full context: ${source.quote}` : null,
      `confidence: ${textOf(confidence.class)} (${textOf(confidence.score)})`,
      `assertion: ${textOf(record.assertion)}; selection: ${textOf(record.selection)}`,
      record.conflict ? `conflict: ${textOf(record.conflict_detail)}` : null,
    ].filter((v) => !isBlank(v)).join("\n");
    evidenceRows.push({
      unit_id: textOf(record.unit_id),
      field: textOf(record.field),
      candidate: clampText(record.candidate, 1200),
      source_and_full_context: clampText(sourceContext),
      flag,
    });
  }
}

const coordinateCounts = new Map();
for (const row of columns) {
  if (isBlank(row.lat) || isBlank(row.lng)) continue;
  const key = `${textOf(row.lat)}|${textOf(row.lng)}`;
  coordinateCounts.set(key, (coordinateCounts.get(key) ?? 0) + 1);
}

const mapCandidates = new Map(
  (mapMetadata?.columns ?? []).map((candidate) => [textOf(candidate.col_id), candidate]),
);
const knownRefIds = refs.map((row) => textOf(row.ref_id)).filter(Boolean);

const columnHeaders = ["col_id", "col_name", "region_basis", "lat", "lng", "geom", "rgeom", "evidence", "status", "ref_ids"];
const columnRows = columns.map((row) => {
  const key = `${textOf(row.lat)}|${textOf(row.lng)}`;
  const hasExistingCoordinate = !isBlank(row.geom)
    || (!isBlank(row.lat) && !isBlank(row.lng));
  const duplicate = hasExistingCoordinate
    && columns.length > 1
    && coordinateCounts.get(key) > 1;
  const candidate = mapCandidates.get(textOf(row.col_id));
  const hasCandidate = candidate && numOf(candidate.lat) !== null && numOf(candidate.lng) !== null;
  const sourceCoordinateEvidence = textOf(row.coordinate_evidence);
  const mapEvidence = hasCandidate
    ? `Map verification: ${textOf(candidate.method)}; inside assigned region: ${candidate.inside_region ? "yes" : "no"}.`
      + (kmlPath ? ` Google Earth KML: ${path.basename(kmlPath)}.` : "")
    : "";
  const candidateEvidence = [sourceCoordinateEvidence, mapEvidence]
    .filter((value) => !isBlank(value))
    .join(" ");
  const originalRefIds = splitColumns(row.ref_ids);
  const resolvedRefIds = originalRefIds.filter((refId) => knownRefIds.includes(refId));
  const refIds = resolvedRefIds.length
    ? resolvedRefIds.join(", ")
    : (knownRefIds.length === 1 ? knownRefIds[0] : row.ref_ids);
  const refCorrection = textOf(refIds) !== textOf(row.ref_ids)
    ? ` Reference ID normalized from ${textOf(row.ref_ids)} to ${textOf(refIds)}.`
    : "";
  return {
    col_id: row.col_id,
    col_name: row.col_name,
    region_basis: textOf(row.region_basis) || row.comments,
    lat: hasCandidate ? numOf(candidate.lat) : row.lat,
    lng: hasCandidate ? numOf(candidate.lng) : row.lng,
    geom: row.geom,
    rgeom: row.rgeom,
    evidence: (candidateEvidence || (duplicate
      ? "Candidate point is still shared with another Column. Review the embedded map/KML."
      : (hasExistingCoordinate
        ? "Candidate point retained from the existing review."
        : "No polygon or representative point is available. Review the PDF regional basis and add or verify coordinates."))) + refCorrection,
    status: textOf(row.status) || (hasCandidate ? "CHECK" : (duplicate ? "CHECK" : ((!isBlank(row.geom) || (!isBlank(row.lat) && !isBlank(row.lng))) ? "OK" : "MISSING"))),
    ref_ids: refIds,
  };
});

function matrixFromRows(headers, rows) {
  return [headers, ...rows.map((r) => headers.map((h) => r[h] ?? null))];
}

function sanitizeTableName(name) {
  return name.replace(/[^A-Za-z0-9_]/g, "_");
}

const workbook = Workbook.create();
const reviewSheet = workbook.worksheets.add("Review");
const columnsSheet = workbook.worksheets.add("Columns");
const evidenceSheet = workbook.worksheets.add("Evidence");
const projectSheet = workbook.worksheets.add("Project");

const reviewMatrix = matrixFromRows(reviewHeaders, reviewRows);
reviewSheet.getRangeByIndexes(0, 0, reviewMatrix.length, reviewHeaders.length).values = reviewMatrix;
const reviewTable = reviewSheet.tables.add(`A1:AA${reviewMatrix.length}`, true, sanitizeTableName("ReviewUnits"));
reviewTable.style = "TableStyleMedium2";
reviewTable.showBandedColumns = false;
reviewTable.showFilterButton = true;

const columnMatrix = matrixFromRows(columnHeaders, columnRows);
columnsSheet.getRangeByIndexes(0, 0, columnMatrix.length, columnHeaders.length).values = columnMatrix;
const columnsTable = columnsSheet.tables.add(`A1:J${columnMatrix.length}`, true, "ReviewColumns");
columnsTable.style = "TableStyleMedium2";
columnsTable.showFilterButton = true;

const evidenceHeaders = ["unit_id", "field", "candidate", "source_and_full_context", "flag"];
const evidenceMatrix = matrixFromRows(evidenceHeaders, evidenceRows);
evidenceSheet.getRangeByIndexes(0, 0, evidenceMatrix.length, evidenceHeaders.length).values = evidenceMatrix;
const evidenceTable = evidenceSheet.tables.add(`A1:E${evidenceMatrix.length}`, true, "ReviewEvidence");
evidenceTable.style = "TableStyleMedium2";
evidenceTable.showFilterButton = true;

const gsjYear = textOf(gsjMeta.find((row) => textOf(row.key) === "pub_year")?.value);
const gsjTitleEn = textOf(gsjMeta.find((row) => textOf(row.key) === "title_en")?.value);
const columnBaseName = textOf(columns[0]?.col_name)
  .replace(/[-\s](west|east|central|north|south)$/i, "")
  .trim();
const englishProjectName = gsjTitleEn
  || (columnBaseName ? `${columnBaseName}${gsjYear ? ` (${gsjYear})` : ""}` : "");
const projectRows = projectMeta.map((r) => [
  r.key,
  textOf(r.key) === "project_name" && englishProjectName ? englishProjectName : r.value,
]);
for (const row of gsjMeta) projectRows.push([`gsj_${textOf(row.key)}`, row.value]);
projectRows.push(["review_schema_version", "2.0.0"]);
projectRows.push(["compiled_schema_version", textOf(compiledDocument?.schema_version) || "not supplied"]);
projectRows.push(["generated_at", `UTC ${new Date().toISOString()}`]);
projectRows.push([inputIsJson ? "source_bundle_file" : "source_review_file", path.basename(inputPath)]);
projectRows.push([inputIsJson ? "source_bundle_sha256" : "source_review_sha256", inputSha256]);
projectRows.push(["input_mode", inputIsJson ? "raw canonical JSON bundle" : "legacy review workbook"]);
projectRows.push([
  "auto_preview_policy",
  "position, section_id, t_pos, t_prop and b_prop are reference-only snapshots. Do not regenerate an edited Review v2; submission export always recalculates them from the reviewed values.",
]);
projectRows.push([
  "candidate_promotion_policy",
  "Only provenance-free canonical candidates are shown in blank Review cells. Yellow cells and AUTO CANDIDATE evidence require human confirmation.",
]);
if (mapJsonPath) projectRows.push(["column_map_metadata", path.basename(mapJsonPath)]);
if (kmlPath) projectRows.push(["column_map_kml", path.basename(kmlPath)]);
let projectRow = 1;
projectSheet.getRange(`A${projectRow}:B${projectRow}`).values = [["PROJECT_METADATA", null]];
projectSheet.getRange(`A${projectRow}:B${projectRow}`).format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" } };
projectRow += 1;
projectSheet.getRange(`A${projectRow}:B${projectRow}`).values = [["key", "value"]];
projectSheet.getRange(`A${projectRow + 1}:B${projectRow + projectRows.length}`).values = projectRows;
projectSheet.tables.add(`A${projectRow}:B${projectRow + projectRows.length}`, true, "ProjectMetadata").style = "TableStyleMedium2";
projectRow += projectRows.length + 2;

const resolvedRefs = refs.map((row) => ({
  ...row,
  title: isBlank(row.title) && columnBaseName
    ? `Geology of the ${columnBaseName} District`
    : row.title,
  comments: textOf(row.comments)
    .replace("ZFK map.json より自動生成。", "Auto-generated from GSJ ZFK map.json. ")
    .replace(/\s*原題\(和\):[\s\S]*$/u, "")
    .trim(),
}));
const refHeaders = resolvedRefs.length ? Object.keys(resolvedRefs[0]) : ["ref_id", "title", "authors", "publication", "compilation", "organization", "date", "doi", "url", "comments"];
projectSheet.getRange(`A${projectRow}:J${projectRow}`).values = [["REFERENCES", ...Array(9).fill(null)]];
projectSheet.getRange(`A${projectRow}:J${projectRow}`).format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" } };
projectRow += 1;
const refMatrix = matrixFromRows(refHeaders, resolvedRefs);
projectSheet.getRangeByIndexes(projectRow - 1, 0, refMatrix.length, refHeaders.length).values = refMatrix;
if (resolvedRefs.length) projectSheet.tables.add(`A${projectRow}:J${projectRow + resolvedRefs.length}`, true, "ProjectReferences").style = "TableStyleMedium2";
projectRow += refMatrix.length + 1;

const resolvedImages = images.map((row) => ({
  ...row,
  description: textOf(row.description)
    .replace(/\s*[（(](?:凡例|断面図)[）)]/gu, "")
    .trim(),
  comments: textOf(row.comments).replace("references/ から自動検出:", "Auto-detected from references:"),
}));
const imageHeaders = resolvedImages.length ? Object.keys(resolvedImages[0]) : ["col_ids", "image_name", "ref_id", "page_no", "fig_no", "description", "comments"];
projectSheet.getRange(`A${projectRow}:G${projectRow}`).values = [["IMAGES", ...Array(6).fill(null)]];
projectSheet.getRange(`A${projectRow}:G${projectRow}`).format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" } };
projectRow += 1;
const imageMatrix = matrixFromRows(imageHeaders, resolvedImages);
projectSheet.getRangeByIndexes(projectRow - 1, 0, imageMatrix.length, imageHeaders.length).values = imageMatrix;
if (resolvedImages.length) projectSheet.tables.add(`A${projectRow}:G${projectRow + resolvedImages.length}`, true, "ProjectImages").style = "TableStyleMedium2";

for (const sheet of [reviewSheet, columnsSheet, evidenceSheet, projectSheet]) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
}
reviewSheet.freezePanes.freezeColumns(7);
columnsSheet.freezePanes.freezeColumns(2);
evidenceSheet.freezePanes.freezeColumns(2);

const header = reviewSheet.getRange("A1:AA1");
header.format = { font: { bold: true, color: "#FFFFFF" }, fill: "#1F4E78", wrapText: true, verticalAlignment: "center" };
reviewSheet.getRange("D1:F1").format = { fill: "#5B9BD5", font: { bold: true, color: "#FFFFFF" } };
reviewSheet.getRange("L1:M1").format = { fill: "#5B9BD5", font: { bold: true, color: "#FFFFFF" } };
reviewSheet.getRange("N1:N1").format = { fill: "#7F8C8D", font: { bold: true, color: "#FFFFFF" } };
reviewSheet.getRange("T1:T1").format = { fill: "#7F8C8D", font: { bold: true, color: "#FFFFFF" } };
reviewSheet.getRange("Y1:Y1").format = { fill: "#7F8C8D", font: { bold: true, color: "#FFFFFF" } };
reviewSheet.getRange("Z1:Z1").format = { fill: "#A64B4B", font: { bold: true, color: "#FFFFFF" } };

const nReview = reviewMatrix.length;
if (nReview > 1) {
  reviewSheet.getRange(`D2:F${nReview}`).format = { fill: "#DDEBF7", font: { color: "#1F1F1F" }, horizontalAlignment: "center" };
  reviewSheet.getRange(`L2:M${nReview}`).format = { fill: "#DDEBF7", numberFormat: "0.000", horizontalAlignment: "right" };
  reviewSheet.getRange(`N2:N${nReview}`).format = { fill: "#F2F2F2", wrapText: true, verticalAlignment: "top" };
  reviewSheet.getRange(`T2:T${nReview}`).format = { fill: "#F2F2F2", wrapText: true, verticalAlignment: "top" };
  reviewSheet.getRange(`Y2:Y${nReview}`).format = { fill: "#F2F2F2", wrapText: true, verticalAlignment: "top" };
  reviewSheet.getRange(`Q2:Q${nReview}`).format = { wrapText: true, verticalAlignment: "top" };
  reviewSheet.getRange(`J2:M${nReview}`).format.numberFormat = "0.000";
  reviewSheet.getRange(`U2:V${nReview}`).format.numberFormat = "0.###";
  const statusFormulas = reviewRows.map((_, index) => {
    const row = index + 2;
    return [`=IF(OR($B${row}="",$C${row}="",$G${row}="",$H${row}="",$I${row}=""),"MISSING",IF(OR(AND($P${row}="",ISERROR(SEARCH("Pyroclastic",$G${row})),ISERROR(SEARCH("Tephra",$G${row})),ISERROR(SEARCH("Lava",$G${row})),ISERROR(SEARCH("Volcanic",$G${row}))),$Q${row}="",$R${row}="",IFERROR(SEARCH("CONFLICT",$N${row}&$T${row}&$Y${row})>0,FALSE),IFERROR(SEARCH("AUTO CANDIDATE",$N${row}&$T${row}&$Y${row})>0,FALSE),AND($U${row}<>"",$V${row}<>"",$U${row}>$V${row}),AND(OR($L${row}<>"",$M${row}<>""),NOT(AND($L${row}<>"",$M${row}<>"",$M${row}>=0,$L${row}<=1,$M${row}<$L${row})))),"CHECK","OK"))`];
  });
  reviewSheet.getRange(`Z2:Z${nReview}`).formulas = statusFormulas;
  reviewSheet.getRange(`Z2:Z${nReview}`).conditionalFormats.add("containsText", { text: "OK", format: { fill: "#E2F0D9", font: { color: "#375623", bold: true } } });
  reviewSheet.getRange(`Z2:Z${nReview}`).conditionalFormats.add("containsText", { text: "CHECK", format: { fill: "#FFF2CC", font: { color: "#7F6000", bold: true } } });
  reviewSheet.getRange(`Z2:Z${nReview}`).conditionalFormats.add("containsText", { text: "MISSING", format: { fill: "#F4CCCC", font: { color: "#9C0006", bold: true } } });
  reviewSheet.getRange(`A2:AA${nReview}`).format.rowHeight = 52;
  for (let index = 0; index < promotedFieldsByRow.length; index += 1) {
    for (const field of promotedFieldsByRow[index]) {
      const column = reviewHeaders.indexOf(field);
      if (column >= 0) reviewSheet.getRangeByIndexes(index + 1, column, 1, 1).format = { fill: "#FFF2CC" };
    }
  }
}

const reviewWidths = {
  A: 17, B: 15, C: 10, D: 15, E: 12, F: 11, G: 34, H: 18, I: 18, J: 11, K: 11, L: 10, M: 10,
  N: 44, O: 34, P: 22, Q: 48, R: 25, S: 22, T: 44, U: 12, V: 12, W: 20, X: 22, Y: 44, Z: 11, AA: 28,
};
for (const [col, width] of Object.entries(reviewWidths)) reviewSheet.getRange(`${col}:${col}`).format.columnWidth = width;

columnsSheet.getRange("A1:J1").format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" }, wrapText: true };
columnsSheet.getRange("A:J").format.verticalAlignment = "top";
for (const [col, width] of Object.entries({ A: 15, B: 26, C: 55, D: 12, E: 12, F: 24, G: 24, H: 65, I: 12, J: 22 })) {
  columnsSheet.getRange(`${col}:${col}`).format.columnWidth = width;
}
if (columnMatrix.length > 1) {
  columnsSheet.getRange(`C2:C${columnMatrix.length}`).format.wrapText = true;
  columnsSheet.getRange(`H2:H${columnMatrix.length}`).format.wrapText = true;
  columnsSheet.getRange(`I2:I${columnMatrix.length}`).dataValidation = { rule: { type: "list", values: ["OK", "CHECK", "MISSING"] } };
  columnsSheet.getRange(`I2:I${columnMatrix.length}`).conditionalFormats.add("containsText", { text: "CHECK", format: { fill: "#FFF2CC", font: { bold: true, color: "#7F6000" } } });
  columnsSheet.getRange(`I2:I${columnMatrix.length}`).conditionalFormats.add("containsText", { text: "MISSING", format: { fill: "#F4CCCC", font: { bold: true, color: "#9C0006" } } });
  columnsSheet.getRange(`A2:J${columnMatrix.length}`).format.rowHeight = 84;
}

evidenceSheet.getRange("A1:E1").format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" }, wrapText: true };
evidenceSheet.getRange("A:A").format.columnWidth = 18;
evidenceSheet.getRange("B:B").format.columnWidth = 16;
evidenceSheet.getRange("C:C").format.columnWidth = 42;
evidenceSheet.getRange("D:D").format.columnWidth = 100;
evidenceSheet.getRange("E:E").format.columnWidth = 15;
if (evidenceMatrix.length > 1) {
  evidenceSheet.getRange(`C2:D${evidenceMatrix.length}`).format = { wrapText: true, verticalAlignment: "top" };
  evidenceSheet.getRange(`E2:E${evidenceMatrix.length}`).dataValidation = { rule: { type: "list", values: ["BEST", "ALTERNATIVE", "CONFLICT", "INFERRED"] } };
  evidenceSheet.getRange(`E2:E${evidenceMatrix.length}`).conditionalFormats.add("containsText", { text: "CONFLICT", format: { fill: "#F4CCCC", font: { bold: true, color: "#9C0006" } } });
  evidenceSheet.getRange(`E2:E${evidenceMatrix.length}`).conditionalFormats.add("containsText", { text: "INFERRED", format: { fill: "#FFF2CC", font: { bold: true, color: "#7F6000" } } });
  evidenceSheet.getRange(`A2:E${evidenceMatrix.length}`).format.rowHeight = 72;
}

projectSheet.getRange("A:A").format.columnWidth = 32;
projectSheet.getRange("B:B").format.columnWidth = 70;
projectSheet.getRange("C:J").format.columnWidth = 24;
projectSheet.getUsedRange(true).format.verticalAlignment = "top";

if (mapPath) {
  try {
    const mapBytes = await fs.readFile(mapPath);
    const dataUrl = `data:image/png;base64,${mapBytes.toString("base64")}`;
    columnsSheet.images.add({
      dataUrl,
      anchor: { from: { row: columnMatrix.length + 2, col: 0 }, extent: { widthPx: 980, heightPx: 650 } },
    });
  } catch (error) {
    console.warn(`Column map was not embedded: ${error.message}`);
  }
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const exported = await SpreadsheetFile.exportXlsx(workbook);
await exported.save(outputPath);

const reviewCheck = await workbook.inspect({
  kind: "table",
  range: `Review!A1:AA${Math.min(nReview, 8)}`,
  include: "values,formulas",
  tableMaxRows: 8,
  tableMaxCols: 27,
  maxChars: 14000,
});
console.log(reviewCheck.ndjson);
const errorScan = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "Review v2 formula error scan",
});
console.log(errorScan.ndjson);
console.log(JSON.stringify({ outputPath, units: reviewRows.length, columns: columnRows.length, evidence: evidenceRows.length }));
