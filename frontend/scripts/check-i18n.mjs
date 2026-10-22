#!/usr/bin/env node
// The interface-language check (P7.10, plan section 8.16). It runs in the frontend-quality CI job on every pull
// request that touches frontend/, and locally with: node scripts/check-i18n.mjs (from frontend/).
//
// It fails when:
//   1. JSX in the customer and owner areas, src/app/(site), src/app/(auth) and src/app/shop, holds literal
//      user-visible text: a text child, a string in a child expression (including both sides of a condition), or a
//      string in an attribute people see or hear (aria-label, title, placeholder, alt, label and the like). Text with
//      no letters (numbers, punctuation, symbols) is not text to translate, brand names in ALLOWED below are the
//      same in every language, and a sample code in a placeholder (G-7KQ2MX) shows a format, not words. Admin screens stay in English and are not checked (plan section 8.16).
//   2. A registered language's message file (src/lib/i18n.ts, LANGUAGES) is missing a key that English has, or has
//      a key English does not, or holds a value of the wrong kind (text where English has a group, or the reverse).
//   3. A message key has no text (an empty value) in a language marked reviewed, which would be served to everyone.
//      In a language not yet reviewed (a preview language, falling back to English per key), empty values are the
//      text still awaited from the team: they are counted and listed with --list-untranslated, and fail the check
//      only with --strict (to confirm a language is complete before marking it reviewed).
//
// A line that must keep a literal (rare: say why) carries the comment i18n-allow on that line or the line above.
// Exit code 0 when clean, 1 when anything above fails.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const args = new Set(process.argv.slice(2));
const strict = args.has("--strict");
const listUntranslated = args.has("--list-untranslated");

// ------------------------------------------------------------------------------------------------ literals in JSX

// I18N_AREAS (comma-separated folders) checks other folders instead, for a review: it never changes what CI checks.
const AREAS = process.env.I18N_AREAS
  ? process.env.I18N_AREAS.split(",").map((a) => a.trim()).filter(Boolean)
  : ["src/app/(site)", "src/app/(auth)", "src/app/shop"];

/** Brand and product names, written the same in every language. Matched against the whole trimmed text. */
const ALLOWED = new Set(["NearShop", "near", "shop", "UPI", "WhatsApp", "Google", "GSTIN", "FSSAI", "Udyam"]);

/** Attributes whose string value is shown or read out. Others (className, href, type, variant, id...) are not. */
const VISIBLE_ATTRIBUTES = new Set([
  "aria-label",
  "aria-description",
  "aria-placeholder",
  "aria-roledescription",
  "aria-valuetext",
  "alt",
  "title",
  "placeholder",
  "label",
  "description",
  "heading",
  "subtitle",
  "hint",
  "message",
  "caption",
  "tooltip",
  "confirmLabel",
  "cancelLabel",
  "emptyLabel",
  "srLabel",
]);

const LETTER = /\p{L}/u;
/** A sample code in a field's placeholder, such as G-7KQ2MX: capitals and digits in dash-separated groups, with a
 * digit. It shows a format, not words. */
const CODE_SAMPLE = /^(?=.*\d)[A-Z0-9]+(-[A-Z0-9]+)+$/;

function isUserText(text) {
  const trimmed = text.replace(/\s+/g, " ").trim();
  if (!trimmed || !LETTER.test(trimmed)) return false;
  return !ALLOWED.has(trimmed) && !CODE_SAMPLE.test(trimmed);
}

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...walk(path));
    else if (path.endsWith(".tsx")) out.push(path);
  }
  return out;
}

/** The string literals an expression can evaluate to, through conditions, ?? / || / && and parentheses. */
function literalParts(node) {
  if (!node) return [];
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return [{ node, text: node.text }];
  if (ts.isTemplateExpression(node)) {
    const parts = [node.head, ...node.templateSpans.map((s) => s.literal)];
    return parts.map((p) => ({ node, text: p.text }));
  }
  if (ts.isParenthesizedExpression(node)) return literalParts(node.expression);
  if (ts.isConditionalExpression(node)) return [...literalParts(node.whenTrue), ...literalParts(node.whenFalse)];
  if (ts.isBinaryExpression(node)) {
    const op = node.operatorToken.kind;
    if (op === ts.SyntaxKind.QuestionQuestionToken || op === ts.SyntaxKind.BarBarToken) {
      return [...literalParts(node.left), ...literalParts(node.right)];
    }
    if (op === ts.SyntaxKind.AmpersandAmpersandToken) return literalParts(node.right);
    if (op === ts.SyntaxKind.PlusToken) return [...literalParts(node.left), ...literalParts(node.right)];
  }
  return [];
}

