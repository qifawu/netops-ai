import { useEffect, useState } from "react";
import { cn, load } from "../lib/api";
import { AiInline } from "../lib/aitext";
import { Badge, Card, CardHead, Empty, Icon, Loading, PATH } from "../lib/ui";
import { fill, useT, type DictKey } from "../lib/i18n";
import { Btn, call } from "../lib/sopui";
import Spark from "../lib/Spark";
import InspectionConfigForm, { type InspectionConfig } from "./InspectionConfigForm";

type Check = { label: string; text: string; passed: boolean };
type Rule = { name: string; kind: string; summary: string; checks: Check[]; points: number };
type Row = {
  host: string; item: string; key: string; detail: string; reason: string;
  // 后加的字段：老记录没有 rule/series，页面按"没有就不画"处理
  rule?: Rule | null; series?: number[][] | null; host_raw?: string; key_raw?: string;
};
type Ignored = { host: string; item: string; key: string; kind: string; reason: string; at: string };
type Group = { label: string; why: string; rows: Row[] };
type Item = { host: string; item_key: string; severity: string; why: string; suggestion: string; evidence: string };
type Advice = { summary: string; needs_attention: Item[]; ignorable: { host: string; item_key: string; why: string }[]; cannot_tell: { host: string; item_key: string; what_data_would_help: string }[]; findings_fed: number; findings_total: number };
type StatusCheck = { check: string; status: "ok" | "warn" | "bad" | "skip"; summary: string; evidence: string[]; command: string };
type StatusDevice = { name: string; role?: string; reachable: boolean; checks: StatusCheck[] };
type StatusSnap = { checked_at: string; devices: StatusDevice[]; summary: { devices: number; bad: number; warn: number; ok: number; skip: number; unreachable: number } };
type DiffRow = { device: string; check: string; from: string; to: string; summary: string };
type Diff = {
  status?: { prev_at: string; worse: DiffRow[]; better: DiffRow[] } | null;
  trend?: { prev_at: string; new_count: number; gone_count: number; new: { host: string; item: string; key: string }[] } | null;
};
type Run = { kind: "trend" | "status"; at: string; note: string };
type Data = {
  status?: StatusSnap | null; diff?: Diff | null; history?: Run[];
  scanned_hosts: number; scanned_items: number; items_with_data: number; finding_count: number; groups: Group[]; errors: string[];
  advice?: Advice | null; scanned_at?: string | null; config?: InspectionConfig; ignored?: Ignored[];
};

const TONE: Record<string, string> = {
  持续走坏: "bg-rose-50 text-rose-700 ring-rose-200",
  持续好转: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  反复抖动又自愈: "bg-amber-50 text-amber-700 ring-amber-200",
  周期性冲高: "bg-white text-slate-700 ring-slate-300",
  持续单向变化: "bg-white text-slate-700 ring-slate-300",
};
/** 后端 `inspection/report.py` 的 `_HINT` 是个固定 5 项的枚举，不是 AI 自由生成文本，
 *  可以像 `ROLE_KEY` 那样整词翻译；万一后端加了新枚举值，前端没收录就原样透传。 */
