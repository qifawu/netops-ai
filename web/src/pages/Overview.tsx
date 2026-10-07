import { useEffect, useState } from "react";
import { cn, load, ts } from "../lib/api";
import { Badge, Card, Empty, Icon, Loading, PATH } from "../lib/ui";
import { MERGE_HINT, REPEAT_HINT, confMeta, faultType, fmtDur, titleOf } from "../lib/incident";
import { goTab } from "../lib/nav";
import { fill, useT, type DictKey } from "../lib/i18n";

type Card_ = { key: string; label: string; value: string | number; sub: string; ratio?: number };
type Inc = {
  incident_id: string; host: string; clock: number; root_cause: string; headline?: string; confidence: string;
  alert_count: number; repeat_count?: number; alerts?: { name: string }[]; elapsed_total?: number;
};
type Data = { cards: Card_[]; recent: Inc[] };

const STEPS: { icon: string; titleKey: DictKey; descKey: DictKey }[] = [
  { icon: PATH.bell, titleKey: "overview.step1.title", descKey: "overview.step1.desc" },
  { icon: PATH.search, titleKey: "overview.step2.title", descKey: "overview.step2.desc" },
  { icon: PATH.target, titleKey: "overview.step3.title", descKey: "overview.step3.desc" },
  { icon: PATH.send, titleKey: "overview.step4.title", descKey: "overview.step4.desc" },
];

