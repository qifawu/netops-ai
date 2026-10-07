import { useEffect, useState } from "react";
import { cn, load, ts } from "../lib/api";
import Inline from "../lib/Inline";
import { AiInline } from "../lib/aitext";
import { Badge, Card, Empty, Icon, Loading, PATH, SectionTitle } from "../lib/ui";
import { fill, useT, type DictKey } from "../lib/i18n";

/** 后端 `dashboard.py::_SOURCE_LABEL` 是固定 3 项枚举（不是 AI 自由生成文本）。 */
const SOURCE_LABEL_KEY: Record<string, DictKey> = {
  告警分析: "audit.sourceLabel.alert",
  "AI 问答": "audit.sourceLabel.chat",
  "SOP 审核 / 巡检建议": "audit.sourceLabel.other",
};
/** 后端 `dashboard.py` 里设备解析不出来时的固定占位符（不是真实设备名，不受"设备名不翻译"这条限制约束）。 */
const UNSPECIFIED_DEVICE = "未指明设备";

type Denial = { clock: number; command: string; reason: string; count: number; devices: string[]; device_count: number; alert_count: number; source: string };
type Command = { command: string; count: number; non_show: boolean };
type Device = { device: string; commands: number; latest_clock: number };
type Timeline = { clock: number; device: string; command: string; source_type: string; source: string; eventid: string; trace_id: string; ok: boolean };
type Data = {
  summary: { command_total: number; unique_commands: number; non_show_commands: number; denied_commands: number; denied_groups: number; devices: number; chat_traces: number };
  commands_top: Command[]; devices: Device[]; timeline: Timeline[]; denials: Denial[];
};

const TH = "px-4 py-1.5 text-left text-[11px] font-semibold tracking-wide text-slate-500 uppercase";

