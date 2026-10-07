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
    <div className="mx-auto max-w-[1280px] space-y-4">
      {/* 首屏只回答一件事：这个系统在干嘛。压成一条说明 + 扁平步骤条，不占半屏。 */}
      <section className="rounded-[4px] border border-line bg-card">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 border-b border-line px-4 py-2.5">
          <h1 className="text-[14px] font-semibold text-slate-900">{t("overview.hero.title")}</h1>
          <p className="text-xs text-dim">{t("overview.hero.subtitle")}</p>
        </div>
        <ol className="grid grid-cols-4">
          {STEPS.map((s, i) => (
            <li key={s.titleKey} className={cn("relative flex gap-3 px-4 py-3", i > 0 && "border-l border-line")}>
              <span className="num mt-px text-[12px] font-semibold text-brand">{String(i + 1).padStart(2, "0")}</span>
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 text-[13px] font-semibold text-slate-800">
                  <Icon d={s.icon} className="h-3.5 w-3.5 text-slate-500" />{t(s.titleKey)}
                </div>
                <p className="mt-0.5 text-xs leading-relaxed text-dim">{t(s.descKey)}</p>
              </div>
              {i < STEPS.length - 1 && (
                <span className="absolute top-1/2 -right-[7px] z-10 flex h-3.5 w-3.5 -translate-y-1/2 items-center justify-center border border-line bg-card">
                  <Icon d={PATH.chevron} className="h-2.5 w-2.5 text-slate-400" />
                </span>
              )}
            </li>
          ))}
        </ol>
      </section>

      {/* 挑最能说明价值的几个数：做了多少、多快、什么时候认怂、花了多少、有没有越界。扁平数字块，一行排开。 */}
      <section className="grid grid-cols-[1fr_1fr_1fr_1.25fr] divide-x divide-line rounded-[4px] border border-line bg-card">
        <Stat label={t("overview.kpi.incidents")} value={inc?.value ?? "—"} unit={t("unit.times")} sub={inc?.sub ? `${t("overview.kpi.incidents.fromPrefix")}${fill(t("overview.kpi.incidents.alerts"), { n: String(inc.sub).match(/\d+/)?.[0] ?? "" })}` : ""} />
        <Stat label={t("overview.kpi.speed")} value={sp?.[1] ?? "—"} unit={sp?.[2] && sp[2] !== "秒" ? sp[2] : t("unit.seconds")} sub={t("overview.kpi.speed.sub")} />
        <Stat label={t("overview.kpi.human")} value={human?.ratio != null ? Math.round(human.ratio * 100) : "—"} unit="%"
          sub={human ? `${human.value} ${t("overview.kpi.human.suffix")}` : ""} />
        <div className="px-4 py-3">
          <div className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
            <Icon d={PATH.shield} className="h-3.5 w-3.5 text-ok" />{t("overview.kpi.denied")}
          </div>
          <div className="mt-1.5 flex items-baseline gap-1.5">
            <span className="num text-[28px] leading-none font-semibold text-ok">{denied?.value ?? "—"}</span>
            <span className="text-xs text-dim">{t("overview.kpi.denied.unit")}</span>
          </div>
          <div className="mt-1.5 text-xs font-semibold text-slate-700">{t("overview.kpi.denied.line1")}</div>
          <div className="mt-0.5 text-[11.5px] leading-snug text-dim">{t("overview.kpi.denied.line2")}</div>
        </div>
      </section>

      <section>
        <div className="mb-3 flex items-end">
          <div>
            <h2 className="text-[13px] font-semibold tracking-wide text-slate-800">{t("overview.recent.title")}</h2>
            <p className="mt-0.5 text-xs text-dim">{t("overview.recent.hint")}</p>
          </div>
          <button onClick={() => goTab("incidents")} className="ml-auto px-1 py-1 text-xs font-medium text-brand hover:underline">{t("overview.recent.viewAll")}</button>
        </div>
        <Card className="overflow-hidden">
          <div className="grid grid-cols-[92px_96px_128px_minmax(0,1fr)_88px_76px] items-center gap-3 border-b border-line bg-[#f6f7f9] px-4 py-1.5 text-[11px] font-semibold tracking-wide text-slate-500 uppercase">
            <span>{t("overview.recent.col.time")}</span><span>{t("overview.recent.col.device")}</span><span>{t("overview.recent.col.type")}</span><span>{t("overview.recent.col.summary")}</span><span>{t("overview.recent.col.confidence")}</span><span className="text-right">{t("overview.recent.col.elapsed")}</span>
          </div>
          {d.recent.length === 0 && <Empty title={t("overview.recent.empty.title")} hint={t("overview.recent.empty.hint")} />}
          {d.recent.map((i) => {
            const ft = faultType(i), c = confMeta(i.confidence);
            return (
              <button key={i.incident_id} onClick={() => goTab("incidents", i.incident_id)} title={i.root_cause}
                className="grid w-full grid-cols-[92px_96px_128px_minmax(0,1fr)_88px_76px] items-center gap-3 border-b border-line px-4 py-2 text-left text-[13px] transition last:border-b-0 even:bg-[#fafbfc] hover:bg-brand/[0.05]">
                <span className="num text-xs text-dim">{ts(i.clock)}</span>
                <span className="num truncate text-xs font-medium text-slate-800">{i.host}</span>
                <span><Badge className={ft.cls}>{t(ft.labelKey)}</Badge></span>
                <span className="min-w-0">
                  <span className="block truncate">{titleOf(i, 44)}</span>
                  <span className={cn("flex items-center gap-1.5", (i.alert_count > 1 || (i.repeat_count ?? 0) > 1) && "mt-1")}>
                  {i.alert_count > 1 && <Badge className="bg-white text-slate-700 ring-slate-300" title={MERGE_HINT(t, i.alert_count)}>{fill(t("incidents.mergedBadge"), { n: i.alert_count })}</Badge>}
                  {(i.repeat_count ?? 0) > 1 && <Badge className="bg-amber-50 text-amber-700 ring-amber-200" title={REPEAT_HINT(t, i.repeat_count!)}>{fill(t("incidents.repeatBadge"), { n: i.repeat_count! })}</Badge>}
                  </span>
                </span>
                <span><Badge className={c.badge} title={t(c.hintKey)}><span className={cn("h-1.5 w-1.5", c.dot)} />{t(c.labelKey)}</Badge></span>
                <span className="num text-right text-xs text-slate-600">{fmtDur(t, i.elapsed_total)}</span>
              </button>
            );
          })}
        </Card>
      </section>
    </div>
  );
}

function Stat({ label, value, unit, sub }: { label: string; value: string | number; unit: string; sub: string }) {
  return (
    <div className="px-4 py-3">
      <div className="text-xs font-medium text-slate-600">{label}</div>
      <div className="mt-1.5 flex items-baseline gap-1.5">
        <span className="num text-[28px] leading-none font-semibold text-slate-900">{value}</span>
        <span className="text-xs text-dim">{unit}</span>
      </div>
      <div className="mt-1.5 text-[11.5px] leading-snug text-dim">{sub}</div>
    </div>
  );
}
