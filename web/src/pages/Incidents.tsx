import { Fragment, useEffect, useState, type ReactNode } from "react";
import { brief, cn, plain, ts } from "../lib/api";
import { AiInline, AiText } from "../lib/aitext";
import { stepLabel } from "../lib/labels";
import { Badge, Card, Empty, Icon, Loading, PATH } from "../lib/ui";
import { MERGE_HINT, REPEAT_HINT, confLevel, confMeta, fmtDur, titleOf } from "../lib/incident";
import { takePending } from "../lib/nav";
import { registerIds } from "../lib/mask";
import { fill, useLang, useT, type DictKey } from "../lib/i18n";
import DeviceHistory, { Seg } from "./DeviceHistory";
import HelpTip from "../lib/HelpTip";
import { FAULT_KEY, dayHead, dayKey, dayTime, hm } from "./incidentShared";

/** 「告警与结论」页 = 左边历史告警（筛选 + 按天分组 + 同类折叠 + 分页），右边一次故障的详情（页签）
 *  或一台设备的排查历史。
 *
 * 以前左边每条堆两行结论 + 好几个徽标，右边六张卡片一路铺下去，挤。现在（又收了一轮：列表两行、标签改纯文字、不再列总数）：
 *  - 列表一次故障一行，同一指纹（同设备同类故障）折成一行「同类 ×N」，点开才看每次；
 *  - 详情默认只给「结论」，证据 / 告警 / 调查步骤 / 假设清单 / 排查历史各自一个页签；
 *  - token、编号、指纹、系统标出的矛盾收进「更多信息」，矛盾条数在折叠标题上露出来，不藏。 */

type Alert = { eventid: string; name: string; clock: number; role: string; role_cn: string; caused_by: string; reason: string };
type Ev = { claim: string; source: string; source_from: string; source_label?: string };
type Check = { status: string; reason: string };
type Inc = {
  incident_id: string; fingerprint: string; host: string; clock: number; next_step: string;
  root_cause: string; headline?: string; confidence: string; repeat_count?: number; alert_count: number;
  alerts: Alert[]; evidence: Ev[]; undistinguishable: any[];
  checklist?: Record<string, Check>;
  related: { eventid: string; host: string; link: string; reason: string }[];
  trace: { tool: string; tool_label?: string; args: any; ok: boolean; result?: any; elapsed_ms?: number; error?: string }[];
  token_cost: { forensics: number; analysis: number; other: number; total: number };
  elapsed: Record<string, number>; elapsed_total: number; feishu: { sent?: boolean }; incomplete: boolean;
  rule_violations?: string[]; fault_type?: string; needs_human?: boolean; devices?: string[];
};
type Brief = {
  incident_id: string; fingerprint: string; host: string; devices: string[]; clock: number; headline: string; root_cause: string;
  confidence: string; fault_type: string; alert_count: number; repeat_count?: number | null; elapsed_total?: number;
  needs_human: boolean; consistent?: boolean | null;
};
type Row = { fingerprint: string; host: string; devices: string[]; fault_type: string; count: number; latest: Brief; occurrences: Brief[] };
type Facet = { value: string; count: number };
type ListResp = {
  total_incidents: number; total_rows: number; page: number; has_more: boolean; rows: Row[];
  facets: { devices: Facet[]; types: Facet[]; confidence: Facet[]; needs_human: number; in_range: number };
};
type Range = "24h" | "7d" | "30d" | "all";
type Filters = { range: Range; device: string; type: string; confidence: string; unresolved: boolean };

const ROLE_COLOR: Record<string, string> = {
  root: "bg-rose-50 text-rose-700 ring-rose-200",
  consequence: "bg-slate-100 text-slate-600 ring-slate-200",
  independent: "bg-white text-slate-700 ring-slate-300",
};
const ROLE_KEY: Record<string, DictKey> = { root: "incidents.role.root", consequence: "incidents.role.consequence", independent: "incidents.role.independent" };
/** 后端有的记录 role 是英文枚举、有的是中文，这里统一成英文键。 */
const roleKey = (r: string) => (r.includes("根因") ? "root" : r.includes("连带") ? "consequence" : r.includes("独立") ? "independent" : r);

/** 排查方向：AI 对六个可能方向逐个下了判断。名字翻成运维人员一眼能懂的话。 */
const CHECK_KEY: Record<string, DictKey> = {
  local_action: "incidents.check.localAction",
  local_hardware_or_resource: "incidents.check.localHardware",
  remote_or_upstream: "incidents.check.remote",
  link_or_path_quality: "incidents.check.link",
  management_plane_or_reachability: "incidents.check.mgmt",
  monitoring_or_collection_artifact: "incidents.check.monitoring",
};
const checkTone = (s: string) =>
  /支持|成立|确认/.test(s) ? "bg-rose-50 text-rose-700 ring-rose-200"
  : /排除|不成立/.test(s) ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
  : "bg-amber-50 text-amber-700 ring-amber-200";
