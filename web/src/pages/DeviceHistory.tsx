import { useEffect, useState } from "react";
import { cn } from "../lib/api";
import { AiText } from "../lib/aitext";
import { Empty } from "../lib/ui";
import { confMeta, fmtDur, oneLine } from "../lib/incident";
import { fill, useLang, useT, type DictKey } from "../lib/i18n";
import { FAULT_KEY, dayTime } from "./incidentShared";

/** 一台设备的排查历史：过去的事件 + 基础 / 趋势 / 自定义巡检发现，倒序。
 *  数据是 `/api/devices/{name}/history` 现算的（不新增存储），巡检连续几次都在的同一条已经在后端合成一条。 */
type Entry = {
  kind: "incident" | "basic" | "trend" | "plan";
  key: string; at: number; last_at: number; occurrences: number;
  type: string; type_label?: string; title: string; headline?: string; root_cause?: string;
  confidence?: string; incident_id?: string; status?: string; plan?: string;
  needs_human?: boolean; elapsed_total?: number; same_type: boolean; same_type_count: number;
};
type Hist = {
  device: string; known: boolean; aliases: string[]; total: number; entries: Entry[];
  stats: { incidents_30d: number; top_type: string; top_type_count: number; avg_elapsed: number | null; needs_human_30d: number; findings_30d: number; incidents_total: number };
};

const KIND_KEY: Record<Entry["kind"], DictKey> = {
  incident: "hist.dev.kind.incident", basic: "hist.dev.kind.basic", trend: "hist.dev.kind.trend", plan: "hist.dev.kind.plan",
};
const CHECK_KEY: Record<string, DictKey> = {
  reachable: "inspection.check.reachable", interfaces: "inspection.check.interfaces", ospf: "inspection.check.ospf",
  bgp: "inspection.check.bgp", errors: "inspection.check.errors",
};
const TREND_KEY: Record<string, DictKey> = {
  trend: "hist.trendKind.trend", periodic_spike: "hist.trendKind.periodicSpike", self_healing_flap: "hist.trendKind.selfHealingFlap",
};
/** 巡检发现的状态：红 = 异常 / 未通过，橙 = 关注 / 出错 / 被拒。只表语义。 */
const STATUS: Record<string, { key: DictKey; dot: string }> = {
  bad: { key: "hist.dev.status.bad", dot: "bg-rose-500" },
  fail: { key: "hist.dev.status.fail", dot: "bg-rose-500" },
  warn: { key: "hist.dev.status.warn", dot: "bg-amber-500" },
  error: { key: "hist.dev.status.error", dot: "bg-amber-500" },
  denied: { key: "hist.dev.status.denied", dot: "bg-amber-500" },
};
const PAGE = 40;

