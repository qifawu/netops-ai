import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { cn } from "../lib/api";
import { Badge, Card, CardHead, Empty, Icon, PATH } from "../lib/ui";
import { fill, useLang, useT, type DictKey } from "../lib/i18n";
import { Btn, call } from "../lib/sopui";
import Md from "../lib/Md";
import Spark from "../lib/Spark";
import { CheckPicker, DevicePicker, Hints, ScheduleChips, WidgetSummary, type Hint, type Selection, type Widget } from "./PlanWidgets";

/** 巡检计划：列表 + 对话式制定（聊天流 + 实时草案预览）+ 单个计划的运行历史 / 指标迷你趋势 / 趋势分析。
 *  草案的真源在后端：每一轮对话、每次「采纳」、每次改周期都回后端重新校验（含只读白名单），前端只负责展示。
 *  对话流程是后端的一张图（LangGraph），一个面板 = 图上的一个会话；图走到「人工确认」环节才会停住等确认，
 *  「确认并启用」就是让图从那里继续往下走到保存。 */

type Sched = { every_minutes?: number; daily_at?: string };
type Expect = { type: string; value: string; severity?: string };
type Extract = { name: string };
type PlanCheck = { id: string; template?: string; title?: string; command: string; devices?: string[]; expect?: Expect[]; extract?: Extract[] };
type Draft = { name: string; description?: string; vendor?: string; devices: { name: string; host: string; role?: string }[]; checks: PlanCheck[]; schedule: Sched; enabled?: boolean };
type Validation = { ok: boolean; problems: string[]; missing: string[] };
type Suggestion = { id: string; text: string; reason: string; patch: unknown | null; source?: string };
type Candidate = { command: string; purpose: string; source: string; origin: "docs" | "catalog"; allowed: boolean; reason: string; needs_interface: boolean };
type Turn = {
  session: string; reply: string; draft: Draft; validation: Validation; ready: boolean; suggestions: Suggestion[]; quick_replies: string[];
  source: string; fix_rounds: number; awaiting_confirm: boolean; editing: string; mode: string;
  candidates: Candidate[]; lookup_note: string; docs_status: string; messages?: { role: "user" | "assistant"; content: string }[];
  widget?: Widget | null; hints?: Hint[];
  saved?: { name: string } | null; save_error?: { message: string; problems?: string[]; status?: number } | null;
};
type Msg = {
  role: "user" | "assistant"; content: string; suggestions?: Suggestion[]; quick?: string[]; source?: string; fix?: number;
  candidates?: Candidate[]; lookupNote?: string;
  widget?: Widget; widgetDone?: string; hints?: Hint[];
  hidden?: boolean;  // 气泡选择回传的那条「用户消息」：发给后端当上下文，聊天流里不单独显示（气泡自己变成摘要）
};
type Confirmed = { commands: boolean; devices: boolean; schedule: boolean };
const NONE: Confirmed = { commands: false, devices: false, schedule: false };
type RunSum = { run_id: string; started_at: string; pass: number; fail: number; denied: number; error: number; unreachable_devices: number };
type PlanRow = Draft & { enabled: boolean; device_count: number; check_count: number; last_run: RunSum | null; next_run_at: string | null; running: boolean };
type RunCheck = { id: string; command: string; status: string; severity?: string; detail?: string; metrics?: Record<string, number | null>; evaluations?: (Expect & { passed: boolean })[]; output?: string };
type Run = RunSum & { devices: { name: string; error: string; checks: RunCheck[] }[] };
type DeletePreview = { name: string; plan_file: string; history_dir: string; history_count: number; trend_file: string; running: boolean };
type Trend = { runs: number; table: string; enough_runs: boolean; llm: boolean; analysis?: string; error?: string; messages?: { role: string; content: string }[] };

/** ISO 时间 → 本地「MM-DD HH:MM」，等宽显示。 */
const when = (iso?: string | null) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

function schedText(t: (k: DictKey) => string, s?: Sched) {
  if (!s) return t("plans.sched.none");
  if (s.every_minutes === 15) return t("plans.sched.every15");
  if (s.every_minutes === 60) return t("plans.sched.hourly");
  if (s.every_minutes === 360) return t("plans.sched.every6h");
  if (s.every_minutes) return fill(t("plans.sched.everyN"), { n: s.every_minutes });
  if (s.daily_at) return fill(t("plans.sched.dailyAt"), { t: s.daily_at });
  return t("plans.sched.none");
}

const STATUS_TONE: Record<string, string> = {
  pass: "bg-white text-emerald-700 ring-slate-300",
  fail: "bg-rose-50 text-rose-700 ring-rose-200",
  error: "bg-amber-50 text-amber-700 ring-amber-200",
  denied: "bg-amber-50 text-amber-700 ring-amber-200",
};
const LAYER_KEY: Record<string, DictKey> = { core: "topology.layer.core", aggregation: "topology.layer.aggregation", access: "topology.layer.access" };
const STATUS_KEY: Record<string, DictKey> = { pass: "plans.status.pass", fail: "plans.status.fail", error: "plans.status.error", denied: "plans.status.denied" };

/** 一次运行的计数：只把非零的异常项单独亮出来。 */
function RunCounts({ r }: { r: RunSum }) {
  const t = useT();
  return (
    <span className="num inline-flex flex-wrap items-center gap-x-2 text-xs">
      <span className="text-emerald-700">{r.pass} {t("plans.sum.pass")}</span>
      {r.fail > 0 && <span className="font-semibold text-bad">{r.fail} {t("plans.sum.fail")}</span>}
      {r.error + r.denied > 0 && <span className="font-semibold text-warn">{r.error + r.denied} {t("plans.sum.error")}</span>}
      {r.unreachable_devices > 0 && <span className="font-semibold text-bad">{r.unreachable_devices} {t("plans.sum.unreachable")}</span>}
    </span>
  );
}

function Toggle({ on, onChange, title }: { on: boolean; onChange: (v: boolean) => void; title?: string }) {
  return (
    <button onClick={() => onChange(!on)} title={title} role="switch" aria-checked={on}
      className={cn("relative inline-block h-4 w-7 shrink-0 rounded-[2px] transition", on ? "bg-brand" : "bg-slate-300")}>
      <span className={cn("absolute top-[2px] h-3 w-3 rounded-[1px] bg-white transition-all", on ? "left-[14px]" : "left-[2px]")} />
    </button>
  );
}