export default function Audit() {
  const t = useT();
  const [d, setD] = useState<Data | null>(null);
  const [device, setDevice] = useState("");
  const [commandsExpanded, setCommandsExpanded] = useState(false);
  const [timelineExpanded, setTimelineExpanded] = useState(false);
  useEffect(() => { load<Data>("audit", "/api/audit").then(setD); }, []);
  if (!d) return <Loading />;
  const devices = Array.from(new Set(d.timeline.map((x) => x.device))).sort();
  const timeline = d.timeline.filter((x) => !device || x.device === device);
  const topCommands = d.commands_top.slice(0, 12);
  const recentTimeline = timeline.slice(0, 20);
  const s = d.summary;
  const devRows = d.devices.filter((x) => !x.device.startsWith("<"));
  const maxCmd = Math.max(1, ...devRows.map((x) => x.commands));
  return (
    <div className="mx-auto max-w-[1280px] space-y-5">
      <section className="grid grid-cols-[1.5fr_1fr_1fr_1fr] divide-x divide-line rounded-[4px] border border-line bg-card">
        <div className="px-4 py-3">
          <div className="flex items-center gap-1.5 text-xs font-medium text-slate-600"><Icon d={PATH.shield} className="h-3.5 w-3.5 text-ok" />{t("audit.summary.title")}</div>
          <div className="mt-1.5 text-[13px] leading-snug font-semibold text-slate-800">
            {t("audit.summary.ranPrefix")} <span className="num text-[26px] text-ok">{s.command_total.toLocaleString()}</span> {t("audit.summary.ranSuffix")}
          </div>
          <div className="mt-1.5 text-[11.5px] text-dim">
            {s.non_show_commands === 0 ? t("audit.summary.allShow") : `${t("audit.summary.someNonShow")} ${s.non_show_commands} ${t("audit.summary.someNonShowSuffix")}`}
          </div>
        </div>
        {[
          [t("audit.kpi.denied"), s.denied_commands, t("unit.times"), s.denied_groups ? `${t("audit.kpi.denied.groupedPrefix")} ${s.denied_groups} ${t("audit.kpi.denied.groupedSuffix")}` : t("audit.kpi.denied.none"), s.denied_commands ? "text-warn" : ""],
          [t("audit.kpi.devices"), s.devices, t("unit.devices"), t("audit.kpi.devices.sub"), ""],
          [t("audit.kpi.chat"), s.chat_traces, t("unit.times"), t("audit.kpi.chat.sub"), ""],
        ].map(([label, v, unit, sub, tint]) => (
          <div key={label as string} className="px-4 py-3">
            <div className="text-xs font-medium text-slate-600">{label}</div>
            <div className="mt-1.5 flex items-baseline gap-1.5">
              <span className={cn("num text-[26px] leading-none font-semibold text-slate-900", tint as string)}>{v}</span>
              <span className="text-xs text-dim">{unit}</span>
            </div>
            <div className="mt-1.5 text-[11.5px] text-dim">{sub}</div>
          </div>
        ))}
      </section>

      <section>
        <SectionTitle title={t("audit.commandsTable.title")} note={t("audit.commandsTable.note")} />
        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-line bg-[#f6f7f9]">
                <tr><th className={TH}>{t("audit.commandsTable.col.command")}</th><th className={TH}>{t("audit.commandsTable.col.count")}</th><th className={TH}>{t("audit.commandsTable.col.type")}</th></tr>
              </thead>
              <tbody className="divide-y divide-line">
                {(commandsExpanded ? d.commands_top : topCommands).map((x) => (
                  <tr key={x.command} className="transition even:bg-[#fafbfc] hover:bg-brand/[0.04]">
                    <td className="px-4 py-1.5"><code className="font-mono text-[12.5px]">{x.command}</code></td>
                    <td className="num px-4 py-1.5 text-[12.5px]">{x.count}</td>
                    <td className="px-4 py-1.5"><Badge className={x.non_show ? "bg-amber-50 text-amber-700 ring-amber-200" : "bg-white text-slate-600 ring-slate-300"}>{x.non_show ? t("audit.commandsTable.nonShow") : t("audit.commandsTable.readonly")}</Badge></td>
                  </tr>
                ))}
                {d.commands_top.length === 0 && <tr><td colSpan={3}><Empty title={t("audit.commandsTable.empty")} /></td></tr>}
              </tbody>
            </table>
          </div>
          {d.commands_top.length > 12 && (
            <button type="button" onClick={() => setCommandsExpanded((v) => !v)}
              className="w-full border-t border-line px-4 py-2 text-left text-xs font-medium text-brand hover:bg-slate-50">
              {commandsExpanded ? t("audit.commandsTable.collapse") : `${t("audit.commandsTable.expandPrefix")} ${d.commands_top.length} ${t("audit.commandsTable.expandSuffix")}`}
            </button>
          )}
        </Card>
      </section>

      <div className="grid grid-cols-2 items-start gap-4">
        <section>
          <SectionTitle title={t("audit.byDevice.title")} note={t("audit.byDevice.note")} />
          <Card className="overflow-hidden">
            <table className="w-full text-sm">
              <thead className="border-b border-line bg-[#f6f7f9]">
                <tr><th className={TH}>{t("audit.byDevice.col.device")}</th><th className={cn(TH, "w-1/3")}>{t("audit.byDevice.col.count")}</th><th className={TH}>{t("audit.byDevice.col.latest")}</th></tr>
              </thead>
              <tbody className="divide-y divide-line">
                {devRows.map((x) => (
                  <tr key={x.device}>
                    <td className="num px-4 py-1.5 text-[12.5px] font-medium">{x.device === UNSPECIFIED_DEVICE ? t("audit.byDevice.unspecified") : x.device}</td>
                    <td className="px-4 py-1.5">
                      <div className="flex items-center gap-2">
                        <div className="h-1.5 flex-1 bg-slate-100"><div className="h-1.5 bg-brand/70" style={{ width: `${(x.commands / maxCmd) * 100}%` }} /></div>
                        <span className="num w-10 text-right text-xs text-slate-600">{x.commands}</span>
                      </div>
                    </td>
                    <td className="num px-4 py-1.5 text-xs text-dim">{x.latest_clock ? ts(x.latest_clock) : t("audit.byDevice.unknown")}</td>
                  </tr>
                ))}
                {d.devices.length === 0 && <tr><td colSpan={3}><Empty title={t("audit.byDevice.empty")} /></td></tr>}
              </tbody>
            </table>
          </Card>
        </section>

        <section>
          <SectionTitle title={t("audit.denials.title")} note={t("audit.denials.note")} />
          {d.denials.length === 0 ? (
            <div className="rounded-[4px] border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm font-medium text-emerald-700">{t("audit.denials.zero")}</div>
          ) : (
            <Card className="max-h-[420px] divide-y divide-line overflow-y-auto">
              {d.denials.map((x, i) => (
                <div key={i} className="space-y-1.5 px-4 py-2.5 text-sm">
                  <div className="flex flex-wrap items-center gap-2 text-xs text-dim">
                    <Badge className="bg-amber-50 text-amber-700 ring-amber-200">{t("audit.denials.blockedCount")} {x.count} {t("audit.denials.timesSuffix")}</Badge>
                    <span>{x.device_count} {t("audit.denials.devicesAndAlerts")} {x.alert_count} {t("audit.denials.alertsSuffix")}</span>
                    <span className="num ml-auto">{ts(x.clock)}</span>
                  </div>
                  <code className="block rounded-[2px] border border-line bg-slate-50 px-2 py-1 font-mono text-[12.5px] break-words">{x.command}</code>
                  <div className="text-xs leading-relaxed text-dim"><Inline text={x.reason} /></div>
                </div>
              ))}
            </Card>
          )}
        </section>
      </div>

      <section>
        <SectionTitle title={t("audit.timeline.title")} note={t("audit.timeline.note")}
          right={
            <select value={device} onChange={(e) => setDevice(e.target.value)} className="rounded-[3px] border border-[#c3cad2] bg-card px-2 py-1 text-[13px]">
              <option value="">{t("audit.timeline.allDevices")}</option>
              {devices.map((x) => <option key={x} value={x}>{x === UNSPECIFIED_DEVICE ? t("audit.byDevice.unspecified") : x}</option>)}
            </select>
          } />
        <Card className="overflow-hidden">
          <div className="divide-y divide-line">
            {(timelineExpanded ? timeline : recentTimeline).map((x, i) => (
              <div key={`${x.clock}-${x.command}-${i}`} className="grid grid-cols-[104px_72px_minmax(0,1fr)_auto] items-center gap-3 px-4 py-1.5 text-sm even:bg-[#fafbfc]">
                <span className="num text-xs text-dim">{x.clock ? ts(x.clock) : t("audit.timeline.unknownTime")}</span>
                <span className="num truncate text-xs font-medium text-slate-800">{x.device}</span>
                <code className="min-w-0 truncate font-mono text-[12.5px]">{x.command}</code>
                <Badge className="bg-white text-slate-600 ring-slate-300">{x.source_type === "chat" ? t("audit.timeline.chat") : t("audit.timeline.alert")}</Badge>
              </div>
            ))}
            {timeline.length === 0 && <Empty title={t("audit.timeline.empty")} />}
          </div>
          {timeline.length > 20 && (
            <button type="button" onClick={() => setTimelineExpanded((v) => !v)}
              className="w-full border-t border-line px-4 py-2 text-left text-xs font-medium text-brand hover:bg-slate-50">
              {timelineExpanded ? t("audit.timeline.collapse") : `${t("audit.timeline.expandPrefix")} ${timeline.length} ${t("audit.timeline.expandSuffix")}`}
            </button>
          )}
        </Card>
      </section>

    </div>
  );
}