export default function DeviceHistory({ device, currentId, onPick }: { device: string; currentId?: string; onPick: (id: string) => void }) {
  const t = useT();
  const lang = useLang();
  const [h, setH] = useState<Hist | null>(null);
  const [failed, setFailed] = useState(false);
  const [kind, setKind] = useState<"all" | "incident" | "inspection">("all");
  const [shown, setShown] = useState(PAGE);
  useEffect(() => {
    setH(null); setFailed(false); setShown(PAGE);
    fetch(`/api/devices/${encodeURIComponent(device)}/history`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setH).catch(() => setFailed(true));
  }, [device]);
  if (failed) return <Empty title={t("hist.dev.loadFailed")} />;
  if (!h) return <div className="px-5 py-8 text-center text-xs text-dim">{t("hist.list.loading")}</div>;

  const typeLabel = (e: Entry) =>
    e.kind === "incident" ? t(FAULT_KEY[e.type] ?? "faultType.other")
    : e.kind === "basic" ? (CHECK_KEY[e.type] ? t(CHECK_KEY[e.type]) : e.type)
    : e.kind === "trend" ? (TREND_KEY[e.type] ? t(TREND_KEY[e.type]) : e.type_label || e.type)
    : `${e.plan} · ${e.type === "unreachable" ? t("hist.dev.plan.unreachable") : e.type}`;
  const list = h.entries.filter((e) => kind === "all" || (kind === "incident") === (e.kind === "incident"));
  const s = h.stats;
  const stats: [DictKey, string, string?][] = [
    ["hist.dev.stat.incidents", String(s.incidents_30d)],
    ["hist.dev.stat.topType", s.top_type ? t(FAULT_KEY[s.top_type] ?? "faultType.other") : "—", s.top_type ? `×${s.top_type_count}` : undefined],
    ["hist.dev.stat.avgElapsed", s.avg_elapsed == null ? "—" : fmtDur(t, s.avg_elapsed)],
    ["hist.dev.stat.human", String(s.needs_human_30d)],
  ];
  return (
    <div>
      <div className="grid grid-cols-2 border-b border-line sm:grid-cols-4">
        {stats.map(([k, v, sub], i) => (
          <div key={k} className={cn("px-4 py-2.5", i > 0 && "sm:border-l sm:border-line")}>
            <div className="text-[0.6875rem] text-dim">{t(k)}</div>
            <div className="mt-0.5 flex items-baseline gap-1.5">
              <span className={cn("truncate text-[0.9375rem] font-semibold text-slate-800", /^[\d.,]+$/.test(v) && "num")}>{v}</span>
              {sub && <span className="num text-xs text-dim">{sub}</span>}
            </div>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2 text-xs text-dim">
        <Seg value={kind} onChange={(v) => { setKind(v); setShown(PAGE); }}
          items={[["all", t("hist.dev.filter.all")], ["incident", t("hist.dev.filter.incident")], ["inspection", t("hist.dev.filter.inspection")]]} />
        <span>{fill(t("hist.dev.stat.footer"), { n: s.findings_30d, m: s.incidents_total })}</span>
        {!h.known && <span className="text-amber-700">{t("hist.dev.unknown")}</span>}
      </div>
      {list.length === 0 ? <Empty title={t("hist.dev.empty")} /> : (
        <ol className="divide-y divide-line">
          {list.slice(0, shown).map((e, i) => {
            const isInc = e.kind === "incident";
            const dot = isInc ? confMeta(e.confidence).dot : (STATUS[e.status ?? ""]?.dot ?? "bg-slate-300");
            const dotTitle = isInc ? t(confMeta(e.confidence).longKey) : (STATUS[e.status ?? ""] ? t(STATUS[e.status!].key) : e.status);
            const on = isInc && e.incident_id === currentId;
            const title = isInc ? oneLine(e.headline || e.root_cause || "", 80) : e.title;
            const Row = isInc ? "button" : "div";
            return (
              <li key={`${e.kind}-${e.key}-${e.at}-${i}`}>
                <Row {...(isInc ? { onClick: () => onPick(e.incident_id!), type: "button" as const } : {})}
                  className={cn("relative grid w-full grid-cols-[86px_8px_76px_minmax(0,1fr)_auto] items-center gap-x-2.5 px-4 py-[7px] text-left text-[0.8125rem]",
                    isInc && "hover:bg-slate-50", on && "!bg-brand/[0.07]")}>
                  {on && <span className="absolute inset-y-0 left-0 w-[3px] bg-brand" />}
                  <span className="num text-xs text-dim" title={e.occurrences > 1 ? fill(t("hist.dev.streakTitle"), { from: dayTime(e.at, lang), to: dayTime(e.last_at, lang), n: e.occurrences }) : undefined}>
                    {dayTime(e.last_at, lang)}
                  </span>
                  <span className={cn("h-2 w-2", dot)} title={dotTitle} />
                  <span className="truncate text-xs text-dim">{t(KIND_KEY[e.kind])}</span>
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="shrink-0 rounded-[2px] px-1.5 text-[0.6875rem] leading-[16px] text-slate-700 ring-1 ring-slate-300 ring-inset">{typeLabel(e)}</span>
                    <span className="min-w-0 truncate text-slate-700" title={title}><AiText text={title} lowPriority /></span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2 text-[0.6875rem] text-dim">
                    {e.occurrences > 1 && <span className="num">{fill(t("hist.dev.streak"), { n: e.occurrences })}</span>}
                    {e.same_type && e.same_type_count > 1 && <span className="num">{fill(t("hist.row.sameType"), { n: e.same_type_count })}</span>}
                    {isInc && e.needs_human && <span className="text-amber-700">{t("hist.detail.needsHuman")}</span>}
                  </span>
                </Row>
              </li>
            );
          })}
        </ol>
      )}
      {list.length > shown && (
        <button onClick={() => setShown(shown + PAGE)} className="block w-full border-t border-line py-2 text-xs font-medium text-brand hover:bg-slate-50">
          {fill(t("hist.list.loadMore"), { n: list.length - shown })}
        </button>
      )}
    </div>
  );
}

/** 分段按钮：网管控制台风，方角细边框，选中用主色。 */
export function Seg<V extends string>({ value, onChange, items }: { value: V; onChange: (v: V) => void; items: [V, string][] }) {
  return (
    <span role="group" className="inline-flex shrink-0 gap-0.5 rounded-[8px] bg-slate-100 p-[3px]">
      {items.map(([v, label]) => (
        <button key={v} type="button" onClick={() => onChange(v)} aria-pressed={value === v}
          className={cn("rounded-[6px] px-2.5 py-1 text-xs leading-4 whitespace-nowrap transition",
            value === v ? "bg-white font-medium text-slate-900 shadow-[0_1px_2px_rgb(0_0_0/0.08)]" : "text-slate-600 hover:text-slate-900")}>
          {label}
        </button>
      ))}
    </span>
  );
}