/** 周期选择：每 15 分钟 / 每小时 / 每 6 小时 / 每天某时刻；草案里是别的分钟数就多出一项原值。 */
function SchedulePicker({ value, onChange, small }: { value?: Sched; onChange: (s: Sched) => void; small?: boolean }) {
  const t = useT();
  const v = value ?? {};
  const preset = v.daily_at ? "daily" : v.every_minutes ? String(v.every_minutes) : "";
  const cls = cn("rounded-[3px] border border-line bg-white px-2 font-mono text-[12.5px] focus:border-brand focus:outline-none", small ? "py-[2px]" : "py-1");
  return (
    <span className="inline-flex items-center gap-1.5">
      <select data-testid="schedule-select" value={preset} className={cls}
        onChange={(e) => onChange(e.target.value === "daily" ? { daily_at: v.daily_at || "08:00" } : { every_minutes: Number(e.target.value) })}>
        {preset === "" && <option value="">{t("plans.sched.none")}</option>}
        <option value="15">{t("plans.sched.every15")}</option>
        <option value="60">{t("plans.sched.hourly")}</option>
        <option value="360">{t("plans.sched.every6h")}</option>
        <option value="daily">{t("plans.sched.daily")}</option>
        {preset && !["15", "60", "360", "daily"].includes(preset) && <option value={preset}>{fill(t("plans.sched.everyN"), { n: preset })}</option>}
      </select>
      {preset === "daily" && (
        <input type="time" value={v.daily_at} className={cn(cls, "w-[92px]")} onChange={(e) => e.target.value && onChange({ daily_at: e.target.value })} />
      )}
    </span>
  );
}

// ---------------------------------------------------------------------------------------- 页面