const TONE_KEY: Record<string, { label: DictKey; why: DictKey }> = {
  持续走坏: { label: "inspection.tone.worsening.label", why: "inspection.tone.worsening.why" },
  持续好转: { label: "inspection.tone.improving.label", why: "inspection.tone.improving.why" },
  反复抖动又自愈: { label: "inspection.tone.flapping.label", why: "inspection.tone.flapping.why" },
  周期性冲高: { label: "inspection.tone.periodicSpike.label", why: "inspection.tone.periodicSpike.why" },
  持续单向变化: { label: "inspection.tone.oneWay.label", why: "inspection.tone.oneWay.why" },
};
const STATE_TONE: Record<string, string> = {
  // 正常是常态，不该满屏绿：白底灰框 + 绿字，异常（橙 / 红）才用实底跳出来。
  ok: "bg-white text-emerald-700 ring-slate-300",
  warn: "bg-amber-50 text-amber-700 ring-amber-200",
  bad: "bg-rose-50 text-rose-700 ring-rose-200",
  skip: "bg-slate-100 text-slate-500 ring-slate-200",
};
const STATE_KEY: Record<string, DictKey> = { ok: "inspection.state.ok", warn: "inspection.state.warn", bad: "inspection.state.bad", skip: "inspection.state.skip" };
const CHECK_KEY: Record<string, DictKey> = {
  reachable: "inspection.check.reachable", interfaces: "inspection.check.interfaces", ospf: "inspection.check.ospf",
  bgp: "inspection.check.bgp", errors: "inspection.check.errors",
};
const RANK: Record<string, number> = { bad: 0, warn: 1, ok: 2, skip: 3 };
const worst = (d: StatusDevice) => Math.min(...d.checks.map((c) => RANK[c.status] ?? 3), 3);
const isHigh = (s: string) => s === "high" || s === "高" || s === "要紧";