/** 后端 `analysis/schema.py` 的 STATUS_SUPPORTED/STATUS_RULED_OUT/STATUS_CANNOT_DETERMINE
 *  是固定 3 项枚举（不是 AI 自由生成文本），比照 ROLE_KEY/CHECK_KEY 的先例翻译；
 *  翻不到的值原样透传，不会因为后端加新值就崩。 */
const STATUS_KEY: Record<string, DictKey> = {
  有证据支持: "incidents.checklist.status.supported",
  已排除: "incidents.checklist.status.ruledOut",
  暂时无法判断: "incidents.checklist.status.cannotDetermine",
};
/** 置信度文字色，按 confLevel（0 未评级 / 1 低 / 2 中 / 3 高）取。 */
const CONF_TEXT = ["text-slate-500", "text-rose-700", "text-amber-700", "text-emerald-700"];
const CONF_KEY: Record<string, DictKey> = { high: "conf.high.long", medium: "conf.medium.long", low: "conf.low.long", none: "conf.none.label" };

/** 设备与拓扑页「查看排查历史」跳过来时带 `#incidents?device=V1&view=history`。 */
function hashParams() {
  const q = location.hash.split("?")[1] ?? "";
  return new URLSearchParams(q);
}

const PAGE_SIZE = 30;
const selCls = "h-8 min-w-0 rounded-[8px] border border-line bg-white px-2 text-xs text-slate-700 hover:border-slate-400 focus:border-brand focus:outline-none";

