import { useEffect, useState } from "react";
import { cn, load, ts } from "../lib/api";
import { Badge, Card, Empty, Icon, Loading, PATH } from "../lib/ui";
import { MERGE_HINT, REPEAT_HINT, confMeta, faultType, fmtDur, titleOf } from "../lib/incident";
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

  return (
    <div className="page-wrap space-y-4">
      {/* 挑最能说明价值的几个数：做了多少、多快、什么时候认怂、花了多少、有没有越界。扁平数字块，一行排开。 */}
      <section className="grid grid-cols-[1fr_1fr_1fr_1.25fr] divide-x divide-line rounded-[4px] border border-line bg-card">
        <Stat label={t("overview.kpi.incidents")} value={inc?.value ?? "—"} unit={t("unit.times")} sub={inc?.sub ? `${t("overview.kpi.incidents.fromPrefix")}${fill(t("overview.kpi.incidents.alerts"), { n: String(inc.sub).match(/\d+/)?.[0] ?? "" })}` : ""} />
        <Stat label={t("overview.kpi.speed")} help={t("overview.kpi.speed.sub")} value={sp?.[1] ?? "—"} unit={sp?.[2] && sp[2] !== "秒" ? sp[2] : t("unit.seconds")} />
        <Stat label={t("overview.kpi.human")} value={human?.ratio != null ? Math.round(human.ratio * 100) : "—"} unit="%"
          sub={human ? fill(t("overview.kpi.human.count"), { n: human.value }) : ""} />
        <div className="px-4 py-3">
          <div className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
            <Icon d={PATH.shield} className="h-3.5 w-3.5 text-ok" />{t("overview.kpi.denied")}
          </div>
          <div className="mt-1.5 flex items-baseline gap-1.5">
            <span className="num text-[1.75rem] leading-none font-semibold text-ok">{denied?.value ?? "—"}</span>
            <span className="text-xs text-dim">{t("overview.kpi.denied.unit")}</span>
          </div>
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-center">
          <h2 className="text-[0.8125rem] font-semibold tracking-wide text-slate-800">{t("overview.recent.title")}</h2>
          <button onClick={() => goTab("incidents")} className="ml-auto px-1 py-1 text-xs font-medium text-brand hover:underline">{t("overview.recent.viewAll")}</button>
        </div>
        <Card className="overflow-hidden">
          <div className="grid grid-cols-[92px_96px_128px_minmax(0,1fr)_88px_76px] items-center gap-3 border-b border-line bg-[#f6f7f9] px-4 py-1.5 text-[0.6875rem] font-semibold tracking-wide text-slate-500 uppercase">
            <span>{t("overview.recent.col.time")}</span><span>{t("overview.recent.col.device")}</span><span>{t("overview.recent.col.type")}</span><span>{t("overview.recent.col.summary")}</span><span>{t("overview.recent.col.confidence")}</span><span className="text-right">{t("overview.recent.col.elapsed")}</span>
          </div>
          {d.recent.length === 0 && <Empty title={t("overview.recent.empty.title")} />}
          {d.recent.map((i) => {
            const ft = faultType(i), c = confMeta(i.confidence);
            return (
              <button key={i.incident_id} onClick={() => goTab("incidents", i.incident_id)} title={i.root_cause}
                className="grid w-full grid-cols-[92px_96px_128px_minmax(0,1fr)_88px_76px] min-h-[58px] items-center gap-3 border-b border-line px-4 py-2 text-left text-[0.8125rem] transition last:border-b-0 even:bg-[#fafbfc] hover:bg-brand/[0.05]">
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

function Stat({ label, value, unit, sub, help }: { label: string; value: string | number; unit: string; sub?: string; help?: string }) {
  return (
    <div className="px-4 py-3">
      <div className="flex items-center gap-1.5 text-xs font-medium text-slate-600">{label}{help && <HelpTip text={help} />}</div>
      <div className="mt-1.5 flex items-baseline gap-1.5">
        <span className="num text-[1.75rem] leading-none font-semibold text-slate-900">{value}</span>
        <span className="text-xs text-dim">{unit}</span>
      </div>
      {sub && <div className="mt-1.5 text-[0.71875rem] leading-snug text-dim">{sub}</div>}
    </div>
  );
}