export default function Inspection() {
  const t = useT();
  const [d, setD] = useState<Data | null>(null);
  const [cfg, setCfg] = useState<InspectionConfig | null>(null);
  const [open, setOpen] = useState<string>("");
  const [note, setNote] = useState("");
  const [running, setRunning] = useState(false);
  const [devOpen, setDevOpen] = useState<string>("");
  const [groupBy, setGroupBy] = useState<"kind" | "host">("kind");
  const empty: Data = { scanned_hosts: 0, scanned_items: 0, items_with_data: 0, finding_count: 0, groups: [], errors: [] };
  const reload = () => load<Data>("inspection", "/api/inspection").then((x) => { setD(x); return x; }).catch(() => { setD(empty); return empty; });
  const reloadCfg = () => load<InspectionConfig>("inspection-config", "/api/inspection/config").then(setCfg).catch(() => {});
  useEffect(() => { reload(); reloadCfg(); }, []);

  /** 重跑是后台任务（趋势读 Zabbix + 状态登设备，要几秒到几十秒）：POST 之后轮询，
   *  趋势的 `scanned_at` 和状态的 `checked_at` 都变了才算跑完（状态那边失败也会写快照，不会一直等）。 */
  const rerun = async () => {
    const beforeTrend = d?.scanned_at ?? null;
    const beforeStatus = d?.status?.checked_at ?? null;
    setRunning(true);
    try {
      const r = await call("POST", "/api/inspection/run");
      if (!r.ok) return;
      for (let i = 0; i < 90; i++) {
        await new Promise((ok) => setTimeout(ok, 2000));
        const x = await reload();
        if ((x.scanned_at ?? null) !== beforeTrend && (x.status?.checked_at ?? null) !== beforeStatus) return;
      }
    } finally {
      setRunning(false);
    }
  };

  const ignore = async (r: Row) => {
    const res = await call("POST", "/api/inspection/ignore", { host: r.host_raw ?? r.host, item_key: r.key_raw ?? r.key });
    if (res.ok) { setNote(t("inspection.ignore.done")); reload(); reloadCfg(); } else setNote(res.data.message || t("inspection.ignore.failed"));
  };
  const unignore = async (x: Ignored) => {
    const res = await call("DELETE", `/api/inspection/ignore?host=${encodeURIComponent(x.host)}&item_key=${encodeURIComponent(x.key)}`);
    if (res.ok) { setNote(t("inspection.ignore.undone")); reload(); reloadCfg(); } else setNote(res.data.message || t("inspection.ignore.failed"));
  };

  if (!d) return <Loading />;
  const trendGroups = d.groups ?? [];
  const groups: Group[] = groupBy === "kind" ? trendGroups : (() => {
    const byHost = new Map<string, Row[]>();
    trendGroups.forEach((g) => g.rows.forEach((r) => byHost.set(r.host, [...(byHost.get(r.host) ?? []), r])));
    return [...byHost.entries()].sort((a, b) => b[1].length - a[1].length).map(([host, rows]) => ({ label: host, why: "", rows }));
  })();
  const st = d.status;
  const devices = [...(st?.devices ?? [])].sort((a, b) => worst(a) - worst(b));
  const diff = d.diff;
  const hasDiff = !!(diff?.status || diff?.trend);
  return (
    <div className="mx-auto max-w-[1280px] space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-0 flex-1 text-[13px] leading-relaxed text-slate-600">{t("inspection.intro")}</p>
        <Btn tone="brand" busy={running} disabled={running} onClick={rerun}>{running ? t("inspection.running") : t("inspection.run")}</Btn>
        <a href="/api/inspection/export?format=md" download data-testid="export-md"><Btn>{t("inspection.export.md")}</Btn></a>
        <a href="/api/inspection/export?format=html" download data-testid="export-html"><Btn>{t("inspection.export.html")}</Btn></a>
      </div>
      <div className="flex flex-wrap gap-x-6 text-xs text-dim">
        {st?.checked_at && <span>{fill(t("inspection.status.checkedAt"), { at: st.checked_at })}</span>}
        {d.scanned_at && <span>{fill(t("inspection.how.lastRun"), { at: d.scanned_at })}</span>}
      </div>
      {note && <div className="rounded-[3px] bg-emerald-50 px-4 py-2.5 text-sm text-emerald-700 ring-1 ring-emerald-200">{note}</div>}

      <div className="grid grid-cols-4 divide-x divide-line rounded-[4px] border border-line bg-card" data-testid="inspection-kpis">
        {(st ? [
          [t("inspection.status.kpi.bad"), st.summary.bad, t("inspection.status.unit.check"), st.summary.bad ? "text-bad" : ""],
          [t("inspection.status.kpi.warn"), st.summary.warn, t("inspection.status.unit.check"), st.summary.warn ? "text-warn" : ""],
          [t("inspection.status.kpi.unreachable"), st.summary.unreachable, t("unit.devices"), st.summary.unreachable ? "text-bad" : ""],
          [t("inspection.kpi.findings"), d.finding_count, t("inspection.unit.finding"), ""],
        ] : [
          [t("inspection.kpi.scannedHosts"), d.scanned_hosts, t("unit.devices"), ""],
          [t("inspection.kpi.scannedItems"), d.scanned_items.toLocaleString(), t("inspection.unit.item"), ""],
          [t("inspection.kpi.itemsWithData"), d.items_with_data.toLocaleString(), t("inspection.unit.item"), ""],
          [t("inspection.kpi.findings"), d.finding_count, t("inspection.unit.finding"), ""],
        ]).map(([label, v, unit, tint]) => (
          <div key={label as string} className="px-4 py-3">
            <div className="text-xs font-medium text-slate-600">{label}</div>
            <div className="mt-1.5 flex items-baseline gap-1.5">
              <span className={cn("num text-[26px] leading-none font-semibold text-slate-900", tint as string)}>{v}</span>
              <span className="text-xs text-dim">{unit}</span>
            </div>
          </div>
        ))}
      </div>

      <div data-testid="status-checks"><Card>
        <CardHead title={t("inspection.status.title")} note={t("inspection.status.note")} icon={<Icon d={PATH.spark} className="text-slate-500" />} />
        {devices.length === 0 ? <Empty title={t("inspection.status.empty")} /> : (
          <div className="divide-y divide-line">
            {devices.map((dev) => {
              const bad = dev.checks.filter((c) => c.status === "bad" || c.status === "warn");
              return (
                <div key={dev.name}>
                  <button onClick={() => setDevOpen(devOpen === dev.name ? "" : dev.name)} className="flex w-full flex-wrap items-center gap-3 px-4 py-2 text-left transition hover:bg-slate-50">
                    <span className="num w-16 text-[13px] font-semibold text-slate-800">{dev.name}</span>
                    {dev.role && <Badge className="bg-white text-slate-600 ring-slate-300">{dev.role}</Badge>}
                    <span className="flex min-w-0 flex-1 flex-wrap gap-1.5">
                      {dev.checks.map((c) => (
                        <Badge key={c.check} className={STATE_TONE[c.status]} title={c.summary}>{CHECK_KEY[c.check] ? t(CHECK_KEY[c.check]) : c.check} · {t(STATE_KEY[c.status])}</Badge>
                      ))}
                    </span>
                    <span className="text-xs text-dim">{bad.length === 0 ? t("inspection.status.allOk") : bad.map((c) => c.summary).join("；").slice(0, 60)}</span>
                    <Icon d={PATH.chevron} className={cn("h-4 w-4 text-slate-400 transition", devOpen === dev.name && "rotate-90")} />
                  </button>
                  {devOpen === dev.name && (
                    <div className="space-y-2 border-t border-line bg-slate-50/50 px-5 py-3">
                      {dev.checks.map((c) => (
                        <div key={c.check} className="text-sm">
                          <div className="flex items-center gap-2">
                            <Badge className={STATE_TONE[c.status]}>{t(STATE_KEY[c.status])}</Badge>
                            <span className="font-medium">{CHECK_KEY[c.check] ? t(CHECK_KEY[c.check]) : c.check}</span>
                            <span className="text-slate-600">{c.summary}</span>
                          </div>
                          {c.evidence.length > 0 && (
                            <>
                              <div className="mt-1 text-xs text-dim">{fill(t("inspection.status.evidence"), { cmd: c.command })}</div>
                              {/* 设备输出原话，不翻译、不改写 */}
                              <div className="term mt-1 overflow-x-auto rounded-[2px] px-3 py-2 font-mono text-xs leading-relaxed whitespace-pre">{c.evidence.join("\n")}</div>
                            </>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card></div>

      <div data-testid="inspection-diff"><Card>
        <CardHead title={t("inspection.diff.title")} />
        <div className="space-y-2 px-5 py-4 text-sm">
          {!hasDiff && <div className="text-dim">{t("inspection.diff.none")}</div>}
          {diff?.status && (
            <div>
              <div className="font-medium text-slate-700">{fill(t("inspection.diff.statusLine"), { worse: diff.status.worse.length, better: diff.status.better.length, at: diff.status.prev_at })}</div>
              <ul className="mt-1 space-y-0.5 text-xs text-slate-600">
                {diff.status.worse.map((r, i) => <li key={"w" + i}><Badge className={STATE_TONE.bad}>{t("inspection.diff.worse")}</Badge> {r.device} · {CHECK_KEY[r.check] ? t(CHECK_KEY[r.check]) : r.check}：{r.summary}</li>)}
                {diff.status.better.map((r, i) => <li key={"b" + i}><Badge className={STATE_TONE.ok}>{t("inspection.diff.better")}</Badge> {r.device} · {CHECK_KEY[r.check] ? t(CHECK_KEY[r.check]) : r.check}</li>)}
              </ul>
            </div>
          )}
          {diff?.trend && (
            <div>
              <div className="font-medium text-slate-700">{fill(t("inspection.diff.trendLine"), { new: diff.trend.new_count, gone: diff.trend.gone_count, at: diff.trend.prev_at })}</div>
              <ul className="mt-1 space-y-0.5 text-xs text-slate-600">
                {diff.trend.new.slice(0, 8).map((r, i) => <li key={i}><Badge className={STATE_TONE.warn}>{t("inspection.diff.new")}</Badge> {r.host} · {r.item}</li>)}
              </ul>
            </div>
          )}
        </div>
      </Card></div>

      {d.advice && (
        <Card>
          <CardHead title={t("inspection.advice.title")} note={fill(t("inspection.advice.noteTemplate"), { total: d.advice.findings_total, fed: d.advice.findings_fed })} icon={<Icon d={PATH.spark} className="text-slate-500" />} />
          <div className="px-5 py-4">
            <p className="text-sm leading-7 text-slate-700"><AiInline text={d.advice.summary} /></p>
            <div className="mt-4 space-y-3">
              {d.advice.needs_attention.map((a, i) => (
                <div key={i} className="rounded-[3px] border border-line p-3.5">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <Badge className={isHigh(a.severity) ? "bg-rose-50 text-rose-700 ring-rose-200" : "bg-amber-50 text-amber-700 ring-amber-200"}>
                      {isHigh(a.severity) ? t("inspection.advice.high") : t("inspection.advice.watch")}
                    </Badge>
                    <span className="text-sm font-semibold text-slate-800">{a.host}</span>
                    <code className="text-dim">{a.item_key}</code>
                  </div>
                  <div className="mt-2 text-sm leading-relaxed text-slate-700"><AiInline text={a.why} /></div>
                  <div className="mt-2 border-l-2 border-brand bg-slate-50 px-3 py-2 text-sm leading-relaxed text-slate-800">
                    <span className="font-semibold">{t("inspection.advice.suggestionLabel")}</span><AiInline text={a.suggestion} />
                  </div>
                  {/* evidence 是 schema 强制要求「从巡检原文逐字抄」的一句话，绝不能翻译——翻了就不再是可核查的原始证据 */}
                  <div className="term mt-2 overflow-x-auto rounded-[2px] px-3 py-2 text-xs leading-relaxed">{a.evidence}</div>
                </div>
              ))}
            </div>
            <div className="mt-4 flex gap-4 text-xs text-dim">
              <span>{t("inspection.advice.ignorablePrefix")}<b className="text-slate-700">{d.advice.ignorable.length}</b> {t("inspection.advice.countSuffix")}</span>
              <span>{t("inspection.advice.needsHumanPrefix")}<b className="text-slate-700">{d.advice.cannot_tell.length}</b> {t("inspection.advice.countSuffix")}</span>
            </div>
          </div>
        </Card>
      )}

      <div>
        <div className="mb-2 flex items-center gap-2">
          <h2 className="text-[13px] font-semibold tracking-wide text-slate-800">{t("inspection.groups.title")}</h2>
          <span className="ml-auto text-xs text-dim">{t("inspection.groupBy")}</span>
          {(["kind", "host"] as const).map((k) => (
            <Btn key={k} small tone={groupBy === k ? "brand" : "plain"} onClick={() => { setGroupBy(k); setOpen(""); }}>{t(k === "kind" ? "inspection.groupBy.kind" : "inspection.groupBy.host")}</Btn>
          ))}
        </div>
        {groups.length === 0 ? <Card><Empty title={t("inspection.groups.empty.title")} hint={t("inspection.groups.empty.hint")} /></Card> : (
          <div className="space-y-2">
            {groups.map((g) => (
              <Card key={g.label} className="overflow-hidden">
                <button onClick={() => setOpen(open === g.label ? "" : g.label)}
                  className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition hover:bg-slate-50">
                  <Badge className={TONE[g.label] ?? "bg-slate-100 text-slate-600 ring-slate-200"}>{TONE_KEY[g.label] ? t(TONE_KEY[g.label].label) : g.label} · {g.rows.length}</Badge>
                  <span className="min-w-0 flex-1 truncate text-[13px] text-dim">{TONE_KEY[g.label] ? t(TONE_KEY[g.label].why) : g.why}</span>
                  <Icon d={PATH.chevron} className={cn("h-4 w-4 text-slate-400 transition", open === g.label && "rotate-90")} />
                </button>
                {open === g.label && (
                  <div className="divide-y divide-line border-t border-line">
                    {g.rows.slice(0, 30).map((r, i) => (
                      <div key={i} className="px-5 py-3 text-sm">
                        <div><span className="font-medium">{r.host}</span> · {r.item} <code className="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">{r.key}</code></div>
                        <div className="mt-1 font-mono text-xs text-slate-500">{r.detail}</div>
                        {r.reason && <div className="mt-0.5 text-xs text-dim">{t("inspection.groups.reasonLabel")}<AiInline text={r.reason} /></div>}
                        <div className="mt-2 flex items-start gap-4">
                          <div className="min-w-0 flex-1" data-testid="finding-rule">
                            {r.rule ? (
                              <>
                                <div className="text-xs font-semibold text-slate-700">{t("inspection.rule.hit")}{r.rule.name}</div>
                                <ul className="mt-1 space-y-0.5 text-xs text-slate-600">
                                  {r.rule.checks.map((c, j) => (
                                    <li key={j} className="flex gap-1.5"><span className={c.passed ? "text-emerald-600" : "text-rose-600"}>{c.passed ? "✓" : "✗"}</span><span>{c.text}</span></li>
                                  ))}
                                </ul>
                              </>
                            ) : <div className="text-xs text-dim">{t("inspection.rule.none")}</div>}
                          </div>
                          <div className="shrink-0 text-brand" data-testid="finding-spark"><Spark series={r.series} /></div>
                          <Btn small onClick={() => ignore(r)} title={t("inspection.ignore.title")}>{t("inspection.ignore.btn")}</Btn>
                        </div>
                      </div>
                    ))}
                    {g.rows.length > 30 && <div className="px-5 py-3 text-xs text-dim">{fill(t("inspection.groups.truncatedTemplate"), { n: g.rows.length - 30 })}</div>}
                  </div>
                )}
              </Card>
            ))}
          </div>
        )}
      </div>
      {(d.ignored?.length ?? 0) > 0 && (
        <div data-testid="ignored-list"><Card>
          <CardHead title={fill(t("inspection.ignored.title"), { n: d.ignored!.length })} note={t("inspection.ignored.note")} />
          <div className="divide-y divide-line">
            {d.ignored!.map((x) => (
              <div key={x.host + x.key} className="flex items-center gap-3 px-5 py-2.5 text-sm">
                <div className="min-w-0 flex-1">
                  <span className="font-medium">{x.host}</span> · {x.item} <code className="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">{x.key}</code>
                  <div className="text-xs text-dim">{x.reason} · {x.at}</div>
                </div>
                <Btn small onClick={() => unignore(x)}>{t("inspection.ignored.undo")}</Btn>
              </div>
            ))}
          </div>
        </Card></div>
      )}
      <div data-testid="inspection-history"><Card>
        <CardHead title={t("inspection.history.title")} />
        {(d.history?.length ?? 0) === 0 ? <Empty title={t("inspection.history.empty")} /> : (
          <div className="divide-y divide-line">
            {d.history!.slice(0, 10).map((r, i) => (
              <div key={i} className="flex items-center gap-3 px-5 py-2 text-sm">
                <Badge className="w-[84px] justify-center bg-white text-slate-700 ring-slate-300">{t(r.kind === "status" ? "inspection.history.kind.status" : "inspection.history.kind.trend")}</Badge>
                <span className="font-mono text-xs text-slate-500">{r.at}</span>
                <span className="text-slate-700">{r.note}</span>
              </div>
            ))}
          </div>
        )}
      </Card></div>

      <details data-testid="inspection-params" className="rounded-[4px] border border-line bg-card">
        <summary className="cursor-pointer select-none bg-[#f6f7f9] px-4 py-2.5 text-[13px] font-semibold tracking-wide text-slate-800">
          {t("inspection.params.title")} <span className="ml-2 text-xs font-normal text-dim">{t("inspection.params.note")}</span>
        </summary>
        <div className="space-y-4 border-t border-line p-5">
          <div data-testid="inspection-how" className="rounded-[3px] border border-line border-l-2 border-l-brand bg-slate-50 px-4 py-3 text-[13px] leading-relaxed text-slate-700">
            <div className="mb-1 font-semibold text-slate-800">{t("inspection.how.title")}</div>
            <p>{t("inspection.how.body")}</p>
          </div>
          {cfg && <InspectionConfigForm cfg={cfg} onSaved={(c) => { setCfg(c); reload(); }} onRun={rerun} />}
        </div>
      </details>

      {d.errors?.length > 0 && (
        <div className="rounded-[3px] border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-700">
          {fill(t("inspection.errorsTemplate"), { n: d.errors.length })}
        </div>
      )}
    </div>
  );
}