export default function Incidents() {
  const t = useT();
  const lang = useLang();
  const init = hashParams();
  const [f, setF] = useState<Filters>({ range: "all", device: init.get("device") ?? "", type: "", confidence: "", unresolved: false });
  const [data, setData] = useState<ListResp | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [sel, setSel] = useState<string>(() => takePending());
  /** 右边显示什么：一次故障的详情，或者某台设备的排查历史。 */
  const [devView, setDevView] = useState<string>(init.get("view") === "history" ? init.get("device") ?? "" : "");

  const fetchPage = (page: number, filters: Filters) => {
    const q = new URLSearchParams({ range: filters.range, page: String(page), page_size: String(PAGE_SIZE) });
    if (filters.device) q.set("device", filters.device);
    if (filters.type) q.set("type", filters.type);
    if (filters.confidence) q.set("confidence", filters.confidence);
    if (filters.unresolved) q.set("unresolved", "1");
    setBusy(true);
    return fetch(`/api/incidents/list?${q}`)
      .then((r) => (r.ok ? (r.json() as Promise<ListResp>) : Promise.reject()))
      .then((d) => {
        setFailed(false);
        setData(d);
        setRows((prev) => (page === 1 ? d.rows : [...prev, ...d.rows]));
        if (page === 1) setSel((cur) => cur || d.rows[0]?.latest.incident_id || "");
      })
      .catch(() => setFailed(true))
      .finally(() => setBusy(false));
  };
  useEffect(() => { fetchPage(1, f); }, [f]);

  const set = (patch: Partial<Filters>) => setF((cur) => ({ ...cur, ...patch }));
  const pick = (id: string) => {
    setSel(id); setDevView("");
    if (location.hash.includes("view=history")) history.replaceState(null, "", "#incidents");
  };
  const toggle = (fp: string) => setOpen((cur) => { const n = new Set(cur); n.has(fp) ? n.delete(fp) : n.add(fp); return n; });

  if (!data && !failed) return <Loading />;
  if (failed && !data) return <Card><Empty title={t("hist.list.loadFailed")} /></Card>;
  const facets = data!.facets;
  const noData = facets.in_range === 0 && f.range === "all" && !f.device;
  if (noData) return <Card><Empty title={t("incidents.list.empty.title")} /></Card>;
  const filtered = !!(f.device || f.type || f.confidence || f.unresolved);

  // 按「这一行最近一次出现」的日期分组
  const days: { key: string; head: string; rows: Row[] }[] = [];
  for (const r of rows) {
    const k = dayKey(r.latest.clock);
    if (days[days.length - 1]?.key !== k) days.push({ key: k, head: dayHead(r.latest.clock, lang), rows: [] });
    days[days.length - 1].rows.push(r);
  }

  return (
    <div className="flex items-start gap-5">
      <div className="sticky top-[80px] flex max-h-[calc(100vh-104px)] w-[23rem] shrink-0 flex-col xl:w-[26rem] min-[1920px]:w-[32rem]">
        {/* 筛选条：时间范围 + 需人工一行，设备 / 类型 / 置信度一行。总数不再单列——总览里已经有了。 */}
        <Card className="mb-3 space-y-2.5 p-3">
          <div className="flex items-center gap-2">
            <Seg<Range> value={f.range} onChange={(v) => set({ range: v })}
              items={[["24h", t("hist.range.24h")], ["7d", t("hist.range.7d")], ["30d", t("hist.range.30d")], ["all", t("hist.range.all")]]} />
            <span className="ml-auto flex items-center gap-1">
              <button type="button" aria-pressed={f.unresolved} onClick={() => set({ unresolved: !f.unresolved })}
                className={cn("flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs whitespace-nowrap transition",
                  f.unresolved ? "border-rose-200 bg-rose-50 font-medium text-rose-700" : "border-line text-slate-700 hover:border-slate-400")}>
                {t("hist.filter.unresolved")}<span className="num">{facets.needs_human}</span>
              </button>
              <HelpTip text={t("hist.filter.unresolved.title")} />
            </span>
          </div>
          <div className="grid grid-cols-[1fr_1fr_1fr_auto] items-center gap-2">
            <select value={f.device} onChange={(e) => set({ device: e.target.value })} className={selCls} aria-label={t("hist.filter.device.all")}>
              <option value="">{t("hist.filter.device.all")}</option>
              {f.device && !facets.devices.some((d) => d.value === f.device) && <option value={f.device}>{f.device}</option>}
              {facets.devices.map((d) => <option key={d.value} value={d.value}>{d.value} ({d.count})</option>)}
            </select>
            <select value={f.type} onChange={(e) => set({ type: e.target.value })} className={selCls} aria-label={t("hist.filter.type.all")}>
              <option value="">{t("hist.filter.type.all")}</option>
              {facets.types.map((x) => <option key={x.value} value={x.value}>{t(FAULT_KEY[x.value] ?? "faultType.other")} ({x.count})</option>)}
            </select>
            <select value={f.confidence} onChange={(e) => set({ confidence: e.target.value })} className={selCls} aria-label={t("hist.filter.conf.all")}>
              <option value="">{t("hist.filter.conf.all")}</option>
              {facets.confidence.map((x) => <option key={x.value} value={x.value}>{t(CONF_KEY[x.value] ?? "conf.none.label")} ({x.count})</option>)}
            </select>
            <button type="button" onClick={() => set({ device: "", type: "", confidence: "", unresolved: false })} disabled={!filtered}
              className="px-1 text-xs text-brand hover:underline disabled:invisible">{t("hist.filter.reset")}</button>
          </div>
        </Card>

        <Card className="min-h-0 flex-1 overflow-y-auto pb-1">
          {rows.length === 0 ? <Empty title={t("hist.list.empty")} hint={t("hist.list.emptyHint")} /> : days.map((d) => (
            <div key={d.key}>
              <div className="sticky top-0 z-10 flex items-center justify-between bg-white/95 px-4 pt-3 pb-1.5 text-xs font-medium text-slate-500 backdrop-blur">
                <span className="num">{d.head}</span>
                <span className="num font-normal">{d.rows.reduce((n, r) => n + r.count, 0)}</span>
              </div>
              {d.rows.map((r) => (
                <ListRow key={r.fingerprint} r={r} sel={sel} devView={!!devView} open={open.has(r.fingerprint)} onPick={pick} onToggle={() => toggle(r.fingerprint)} />
              ))}
            </div>
          ))}
          {data!.has_more && (
            <button type="button" disabled={busy} onClick={() => fetchPage(data!.page + 1, f)}
              className="mx-auto my-2 block h-8 w-[calc(100%-2rem)] rounded-[8px] border border-line text-xs font-medium text-slate-700 hover:border-slate-400 disabled:text-dim">
              {busy ? t("hist.list.loading") : fill(t("hist.list.loadMore"), { n: data!.total_rows - rows.length })}
            </button>
          )}
        </Card>
      </div>

      <div className="min-w-0 flex-1">
        {devView ? (
          <Card className="overflow-hidden">
            <div className="flex min-h-[48px] items-center gap-3 border-b border-line px-5 py-2.5">
              <Icon d={PATH.clock} className="text-slate-500" />
              <span className="text-[0.9375rem] font-semibold text-slate-900">{devView}</span>
              <span className="text-[0.8125rem] font-semibold text-slate-800">{t("hist.tab.history")}</span>
              <HelpTip text={t("hist.dev.note")} />
              {sel && <button type="button" onClick={() => pick(sel)} className="ml-auto shrink-0 text-xs font-medium text-brand hover:underline">{t("hist.dev.back")}</button>}
            </div>
            <DeviceHistory device={devView} currentId={sel} onPick={pick} />
          </Card>
        ) : sel ? <Detail id={sel} onPick={pick} onDevice={(d) => { setDevView(d); set({ device: d }); }} /> : null}
      </div>
    </div>
  );
}