export default function InspectionPlans() {
  const t = useT();
  const [rows, setRows] = useState<PlanRow[] | null>(null);
  const [building, setBuilding] = useState<{ mode: "new" | "edit"; plan: string; key: number } | null>(null);
  const [open, setOpen] = useState("");
  const [note, setNote] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [trendFor, setTrendFor] = useState("");
  const [deleting, setDeleting] = useState("");

  const reload = async () => {
    const r = await call<PlanRow[]>("GET", "/api/inspection/plans");
    const list = r.ok && Array.isArray(r.data) ? r.data : [];
    setRows(list);
    return list;
  };
  useEffect(() => { reload(); }, []);
  // 有计划在跑就每 2 秒刷新一次，跑完自动停
  useEffect(() => {
    if (!rows?.some((r) => r.running)) return;
    const h = setTimeout(reload, 2000);
    return () => clearTimeout(h);
  }, [rows]);

  const patch = async (name: string, body: object) => {
    const r = await call<PlanRow & { message?: string }>("PATCH", `/api/inspection/plans/${encodeURIComponent(name)}`, body);
    if (!r.ok) setNote({ tone: "bad", text: r.data.message || t("plans.saveFailed") });
    reload();
  };
  const runNow = async (name: string) => {
    const r = await call("POST", `/api/inspection/plans/${encodeURIComponent(name)}/run`);
    if (!r.ok && r.data.message) setNote({ tone: "bad", text: r.data.message });
    setOpen(name);
    setRows((xs) => xs?.map((x) => (x.name === name ? { ...x, running: true } : x)) ?? xs);
    setTimeout(reload, 800);
  };

  return (
    <div className="space-y-4" data-testid="inspection-plans">
      <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-0 flex-1 text-[13px] leading-relaxed text-slate-600">{t("plans.intro")}</p>
        {(rows?.length ?? 0) > 0 && <Btn onClick={() => { setBuilding({ mode: "edit", plan: "", key: Date.now() }); setNote(null); }}>{t("plans.adjustExisting")}</Btn>}
        <Btn tone="brand" onClick={() => { setBuilding({ mode: "new", plan: "", key: Date.now() }); setNote(null); }}>{t("plans.new")}</Btn>
      </div>
      {note && (
        <div className={cn("rounded-[3px] px-4 py-2.5 text-sm ring-1", note.tone === "ok" ? "bg-emerald-50 text-emerald-700 ring-emerald-200" : "bg-rose-50 text-rose-700 ring-rose-200")}>{note.text}</div>
      )}
      {building && (
        <PlanBuilder key={building.key} mode={building.mode} plan={building.plan} onClose={() => setBuilding(null)}
          onSaved={(name) => { setBuilding(null); setNote({ tone: "ok", text: fill(t("plans.saved"), { name }) }); setOpen(name); reload(); }} />
      )}

      <Card>
        <CardHead title={t("plans.list.title")} icon={<Icon d={PATH.clock} />} right={rows && <span className="num text-xs text-dim">{rows.length}</span>} />
        {rows === null ? <div className="h-20 animate-pulse bg-slate-100" /> : rows.length === 0 ? (
          <Empty title={t("plans.list.empty")} hint={t("plans.list.emptyHint")} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[13px]" data-testid="plans-table">
              <thead>
                <tr className="border-b border-line text-left text-xs text-dim">
                  {(["plans.col.name", "plans.col.devices", "plans.col.checks", "plans.col.schedule", "plans.col.enabled", "plans.col.last", "plans.col.next", "plans.col.actions"] as DictKey[]).map((k) => (
                    <th key={k} className="px-3 py-2 font-medium whitespace-nowrap first:pl-4">{t(k)}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rows.map((p) => (
                  <Fragment key={p.name}>
                    <tr className={cn("align-middle", open === p.name && "bg-slate-50/70")}>
                      <td className="max-w-[260px] py-2 pr-3 pl-4">
                        <button onClick={() => setOpen(open === p.name ? "" : p.name)} className="flex items-center gap-1.5 text-left font-mono text-[13px] font-semibold text-slate-800 hover:text-brand">
                          <Icon d={PATH.chevron} className={cn("h-3.5 w-3.5 text-slate-400 transition", open === p.name && "rotate-90")} />
                          {p.name}
                        </button>
                        {p.description && <div className="truncate pl-5 text-xs text-dim" title={p.description}>{p.description}</div>}
                      </td>
                      <td className="num px-3 py-2" title={p.devices.map((d) => d.name).join(", ")}>{p.device_count}</td>
                      <td className="num px-3 py-2">{p.check_count}</td>
                      <td className="px-3 py-2"><SchedulePicker small value={p.schedule} onChange={(s) => patch(p.name, { schedule: s })} /></td>
                      <td className="px-3 py-2"><Toggle on={p.enabled} onChange={(v) => patch(p.name, { enabled: v })} /></td>
                      <td className="px-3 py-2 whitespace-nowrap">
                        {p.running ? <span className="flex items-center gap-1.5 text-xs text-brand"><span className="pulse-dot h-1.5 w-1.5 rounded-full bg-brand" />{t("plans.running")}</span>
                          : p.last_run ? <div className="leading-tight"><RunCounts r={p.last_run} /><div className="num text-[11px] text-dim">{when(p.last_run.started_at)}</div></div>
                            : <span className="text-xs text-dim">{t("plans.never")}</span>}
                      </td>
                      <td className="num px-3 py-2 text-xs whitespace-nowrap text-slate-600">{p.enabled ? when(p.next_run_at) : <span className="text-dim">{t("plans.paused")}</span>}</td>
                      <td className="px-3 py-2">
                        <span className="flex gap-1.5">
                          <Btn small busy={p.running} disabled={p.running} onClick={() => runNow(p.name)}>{t("plans.runNow")}</Btn>
                          <Btn small onClick={() => { setOpen(p.name); setTrendFor(p.name + ":" + Date.now()); }}>{t("plans.trend")}</Btn>
                          <Btn small onClick={() => { setBuilding({ mode: "edit", plan: p.name, key: Date.now() }); setNote(null); window.scrollTo({ top: 0 }); }}>{t("plans.adjust")}</Btn>
                          <button data-testid={`delete-${p.name}`} onClick={() => setDeleting(p.name)}
                            className="inline-flex items-center rounded-[3px] px-2 py-[3px] text-xs font-medium whitespace-nowrap text-bad ring-1 ring-[#c3cad2] ring-inset transition hover:bg-rose-50 hover:ring-rose-300">
                            {t("plans.delete")}
                          </button>
                        </span>
                      </td>
                    </tr>
                    {open === p.name && (
                      <tr><td colSpan={8} className="border-t border-line bg-[#fafbfc] p-0">
                        <PlanDetail name={p.name} running={p.running} lastRunId={p.last_run?.run_id ?? ""} trendKey={trendFor.startsWith(p.name + ":") ? trendFor : ""}
                          onRenamed={(n) => { setOpen(n); setNote({ tone: "ok", text: fill(t("plans.renamed"), { name: n }) }); reload(); }} />
                      </td></tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {deleting && (
        <DeleteDialog name={deleting} onClose={() => setDeleting("")}
          onDeleted={(msg) => { setDeleting(""); if (open === deleting) setOpen(""); setNote({ tone: "ok", text: msg }); reload(); }} />
      )}
    </div>
  );
}

/** 删除确认框：明确列出要删的文件；「连历史运行记录一起删」默认不勾（只删计划、保留历史）。
 *  正在运行的计划不让删（后端同样会 409），提示稍后再来。 */
function DeleteDialog({ name, onClose, onDeleted }: { name: string; onClose: () => void; onDeleted: (msg: string) => void }) {
  const t = useT();
  const [pv, setPv] = useState<DeletePreview | null>(null);
  const [err, setErr] = useState("");
  const [withHistory, setWithHistory] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    call<DeletePreview>("GET", `/api/inspection/plans/${encodeURIComponent(name)}/delete-preview`)
      .then((r) => (r.ok ? setPv(r.data) : setErr(r.data.message || t("plans.saveFailed"))));
  }, [name]);
  const go = async () => {
    setBusy(true);
    const r = await call<{ deleted: string[]; history_deleted: number }>("DELETE",
      `/api/inspection/plans/${encodeURIComponent(name)}${withHistory ? "?with_history=true" : ""}`);
    setBusy(false);
    if (r.ok) onDeleted(fill(t("plans.delete.done"), { name, n: r.data.history_deleted }));
    else setErr(r.data.message || t("plans.saveFailed"));
  };
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#10182080]" role="dialog" aria-modal="true" data-testid="delete-dialog">
      <div className="w-[560px] rounded-[4px] border border-line bg-card shadow-lg">
        <div className="flex items-center gap-2 border-b border-line bg-[#f6f7f9] px-4 py-2.5">
          <Icon d={PATH.trash} className="text-bad" />
          <span className="text-[14px] font-semibold text-slate-800">{fill(t("plans.delete.title"), { name })}</span>
        </div>
        <div className="space-y-3 px-4 py-3 text-[13px] text-slate-700">
          {!pv && !err && <div className="h-12 animate-pulse bg-slate-100" />}
          {pv && (
            <>
              <div>
                <div className="text-xs font-medium text-slate-600">{t("plans.delete.willDelete")}</div>
                <ul className="mt-1 list-disc space-y-0.5 pl-5 font-mono text-[12px]">
                  <li>{pv.plan_file}</li>
                  {withHistory && pv.history_count > 0 && <li>{fill(t("plans.delete.historyItem"), { dir: pv.history_dir, n: pv.history_count, name })}</li>}
                  {withHistory && pv.trend_file && <li>{pv.trend_file}</li>}
                </ul>
              </div>
              <label className={cn("flex items-start gap-2 rounded-[3px] border border-line px-3 py-2", pv.history_count === 0 && "opacity-60")}>
                <input type="checkbox" data-testid="delete-with-history" className="mt-[3px] accent-[#c42b2b]" checked={withHistory}
                  disabled={pv.history_count === 0 && !pv.trend_file} onChange={(e) => setWithHistory(e.target.checked)} />
                <span>
                  {fill(t("plans.delete.history"), { dir: pv.history_dir, n: pv.history_count })}
                  <span className="block text-xs text-dim">{t("plans.delete.historyNote")}</span>
                </span>
              </label>
              {pv.running && <div className="rounded-[3px] bg-amber-50 px-3 py-2 text-xs text-amber-800 ring-1 ring-amber-200">{t("plans.delete.running")}</div>}
            </>
          )}
          {err && <div className="rounded-[3px] bg-rose-50 px-3 py-2 text-xs text-rose-800 ring-1 ring-rose-200">{err}</div>}
        </div>
        <div className="flex justify-end gap-2 border-t border-line px-4 py-3">
          <Btn onClick={onClose}>{t("plans.delete.cancel")}</Btn>
          <button data-testid="delete-confirm" disabled={!pv || pv.running || busy} onClick={go}
            className="inline-flex items-center gap-1.5 rounded-[3px] bg-bad px-3 py-1.5 text-[13px] font-medium whitespace-nowrap text-white transition hover:bg-[#ad2424] disabled:cursor-not-allowed disabled:opacity-45">
            {busy && <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />}
            {t("plans.delete.confirm")}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------- 对话面板

function PlanBuilder({ mode, plan, onClose, onSaved }: { mode: "new" | "edit"; plan: string; onClose: () => void; onSaved: (name: string) => void }) {
  const t = useT();
  const lang = useLang();
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [valid, setValid] = useState<Validation | null>(null);
  const [awaiting, setAwaiting] = useState(false);
  const session = useRef(Math.random().toString(36).slice(2) + Date.now().toString(36));
  const [busy, setBusy] = useState(false);
  const [input, setInput] = useState("");
  const [adopted, setAdopted] = useState<Set<string>>(new Set());
  const [saveErr, setSaveErr] = useState<string[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [ifname, setIfname] = useState<Record<string, string>>({});
  const [closedHints, setClosedHints] = useState<Set<string>>(new Set());
  const scroller = useRef<HTMLDivElement>(null);

  const apply = (r: Turn) => {
    setDraft(r.draft);
    setValid(r.validation);
    setAwaiting(r.awaiting_confirm);
    setEditing(r.editing || "");
    setSaveErr(null);
  };
  /** 只是提醒、没有可并入改动的建议（比如「趋势至少要 3 次运行」）同一个对话里只出现一次，不在每条回复下面重复。 */
  const shownNotes = useRef(new Set<string>());
  const freshSuggestions = (xs: Suggestion[]) => xs.filter((x) => {
    if (x.patch) return true;
    if (shownNotes.current.has(x.id)) return false;
    shownNotes.current.add(x.id);
    return true;
  });
  const asMsg = (r: Turn): Msg => ({ role: "assistant", content: r.reply, suggestions: freshSuggestions(r.suggestions ?? []), quick: r.quick_replies, source: r.source,
    fix: r.fix_rounds, candidates: r.candidates, lookupNote: r.docs_status ? r.lookup_note : "",  // docs_status 有值 = 这一轮查过命令（哪怕没找到也要说）
    widget: r.widget ?? undefined, hints: r.hints ?? [] });
  const turn = async (history: Msg[], d: Draft | null, selection?: Selection) => {
    setBusy(true);
    const r = await call<Turn>("POST", "/api/inspection/plans/chat", {
      session: session.current, messages: history.filter((m) => m.source !== "opening" && m.source !== "widget").map((m) => ({ role: m.role, content: m.content })),
      draft: d, lang, mode, plan, selection: selection ?? null,
    });
    setBusy(false);
    if (!r.ok) { setMsgs([...history, { role: "assistant", content: r.data.message || t("plans.chat.failed") }]); return; }
    apply(r.data);
    setMsgs([...history, asMsg(r.data)]);
  };
  // 开场白由助手先说（后端不调模型，按拓扑拼）
  useEffect(() => { turn([], null); }, []);
  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" }); }, [msgs, busy]);

  const send = (text: string) => {
    const s = text.trim();
    if (!s || busy) return;
    setInput("");
    const history: Msg[] = [...msgs, { role: "user", content: s }];
    setMsgs(history);
    turn(history, draft); // 开场白是固定话术，发请求时滤掉，不进模型上下文
  };
  /** 采纳 / 改周期 / 改名：确定性地并进草案，后端重新校验；不改变图停在哪（确认时提交的是这份最新草案，保存前照样再校验）。 */
  const patchDraft = async (body: { patch?: unknown; draft?: Draft | null }, sid?: string) => {
    const last = [...msgs].reverse().find((m) => m.role === "assistant");
    const shown = (last?.suggestions ?? []).filter((s) => s.patch);
    const r = await call<Turn & { live_suggestions: string[] }>("POST", "/api/inspection/plans/draft",
      { draft: body.draft ?? draft, patch: body.patch ?? null, lang, suggestions: shown });
    if (!r.ok) return;
    setDraft(r.data.draft);
    setValid(r.data.validation);
    const idx = msgs.lastIndexOf(last as Msg);
    // 采纳的那条记「已采纳」；别的建议因此变得多余的（效果已经在草案里了）也一并标掉，免得点了没反应
    setAdopted((s) => {
      const n = new Set(s);
      if (sid) n.add(sid);
      shown.forEach((x) => { if (!r.data.live_suggestions?.includes(x.id)) n.add(`${idx}:${x.id}`); });
      return n;
    });
  };
  /** 确认卡提交：三项都确认 → 后端从确认中断恢复到保存；有一项没确认 → 回到对话（后端替用户补一句「我还没确认 X」）。 */
  const submit = async (confirmed: Confirmed) => {
    if (!draft) return;
    setSaving(true);
    const r = await call<Turn & { problems?: string[] }>("POST", "/api/inspection/plans/confirm", {
      session: session.current, draft, confirmed, messages: msgs.filter((m) => m.source !== "opening").map((m) => ({ role: m.role, content: m.content })),
    });
    setSaving(false);
    if (r.ok && r.data.saved) { onSaved(r.data.saved.name); return; }
    if (r.data.save_error || !r.ok) {
      setSaveErr([r.data.message || r.data.save_error?.message || t("plans.saveFailed"), ...(r.data.problems ?? r.data.save_error?.problems ?? [])]);
      if (typeof r.data.awaiting_confirm === "boolean") setAwaiting(r.data.awaiting_confirm);
      return;
    }
    // 回到了对话：把后端补的那句「我还没确认……」和助手的新回复接到聊天流里
    const note = [...(r.data.messages ?? [])].reverse().find((m) => m.role === "user");
    apply(r.data);
    setMsgs((xs) => [...xs, ...(note ? [{ role: "user" as const, content: note.content }] : []), asMsg(r.data)]);
  };
  /** 气泡里选完：这个气泡变成只读摘要，选择作为一条结构化消息回传（后端确定性地写进草案，不调模型）。 */
  const submitWidget = (i: number, sel: Selection, summary: string) => {
    if (busy) return;
    const history: Msg[] = [...msgs.map((m, j) => (j === i ? { ...m, widgetDone: summary } : m)), { role: "user", content: summary, hidden: true }];
    setMsgs(history);
    turn(history, draft, sel);
  };
  /** 重新打开一个选择气泡（草案预览里的「选择设备」、摘要上的「修改」）：它成为最新、唯一能操作的那个。 */
  const reopen = async (type: Widget["type"]) => {
    if (busy) return;
    const r = await call<Widget>("POST", "/api/inspection/plans/widget", { session: session.current, type, draft, lang });
    if (!r.ok) return;
    setMsgs((xs) => [...xs, { role: "assistant", content: r.data.prompt || "", widget: r.data, source: "widget" }]);
  };
  const pick = (i: number, c: Candidate) => {
    const key = `${i}:${c.command}`;
    const cmd = c.needs_interface ? c.command.replace("{interface}", (ifname[key] || "").trim()) : c.command;
    if (c.needs_interface && !(ifname[key] || "").trim()) return;
    patchDraft({ patch: { add_custom_checks: [{ command: cmd }] } });
    setPicked((s) => new Set(s).add(key));
  };

  const lastAssistant = msgs.map((m) => m.role).lastIndexOf("assistant");

  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]" data-testid="plan-builder">
      <Card className="flex h-[640px] flex-col">
        <CardHead title={editing ? fill(t("plans.chat.titleEdit"), { name: editing }) : t(mode === "edit" ? "plans.adjustExisting" : "plans.chat.title")}
          note={t("plans.chat.note")} icon={<Icon d={PATH.send} />}
          right={<Btn small onClick={onClose}>{t("plans.cancel")}</Btn>} />
        <div ref={scroller} className="flex-1 space-y-3 overflow-y-auto px-4 py-3" data-testid="plan-chat">
          {msgs.map((m, i) => m.hidden ? null : m.role === "user" ? (
            <div key={i} className="flex justify-end">
              <div className="max-w-[85%] rounded-[3px] bg-steel-50 px-3 py-2 text-[13px] leading-relaxed whitespace-pre-wrap text-slate-800 ring-1 ring-steel-200">{m.content}</div>
            </div>
          ) : (
            <div key={i} className="max-w-[94%]">
              <div className="mb-1 flex items-center gap-2 text-[11px] text-dim">
                <span className="font-semibold text-slate-600">{t("plans.chat.assistant")}</span>
                {m.source === "rules" && <Badge className="bg-amber-50 text-amber-700 ring-amber-200">{t("plans.chat.rules")}</Badge>}
                {(m.fix ?? 0) > 0 && <Badge className="bg-white text-slate-600 ring-slate-300">{fill(t("plans.chat.fixed"), { n: m.fix! })}</Badge>}
              </div>
              <div className="rounded-[3px] border border-line bg-white px-3 py-2 text-[13px] leading-relaxed whitespace-pre-wrap text-slate-800">{m.content}</div>
              <Hints hints={(m.hints ?? []).filter((h) => !closedHints.has(h.id))} onClose={(id) => setClosedHints((s) => new Set(s).add(id))} />
              {m.widget && (m.widgetDone
                ? <WidgetSummary text={m.widgetDone} onEdit={() => reopen(m.widget!.type)} />
                : i !== lastAssistant ? <WidgetSummary text="" expired />
                  : m.widget.type === "device_picker" ? <DevicePicker key={m.widget.id} w={m.widget} onSubmit={(sel, sum) => submitWidget(i, sel, sum)} />
                    : m.widget.type === "schedule_picker" ? <ScheduleChips key={m.widget.id} w={m.widget} label={(v) => schedText(t, v)} onSubmit={(sel, sum) => submitWidget(i, sel, sum)} />
                      : <CheckPicker key={m.widget.id} w={m.widget} onSubmit={(sel, sum) => submitWidget(i, sel, sum)} />)}
              {(m.suggestions?.length ?? 0) > 0 && (
                <div className="mt-2 space-y-1.5" data-testid="plan-suggestions">
                  {m.suggestions!.map((s) => (
                    <div key={s.id} className="flex items-start gap-3 border border-line border-l-2 border-l-brand bg-[#f7f9fa] px-3 py-2">
                      <div className="min-w-0 flex-1">
                        <div className="text-[13px] font-medium text-slate-800">{s.text}</div>
                        <div className="mt-0.5 text-xs leading-relaxed text-dim">{s.reason}</div>
                      </div>
                      {s.patch ? (
                        adopted.has(`${i}:${s.id}`) ? <Badge className="mt-0.5 bg-white text-emerald-700 ring-slate-300">{t("plans.sugg.adopted")}</Badge>
                          : <Btn small disabled={i !== lastAssistant || busy} onClick={() => patchDraft({ patch: s.patch }, `${i}:${s.id}`)}>{t("plans.sugg.adopt")}</Btn>
                      ) : <Badge className="mt-0.5 bg-white text-slate-500 ring-slate-300">{t("plans.sugg.note")}</Badge>}
                    </div>
                  ))}
                </div>
              )}
              {((m.candidates?.length ?? 0) > 0 || !!m.lookupNote) && (
                <div className="mt-2 border border-line bg-white" data-testid="plan-candidates">
                  <div className="flex items-center gap-2 border-b border-line bg-[#f6f7f9] px-3 py-1.5 text-xs">
                    <Icon d={PATH.search} className="h-3.5 w-3.5 text-slate-500" />
                    <span className="font-semibold text-slate-700">{t("plans.cand.title")}</span>
                  </div>
                  {m.lookupNote && <div className="border-b border-line px-3 py-1.5 text-[11.5px] leading-relaxed text-slate-600">{m.lookupNote}</div>}
                  <div className="divide-y divide-line">
                    {(m.candidates ?? []).map((c) => {
                      const key = `${i}:${c.command}`;
                      return (
                        <div key={c.command} className={cn("flex items-start gap-3 px-3 py-2", !c.allowed && "bg-rose-50/40")}>
                          <div className="min-w-0 flex-1">
                            <div className={cn("font-mono text-[12px] break-words", c.allowed ? "text-slate-800" : "text-slate-500 line-through")}>{c.command}</div>
                            {c.purpose && <div className="mt-0.5 text-xs text-slate-600">{c.purpose}</div>}
                            <div className="mt-0.5 truncate text-[11px] text-dim" title={c.source}>
                              {t(c.origin === "docs" ? "plans.cand.docs" : "plans.cand.catalog")} · {c.source}
                            </div>
                            {!c.allowed && <div className="mt-0.5 text-[11px] text-bad">{c.reason}</div>}
                          </div>
                          {c.allowed && (picked.has(key) ? <Badge className="mt-0.5 bg-white text-emerald-700 ring-slate-300">{t("plans.cand.picked")}</Badge> : (
                            <span className="flex shrink-0 items-center gap-1.5">
                              {c.needs_interface && (
                                <input value={ifname[key] || ""} onChange={(e) => setIfname({ ...ifname, [key]: e.target.value })} placeholder={t("plans.cand.ifPlaceholder")}
                                  className="w-[132px] rounded-[3px] border border-line px-1.5 py-[2px] font-mono text-[11.5px] focus:border-brand focus:outline-none" />
                              )}
                              <Btn small disabled={i !== lastAssistant || busy || (c.needs_interface && !(ifname[key] || "").trim())} onClick={() => pick(i, c)}>{t("plans.cand.pick")}</Btn>
                            </span>
                          ))}
                          {!c.allowed && <Badge className="mt-0.5 bg-rose-50 text-rose-700 ring-rose-200">{t("plans.cand.unusable")}</Badge>}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
              {i === lastAssistant && !busy && (m.quick?.length ?? 0) > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5" data-testid="plan-quick">
                  {m.quick!.map((q) => <Btn key={q} small onClick={() => send(q)}>{q}</Btn>)}
                </div>
              )}
            </div>
          ))}
          {busy && <div className="flex items-center gap-2 text-xs text-dim"><span className="pulse-dot h-1.5 w-1.5 rounded-full bg-brand" />{t("plans.chat.thinking")}</div>}
        </div>
        <div className="flex items-end gap-2 border-t border-line p-3">
          <textarea data-testid="plan-input" value={input} rows={2} placeholder={t("plans.chat.placeholder")}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(input); } }}
            className="min-h-[44px] flex-1 resize-none rounded-[3px] border border-line bg-white px-3 py-2 text-[13px] leading-relaxed focus:border-brand focus:outline-none" />
          <Btn tone="brand" disabled={busy || !input.trim()} onClick={() => send(input)}>{t("plans.chat.send")}</Btn>
        </div>
      </Card>

      <Card className="flex h-[640px] flex-col">
        <CardHead title={t("plans.draft.title")} icon={<Icon d={PATH.list} />}
          right={valid && (valid.ok ? <Badge className="bg-white text-emerald-700 ring-slate-300">✓ {t("plans.status.pass")}</Badge>
            : valid.problems.length ? <Badge className="bg-rose-50 text-rose-700 ring-rose-200">{t("plans.status.fail")}</Badge> : null)} />
        <div className="flex-1 space-y-4 overflow-y-auto px-4 py-3" data-testid="plan-draft">
          {!draft ? <div className="h-24 animate-pulse bg-slate-100" /> : (
            <>
              <div className="grid grid-cols-[84px_minmax(0,1fr)] items-center gap-x-3 gap-y-2.5 text-[13px]">
                <span className="text-xs font-medium text-slate-600">{t("plans.draft.name")}</span>
                <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} onBlur={() => patchDraft({ draft })}
                  className="w-full max-w-[280px] rounded-[3px] border border-line bg-white px-2 py-1 font-mono text-[12.5px] focus:border-brand focus:outline-none" />
                <span className="text-xs font-medium text-slate-600">{t("plans.draft.schedule")}</span>
                <span><SchedulePicker value={draft.schedule} onChange={(s) => patchDraft({ draft: { ...draft, schedule: s } })} /></span>
                <span className="self-start pt-0.5 text-xs font-medium text-slate-600">{t("plans.draft.devices")}</span>
                <span className="flex flex-wrap items-center gap-1.5">
                  <button type="button" onClick={() => reopen("device_picker")} disabled={busy} data-testid="reopen-devices"
                    className="order-last rounded-[2px] border border-dashed border-brand/60 px-1.5 py-px text-xs text-brand transition hover:bg-steel-50 disabled:opacity-45">
                    {t("plans.widget.pickDevices")}
                  </button>
                  {draft.devices.length === 0 ? <span className="text-xs text-dim">—</span> : draft.devices.map((d) => (
                    <span key={d.name} className="inline-flex items-center gap-1.5 rounded-[2px] border border-line bg-white px-1.5 py-px text-xs">
                      <b className="font-mono font-semibold text-slate-800">{d.name}</b>
                      {d.role && <span className="text-dim">{LAYER_KEY[d.role] ? t(LAYER_KEY[d.role]) : d.role}</span>}
                    </span>
                  ))}
                </span>
              </div>
              {draft.description && <p className="text-xs leading-relaxed text-slate-600">{draft.description}</p>}

              <div>
                <div className="mb-1.5 flex items-baseline gap-2">
                  <span className="text-xs font-semibold text-slate-700">{t("plans.draft.checks")}</span>
                  <span className="num text-xs text-dim">{draft.checks.length}</span>
                </div>
                {draft.checks.length === 0 ? <div className="border border-dashed border-line px-3 py-6 text-center text-xs text-dim">{t("plans.draft.empty")}</div> : (
                  <table className="w-full table-fixed border border-line text-xs" data-testid="draft-checks">
                    <thead className="bg-[#f6f7f9] text-left text-dim">
                      <tr>
                        <th className="w-[30%] px-2 py-1.5 font-medium">{t("plans.draft.col.check")}</th>
                        <th className="px-2 py-1.5 font-medium">{t("plans.draft.col.command")}</th>
                        <th className="w-[18%] px-2 py-1.5 font-medium">{t("plans.draft.col.scope")}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line">
                      {draft.checks.map((c) => (
                        <tr key={c.id} className="align-top">
                          <td className="px-2 py-1.5">
                            <div className="font-medium break-words text-slate-800">{c.title || c.id}</div>
                            <div className="mt-0.5 text-[11px] text-dim">{fill(t("plans.draft.rules"), { e: c.expect?.length ?? 0, m: c.extract?.length ?? 0 })}</div>
                          </td>
                          <td className="px-2 py-1.5 font-mono text-[11.5px] leading-snug break-words text-slate-700">{c.command}</td>
                          <td className="px-2 py-1.5 font-mono text-[11.5px] break-words text-slate-600">{c.devices ? c.devices.join(", ") : t("plans.draft.allDevices")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>

              {valid && (
                <div data-testid="draft-validation" className={cn("rounded-[3px] px-3 py-2 text-xs leading-relaxed ring-1",
                  valid.problems.length ? "bg-rose-50 text-rose-800 ring-rose-200" : valid.ok ? "bg-emerald-50 text-emerald-800 ring-emerald-200" : "bg-slate-50 text-slate-600 ring-line")}>
                  {valid.problems.length > 0 ? (
                    <>
                      <div className="font-semibold">{t("plans.valid.bad")}</div>
                      <ul className="mt-1 list-disc space-y-0.5 pl-4 font-mono text-[11.5px] break-words">{valid.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>
                    </>
                  ) : valid.ok ? <div className="flex items-center gap-1.5 font-medium"><Icon d={PATH.shield} className="h-3.5 w-3.5" />{t("plans.valid.ok")}</div> : null}
                  {valid.missing.length > 0 && (
                    <div className={cn(valid.problems.length > 0 && "mt-1.5")}>
                      {t("plans.valid.missing")}
                      {valid.missing.map((m) => t(m.startsWith("devices") ? "plans.missing.devices" : m.startsWith("checks") ? "plans.missing.checks" : "plans.missing.schedule")).join(lang === "zh" ? "、" : ", ")}
                    </div>
                  )}
                </div>
              )}
              {saveErr && (
                <div className="rounded-[3px] bg-rose-50 px-3 py-2 text-xs text-rose-800 ring-1 ring-rose-200">
                  <div className="font-semibold">{saveErr[0]}</div>
                  <ul className="mt-1 list-disc pl-4 font-mono text-[11.5px] break-words">{saveErr.slice(1).map((p, i) => <li key={i}>{p}</li>)}</ul>
                </div>
              )}
            </>
          )}
        </div>
        <div className="flex min-h-[52px] items-center gap-3 border-t border-line px-4 py-3 text-xs">
          {awaiting && valid?.ok
            ? <span className="flex items-center gap-1.5 font-medium text-brand"><Icon d={PATH.check} className="h-3.5 w-3.5" />{t("plans.confirm.hint")}</span>
            : <span className="text-dim">{t("plans.confirm.notReady")}</span>}
        </div>
      </Card>
      {awaiting && draft && (
        <div className="xl:col-span-2">
          <ConfirmCard draft={draft} valid={!!valid?.ok} busy={saving || busy} errors={saveErr}
            onPatch={(patch) => patchDraft({ patch })} onDraft={(d) => patchDraft({ draft: d })} onSubmit={submit} />
        </div>
      )}
    </div>
  );
}

/** 保存前的三项确认：①每台设备要执行的只读命令 ②实施机器 ③实施周期。每栏都能改，改了那一栏要重新勾确认；
 *  三项都勾上才能启用。点「还要改」= 带着没勾的那几项回到对话。 */
function ConfirmCard({ draft, valid, busy, errors, onPatch, onDraft, onSubmit }: {
  draft: Draft; valid: boolean; busy: boolean; errors: string[] | null;
  onPatch: (patch: unknown) => void; onDraft: (d: Draft) => void; onSubmit: (c: Confirmed) => void;
}) {
  const t = useT();
  const [ok, setOk] = useState<Confirmed>(NONE);
  const all = ok.commands && ok.devices && ok.schedule;
  const edit = (col: keyof Confirmed, fn: () => void) => { setOk((x) => ({ ...x, [col]: false })); fn(); };
  const paused = draft.enabled === false;
  const col = (col: keyof Confirmed, title: DictKey, children: ReactNode) => (
    <div className={cn("flex min-w-0 flex-col border-line", col !== "schedule" && "border-r")}>
      <label className={cn("flex cursor-pointer items-center gap-2 border-b border-line px-3 py-2 text-[13px] font-semibold",
        ok[col] ? "bg-emerald-50 text-emerald-800" : "bg-[#f6f7f9] text-slate-800")}>
        <input type="checkbox" data-testid={`confirm-${col}`} checked={ok[col]} onChange={(e) => setOk({ ...ok, [col]: e.target.checked })} className="accent-[#0f5c7a]" />
        {t(title)}
        <span className="ml-auto text-[11px] font-normal text-dim">{ok[col] ? t("plans.confirm.done") : t("plans.confirm.tick")}</span>
      </label>
      <div className="max-h-[260px] flex-1 overflow-y-auto px-3 py-2">{children}</div>
    </div>
  );
  const byDevice = draft.devices.map((d) => ({ d, checks: draft.checks.filter((c) => !c.devices || c.devices.includes(d.name)) }));
  return (
    <Card className="border-brand/50" >
      <div data-testid="confirm-card">
        <CardHead title={t("plans.confirm.title")} note={t("plans.confirm.note")} icon={<Icon d={PATH.shield} />} />
        <div className="grid grid-cols-1 md:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)_minmax(0,0.9fr)]">
          {col("commands", "plans.confirm.commands", (
            <div className="space-y-2">
              {byDevice.map(({ d, checks }) => (
                <div key={d.name}>
                  <div className="font-mono text-xs font-semibold text-slate-800">{d.name}</div>
                  {checks.length === 0 ? <div className="text-[11px] text-dim">—</div> : checks.map((c) => (
                    <div key={c.id} className="group flex items-start gap-1.5 pl-2">
                      <span className="min-w-0 flex-1 font-mono text-[11.5px] leading-snug break-words text-slate-700">{c.command}</span>
                      <button title={t("plans.confirm.remove")} onClick={() => edit("commands", () => onPatch({ remove_checks: [c.id] }))}
                        className="shrink-0 px-1 text-[12px] leading-none text-slate-400 hover:text-bad">×</button>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          ))}
          {col("devices", "plans.confirm.devices", (
            <div className="space-y-1">
              {draft.devices.map((d) => (
                <div key={d.name} className="flex items-center gap-2 text-xs">
                  <b className="w-10 font-mono text-slate-800">{d.name}</b>
                  <span className="text-dim">{d.role ? (LAYER_KEY[d.role] ? t(LAYER_KEY[d.role]) : d.role) : ""}</span>
                  <span className="num text-[11px] text-slate-500">{d.host}</span>
                  <button title={t("plans.confirm.remove")} onClick={() => edit("devices", () => onPatch({ remove_devices: [d.name] }))}
                    className="ml-auto px-1 text-[12px] leading-none text-slate-400 hover:text-bad">×</button>
                </div>
              ))}
            </div>
          ))}
          {col("schedule", "plans.confirm.schedule", (<>
            <SchedulePicker value={draft.schedule} onChange={(sc) => edit("schedule", () => onDraft({ ...draft, schedule: sc }))} />
            <div className="mt-2 text-xs text-slate-600">{schedText(t, draft.schedule)}</div>
            {paused && <div className="mt-1 text-xs text-warn">{t("plans.confirm.paused")}</div>}
          </>))}
        </div>
        {errors && (
          <div className="mx-3 mb-2 rounded-[3px] bg-rose-50 px-3 py-2 text-xs text-rose-800 ring-1 ring-rose-200">
            <div className="font-semibold">{errors[0]}</div>
            <ul className="mt-1 list-disc pl-4 font-mono text-[11.5px] break-words">{errors.slice(1).map((p, i) => <li key={i}>{p}</li>)}</ul>
          </div>
        )}
        <div className="flex items-center gap-3 border-t border-line px-4 py-3">
          <span className="min-w-0 flex-1 text-xs text-dim">{!valid ? t("plans.valid.bad") : all ? "" : t("plans.confirm.needAll")}</span>
          <Btn disabled={busy || all} onClick={() => onSubmit(ok)}>{t("plans.confirm.back")}</Btn>
          <Btn tone="brand" busy={busy} disabled={!all || !valid || busy} onClick={() => onSubmit(ok)}>{t(paused ? "plans.confirm.allPaused" : "plans.confirm.all")}</Btn>
        </div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------------------- 单个计划：历史 / 迷你趋势 / 趋势分析

function PlanDetail({ name, running, lastRunId, trendKey, onRenamed }: {
  name: string; running: boolean; lastRunId: string; trendKey: string; onRenamed: (name: string) => void;
}) {
  const t = useT();
  const lang = useLang();
  const [fmt, setFmt] = useState<"md" | "html">("md");
  const [newName, setNewName] = useState(name);
  const [renameErr, setRenameErr] = useState("");
  const rename = async () => {
    const r = await call<{ name: string }>("PATCH", `/api/inspection/plans/${encodeURIComponent(name)}`, { name: newName.trim() });
    if (r.ok) { setRenameErr(""); onRenamed(r.data.name); } else setRenameErr(r.data.message || t("plans.saveFailed"));
  };
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [trend, setTrend] = useState<Trend | null>(null);
  const [trendBusy, setTrendBusy] = useState(false);

  useEffect(() => {
    call<{ runs: Run[] }>("GET", `/api/inspection/plans/${encodeURIComponent(name)}/runs?last=20`).then((r) => setRuns(r.ok ? r.data.runs : []));
  }, [name, running, lastRunId]);
  useEffect(() => {
    if (!trendKey) return;
    setTrendBusy(true);
    setTrend(null);
    call<Trend>("POST", `/api/inspection/plans/${encodeURIComponent(name)}/trend`, { last: 12, lang }).then((r) => {
      setTrendBusy(false);
      setTrend(r.ok ? r.data : { runs: 0, table: "", enough_runs: false, llm: false, error: r.data.message || "" });
    });
  }, [trendKey]);

  if (!runs) return <div className="h-16 animate-pulse bg-slate-100" />;  // 加载很快，不值得为它单独渲染工具条
  const latest = runs[0];
  // 指标迷你趋势：设备/检查项/指标 → 按时间从旧到新的数值序列
  const series = new Map<string, number[][]>();
  [...runs].reverse().forEach((r, i) => r.devices.forEach((d) => d.checks.forEach((c) => Object.entries(c.metrics ?? {}).forEach(([m, v]) => {
    if (typeof v !== "number") return;
    const k = `${d.name}\u0000${c.id}\u0000${m}`;
    series.set(k, [...(series.get(k) ?? []), [i, v]]);
  }))));
  // 变化过的指标排前面（那才是要看的），最多列 16 条；只有 1 次运行时画不出趋势，只给一句提示
  const varies = (pts: number[][]) => pts.some((p) => p[1] !== pts[0][1]);
  const allMetrics = [...series.entries()].sort((a, b) => Number(varies(b[1])) - Number(varies(a[1])));
  const metricRows = allMetrics.slice(0, 16);

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2 border-b border-line pb-3" data-testid="plan-toolbar">
      <span className="text-xs text-slate-600">{t("plans.draft.name")}</span>
      <input value={newName} onChange={(e) => setNewName(e.target.value)} data-testid="rename-input"
        className="w-[200px] rounded-[3px] border border-line bg-white px-2 py-[3px] font-mono text-[12px] focus:border-brand focus:outline-none" />
      <Btn small disabled={!newName.trim() || newName.trim() === name || running} onClick={rename}>{t("plans.rename")}</Btn>
      {renameErr && <span className="text-xs text-bad">{renameErr}</span>}
      <span className="ml-auto flex items-center gap-1.5">
        <select value={fmt} onChange={(e) => setFmt(e.target.value as "md" | "html")} data-testid="report-format"
          className="rounded-[3px] border border-line bg-white px-1.5 py-[3px] text-[12px] focus:border-brand focus:outline-none">
          <option value="md">Markdown</option>
          <option value="html">HTML</option>
        </select>
        <a href={`/api/inspection/plans/${encodeURIComponent(name)}/report?format=${fmt}&runs=20`} download data-testid="report-export">
          <Btn small>{t("plans.export")}</Btn>
        </a>
      </span>
    </div>
  );
  return (
    <div className="space-y-4 px-5 py-4" data-testid="plan-detail">
      {toolbar}
      {(trendBusy || trend) && (
        <div className="rounded-[4px] border border-line bg-white" data-testid="plan-trend">
          <div className="flex items-center gap-2 border-b border-line bg-[#f6f7f9] px-3 py-1.5 text-xs font-semibold text-slate-700">
            <Icon d={PATH.spark} className="h-3.5 w-3.5 text-slate-500" />{t("plans.trend.title")}
            {trendBusy && <span className="flex items-center gap-1.5 font-normal text-dim"><span className="pulse-dot h-1.5 w-1.5 rounded-full bg-brand" />{t("plans.trend.busy")}</span>}
          </div>
          {trend && (
            <div className="space-y-2 px-3 py-2.5">
              {trend.runs > 0 && !trend.enough_runs && <div className="text-xs text-warn">{fill(t("plans.trend.few"), { n: trend.runs })}</div>}
              {trend.llm && trend.analysis ? <Md text={trend.analysis} /> : (
                <>
                  <div className="text-xs text-slate-600">{fill(t("plans.trend.noLlm"), { err: trend.error || "-" })}</div>
                  {trend.table && <pre className="term overflow-x-auto px-3 py-2 text-[11.5px] leading-relaxed">{trend.table}</pre>}
                </>
              )}
            </div>
          )}
        </div>
      )}

      {runs.length === 0 ? <div className="text-xs text-dim">{t("plans.runs.empty")}</div> : (
        <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-4">
          <div>
            <div className="mb-1.5 text-xs font-semibold text-slate-700">{fill(t("plans.runs.title"), { name })}</div>
            <table className="w-full border border-line bg-white text-xs" data-testid="plan-runs">
              <thead className="bg-[#f6f7f9] text-left text-dim">
                <tr><th className="px-2 py-1.5 font-medium">{t("plans.runs.col.time")}</th><th className="px-2 py-1.5 font-medium">run</th><th className="px-2 py-1.5 font-medium">{t("plans.runs.col.result")}</th></tr>
              </thead>
              <tbody className="divide-y divide-line">
                {runs.slice(0, 10).map((r) => (
                  <tr key={r.run_id}>
                    <td className="num px-2 py-1.5 whitespace-nowrap text-slate-700">{when(r.started_at)}</td>
                    <td className="num px-2 py-1.5 text-dim">{r.run_id}</td>
                    <td className="px-2 py-1.5"><RunCounts r={r} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {metricRows.length > 0 && runs.length < 2 && (
              <div className="mt-4 text-xs text-dim">{t("plans.runs.metricsNeedTwo")}</div>
            )}
            {metricRows.length > 0 && runs.length >= 2 && (
              <>
                <div className="mt-4 mb-1.5 flex items-baseline gap-2">
                  <span className="text-xs font-semibold text-slate-700">{t("plans.runs.metrics")}</span>
                  <span className="text-[11px] text-dim">{fill(t("plans.runs.metricsNote"), { n: runs.length })}</span>
                </div>
                <div className="divide-y divide-line border border-line bg-white" data-testid="plan-metrics">
                  {metricRows.map(([k, pts]) => {
                    const [dev, cid, m] = k.split("\u0000");
                    const last = pts[pts.length - 1][1];
                    const delta = pts.length > 1 ? last - pts[pts.length - 2][1] : null;
                    return (
                      <div key={k} className="flex items-center gap-3 px-2 py-1">
                        <span className="w-[46%] min-w-0 truncate font-mono text-[11.5px] text-slate-700" title={`${dev} / ${cid} / ${m}`}>{dev} · {m}</span>
                        <span className="text-brand">{pts.length > 1 ? <Spark series={pts} className="h-5 w-[110px]" /> : <span className="inline-block w-[110px]" />}</span>
                        <span className="num ml-auto text-xs font-semibold text-slate-800">{last}</span>
                        <span className={cn("num w-12 text-right text-[11px]", delta ? (delta > 0 ? "text-warn" : "text-dim") : "text-dim")}>{delta === null ? "" : (delta > 0 ? "+" : "") + delta}</span>
                      </div>
                    );
                  })}
                  {allMetrics.length > metricRows.length && (
                    <div className="px-2 py-1 text-[11px] text-dim">{fill(t("plans.runs.metricsMore"), { n: allMetrics.length - metricRows.length })}</div>
                  )}
                </div>
              </>
            )}
          </div>

          <div>
            <div className="mb-1.5 flex items-baseline gap-2">
              <span className="text-xs font-semibold text-slate-700">{t("plans.runs.latest")}</span>
              <span className="num text-[11px] text-dim">{latest.run_id} · {when(latest.started_at)}</span>
            </div>
            <table className="w-full table-fixed border border-line bg-white text-xs" data-testid="plan-latest">
              <tbody className="divide-y divide-line">
                {latest.devices.map((d) => d.error ? (
                  <tr key={d.name}><td className="w-16 px-2 py-1.5 font-mono font-semibold">{d.name}</td><td className="px-2 py-1.5" colSpan={2}>
                    <Badge className={STATUS_TONE.fail}>{t("plans.runs.unreachable")}</Badge> <span className="break-words text-dim">{d.error}</span></td></tr>
                ) : d.checks.map((c, j) => (
                  <tr key={d.name + c.id} className="align-top">
                    <td className="w-16 px-2 py-1.5 font-mono font-semibold text-slate-800">{j === 0 ? d.name : ""}</td>
                    <td className="w-[36%] px-2 py-1.5 font-mono text-[11.5px] break-words text-slate-700">{c.id}</td>
                    <td className="px-2 py-1.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Badge className={STATUS_TONE[c.status] ?? STATUS_TONE.error}>{STATUS_KEY[c.status] ? t(STATUS_KEY[c.status]) : c.status}</Badge>
                        {Object.entries(c.metrics ?? {}).map(([m, v]) => <span key={m} className="num text-[11px] text-slate-600">{m}={v ?? "-"}</span>)}
                      </div>
                      {c.detail && <div className="mt-0.5 text-[11px] break-words text-warn">{c.detail}</div>}
                      {c.status === "fail" && (c.evaluations ?? []).filter((e) => !e.passed).map((e, k) => (
                        <div key={k} className="mt-0.5 font-mono text-[11px] break-words text-bad">✗ {e.type} {e.value}</div>
                      ))}
                      {c.output && <pre className="term mt-1 max-h-32 overflow-auto px-2 py-1 text-[11px] leading-snug whitespace-pre">{c.output}</pre>}
                    </td>
                  </tr>
                )))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
