import { useEffect, useState } from "react";
import { cn } from "../lib/api";
import { Badge, Icon, PATH } from "../lib/ui";
import { fill, useLang, useT } from "../lib/i18n";
import { Btn, call } from "../lib/sopui";
import HelpTip from "../lib/HelpTip";

/** 基础巡检页顶部「这套基础巡检检查什么」：内容全部来自后端 `/api/inspection/basic/spec`
 *  （状态巡检的命令现读 status.COMMANDS，趋势检测器阈值现读 inspection.yaml），前端不写死。
 *  里面能直接改基础巡检的周期（写回 SCHEDULE_INTERVAL_MINUTES，下一轮生效），也能以它为起点新建自定义巡检。 */

type Spec = {
  status_checks: { key: string; title: string; rule: string; command: string; scope: string }[];
  detectors: { name: string; label: string; help: string; params: { key: string; label: string; unit: string; value: number | null }[] }[];
  lookback_days: number | null; config_file: string; config_error: string; note: string;
  schedule: { interval_minutes: number; options: number[]; last_run_at: string | null; next_run_at: string | null; overridden_by_process_env: boolean };
};

const when = (iso?: string | null) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

export default function BasicSpec({ onStartCustom }: { onStartCustom: () => void }) {
  const t = useT();
  const lang = useLang();
  const [spec, setSpec] = useState<Spec | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const reload = () => call<Spec>("GET", `/api/inspection/basic/spec?lang=${lang}`).then((r) => r.ok && setSpec(r.data));
  useEffect(() => { reload(); }, [lang]);

  const label = (m: number) => (m === 0 ? t("basic.sched.off") : m === 1440 ? t("basic.sched.daily")
    : m % 60 === 0 ? (m === 60 ? t("plans.sched.hourly") : fill(t("basic.sched.hours"), { n: m / 60 })) : fill(t("plans.sched.everyN"), { n: m }));
  const setInterval_ = async (m: number) => {
    setSaving(true);
    const r = await call<Spec["schedule"]>("PUT", "/api/inspection/basic/schedule", { minutes: m });
    setSaving(false);
    if (r.ok) { setSpec((s) => (s ? { ...s, schedule: r.data } : s)); setMsg({ ok: true, text: fill(t("basic.sched.saved"), { s: label(m) }) }); }
    else setMsg({ ok: false, text: r.data.message || t("plans.saveFailed") });
  };

  if (!spec) return <div className="h-24 animate-pulse rounded-[4px] bg-slate-200/70" />;
  const sch = spec.schedule;
  const fmtVal = (p: Spec["detectors"][number]["params"][number]) =>
    p.key.endsWith("_seconds") && typeof p.value === "number"
      ? (p.value % 86400 === 0 ? fill(t("basic.unit.days"), { n: p.value / 86400 }) : p.value % 3600 === 0 ? fill(t("basic.unit.hours"), { n: p.value / 3600 }) : `${p.value}s`)
      : `${p.value ?? "—"}${p.unit && !p.unit.startsWith("比例") ? " " + p.unit : ""}`;

  return (
    <details open className="rounded-[4px] border border-line bg-card" data-testid="basic-spec">
      <summary className="flex cursor-pointer list-none items-center gap-2 border-b border-line bg-[#f6f7f9] px-4 py-2.5 select-none">
        <Icon d={PATH.shield} className="text-slate-500" />
        <span className="text-[0.8125rem] font-semibold tracking-wide text-slate-800">{t("basic.title")}</span>
        <Badge className="bg-white text-slate-600 ring-slate-300">{t("basic.badge")}</Badge>
        <HelpTip text={spec.note} />
      </summary>

      <div className="space-y-4 px-4 py-3">
        {/* 周期 + 关系入口 */}
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-[3px] border border-line bg-[#fafbfc] px-3 py-2 text-[0.8125rem]" data-testid="basic-schedule">
          <span className="flex items-center gap-2">
            <span className="text-xs font-medium text-slate-600">{t("basic.sched.label")}</span>
            <HelpTip text={t("basic.sched.hint")} />
            <select value={sch.interval_minutes} disabled={saving} onChange={(e) => setInterval_(Number(e.target.value))} data-testid="basic-interval"
              className="rounded-[3px] border border-line bg-white px-2 py-1 font-mono text-[0.78rem] focus:border-brand focus:outline-none">
              {(sch.options.includes(sch.interval_minutes) ? sch.options : [...sch.options, sch.interval_minutes]).map((m) => <option key={m} value={m}>{label(m)}</option>)}
            </select>
          </span>
          <span className="text-xs text-slate-600">{t("basic.sched.last")} <span className="num text-slate-800">{when(sch.last_run_at)}</span></span>
          <span className="text-xs text-slate-600">{t("basic.sched.next")} <span className="num text-slate-800">{sch.interval_minutes ? when(sch.next_run_at) : t("basic.sched.off")}</span></span>
          <span className="ml-auto"><Btn small tone="brand" onClick={onStartCustom}>{t("basic.startCustom")}</Btn></span>
          {sch.overridden_by_process_env && <div className="basis-full text-xs text-warn">{t("basic.sched.envOverride")}</div>}
          {msg && <div className={cn("basis-full text-xs", msg.ok ? "text-ok" : "text-bad")}>{msg.text}</div>}
        </div>

        {/* ① 状态巡检 */}
        <div>
          <div className="mb-1.5 text-xs font-semibold text-slate-700">{t("basic.status.title")}</div>
          <div className="overflow-x-auto">
            <table className="w-full border border-line text-xs" data-testid="basic-status-table">
              <thead className="bg-[#f6f7f9] text-left text-dim">
                <tr>
                  <th className="w-[16%] px-2 py-1.5 font-medium">{t("basic.col.what")}</th>
                  <th className="px-2 py-1.5 font-medium">{t("basic.col.rule")}</th>
                  <th className="w-[20%] px-2 py-1.5 font-medium">{t("basic.col.command")}</th>
                  <th className="w-[18%] px-2 py-1.5 font-medium">{t("basic.col.scope")}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {spec.status_checks.map((c) => (
                  <tr key={c.key} className="align-top">
                    <td className="px-2 py-1.5 font-medium text-slate-800">{c.title}</td>
                    <td className="px-2 py-1.5 leading-relaxed text-slate-700">{c.rule}</td>
                    <td className="px-2 py-1.5 font-mono text-[0.72rem] break-words text-slate-700">{c.command}</td>
                    <td className="px-2 py-1.5 text-slate-600">{c.scope}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* ② 趋势巡检 */}
        <div>
          <div className="mb-1.5 flex flex-wrap items-baseline gap-2 text-xs">
            <span className="font-semibold text-slate-700">{t("basic.trend.title")}</span>
            {spec.lookback_days != null && <span className="text-dim">{fill(t("basic.trend.lookback"), { n: spec.lookback_days })}</span>}
            {spec.config_error && <span className="text-bad">{spec.config_error}</span>}
          </div>
          <div className="grid gap-2 md:grid-cols-3" data-testid="basic-detectors">
            {spec.detectors.map((d) => (
              <div key={d.name} className="rounded-[3px] border border-line px-3 py-2">
                <div className="flex items-center gap-1.5 text-[0.8125rem] font-medium text-slate-800">{d.label}{d.help && <HelpTip text={d.help} />}</div>
                <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[0.72rem] text-dim">
                  {d.params.map((p) => (
                    <span key={p.key}>{p.label} <span className="num text-slate-700">{fmtVal(p)}</span></span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>

      </div>
    </details>
  );
}