/** 列表一行：时间 · 置信度小方点 · 设备 · 类型 · 一句话结论（单行省略）· 合并 / 同类标记。 */
function ListRow({ r, sel, devView, open, onPick, onToggle }: { r: Row; sel: string; devView: boolean; open: boolean; onPick: (id: string) => void; onToggle: () => void }) {
  const t = useT();
  const lang = useLang();
  const b = r.latest;
  const c = confMeta(b.confidence);
  const title = titleOf(b, 80);
  const inGroup = r.occurrences.some((o) => o.incident_id === sel);
  const on = !devView && (b.incident_id === sel || (inGroup && !open));
  const meta: string[] = [t(FAULT_KEY[r.fault_type] ?? "faultType.other")];
  if (b.alert_count > 1) meta.push(fill(t("incidents.mergedBadge"), { n: b.alert_count }));
  return (
    <div>
      <div className={cn("relative mx-2 rounded-[8px]", on ? "bg-brand/[0.07]" : "hover:bg-slate-50")}>
        {on && <span className="absolute inset-y-2 left-0 w-[3px] rounded-full bg-brand" />}
        <button type="button" onClick={() => onPick(b.incident_id)} title={title}
          className="flex w-full min-w-0 gap-3 py-2.5 pr-3 pl-3.5 text-left">
          <span className={cn("mt-[6px] h-2 w-2 shrink-0 rounded-full", c.dot)} title={`${t(c.longKey)}：${t(c.hintKey)}`} />
          <span className="flex min-w-0 flex-1 flex-col gap-0.5">
            <span className="flex min-w-0 items-baseline gap-2 text-xs text-dim">
              <span className="max-w-[9rem] shrink-0 truncate text-[0.8125rem] font-medium text-slate-900">{r.devices.join(" / ") || b.host}</span>
              <span className="truncate">{meta.join(" · ")}</span>
              <span className="num ml-auto shrink-0">{hm(b.clock)}</span>
            </span>
            <span className={cn("truncate text-sm text-slate-800", r.count > 1 && "pr-16")}><AiText text={title} lowPriority /></span>
          </span>
        </button>
        {r.count > 1 && (
          <button type="button" onClick={onToggle} title={fill(t("hist.row.sameTypeTitle"), { n: r.count })} aria-expanded={open}
            className={cn("num absolute right-2.5 bottom-2 flex items-center gap-0.5 rounded-full px-2 text-xs leading-5 whitespace-nowrap transition",
              open ? "bg-brand text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200")}>
            {fill(t("hist.row.sameType"), { n: r.count })}
            <svg viewBox="0 0 12 12" className={cn("h-2.5 w-2.5 transition", open && "rotate-180")} fill="none" stroke="currentColor" strokeWidth="1.6"><path d="m3 4.5 3 3 3-3" /></svg>
          </button>
        )}
      </div>
      {open && (
        <ol className="mx-2 mb-1 rounded-[8px] bg-slate-50 py-1">
          {r.occurrences.map((o) => {
            const oc = confMeta(o.confidence);
            const active = !devView && o.incident_id === sel;
            return (
              <li key={o.incident_id}>
                <button type="button" onClick={() => onPick(o.incident_id)}
                  className={cn("relative grid w-full grid-cols-[10px_80px_8px_minmax(0,1fr)_auto] items-center gap-x-2 py-[5px] pr-3 pl-5 text-left text-xs",
                    active ? "bg-brand/[0.08]" : "hover:bg-white")}>
                  {active && <span className="absolute inset-y-1 left-0 w-[3px] rounded-full bg-brand" />}
                  <span className="h-full w-px justify-self-center bg-line" />
                  <span className="num text-dim">{dayTime(o.clock, lang)}</span>
                  <span className={cn("h-2 w-2 rounded-full", oc.dot)} title={t(oc.longKey)} />
                  <span className={cn("truncate", o.consistent === false ? "text-amber-700" : "text-slate-600")} title={t("hist.occ.consistentTitle")}>
                    {o.consistent === false ? t("hist.occ.inconsistent") : t("hist.occ.consistent")} · {t(oc.longKey)}
                  </span>
                  <span className="num text-dim">{fmtDur(t, o.elapsed_total)}</span>
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

type Tab = "conclusion" | "evidence" | "alerts" | "steps" | "hypotheses" | "history";

function Detail({ id, onPick, onDevice }: { id: string; onPick: (id: string) => void; onDevice: (d: string) => void }) {
  const t = useT();
  const lang = useLang();
  const [cur, setCur] = useState<Inc | null>(null);
  const [failed, setFailed] = useState(false);
  const [tab, setTab] = useState<Tab>("conclusion");
  const [openStep, setOpenStep] = useState<number | null>(null);
  useEffect(() => {
    setFailed(false); setOpenStep(null);
    fetch(`/api/incidents/${encodeURIComponent(id)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((x: Inc) => {
        // 先登记这次故障涉及的告警编号再渲染：演示模式下理由正文里的裸编号也要遮
        registerIds([...x.alerts.flatMap((a) => [a.eventid, a.caused_by]), ...(x.related ?? []).map((r) => r.eventid)]);
        setCur(x);
      })
      .catch(() => setFailed(true));
  }, [id]);
  useEffect(() => { setTab((cur) => (cur === "history" ? cur : "conclusion")); }, [id]);
  if (failed) return <Card><Empty title={t("hist.detail.loadFailed")} /></Card>;
  if (!cur || cur.incident_id !== id) return <Loading />;

  const ft = FAULT_KEY[cur.fault_type ?? "other"] ?? "faultType.other";
  const c = confMeta(cur.confidence), lvl = confLevel(cur.confidence);
  const full = brief(cur.root_cause);
  const checks = Object.entries(cur.checklist ?? {}).filter(([, v]) => v && typeof v === "object");
  const device = cur.devices?.[0] || cur.host.split(" / ")[0];
  const violations = cur.rule_violations ?? [];
  const tabs: [Tab, DictKey, number | null][] = [
    ["conclusion", "hist.tab.conclusion", null],
    ["evidence", "hist.tab.evidence", cur.evidence.length],
    ["alerts", "hist.tab.alerts", cur.alerts.length],
    ["steps", "hist.tab.steps", cur.trace.length],
    ["hypotheses", "hist.tab.hypotheses", checks.length],
    ["history", "hist.tab.history", null],
  ];

  return (
    <div className="space-y-4">
      {/* 头：这是什么故障、在哪、AI 怎么看。只留一行标签 + 标题 + 一行元信息。 */}
      <Card className="px-6 py-5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.8125rem] text-dim">
          <span title={t(c.hintKey)} className={cn("flex items-center gap-1.5 font-medium", CONF_TEXT[lvl])}>
            <span className={cn("h-2 w-2 rounded-full", c.dot)} />{t(c.longKey)}{cur.needs_human && ` · ${t("hist.detail.needsHuman")}`}
          </span>
          <span>·</span><span>{t(ft)}</span>
          <span className="ml-auto flex items-center gap-2">
            <button type="button" onClick={() => setTab("history")}
              className="h-8 rounded-[8px] border border-line px-3 text-xs font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50">{t("hist.detail.viewHistory")}</button>
            <a href={`/api/records/${cur.alerts[0]?.eventid}/report.md`}
              className="flex h-8 items-center rounded-[8px] border border-line px-3 text-xs font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50">{t("incidents.exportReport")}</a>
          </span>
        </div>
        <h1 className="mt-2 text-[1.25rem] leading-snug font-semibold text-slate-900"><AiText text={titleOf(cur, 90)} /></h1>
        <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[0.8125rem] text-dim">
          <span>{t("incidents.meta.device")} <b className="font-medium text-slate-900">{cur.host}</b></span>
          <span>{t("incidents.meta.occurredAt")} <b className="num font-medium text-slate-900">{ts(cur.clock)}</b></span>
          <span>{t("incidents.meta.elapsed")} <b className="num font-medium text-slate-900">{fmtDur(t, cur.elapsed_total)}</b></span>
          {cur.alert_count > 1 && <span title={MERGE_HINT(t, cur.alert_count)}>{fill(t("incidents.mergedBadge"), { n: cur.alert_count })}</span>}
          {(cur.repeat_count ?? 0) > 1 && <span title={REPEAT_HINT(t, cur.repeat_count!)}>{fill(t("incidents.repeatBadge"), { n: cur.repeat_count! })}</span>}
        </div>
      </Card>

      {/* 研判摘要：这件事是什么方向、波及哪里、要不要人处理、多少条告警收敛成了这一个事件。只放数据，不放说明文字。 */}
      <Card className="grid grid-cols-2 divide-line xl:grid-cols-4 xl:divide-x">
        {(() => {
          const supported = checks.filter(([, v]) => /支持|成立|确认|supported/i.test(String(v.status))).map(([k]) => (CHECK_KEY[k] ? t(CHECK_KEY[k]) : k));
          const devs = cur.devices?.length ? cur.devices : [cur.host];
          const cell = (label: string, body: ReactNode, sub?: ReactNode) => (
            <div className="min-w-0 px-5 py-4">
              <div className="text-xs text-slate-500">{label}</div>
              <div className="mt-1 text-sm leading-snug font-semibold text-slate-900">{body}</div>
              {sub && <div className="mt-0.5 text-xs text-slate-500">{sub}</div>}
            </div>
          );
          return (
            <>
              {cell(t("hist.judge.nature"), supported.length ? supported.join("、") : <span className="text-slate-400">{t("hist.judge.undetermined")}</span>)}
              {cell(t("hist.judge.scope"), fill(t("hist.judge.scopeDevices"), { n: devs.length }),
                (cur.related ?? []).length > 0 ? fill(t("hist.judge.scopeRelated"), { n: cur.related.length }) : devs.join("、"))}
              {cell(t("hist.judge.action"), <span className={cur.needs_human ? "text-rose-600" : "text-emerald-600"}>{t(cur.needs_human ? "hist.judge.needsHuman" : "hist.judge.noIntervention")}</span>)}
              {cell(t("hist.judge.converge"), fill(t("hist.judge.convergeValue"), { n: cur.alert_count }),
                (cur.repeat_count ?? 0) > 1 ? fill(t("incidents.repeatBadge"), { n: cur.repeat_count! }) : undefined)}
            </>
          );
        })()}
      </Card>

      <Card className="overflow-hidden">
        {/* 页签：窄屏横向滚动，不折行。数字角标只是数量，不上色。 */}
        <div className="flex gap-6 overflow-x-auto border-b border-line px-6" role="tablist">
          {tabs.map(([k, label, n]) => (
            <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => setTab(k)}
              className={cn("relative flex shrink-0 items-center gap-1.5 py-3 text-sm whitespace-nowrap transition",
                tab === k ? "font-medium text-slate-900" : "text-slate-500 hover:text-slate-900")}>
              {tab === k && <span className="absolute inset-x-0 bottom-0 h-[2px] rounded-full bg-brand" />}
              {t(label)}
              {n != null && <span className="num text-xs text-slate-400">{n}</span>}
            </button>
          ))}
        </div>

        {tab === "conclusion" && (
          <div>
            <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]">
              <div className="px-6 py-5">
                <h3 className="mb-2 text-sm font-semibold text-slate-900">{t("incidents.rootCause.title")}</h3>
                <div className="text-sm leading-7 text-slate-700">
                  <AiInline text={full} />
                  {lang === "zh" && full !== cur.root_cause && <span className="text-slate-400">{cur.root_cause.slice(full.length)}</span>}
                </div>
                {(cur.related ?? []).length > 0 && (
                  <div className="mt-4 rounded-[10px] bg-slate-50 px-4 py-3 text-[0.8125rem] leading-relaxed text-slate-800">
                    <div className="mb-0.5 font-semibold">{t("incidents.related.title")}</div>
                    {cur.related.map((r) => (
                      <div key={r.eventid}><span data-mask="id">#{r.eventid}</span> {r.host}：{r.link}<span className="text-slate-500">（<AiInline text={r.reason} />）</span></div>
                    ))}
                  </div>
                )}
              </div>
              <div className="divide-y divide-line border-t border-line xl:border-t-0 xl:border-l">
                <div className="px-6 py-5">
                  <h3 className="flex items-center gap-1 text-sm font-semibold text-slate-900">{t("incidents.confidence.title")}<HelpTip text={t(c.hintKey)} /></h3>
                  <div className={cn("mt-2 text-[1.375rem] leading-none font-semibold", lvl === 3 ? "text-emerald-600" : lvl === 2 ? "text-amber-600" : lvl === 1 ? "text-rose-600" : "text-slate-400")}>{t(c.labelKey)}</div>
                  <div className="mt-2.5 flex gap-1.5">
                    {[1, 2, 3].map((k) => <span key={k} className={cn("h-1.5 flex-1 rounded-full", k <= lvl ? c.bar : "bg-slate-200")} />)}
                  </div>
                </div>
                {cur.next_step && (
                  <div className="px-6 py-5">
                    <h3 className="mb-2 text-sm font-semibold text-slate-900">{t("incidents.nextStep.title")}</h3>
                    <div className="text-sm leading-relaxed text-slate-700"><AiInline text={cur.next_step} /></div>
                  </div>
                )}
              </div>
            </div>
            <More cur={cur} violations={violations} />
          </div>
        )}

        {tab === "evidence" && (cur.evidence.length === 0 ? <Empty title={t("incidents.evidence.empty")} /> : (
          <ol className="divide-y divide-line">
            {cur.evidence.map((e, i) => (
              <li key={i} className="flex gap-3 px-5 py-3">
                <span className="num mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[0.6875rem] font-medium text-slate-600">{i + 1}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-start gap-2">
                    <div className="min-w-0 flex-1 text-sm leading-relaxed text-slate-800"><AiInline text={e.claim} /></div>
                    <Badge className="bg-white text-slate-600 ring-slate-300">
                      {e.source_from === "zabbix" || e.source_from === "监控" ? t("incidents.evidence.monitoring") : t("incidents.evidence.device")}
                    </Badge>
                  </div>
                  <div className="term mt-2 overflow-x-auto rounded-[2px] px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap">{e.source}</div>
                </div>
              </li>
            ))}
          </ol>
        ))}

        {tab === "alerts" && (
          <ol className="px-5 py-4">
            {cur.alerts.map((a, i) => (
              <li key={a.eventid} className="relative flex gap-3 pb-4 last:pb-0">
                {i < cur.alerts.length - 1 && <span className="absolute top-4 bottom-0 left-[5px] w-px bg-line" />}
                <span className={cn("relative mt-1.5 h-[11px] w-[11px] shrink-0 rounded-full ring-2 ring-white", roleKey(a.role) === "root" ? "bg-rose-500" : "bg-slate-300")} />
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="num text-xs text-dim">{ts(a.clock)}</span>
                    <span className="text-sm font-medium text-slate-800">{a.name}</span>
                    <Badge className={ROLE_COLOR[roleKey(a.role)] ?? "bg-slate-100 text-slate-600 ring-slate-200"}>{(ROLE_KEY[roleKey(a.role)] && t(ROLE_KEY[roleKey(a.role)])) || a.role_cn || a.role || t("incidents.role.unlabeled")}</Badge>
                  </div>
                  {(a.caused_by || a.reason) && (
                    <div className="mt-0.5 text-xs leading-relaxed text-dim">
                      {a.caused_by && <>{t("incidents.alertTimeline.causedBy")} <span data-mask="id">#{a.caused_by}</span> {t("incidents.alertTimeline.causedBySuffix")}</>}
                      {a.caused_by && a.reason && " · "}
                      {a.reason && <AiInline text={plain(a.reason)} />}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}

        {tab === "steps" && (cur.trace.length === 0 ? <Empty title={t("incidents.trace.empty")} /> : (
          <ol className="divide-y divide-line">
            {cur.trace.map((step, i) => (
              <li key={i}>
                <button type="button" onClick={() => setOpenStep(openStep === i ? null : i)}
                  className="grid w-full grid-cols-[24px_8px_minmax(0,auto)_minmax(0,1fr)_auto_auto] items-center gap-x-2.5 px-5 py-2 text-left hover:bg-slate-50">
                  <span className="num text-xs text-dim">{i + 1}</span>
                  <span className={cn("h-2 w-2 rounded-full", step.ok ? "bg-emerald-500" : "bg-rose-500")} title={step.ok ? "ok" : t("incidents.trace.failed")} />
                  <span title={step.tool} className="truncate text-[0.8125rem] font-medium text-slate-800">{stepLabel(t, step)}</span>
                  <span className="truncate font-mono text-xs text-dim">
                    {typeof step.args === "object" ? Object.values(step.args ?? {}).flat().filter((x) => x !== "").join(" · ") : String(step.args)}
                  </span>
                  <span className="num text-xs text-slate-500">{step.elapsed_ms == null ? "" : step.elapsed_ms >= 1000 ? `${(step.elapsed_ms / 1000).toFixed(1)}${t("incidents.trace.seconds")}` : `${step.elapsed_ms}${t("incidents.trace.ms")}`}</span>
                  <span className="shrink-0 text-xs text-brand">{openStep === i ? t("incidents.trace.collapse") : t("incidents.trace.expand")}</span>
                </button>
                {openStep === i && (
                  <div className="mx-5 mb-3 space-y-2 rounded-[3px] border border-line bg-slate-50 p-3">
                    <div>
                      <div className="mb-1 text-xs font-medium text-slate-500">{t("incidents.trace.args")}</div>
                      <pre className="overflow-x-auto whitespace-pre-wrap break-all font-mono text-xs text-slate-700">{JSON.stringify(step.args ?? {}, null, 2)}</pre>
                    </div>
                    <div>
                      <div className="mb-1 text-xs font-medium text-slate-500">{step.ok ? t("incidents.trace.result") : t("incidents.trace.failReason")}</div>
                      {step.result !== undefined ? (
                        <pre className="term max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-[2px] p-2.5 text-xs">
                          {typeof step.result === "string" ? step.result : JSON.stringify(step.result, null, 2)}
                        </pre>
                      ) : step.error ? (
                        <pre className="term max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-[2px] p-2.5 text-xs text-rose-300">{step.error}</pre>
                      ) : (
                        <div className="text-xs text-dim">{t("incidents.trace.noResult")}</div>
                      )}
                    </div>
                  </div>
                )}
              </li>
            ))}
          </ol>
        ))}

        {tab === "hypotheses" && (
          <div>
            {checks.length === 0 ? <Empty title={t("hist.hypotheses.empty")} /> : (
              <>
                <div className="grid grid-cols-1 divide-line md:grid-cols-2 [&>*]:border-b [&>*]:border-line md:[&>*:nth-child(odd)]:border-r">
                  {checks.map(([k, v]) => (
                    <div key={k} className="px-5 py-3">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-slate-800">{(CHECK_KEY[k] && t(CHECK_KEY[k])) || k}</span>
                        <Badge className={checkTone(v.status)}>{(STATUS_KEY[v.status] && t(STATUS_KEY[v.status])) || v.status}</Badge>
                      </div>
                      <p className="mt-1 text-xs leading-relaxed text-dim"><AiText text={plain(v.reason)} /></p>
                    </div>
                  ))}
                </div>
              </>
            )}
            {cur.undistinguishable.length > 0 && (
              <div>
                <div className="flex items-center gap-1 border-b border-line px-6 py-3 text-sm font-semibold text-slate-900">
                  {t("incidents.undistinguishable.title")}<HelpTip text={t("incidents.undistinguishable.note")} />
                </div>
                <div className="divide-y divide-line">
                  {cur.undistinguishable.map((u: any, i: number) => (
                    <div key={i} className="px-5 py-3 text-sm">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {(u.candidates ?? []).map((cnd: string, j: number) => (
                          <Badge key={cnd} title={u.candidate_codes?.[j] || cnd} className="bg-amber-50 text-amber-700 ring-amber-200">{cnd}</Badge>
                        ))}
                      </div>
                      {u.why_indistinguishable && <div className="mt-2 text-[0.8125rem] leading-relaxed text-slate-600"><AiInline text={u.why_indistinguishable} /></div>}
                      {u.what_data_would_help && <div className="mt-1.5 text-[0.8125rem] leading-relaxed text-amber-700">{t("incidents.undistinguishable.missingLabel")}<AiInline text={u.what_data_would_help} /></div>}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {tab === "history" && (
          <div>
            <div className="flex items-center gap-2 border-b border-line px-4 py-2 text-xs text-dim">
              <span className="font-semibold text-slate-900">{device}</span>
              <HelpTip text={t("hist.dev.note")} />
              <button type="button" onClick={() => onDevice(device)} className="ml-auto shrink-0 font-medium text-brand hover:underline">{t("hist.dev.openWide")}</button>
            </div>
            <DeviceHistory device={device} currentId={cur.incident_id} onPick={onPick} />
          </div>
        )}
      </Card>
    </div>
  );
}

/** 「更多信息」：token 用量、编号、指纹、推送状态、系统标出的矛盾。默认收起，矛盾条数露在标题上。 */
function More({ cur, violations }: { cur: Inc; violations: string[] }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  useEffect(() => setOpen(false), [cur.incident_id]);
  const tc = cur.token_cost ?? { forensics: 0, analysis: 0, other: 0, total: 0 };
  const rows: [DictKey, ReactNode, boolean?][] = [
    ["hist.more.tokens", <>{(tc.total ?? 0).toLocaleString()} <span className="text-dim">({fill(t("hist.more.tokenSplit"), { f: tc.forensics.toLocaleString(), a: tc.analysis.toLocaleString(), o: tc.other.toLocaleString() })})</span></>],
    ["hist.more.incidentId", cur.incident_id, true],
    ["hist.more.fingerprint", cur.fingerprint, true],
    ["hist.more.eventids", cur.alerts.map((a) => a.eventid).join(", "), true],
    ["hist.more.feishu", cur.feishu?.sent ? t("hist.more.feishuSent") : t("hist.more.feishuNo")],
  ];
  return (
    <div className="border-t border-line">
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 px-5 py-2 text-left text-xs text-slate-600 hover:bg-slate-50">
        <svg viewBox="0 0 12 12" className={cn("h-2.5 w-2.5 transition", open && "rotate-90")} fill="none" stroke="currentColor" strokeWidth="1.6"><path d="m4.5 3 3 3-3 3" /></svg>
        <span className="font-medium">{t("hist.more")}</span>
        {violations.length > 0 && <span className="text-amber-700">{fill(t("hist.more.violations"), { n: violations.length })}</span>}
      </button>
      {open && (
        <div className="space-y-3 px-5 pb-4">
          <dl className="grid grid-cols-[120px_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-xs">
            {rows.map(([k, v, id]) => (
              <Fragment key={k}>
                <dt className="text-dim">{t(k)}</dt>
                <dd className="num break-all text-slate-700" data-mask={id ? "id" : undefined}>{v}</dd>
              </Fragment>
            ))}
          </dl>
          {violations.length > 0 && (
            <div className="rounded-[3px] border border-amber-200 border-l-2 border-l-amber-500 bg-amber-50/60 px-3.5 py-2.5 text-xs leading-relaxed text-slate-700">
              <div className="mb-1 flex items-center gap-1 font-semibold text-amber-800">{t("hist.more.violationsTitle")}<HelpTip text={t("hist.more.violationsNote")} /></div>
              <ul className="list-disc space-y-0.5 pl-4">{violations.map((v) => <li key={v}><AiInline text={v} /></li>)}</ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
