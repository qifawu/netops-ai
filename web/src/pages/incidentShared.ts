import type { DictKey } from "../lib/i18n";

/** 后端 `incident_history.fault_type_of` 给的类型键 → 字典键。跟 `lib/incident.ts::faultType` 是同一套分类。 */
export const FAULT_KEY: Record<string, DictKey> = {
  restart: "faultType.restart", interfaceDown: "faultType.interfaceDown", bgp: "faultType.bgp", ospf: "faultType.ospf",
  unreachable: "faultType.unreachable", monitoringDown: "faultType.monitoringDown", configChange: "faultType.configChange",
  other: "faultType.other",
};

const loc = (lang: string) => (lang === "en" ? "en-US" : "zh-CN");

/** `14:03` */
export const hm = (s: number) =>
  new Date(s * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });

/** `10-11 14:03` */
export const dayTime = (s: number, _lang = "zh") => {
  const d = new Date(s * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

/** 日期分隔头：`10-11 周六` / `10-11 Sat` */
export const dayHead = (s: number, lang: string) => {
  const d = new Date(s * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${d.toLocaleDateString(loc(lang), { weekday: "short" })}`;
};

export const dayKey = (s: number) => {
  const d = new Date(s * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
};
