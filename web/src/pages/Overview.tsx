import { useEffect, useState } from "react";
import { cn, load, ts } from "../lib/api";
import { Card, Empty, Icon, Loading, PATH } from "../lib/ui";
import { confLevel, confMeta, faultType, fmtDur, titleOf } from "../lib/incident";
import { goTab } from "../lib/nav";
import { fill, useT } from "../lib/i18n";
import HelpTip from "../lib/HelpTip";

type Card_ = { key: string; label: string; value: string | number; sub: string; ratio?: number };
type Inc = {
  incident_id: string; host: string; clock: number; root_cause: string; headline?: string; confidence: string;
  alert_count: number; repeat_count?: number; alerts?: { name: string }[]; elapsed_total?: number;
};
type Data = { cards: Card_[]; recent: Inc[] };

export default function Overview() {
  const t = useT();
  const [d, setD] = useState<Data | null>(null);
  useEffect(() => { load<Data>("overview", "/api/overview").then(setD).catch(() => setD({ cards: [], recent: [] })); }, []);
  if (!d) return <Loading />;
  const card = (k: string) => d.cards.find((c) => c.key === k);
  const inc = card("incidents"), speed = card("speed"), human = card("undetermined"), denied = card("denied");
  const sp = /^([\d.]+)\s*(.*)$/.exec(String(speed?.value ?? ""));

  const secs = sp && (!sp[2] || sp[2] === "秒") ? Number(sp[1]) : NaN;

  return (
    <div className="page-wrap space-y-6">
      {/* 挑最能说明价值的几个数：做了多少、多快、什么时候认怂、花了多少、有没有越界。每个数一张卡。 */}
      <section className="grid grid-cols-2 gap-4 xl:grid-cols-[repeat(auto-fit,minmax(200px,1fr))]">
        <Stat label={t("overview.kpi.incidents")} value={inc?.value ?? "—"} unit={t("unit.times")} sub={inc?.sub ? `${t("overview.kpi.incidents.fromPrefix")}${fill(t("overview.kpi.incidents.alerts"), { n: String(inc.sub).match(/\d+/)?.[0] ?? "" })}` : ""} />
        {/* 超过一分钟就写成「2 分 38 秒」，比「157.7 秒」好读 */}
        {secs >= 60
          ? <Stat label={t("overview.kpi.speed")} help={t("overview.kpi.speed.sub")} value={fmtDur(t, secs)} unit="" compact />
          : <Stat label={t("overview.kpi.speed")} help={t("overview.kpi.speed.sub")} value={sp?.[1] ?? "—"} unit={sp?.[2] && sp[2] !== "秒" ? sp[2] : t("unit.seconds")} />}
        <Stat label={t("overview.kpi.human")} value={human?.ratio != null ? Math.round(human.ratio * 100) : "—"} unit="%"
          sub={human ? fill(t("overview.kpi.human.count"), { n: human.value }) : ""} />
        <div className="rounded-[10px] border border-line bg-card px-5 py-4">
          <div className="flex items-center gap-1.5 text-[0.8125rem] text-slate-600">
            <Icon d={PATH.shield} className="h-3.5 w-3.5 text-ok" />{t("overview.kpi.denied")}
          </div>
          <div className="mt-2 flex items-baseline gap-1.5">
            <span className="num text-[1.875rem] leading-none font-semibold text-slate-900">{denied?.value ?? "—"}</span>
            <span className="text-sm text-dim">{t("overview.kpi.denied.unit")}</span>
          </div>
        </div>
      </section>

      <Card className="overflow-hidden">
        <div className="flex items-center border-b border-line px-5 py-3.5">
          <h2 className="text-[0.9375rem] font-semibold text-slate-900">{t("overview.recent.title")}</h2>
          <button onClick={() => goTab("incidents")} className="ml-auto text-[0.8125rem] font-medium text-brand hover:underline">{t("overview.recent.viewAll")}</button>
        </div>
        {d.recent.length === 0 && <Empty title={t("overview.recent.empty.title")} />}
        <div className="overflow-x-auto">
          {d.recent.map((i) => {
            const ft = faultType(i), c = confMeta(i.confidence), lvl = confLevel(i.confidence);
            const meta = [t(ft.labelKey)];
            if (i.alert_count > 1) meta.push(fill(t("incidents.mergedBadge"), { n: i.alert_count }));
            if ((i.repeat_count ?? 0) > 1) meta.push(fill(t("incidents.repeatBadge"), { n: i.repeat_count! }));
            return (
              <button key={i.incident_id} onClick={() => goTab("incidents", i.incident_id)} title={i.root_cause}
                className="grid w-full min-w-[640px] grid-cols-[96px_80px_minmax(0,1fr)_72px_80px] items-center gap-4 border-b border-line px-5 py-3 text-left text-sm transition last:border-b-0 hover:bg-slate-50">
                <span className="num text-[0.8125rem] text-dim">{ts(i.clock)}</span>
                <span className="truncate font-medium text-slate-900">{i.host}</span>
                <span className="min-w-0">
                  <span className="block truncate text-slate-900">{titleOf(i, 44)}</span>
                  <span className="mt-0.5 block truncate text-xs text-dim">{meta.join(" · ")}</span>
                </span>
                <span title={t(c.hintKey)} className={cn("flex items-center gap-1.5 text-[0.8125rem]", CONF_TEXT[lvl])}>
                  <span className={cn("h-2 w-2 shrink-0 rounded-full", c.dot)} />{t(c.labelKey)}
                </span>
                <span className="num text-right text-[0.8125rem] text-slate-600">{fmtDur(t, i.elapsed_total)}</span>
              </button>
            );
          })}
        </div>
      </Card>
    </div>
  );
}

const CONF_TEXT = ["text-slate-500", "text-rose-700", "text-amber-700", "text-emerald-700"];

function Stat({ label, value, unit, sub, help, compact }: { label: string; value: string | number; unit: string; sub?: string; help?: string; compact?: boolean }) {
  return (
    <div className="rounded-[10px] border border-line bg-card px-5 py-4">
      <div className="flex items-center gap-1.5 text-[0.8125rem] text-slate-600">{label}{help && <HelpTip text={help} />}</div>
      <div className="mt-2 flex items-baseline gap-1.5">
        <span className={cn("num leading-none font-semibold text-slate-900", compact ? "text-[1.5rem]" : "text-[1.875rem]")}>{value}</span>
        {unit && <span className="text-sm text-dim">{unit}</span>}
      </div>
      {sub && <div className="mt-2 text-xs leading-snug text-dim">{sub}</div>}
    </div>
  );
}