function checkFile(path) {
  const source = readFileSync(path, "utf8");
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const lines = source.split("\n");
  const found = [];
  const allowed = (node) => {
    const line = file.getLineAndCharacterOfPosition(node.getStart(file)).line;
    return /i18n-allow/.test(lines[line] ?? "") || /i18n-allow/.test(lines[line - 1] ?? "");
  };
  const report = (node, text, where) => {
    if (!isUserText(text) || allowed(node)) return;
    const { line, character } = file.getLineAndCharacterOfPosition(node.getStart(file));
    found.push({ file: relative(root, path), line: line + 1, column: character + 1, where, text: text.replace(/\s+/g, " ").trim() });
  };
  const visit = (node) => {
    if (ts.isJsxText(node)) {
      report(node, node.text, "text");
    } else if (ts.isJsxExpression(node) && node.expression) {
      const parent = node.parent;
      if (ts.isJsxElement(parent) || ts.isJsxFragment(parent)) {
        for (const part of literalParts(node.expression)) report(part.node, part.text, "text");
      }
    } else if (ts.isJsxAttribute(node) && node.initializer) {
      const name = node.name.getText(file);
      if (VISIBLE_ATTRIBUTES.has(name)) {
        const init = node.initializer;
        const parts = ts.isStringLiteral(init) ? [{ node: init, text: init.text }] : literalParts(init.expression);
        for (const part of parts) report(part.node, part.text, name);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
}

const literals = AREAS.flatMap((area) => walk(join(root, area))).flatMap(checkFile);

// ------------------------------------------------------------------------------------------------ message files

/** The registered languages, read from src/lib/i18n.ts so a language added there (P8.4) is checked at once. */
function registeredLanguages() {
  const source = readFileSync(join(root, "src/lib/i18n.ts"), "utf8");
  const block = /export const LANGUAGES = \[([\s\S]*?)\] as const/.exec(source);
  if (!block) throw new Error("check-i18n: LANGUAGES not found in src/lib/i18n.ts");
  const entries = [...block[1].matchAll(/code: "([a-z-]+)"[\s\S]*?reviewed: (true|false)/g)];
  if (!entries.length) throw new Error("check-i18n: no language entries in LANGUAGES");
  return entries.map((m) => ({ code: m[1], reviewed: m[2] === "true" }));
}

const isTree = (value) => typeof value === "object" && value !== null && !Array.isArray(value);

function compare(english, theirs, prefix, result) {
  for (const [key, value] of Object.entries(english)) {
    const path = prefix + key;
    if (!isTree(theirs) || !(key in theirs)) {
      result.missing.push(path);
      continue;
    }
    const other = theirs[key];
    if (typeof value === "string") {
      if (typeof other !== "string") result.wrongKind.push(path);
      else if (!other.trim()) result.empty.push(path);
      else {
        // The placeholders must match, or a value would show "{count}" or lose a number.
        const names = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join(",");
        if (names(value) !== names(other)) result.placeholders.push(`${path} (English {${names(value)}}, here {${names(other)}})`);
      }
    } else if (!isTree(other)) {
      result.wrongKind.push(path);
    } else {
      compare(value, other, `${path}.`, result);
    }
  }
  if (isTree(theirs)) {
    for (const key of Object.keys(theirs)) if (!(key in english)) result.extra.push(prefix + key);
  }
}

const languages = registeredLanguages();
const english = JSON.parse(readFileSync(join(root, "src/messages/en.json"), "utf8"));
const messageProblems = [];
const untranslated = [];
for (const { code, reviewed } of languages) {
  if (code === "en") continue;
  let theirs;
  try {
    theirs = JSON.parse(readFileSync(join(root, `src/messages/${code}.json`), "utf8"));
  } catch (error) {
    messageProblems.push(`${code}: src/messages/${code}.json cannot be read: ${error.message}`);
    continue;
  }
  const result = { missing: [], extra: [], wrongKind: [], empty: [], placeholders: [] };
  compare(english, theirs, "", result);
  for (const key of result.missing) messageProblems.push(`${code}: missing key ${key}`);
  for (const key of result.extra) messageProblems.push(`${code}: key not in English (remove it) ${key}`);
  for (const key of result.wrongKind) messageProblems.push(`${code}: text and group mixed up at ${key}`);
  for (const key of result.placeholders) messageProblems.push(`${code}: placeholders differ at ${key}`);
  if (reviewed || strict) {
    for (const key of result.empty) messageProblems.push(`${code}: no text for ${key}${reviewed ? " (marked reviewed)" : ""}`);
  }
  untranslated.push({ code, reviewed, empty: result.empty, missing: result.missing.length + result.wrongKind.length });
}

// ------------------------------------------------------------------------------------------------ report

const countKeys = (tree) => Object.values(tree).reduce((n, v) => n + (isTree(v) ? countKeys(v) : 1), 0);
const total = countKeys(english);

for (const l of literals) console.log(`${l.file}:${l.line}:${l.column}  literal ${l.where}: ${JSON.stringify(l.text)}`);
for (const p of messageProblems) console.log(p);
console.log("");
console.log(`Literal user-visible text in ${AREAS.join(", ")}: ${literals.length}`);
console.log(`Message keys in English: ${total}`);
for (const { code, reviewed, empty, missing } of untranslated) {
  const state = reviewed ? "reviewed" : "preview";
  const withText = total - empty.length - missing;
  console.log(`${code} (${state}): ${withText} of ${total} keys have text; ${empty.length} await the team's text`);
  if (listUntranslated) for (const key of empty) console.log(`  ${key}`);
}
console.log(`Missing or mismatched keys: ${messageProblems.length}`);
if (literals.length || messageProblems.length) {
  console.log("check-i18n: FAILED");
  process.exit(1);
}
console.log("check-i18n: passed");
