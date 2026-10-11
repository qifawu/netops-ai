import { Fragment, useEffect, useRef, useState } from "react";
import { cn } from "../lib/api";
import { Badge, Icon, PATH } from "../lib/ui";
import { fill, useT, type DictKey } from "../lib/i18n";
import { Btn, call } from "../lib/sopui";
import HelpTip from "../lib/HelpTip";

/** 试运行：保存前对草案里的设备真的跑一遍只读命令（同一个白名单和设备适配器），不存记录、不告警。
 *  后台任务：POST 拿 job_id，每 0.8 秒轮询进度和结果，可以取消。结果带时间戳留在草案区；
 *  草案里设备 / 检查项变了，结果就标成「已过期」（用页面这边的草案指纹比，不再请求后端）。 */

type Draft = { devices: { name: string; host: string }[]; checks: { id: string; command: string; devices?: string[]; expect?: unknown; extract?: unknown }[] };
type Evaluation = { type: string; value?: string; passed: boolean | null; state?: string; current?: number | null; reference?: number | null; reference_run_id?: string | null };
type DryCheck = {
  id: string; command: string; status: "ok" | "denied" | "timeout" | "error" | "cancelled"; detail: string; elapsed_ms: number;
  lines: number; bytes: number; metrics: Record<string, number | null>; evaluations: Evaluation[]; would_pass: boolean | null;
  output_head: string; truncated_lines: number;
};
type DryResult = { state: string; started_at: string; finished_at: string; devices: { name: string; error: string; checks: DryCheck[] }[]; summary: Record<string, number> };
export type DryRun = { jobId: string; state: "running" | "done" | "cancelled" | "error"; done: number; total: number; result: DryResult | null; key: string; at: string; error?: string };

/** 影响试运行结果的那部分草案（设备 + 检查项），跟后端 draft_key 取的是同一些字段。 */
export const dryKey = (d: Draft | null) => (d ? JSON.stringify({
  devices: d.devices.map((x) => [x.name, x.host]).sort(),
  checks: d.checks.map((c) => [c.id, c.command, c.devices ?? null, c.expect ?? null, c.extract ?? null]),
}) : "");

export function useDryRun(draft: Draft | null) {
  const [dry, setDry] = useState<DryRun | null>(null);
  const timer = useRef<number | null>(null);
  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);
  const poll = (jobId: string, key: string) => {
    timer.current = window.setTimeout(async () => {
      const r = await call<{ state: DryRun["state"]; done: number; total: number; result: DryResult | null; error?: string }>("GET", `/api/inspection/plans/dry-run/${jobId}`);
      if (!r.ok) { setDry((x) => (x ? { ...x, state: "error", error: r.data.message || "" } : x)); return; }
      setDry((x) => (x && x.jobId === jobId ? { ...x, state: r.data.state, done: r.data.done, total: r.data.total || x.total, result: r.data.result, error: r.data.error } : x));
      if (r.data.state === "running") poll(jobId, key);
    }, 800);
  };
  const start = async () => {
    if (!draft || (dry && dry.state === "running")) return;
    const key = dryKey(draft);
    const r = await call<{ job_id: string; total: number }>("POST", "/api/inspection/plans/dry-run", { draft });
    if (!r.ok) { setDry({ jobId: "", state: "error", done: 0, total: 0, result: null, key, at: new Date().toISOString(), error: r.data.message || "" }); return; }
    setDry({ jobId: r.data.job_id, state: "running", done: 0, total: r.data.total, result: null, key, at: new Date().toISOString() });
    poll(r.data.job_id, key);
  };
  const cancel = async () => { if (dry?.jobId) await call("DELETE", `/api/inspection/plans/dry-run/${dry.jobId}`); };
  const stale = !!dry && !!draft && dry.state !== "running" && dry.key !== dryKey(draft);
  return { dry, start, cancel, stale };
}

const STATUS_TONE: Record<string, string> = {
  ok: "bg-white text-emerald-700 ring-slate-300", denied: "bg-rose-50 text-rose-700 ring-rose-200",
  timeout: "bg-amber-50 text-amber-700 ring-amber-200", error: "bg-amber-50 text-amber-700 ring-amber-200", cancelled: "bg-slate-100 text-slate-500 ring-slate-200",
};
const STATUS_KEY: Record<string, DictKey> = { ok: "dry.status.ok", denied: "dry.status.denied", timeout: "dry.status.timeout", error: "dry.status.error", cancelled: "dry.status.cancelled" };

function outcome(t: (k: DictKey) => string, e: Evaluation): { text: string; tone: string } {
  if (e.state === "baseline") return { text: t("dry.rule.baseline"), tone: "text-slate-600" };
  if (e.state === "no_baseline") return { text: t("dry.rule.noBaseline"), tone: "text-slate-600" };
  if (e.state === "no_value") return { text: t("dry.rule.noValue"), tone: "text-warn" };
  if (e.state === "reset") return { text: t("dry.rule.reset"), tone: "text-slate-600" };
  const vs = e.reference != null && e.state ? fill(t("dry.rule.vs"), { ref: e.reference, cur: e.current ?? "-" }) + " " : "";
  return e.passed ? { text: vs + t("dry.rule.pass"), tone: "text-ok" } : { text: vs + t("dry.rule.fail"), tone: "text-bad" };
}

const when = (iso: string) => { const d = new Date(iso); const p = (n: number) => String(n).padStart(2, "0"); return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };

