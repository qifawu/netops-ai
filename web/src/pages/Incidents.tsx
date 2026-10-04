import { useEffect, useState } from "react";
import { brief, cn, load, plain, ts } from "../lib/api";
import { AiInline, AiText } from "../lib/aitext";
import { stepLabel } from "../lib/labels";
import { Badge, Card, CardHead, Empty, Icon, Loading, PATH } from "../lib/ui";
import { MERGE_HINT, REPEAT_HINT, confLevel, confMeta, faultType, fmtDur, titleOf } from "../lib/incident";
import { takePending } from "../lib/nav";
import { fill, useLang, useT, type DictKey } from "../lib/i18n";

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
};

const ROLE_COLOR: Record<string, string> = {
  root: "bg-rose-50 text-rose-700 ring-rose-200",
  consequence: "bg-slate-100 text-slate-600 ring-slate-200",
  independent: "bg-violet-50 text-violet-700 ring-violet-200",
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

export default function Incidents() {
  const t = useT();
  const [rows, setRows] = useState<Inc[] | null>(null);
  const [sel, setSel] = useState<string>("");
  useEffect(() => {
    load<Inc[]>("incidents", "/api/incidents").then((r) => {
      setRows(r);
      const want = takePending();
      setSel(r.some((x) => x.incident_id === want) ? want : r[0]?.incident_id ?? "");
    }).catch(() => setRows([]));
  }, []);
  if (!rows) return <Loading />;
  const cur = rows.find((r) => r.incident_id === sel);
  if (rows.length === 0) return <Card><Empty title={t("incidents.list.empty.title")} hint={t("incidents.list.empty.hint")} /></Card>;
  return (
    <div className="flex items-start gap-5">
      <div className="sticky top-4 w-80 shrink-0">
        <div className="mb-2 flex items-baseline justify-between px-1">
          <span className="text-[15px] font-semibold text-slate-800">{t("incidents.list.title")}</span>
          <span className="text-xs text-dim">{fill(t("incidents.list.countTemplate"), { n: rows.length })}</span>
        </div>
        <Card className="max-h-[calc(100vh-9.5rem)] divide-y divide-line overflow-y-auto">
          {rows.map((r) => {
            const ft = faultType(r), c = confMeta(r.confidence);
            return (
              <button key={r.incident_id} onClick={() => setSel(r.incident_id)}
                className={cn("relative block w-full px-4 py-3 text-left transition", sel === r.incident_id ? "bg-brand/[0.06]" : "hover:bg-slate-50")}>
                {sel === r.incident_id && <span className="absolute top-2 bottom-2 left-0 w-[3px] rounded-r-full bg-brand" />}
                <div className="flex items-center gap-2 text-xs text-dim">
                  <span className={cn("h-2 w-2 shrink-0 rounded-full", c.dot)} title={`${t(c.longKey)}：${t(c.hintKey)}`} />
                  <span className="tabular-nums">{ts(r.clock)}</span>
                  <span className="truncate font-medium text-slate-600">{r.host}</span>
                  <Badge className={cn("ml-auto", ft.cls)}>{t(ft.labelKey)}</Badge>
                </div>
                <div className="mt-1.5 line-clamp-2 text-[13px] leading-snug text-slate-700"><AiText text={titleOf(r, 60)} lowPriority /></div>
              </button>
            );
          })}
        </Card>
      </div>

      {cur && <Detail cur={cur} />}
    </div>
  );
}

function Detail({ cur }: { cur: Inc }) {
  const t = useT();
  const lang = useLang();
  const [traceAll, setTraceAll] = useState(false);
  const [openStep, setOpenStep] = useState<number | null>(null);
  useEffect(() => { setTraceAll(false); setOpenStep(null); }, [cur.incident_id]);
  const traceShown = traceAll ? cur.trace : cur.trace.slice(0, 6);
  const ft = faultType(cur), c = confMeta(cur.confidence), lvl = confLevel(cur.confidence);
  const full = brief(cur.root_cause);
  const checks = Object.entries(cur.checklist ?? {}).filter(([, v]) => v && typeof v === "object");
  return (
    <div className="min-w-0 flex-1 space-y-4">
      {/* 第一层：这是什么故障、发生在哪、AI 怎么看。先给一句话，细节往下走。 */}
      <Card className="overflow-hidden">
        <div className={cn("h-1", c.bar)} />
        <div className="p-5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge className={ft.cls}>{t(ft.labelKey)}</Badge>
            <Badge className={c.badge} title={t(c.hintKey)}><span className={cn("h-1.5 w-1.5 rounded-full", c.dot)} />{t(c.longKey)}</Badge>
            {(cur.repeat_count ?? 0) > 1 && <Badge className="bg-amber-50 text-amber-700 ring-amber-200" title={REPEAT_HINT(t, cur.repeat_count!)}>{fill(t("incidents.repeatBadge"), { n: cur.repeat_count! })}</Badge>}
            {cur.alert_count > 1 && <Badge className="bg-sky-50 text-sky-700 ring-sky-200" title={MERGE_HINT(t, cur.alert_count)}>{fill(t("incidents.mergedBadge"), { n: cur.alert_count })}</Badge>}
            {cur.feishu?.sent && <Badge className="bg-emerald-50 text-emerald-700 ring-emerald-200"><Icon d={PATH.check} className="h-3 w-3" />{t("incidents.feishuSent")}</Badge>}
            <a href={`/api/records/${cur.alerts[0]?.eventid}/report.md`}
              className="ml-auto rounded-lg border border-line px-3 py-1 text-xs font-medium text-slate-600 transition hover:bg-slate-50">{t("incidents.exportReport")}</a>
          </div>
          <h1 className="mt-3 text-xl leading-snug font-semibold tracking-tight text-slate-900"><AiText text={titleOf(cur, 70)} /></h1>
          <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-dim">
            <span>{t("incidents.meta.device")} <b className="font-medium text-slate-700">{cur.host}</b></span>
            <span>{t("incidents.meta.occurredAt")} <b className="font-medium text-slate-700 tabular-nums">{ts(cur.clock)}</b></span>
            <span>{t("incidents.meta.elapsed")} <b className="font-medium text-slate-700">{fmtDur(t, cur.elapsed_total)}</b></span>
            <span>{t("incidents.meta.usage")} <b className="font-medium text-slate-700 tabular-nums">{(cur.token_cost?.total ?? 0).toLocaleString()}</b> token</span>
            <span>{t("incidents.meta.id")} <b className="font-mono font-medium text-slate-700" data-mask="id">{cur.fingerprint.slice(0, 12)}</b></span>
          </div>
        </div>
      </Card>

      {/* 第二层：完整根因 + 置信度 + 处置建议，并排。 */}
      <div className="grid grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)] items-start gap-4">
        <Card>
          <CardHead title={t("incidents.rootCause.title")} note={t("incidents.rootCause.note")} icon={<Icon d={PATH.target} className="text-brand" />} />
          <div className="px-5 py-4 text-sm leading-7 text-slate-700">
            <AiInline text={full} />
            {lang === "zh" && full !== cur.root_cause && <span className="text-slate-400">{cur.root_cause.slice(full.length)}</span>}
          </div>
          {(cur.related ?? []).length > 0 && (
            <div className="mx-5 mb-4 rounded-xl bg-sky-50 px-3.5 py-2.5 text-[13px] leading-relaxed text-sky-900">
              <div className="mb-0.5 font-semibold">{t("incidents.related.title")}</div>
              {cur.related.map((r) => (
                <div key={r.eventid}><span data-mask="id">#{r.eventid}</span> {r.host}：{r.link}<span className="text-sky-700">（<AiInline text={r.reason} />）</span></div>
              ))}
            </div>
          )}
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHead title={t("incidents.confidence.title")} icon={<Icon d={PATH.spark} className="text-violet-500" />} />
            <div className="px-5 py-4">
              <div className="flex items-baseline gap-2">
                <span className={cn("text-3xl leading-none font-semibold", lvl === 3 ? "text-emerald-600" : lvl === 2 ? "text-amber-600" : lvl === 1 ? "text-rose-600" : "text-slate-400")}>{t(c.labelKey)}</span>
                <span className="text-sm text-dim">{lvl ? t("incidents.confidence.title") : ""}</span>
              </div>
              <div className="mt-3 flex gap-1.5">
                {[1, 2, 3].map((k) => <span key={k} className={cn("h-1.5 flex-1 rounded-full", k <= lvl ? c.bar : "bg-slate-200")} />)}
              </div>
              <p className="mt-3 text-xs leading-relaxed text-dim">{t(c.hintKey)}</p>
            </div>
          </Card>
          <Card className={cn(!cur.next_step && "hidden")}>
            <CardHead title={t("incidents.nextStep.title")} icon={<Icon d={PATH.list} className="text-amber-500" />} />
            <div className="px-5 py-4 text-sm leading-relaxed text-slate-700"><AiInline text={cur.next_step} /></div>
          </Card>
        </div>
      </div>

      {/* 第三层：证据链。 */}
      <Card>
        <CardHead title={fill(t("incidents.evidence.titleTemplate"), { n: cur.evidence.length })} note={t("incidents.evidence.note")} icon={<Icon d={PATH.search} className="text-sky-500" />} />
        {cur.evidence.length === 0 ? <Empty title={t("incidents.evidence.empty")} /> : (
          <ol className="divide-y divide-line">
            {cur.evidence.map((e, i) => (
              <li key={i} className="flex gap-3.5 px-5 py-3.5">
                <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-100 text-xs font-semibold text-slate-500">{i + 1}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-start gap-2">
                    <div className="min-w-0 flex-1 text-sm leading-relaxed text-slate-800"><AiInline text={e.claim} /></div>
                    <Badge className={e.source_from === "zabbix" || e.source_from === "监控" ? "bg-violet-50 text-violet-700 ring-violet-200" : "bg-sky-50 text-sky-700 ring-sky-200"}>
                      {e.source_from === "zabbix" || e.source_from === "监控" ? t("incidents.evidence.monitoring") : t("incidents.evidence.device")}
                    </Badge>
                  </div>
                  <div className="term mt-2 overflow-x-auto rounded-lg px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap">{e.source}</div>
                </div>
              </li>
            ))}
          </ol>
        )}
      </Card>

      {/* 第四层：时间线（告警合并 + 取证轨迹）。 */}
      <div className="grid grid-cols-2 items-start gap-4">
        <Card>
          <CardHead title={cur.alert_count > 1 ? fill(t("incidents.alertTimeline.mergedTemplate"), { n: cur.alert_count }) : t("incidents.alertTimeline.single")} icon={<Icon d={PATH.bell} className="text-rose-500" />} />
          <ol className="px-5 py-4">
            {cur.alerts.map((a, i) => (
              <li key={a.eventid} className="relative flex gap-3 pb-4 last:pb-0">
                {i < cur.alerts.length - 1 && <span className="absolute top-4 bottom-0 left-[5px] w-px bg-line" />}
                <span className={cn("relative mt-1.5 h-[11px] w-[11px] shrink-0 rounded-full ring-2 ring-white", roleKey(a.role) === "root" ? "bg-rose-500" : "bg-slate-300")} />
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-sm font-medium text-slate-800">{a.name}</span>
                    <Badge className={ROLE_COLOR[roleKey(a.role)] ?? "bg-slate-100 text-slate-600 ring-slate-200"}>{(ROLE_KEY[roleKey(a.role)] && t(ROLE_KEY[roleKey(a.role)])) || a.role_cn || a.role || t("incidents.role.unlabeled")}</Badge>
                  </div>
                  <div className="mt-0.5 text-xs leading-relaxed text-dim">
                    <span className="tabular-nums">{ts(a.clock)}</span>
                    {a.caused_by && <> · {t("incidents.alertTimeline.causedBy")} <span data-mask="id">#{a.caused_by}</span> {t("incidents.alertTimeline.causedBySuffix")}</>}
                    {a.reason && <> · <AiInline text={plain(a.reason)} /></>}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        </Card>

        <Card>
          <CardHead title={fill(t("incidents.trace.titleTemplate"), { n: cur.trace.length })} note={t("incidents.trace.note")} icon={<Icon d={PATH.terminal} className="text-violet-500" />} />
          {cur.trace.length === 0 ? <Empty title={t("incidents.trace.empty")} /> : (
            <ol className="px-5 py-4">
              {traceShown.map((step, i) => (
                <li key={i} className="relative flex gap-3 pb-3 last:pb-0">
                  {i < traceShown.length - 1 && <span className="absolute top-4 bottom-0 left-[5px] w-px bg-line" />}
                  <span className={cn("relative mt-1.5 h-[11px] w-[11px] shrink-0 rounded-full ring-2 ring-white", step.ok ? "bg-emerald-500" : "bg-rose-500")} />
                  <div className="min-w-0 flex-1">
                    <button onClick={() => setOpenStep(openStep === i ? null : i)} className="flex w-full items-center gap-2 text-left">
                      <span title={step.tool} className="text-[13px] font-medium text-slate-800">{stepLabel(t, step)}</span>
                      {!step.ok && <Badge className="bg-rose-50 text-rose-700 ring-rose-200">{t("incidents.trace.failed")}</Badge>}
                      {step.elapsed_ms != null && <span className="text-xs text-slate-400 tabular-nums">{step.elapsed_ms >= 1000 ? `${(step.elapsed_ms / 1000).toFixed(1)}${t("incidents.trace.seconds")}` : `${step.elapsed_ms}${t("incidents.trace.ms")}`}</span>}
                      <span className="ml-auto shrink-0 text-xs text-brand">{openStep === i ? t("incidents.trace.collapse") : t("incidents.trace.expand")}</span>
                    </button>
                    <div className="truncate font-mono text-xs text-dim">
                      {typeof step.args === "object" ? Object.values(step.args ?? {}).flat().filter((x) => x !== "").join(" · ") : String(step.args)}
                    </div>
                    {openStep === i && (
                      <div className="mt-2 space-y-2 rounded-lg border border-line bg-slate-50/70 p-3">
                        <div>
                          <div className="mb-1 text-xs font-medium text-slate-500">{t("incidents.trace.args")}</div>
                          <pre className="overflow-x-auto whitespace-pre-wrap break-all font-mono text-xs text-slate-700">{JSON.stringify(step.args ?? {}, null, 2)}</pre>
                        </div>
                        <div>
                          <div className="mb-1 text-xs font-medium text-slate-500">{step.ok ? t("incidents.trace.result") : t("incidents.trace.failReason")}</div>
                          {step.result !== undefined ? (
                            <pre className="term max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md p-2.5 text-xs">
                              {typeof step.result === "string" ? step.result : JSON.stringify(step.result, null, 2)}
                            </pre>
                          ) : step.error ? (
                            <pre className="term max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md p-2.5 text-xs text-rose-300">{step.error}</pre>
                          ) : (
                            <div className="text-xs text-dim">{t("incidents.trace.noResult")}</div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                </li>
              ))}
              {cur.trace.length > 6 && (
                <li><button onClick={() => setTraceAll(!traceAll)} className="ml-6 text-xs font-medium text-brand hover:underline">
                  {traceAll ? t("incidents.trace.collapseList") : fill(t("incidents.trace.expandListTemplate"), { n: cur.trace.length - 6 })}
                </button></li>
              )}
            </ol>
          )}
        </Card>
      </div>

      {checks.length > 0 && (
        <Card>
          <CardHead title={t("incidents.checklist.title")} note={t("incidents.checklist.note")} icon={<Icon d={PATH.list} className="text-emerald-500" />} />
          <div className="grid grid-cols-2 divide-x divide-y divide-line">
            {checks.map(([k, v]) => (
              <div key={k} className="px-5 py-3">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-slate-800">{(CHECK_KEY[k] && t(CHECK_KEY[k])) || k}</span>
                  <Badge className={checkTone(v.status)}>{(STATUS_KEY[v.status] && t(STATUS_KEY[v.status])) || v.status}</Badge>
                </div>
                <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-dim" title={v.reason}><AiText text={plain(v.reason)} /></p>
              </div>
            ))}
          </div>
        </Card>
      )}

      {cur.undistinguishable.length > 0 && (
        <Card>
          <CardHead title={t("incidents.undistinguishable.title")} note={t("incidents.undistinguishable.note")} icon={<Icon d={PATH.list} className="text-amber-500" />} />
          <div className="divide-y divide-line">
            {cur.undistinguishable.map((u: any, i: number) => (
              <div key={i} className="px-5 py-3.5 text-sm">
                <div className="flex flex-wrap items-center gap-1.5">
                  {(u.candidates ?? []).map((c: string, j: number) => (
                    <Badge key={c} title={u.candidate_codes?.[j] || c} className="bg-amber-50 text-amber-700 ring-amber-200">{c}</Badge>
                  ))}
                </div>
                {u.why_indistinguishable && <div className="mt-2 text-[13px] leading-relaxed text-slate-600"><AiInline text={u.why_indistinguishable} /></div>}
                {u.what_data_would_help && <div className="mt-1.5 text-[13px] leading-relaxed text-amber-700">{t("incidents.undistinguishable.missingLabel")}<AiInline text={u.what_data_would_help} /></div>}
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
