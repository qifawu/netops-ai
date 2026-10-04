import { brief, plain } from "./api";
import { fill, type DictKey } from "./i18n";

type IncLike = { alerts?: { name?: string }[]; root_cause?: string };

/** 置信度统一中英：high/medium/low。空值（旧记录没有）显示「未评级」。
 *  这几张表只返回 key，不返回翻译好的文案——这两个函数不是 hook，会在 `.map()` 循环里
 *  按行调用，不能在里面调 useT()（违反 Hooks 规则）。调用方自己在组件顶层拿到的 `t()`
 *  上，对 `.labelKey`/`.longKey`/`.hintKey` 按需 `t(...)`。 */
export const CONF: Record<string, { labelKey: DictKey; longKey: DictKey; badge: string; dot: string; bar: string; hintKey: DictKey }> = {
  high: { labelKey: "conf.high.label", longKey: "conf.high.long", dot: "bg-emerald-500", bar: "bg-emerald-500", badge: "bg-emerald-50 text-emerald-700 ring-emerald-200", hintKey: "conf.high.hint" },
  medium: { labelKey: "conf.medium.label", longKey: "conf.medium.long", dot: "bg-amber-500", bar: "bg-amber-500", badge: "bg-amber-50 text-amber-700 ring-amber-200", hintKey: "conf.medium.hint" },
  low: { labelKey: "conf.low.label", longKey: "conf.low.long", dot: "bg-rose-500", bar: "bg-rose-500", badge: "bg-rose-50 text-rose-700 ring-rose-200", hintKey: "conf.low.hint" },
};
const NONE: { labelKey: DictKey; longKey: DictKey; badge: string; dot: string; bar: string; hintKey: DictKey } =
  { labelKey: "conf.none.label", longKey: "conf.none.label", dot: "bg-slate-300", bar: "bg-slate-300", badge: "bg-slate-100 text-slate-500 ring-slate-200", hintKey: "conf.none.hint" };
export const confMeta = (c?: string) => CONF[c ?? ""] ?? NONE;
export const confLevel = (c?: string) => ({ high: 3, medium: 2, low: 1 } as Record<string, number>)[c ?? ""] ?? 0;

/** 故障类型：按告警名 + 结论关键词粗分。顺序有讲究——
 *  接口 down 会连带引发 OSPF / BGP 邻居掉线，所以有接口告警时先归接口。
 *  正则匹配的是告警名原文（英文/厂商日志关键词，跟界面语言无关），不用翻译。 */
const TYPES: { labelKey: DictKey; cls: string; test: RegExp }[] = [
  { labelKey: "faultType.restart", cls: "bg-amber-50 text-amber-700 ring-amber-200", test: /restarted|coldStart|warmStart|重启/i },
  { labelKey: "faultType.interfaceDown", cls: "bg-rose-50 text-rose-700 ring-rose-200", test: /link down|linkDown|line protocol|lower speed|Interface/i },
  { labelKey: "faultType.bgp", cls: "bg-violet-50 text-violet-700 ring-violet-200", test: /BGP/i },
  { labelKey: "faultType.ospf", cls: "bg-sky-50 text-sky-700 ring-sky-200", test: /OSPF/i },
  { labelKey: "faultType.unreachable", cls: "bg-orange-50 text-orange-700 ring-orange-200", test: /ICMP|unreachable|不可达/i },
  { labelKey: "faultType.monitoringDown", cls: "bg-slate-100 text-slate-600 ring-slate-200", test: /No SNMP|SNMP/i },
  { labelKey: "faultType.configChange", cls: "bg-indigo-50 text-indigo-700 ring-indigo-200", test: /configChange|config/i },
];
export function faultType(i: IncLike): { labelKey: DictKey; cls: string } {
  const names = (i.alerts ?? []).map((a) => a.name ?? "").join(" | ");
  for (const t of TYPES) if (t.test.test(names)) return t;
  const rc = i.root_cause ?? "";
  for (const t of TYPES) if (t.test.test(rc)) return t;
  return { labelKey: "faultType.other", cls: "bg-slate-100 text-slate-600 ring-slate-200" };
}

/** 一句话结论：优先用模型给的 headline（≤20 字定性），旧记录没有就取根因的第一句、太长在逗号处截断。
 *  完整根因在详情页原样保留，这里只是列表里的概览。 */
export function titleOf(i: { headline?: string; root_cause?: string }, max = 54) {
  const h = plain(i.headline ?? "").replace(/\s+/g, " ").trim();
  // headline 要求 ≤20 字，但模型常常写长；长了就按根因同样的规则截断。
  return h ? oneLine(h, max) : oneLine(i.root_cause ?? "", max);
}

export function oneLine(rc: string, max = 54) {
  const s = plain(brief(rc)).replace(/\s+/g, " ").trim();
  if (!s || s === "（无结论）") return "（暂无结论）";
  const first = s.split(/[。；;！？!?]|\.\s/)[0] || s;
  if (first.length <= max) return first;
  const cut = first.lastIndexOf("，", max);
  if (cut > 16) return first.slice(0, cut);
  // 别把 IP 从中间截断：半截 IP 认不出来，演示模式的脱敏就漏了。
  let head = first.slice(0, max);
  if (/[\d.]/.test(first[max] ?? "")) head = head.replace(/[\d.]+$/, "");
  return head + "…";
}

/** `t` 是调用方组件顶层已经拿到的 `useT()` 结果，这里不是 hook，可以在 `.map()` 里安全调用。 */
export function fmtDur(t: (k: DictKey) => string, sec?: number) {
  if (sec == null || !isFinite(sec)) return "—";
  if (sec < 60) return fill(t("fmtDur.seconds"), { n: Math.round(sec) });
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return s ? fill(t("fmtDur.minSec"), { m, s }) : fill(t("fmtDur.minOnly"), { m });
}

export const REPEAT_HINT = (t: (k: DictKey) => string, n: number) => fill(t("repeatHintTemplate"), { n });
export const MERGE_HINT = (t: (k: DictKey) => string, n: number) => fill(t("mergeHintTemplate"), { n });