export default function Overview() {
  const t = useT();
  const [d, setD] = useState<Data | null>(null);
  useEffect(() => { load<Data>("overview", "/api/overview").then(setD).catch(() => setD({ cards: [], recent: [] })); }, []);
  if (!d) return <Loading />;
  const card = (k: string) => d.cards.find((c) => c.key === k);
  const inc = card("incidents"), speed = card("speed"), human = card("undetermined"), denied = card("denied");
  const sp = /^([\d.]+)\s*(.*)$/.exec(String(speed?.value ?? ""));

  return (
    <div className="mx-auto max-w-[1180px] space-y-5">
      {/* 首屏只回答一件事：这个系统在干嘛。深色底让它跟下面的白色数据区拉开层次。 */}
      <section className="relative overflow-hidden rounded-3xl bg-gradient-to-br from-[#1e1b4b] via-[#312e81] to-[#5b21b6] p-7 text-white shadow-[0_12px_40px_-16px_rgba(79,70,229,0.6)]">
        <div className="pointer-events-none absolute -top-24 -right-16 h-72 w-72 rounded-full bg-violet-400/20 blur-3xl" />
        <div className="pointer-events-none absolute -bottom-28 left-1/3 h-64 w-64 rounded-full bg-sky-400/10 blur-3xl" />
        <h1 className="relative max-w-3xl text-[28px] leading-snug font-semibold tracking-tight">
          {t("overview.hero.title")}
        </h1>
        <p className="relative mt-2 max-w-3xl text-sm leading-relaxed text-indigo-200">
          {t("overview.hero.subtitle")}
        </p>
        <ol className="relative mt-6 grid grid-cols-4 gap-2">
          {STEPS.map((s, i) => (
            <li key={s.titleKey} className="relative rounded-2xl bg-white/[0.07] p-4 ring-1 ring-white/10 backdrop-blur-sm">
              <div className="mb-2 flex items-center gap-2.5">
                <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-white/15">
                  <Icon d={s.icon} className="h-4 w-4 text-white" />
                </span>
                <span className="text-[15px] font-semibold">{t(s.titleKey)}</span>
              </div>
              <p className="text-xs leading-relaxed text-indigo-200">{t(s.descKey)}</p>
              {i < STEPS.length - 1 && (
                <span className="absolute top-1/2 -right-2.5 z-10 flex h-5 w-5 -translate-y-1/2 items-center justify-center rounded-full bg-indigo-500 ring-2 ring-[#312e81]">
                  <Icon d={PATH.chevron} className="h-3 w-3 text-white" />
                </span>
              )}
            </li>
          ))}
        </ol>
      </section>

      {/* 挑最能说明价值的几个数：做了多少、多快、什么时候认怂、花了多少、有没有越界。最后一张是卖点，做大做绿。 */}
      <section className="grid grid-cols-[1fr_1fr_1fr_1.25fr] gap-4">
        <Stat label={t("overview.kpi.incidents")} value={inc?.value ?? "—"} unit={t("unit.times")} sub={inc?.sub ? `${t("overview.kpi.incidents.fromPrefix")}${fill(t("overview.kpi.incidents.alerts"), { n: String(inc.sub).match(/\d+/)?.[0] ?? "" })}` : ""} tint="bg-indigo-500" />
        <Stat label={t("overview.kpi.speed")} value={sp?.[1] ?? "—"} unit={sp?.[2] && sp[2] !== "秒" ? sp[2] : t("unit.seconds")} sub={t("overview.kpi.speed.sub")} tint="bg-sky-500" />
        <Stat label={t("overview.kpi.human")} value={human?.ratio != null ? Math.round(human.ratio * 100) : "—"} unit="%"
          sub={human ? `${human.value} ${t("overview.kpi.human.suffix")}` : ""} tint="bg-amber-500" />
        <div className="relative overflow-hidden rounded-2xl border border-emerald-200 bg-gradient-to-br from-emerald-50 to-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]">
          <span className="absolute inset-x-0 top-0 h-1 bg-emerald-500" />
          <div className="flex items-center gap-1.5 text-[13px] font-medium text-emerald-700">
            <Icon d={PATH.shield} className="h-4 w-4" />{t("overview.kpi.denied")}
          </div>
          <div className="mt-1.5 flex items-baseline gap-1.5">
            <span className="text-[40px] leading-none font-semibold tracking-tight text-emerald-700 tabular-nums">{denied?.value ?? "—"}</span>
            <span className="text-sm text-emerald-700/80">{t("overview.kpi.denied.unit")}</span>
          </div>
          <div className="mt-2 text-[13px] font-semibold text-emerald-800">{t("overview.kpi.denied.line1")}</div>
          <div className="mt-0.5 text-xs text-emerald-700/70">{t("overview.kpi.denied.line2")}</div>
        </div>
      </section>

      <section>
        <div className="mb-3 flex items-end">
          <div>
            <h2 className="text-[15px] font-semibold text-slate-800">{t("overview.recent.title")}</h2>
            <p className="mt-0.5 text-xs text-dim">{t("overview.recent.hint")}</p>
          </div>
          <button onClick={() => goTab("incidents")} className="ml-auto rounded-lg px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand/5">{t("overview.recent.viewAll")}</button>
        </div>
        <Card className="overflow-hidden">
          <div className="grid grid-cols-[96px_104px_96px_minmax(0,1fr)_64px_84px] items-center gap-3 border-b border-line bg-slate-50/70 px-5 py-2 text-xs text-dim">
            <span>{t("overview.recent.col.time")}</span><span>{t("overview.recent.col.device")}</span><span>{t("overview.recent.col.type")}</span><span>{t("overview.recent.col.summary")}</span><span>{t("overview.recent.col.confidence")}</span><span className="text-right">{t("overview.recent.col.elapsed")}</span>
          </div>
          {d.recent.length === 0 && <Empty title={t("overview.recent.empty.title")} hint={t("overview.recent.empty.hint")} />}
          {d.recent.map((i) => {
            const ft = faultType(i), c = confMeta(i.confidence);
            return (
              <button key={i.incident_id} onClick={() => goTab("incidents", i.incident_id)} title={i.root_cause}
                className="grid w-full grid-cols-[96px_104px_96px_minmax(0,1fr)_64px_84px] items-center gap-3 border-b border-line px-5 py-2.5 text-left text-sm transition last:border-b-0 hover:bg-indigo-50/40">
                <span className="text-xs text-dim tabular-nums">{ts(i.clock)}</span>
                <span className="truncate font-medium text-slate-700">{i.host}</span>
                <span><Badge className={ft.cls}>{t(ft.labelKey)}</Badge></span>
                <span className="min-w-0">
                  <span className="block truncate">{titleOf(i, 44)}</span>
                  <span className="mt-1 flex min-h-[20px] items-center gap-1.5">
                  {i.alert_count > 1 && <Badge className="bg-sky-50 text-sky-700 ring-sky-200" title={MERGE_HINT(t, i.alert_count)}>{fill(t("incidents.mergedBadge"), { n: i.alert_count })}</Badge>}
                  {(i.repeat_count ?? 0) > 1 && <Badge className="bg-amber-50 text-amber-700 ring-amber-200" title={REPEAT_HINT(t, i.repeat_count!)}>{fill(t("incidents.repeatBadge"), { n: i.repeat_count! })}</Badge>}
                  </span>
                </span>
                <span><Badge className={c.badge} title={t(c.hintKey)}><span className={cn("h-1.5 w-1.5 rounded-full", c.dot)} />{t(c.labelKey)}</Badge></span>
                <span className="text-right text-xs text-slate-500 tabular-nums">{fmtDur(t, i.elapsed_total)}</span>
              </button>
            );
          })}
        </Card>
      </section>
    </div>
  );
}

function Stat({ label, value, unit, sub, tint }: { label: string; value: string | number; unit: string; sub: string; tint: string }) {
  return (
    <div className="card-shadow relative overflow-hidden rounded-2xl border border-line bg-card p-5">
      <span className={cn("absolute inset-x-0 top-0 h-1", tint)} />
      <div className="text-[13px] font-medium text-slate-500">{label}</div>
      <div className="mt-1.5 flex items-baseline gap-1.5">
        <span className="text-[40px] leading-none font-semibold tracking-tight text-slate-900 tabular-nums">{value}</span>
        <span className="text-sm text-slate-500">{unit}</span>
      </div>
      <div className="mt-2 text-xs leading-relaxed text-dim">{sub}</div>
    </div>
  );
}