export default function DryRunPanel({ dry, stale, onStart, onCancel, disabled }: {
  dry: DryRun | null; stale: boolean; onStart: () => void; onCancel: () => void; disabled?: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState("");
  const running = dry?.state === "running";
  const res = dry?.result;
  return (
    <div className="rounded-[3px] border border-line" data-testid="dry-run">
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-[#f6f7f9] px-3 py-1.5 text-xs">
        <Icon d={PATH.terminal} className="h-3.5 w-3.5 text-slate-500" />
        <span className="font-semibold text-slate-700">{t("dry.title")}</span>
        <HelpTip text={t("dry.note")} />
        <span className="ml-auto flex items-center gap-2">
          {running && (
            <>
              <span className="num flex items-center gap-1.5 text-brand"><span className="pulse-dot h-1.5 w-1.5 rounded-full bg-brand" />{fill(t("dry.progress"), { done: dry!.done, total: dry!.total })}</span>
              <Btn small onClick={onCancel}>{t("dry.cancel")}</Btn>
            </>
          )}
          {!running && <Btn small tone="brand" disabled={disabled} onClick={onStart}>{dry ? t("dry.again") : t("dry.run")}</Btn>}
        </span>
      </div>
      {running && dry!.total > 0 && (
        <div className="h-1 bg-slate-100"><div className="h-1 bg-brand transition-all" style={{ width: `${Math.round((100 * dry!.done) / dry!.total)}%` }} /></div>
      )}
      {dry?.state === "error" && <div className="px-3 py-2 text-xs text-bad">{dry.error || t("plans.chat.failed")}</div>}
      {res && (
        <div className="space-y-1.5 px-3 py-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.72rem]">
            <span className="num text-dim">{fill(t("dry.at"), { at: when(dry!.at) })}</span>
            {stale && <Badge className="bg-amber-50 text-amber-700 ring-amber-200" title={t("dry.staleHint")}>{t("dry.stale")}</Badge>}
            {res.state === "cancelled" && <Badge className="bg-slate-100 text-slate-500 ring-slate-200">{t("dry.status.cancelled")}</Badge>}
            {res.state === "timeout" && <Badge className="bg-amber-50 text-amber-700 ring-amber-200">{t("dry.overallTimeout")}</Badge>}
            {(["ok", "denied", "timeout", "error"] as const).map((k) => (res.summary[k] ?? 0) > 0 && (
              <span key={k} className="num text-slate-600">{t(STATUS_KEY[k])} {res.summary[k]}</span>
            ))}
            {(res.summary.would_fail ?? 0) > 0 && <span className="num font-semibold text-bad">{fill(t("dry.wouldFail"), { n: res.summary.would_fail })}</span>}
          </div>
          <div className="overflow-x-auto">
            <table className={cn("w-full border border-line text-xs", stale && "opacity-60")} data-testid="dry-table">
              <thead className="bg-[#f6f7f9] text-left text-dim">
                <tr>
                  <th className="px-2 py-1 font-medium">{t("dry.col.target")}</th>
                  <th className="px-2 py-1 font-medium">{t("dry.col.status")}</th>
                  <th className="px-2 py-1 text-right font-medium">{t("dry.col.time")}</th>
                  <th className="px-2 py-1 text-right font-medium">{t("dry.col.size")}</th>
                  <th className="px-2 py-1 font-medium">{t("dry.col.metrics")}</th>
                  <th className="px-2 py-1 font-medium">{t("dry.col.outcome")}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {res.devices.flatMap((d) => d.checks.map((c) => {
                  const k = `${d.name}/${c.id}`;
                  return (
                    <Fragment key={k}>
                      <tr className={cn("cursor-pointer align-top hover:bg-slate-50", open === k && "bg-slate-50")} onClick={() => setOpen(open === k ? "" : k)}>
                        <td className="px-2 py-1">
                          <span className="font-mono font-semibold text-slate-800">{d.name}</span>
                          <span className="ml-1.5 font-mono text-[0.7rem] text-slate-600">{c.id}</span>
                        </td>
                        <td className="px-2 py-1"><Badge className={STATUS_TONE[c.status]} title={c.detail}>{t(STATUS_KEY[c.status])}</Badge></td>
                        <td className="num px-2 py-1 text-right text-slate-600">{c.status === "ok" || c.elapsed_ms ? `${c.elapsed_ms} ms` : "—"}</td>
                        <td className="num px-2 py-1 text-right text-slate-600">{c.status === "ok" ? fill(t("dry.size"), { lines: c.lines, bytes: c.bytes }) : "—"}</td>
                        <td className="num px-2 py-1 text-[0.7rem] text-slate-700">{Object.entries(c.metrics).map(([m, v]) => `${m}=${v ?? "-"}`).join(" ") || "—"}</td>
                        <td className="px-2 py-1">
                          {c.evaluations.length === 0 ? <span className="text-dim">{c.status === "ok" ? t("dry.rule.none") : t("dry.rule.notRun")}</span>
                            : c.evaluations.map((e, i) => { const o = outcome(t, e); return <div key={i} className={cn("text-[0.7rem] leading-snug", o.tone)} title={e.value}>{o.text}</div>; })}
                        </td>
                      </tr>
                      {open === k && (
                        <tr><td colSpan={6} className="bg-[#fafbfc] px-2 py-1.5">
                          <div className="mb-1 font-mono text-[0.7rem] text-slate-600">{c.command}</div>
                          {c.output_head
                            ? <pre className="term max-h-48 overflow-auto px-2 py-1 text-[0.7rem] leading-snug whitespace-pre">{c.output_head}{c.truncated_lines > 0 ? `\n${fill(t("dry.more"), { n: c.truncated_lines })}` : ""}</pre>
                            : <div className="text-[0.7rem] text-dim">{c.detail || t("dry.noOutput")}</div>}
                        </td></tr>
                      )}
                    </Fragment>
                  );
                }))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
