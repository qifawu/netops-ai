import type { DictKey } from "./i18n";

export const TOOL_LABEL_KEYS: Record<string, DictKey> = {
  device_show: "tool.device_show",
  device_show_many: "tool.device_show_many",
  sop_lookup: "tool.sop_lookup",
  fetch_syslog_fallback: "tool.fetch_syslog_fallback",
  zbx_history: "tool.zbx_history",
  zbx_trends: "tool.zbx_trends",
  zbx_chart: "tool.zbx_chart",
  zbx_top_talkers: "tool.zbx_top_talkers",
  zbx_items: "tool.zbx_items",
  nb_topology: "tool.nb_topology",
  topology_neighbors: "tool.topology_neighbors",
  doc_search: "tool.doc_search",
  zbx_problems: "tool.zbx_problems",
  zbx_syslog: "tool.zbx_syslog",
  zbx_hosts: "tool.zbx_hosts",
  run_inspection: "tool.run_inspection",
  list_analyses: "tool.list_analyses",
  get_analysis: "tool.get_analysis",
  nb_devices: "tool.nb_devices",
};

/** `t` 是调用方组件顶层的 `useT()`——这俩不是 hook，可以在 `.map()` 循环里安全调用。
 *  后端多数时候会给 `tool_label`（比如"查当前告警"，这是 AI/Python 生成的中文，不在这个
 *  字典管辖范围内，跟着后端语言走，不随界面语言切换）；只有后端没给、或者给的就是工具原名
 *  （doc_search）时，才用这张前端表兜底翻译。 */
export const toolLabel = (t: (k: DictKey) => string, name: string) =>
  (TOOL_LABEL_KEYS[name] && t(TOOL_LABEL_KEYS[name])) || name || t("tool.fallback");

export const stepLabel = (t: (k: DictKey) => string, step: { tool?: string; tool_label?: string }) =>
  // 已登记的工具一律用前端字典，随界面语言切换；字典里没有的才用后端给的 tool_label
  (step.tool && TOOL_LABEL_KEYS[step.tool]) ? toolLabel(t, step.tool)
    : (step.tool_label && step.tool_label !== step.tool ? step.tool_label : toolLabel(t, step.tool ?? ""));
