import { useEffect, useState } from "react";

/** 知识库相关的类型和小工具，知识库页和对话页共用。 */

export type KbDoc = {
  source: string; title: string; url: string; version: string;
  chunks: number; fetched: string; kind: "official" | "lab_reference"; family: string;
};
export type KbOverview = {
  doc_count: number; chunk_count: number; db_size_bytes: number; db_modified: number | null;
  method: string; documents: KbDoc[]; available: boolean;
};
export type KbExpression = {
  tokens: string[]; and: string; or: string; used_or: boolean; zh_mapped: string[]; match_all_required: boolean;
};
export type KbHit = {
  source: string; title: string; heading_path: string; snippet: string; score: number;
  url: string; version: string; kind: "official" | "lab_reference";
};
export type KbSearch = { q: string; results: KbHit[]; expression: KbExpression; method: string };
/** 对话流里 doc_search 步骤带出来的出处（没有片段正文）。 */
export type ChatHit = { source: string; title: string; heading_path: string };

/** 页面标题里的 " - Cisco" 后缀和 "[Cisco IOS Software Release …]" 方括号说明只是啰嗦，卡片里去掉。 */
export const shortTitle = (t: string) =>
  (t || "").replace(/\s*\[[^\]]*\]\s*(?:-\s*Cisco)?\s*$/, "").replace(/\s*-\s*Cisco\s*$/, "").trim();

/** heading_path 第一段是文档标题，后面才是小节。 */
export const sectionOf = (headingPath: string) => (headingPath || "").split(" › ").slice(1).join(" › ");
export const firstSeg = (headingPath: string) => (headingPath || "").split(" › ")[0].trim();

let overviewCache: Promise<KbOverview | null> | null = null;
export function fetchOverview(force = false): Promise<KbOverview | null> {
  if (!overviewCache || force) {
    overviewCache = fetch("/api/kb/overview").then((r) => (r.ok ? (r.json() as Promise<KbOverview>) : null)).catch(() => null);
  }
  return overviewCache;
}

/** source → 真实页面标题。库里 title 列存的是文件名，不能直接给人看。 */
export function useDocTitles(): Map<string, KbDoc> {
  const [m, setM] = useState<Map<string, KbDoc>>(new Map());
  useEffect(() => {
    fetchOverview().then((o) => { if (o) setM(new Map(o.documents.map((d) => [d.source, d]))); });
  }, []);
  return m;
}

export function hitTitle(h: { source: string; heading_path: string }, docs: Map<string, KbDoc>) {
  const d = docs.get(h.source);
  return shortTitle(d?.title || firstSeg(h.heading_path) || h.source);
}

/** 版本文字的粗分类，只决定徽标颜色；文字本身原样显示。 */
export function versionTone(v: string): "match" | "near" | "agnostic" | "other" | "none" {
  if (!v) return "none";
  if (/15\.9/.test(v) && !/未标|非\s*15\.9|不是\s*15\.9/.test(v)) return "match";
  if (/版本无关/.test(v)) return "agnostic";
  if (/15\.\d|15M/.test(v)) return "near";
  return "other";
}

export const TONE_CLASS: Record<ReturnType<typeof versionTone>, string> = {
  match: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  near: "bg-sky-50 text-sky-700 ring-sky-200",
  agnostic: "bg-slate-100 text-slate-600 ring-slate-200",
  other: "bg-violet-50 text-violet-700 ring-violet-200",
  none: "bg-slate-50 text-slate-400 ring-slate-200",
};

export function fmtBytes(n: number) {
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`;
  if (n >= 1 << 10) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

/** 跳到知识库页并预填搜索词。用 `#knowledge?q=…`，App 读 hash 时会把问号后面的参数剥掉再选页。 */
export function goKnowledge(q: string) {
  window.dispatchEvent(new CustomEvent("netops:nav", { detail: `knowledge?q=${encodeURIComponent(q)}` }));
}

export function hashParam(name: string): string {
  const i = location.hash.indexOf("?");
  if (i < 0) return "";
  return new URLSearchParams(location.hash.slice(i + 1)).get(name) ?? "";
}
