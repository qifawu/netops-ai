import { useCallback, useEffect, useRef, useState } from "react";
import { cn, load, ts } from "../lib/api";
import { AiText } from "../lib/aitext";
import { Badge, Card, Empty, Loading } from "../lib/ui";
import { oneLine } from "../lib/incident";
import { fill, useLang, useT, type DictKey } from "../lib/i18n";
import HelpTip from "../lib/HelpTip";
import { goTab } from "../lib/nav";

type Dev = {
  managed: boolean;
  name: string; role: string; role_cn: string; mgmt_ip: string; zabbix_host: string;
  inventory_source: string; model: string; serial: string; site: string; rack: string; platform: string; software_version: string;
  interfaces: { name: string; peer: string; peer_interface: string; description: string }[];
  uplinks: string[]; single_homed: boolean;
  incident_count: number; latest_incident: number; latest_root_cause: string;
};
type Link = { a: string; a_if: string; b: string; b_if: string; managed: boolean };
type Data = { source: string; netbox_error: string; cached_seconds?: number | null; window_hours: number; devices: Dev[]; links: Link[] };

const ROWS = ["core", "aggregation", "access", "unmanaged"];
const LAYER_KEY: Record<string, DictKey> = { core: "topology.layer.core", aggregation: "topology.layer.aggregation", access: "topology.layer.access", unmanaged: "topology.layer.unmanaged" };
/** 画布跟着卡片的实际像素尺寸走（1 单位 = 1px），节点和字号不随屏幕缩放。
 *  以前固定 780×380 再按比例拉满整列，大屏上 7 台设备占了大半屏；固定 660×300 又在 2560 宽的屏上缩在中间。
 *  现在：宽度取卡片宽、但图形最多铺 MAX_W 居中；高度取卡片高（跟右边详情栏一样高），最少 MIN_H。 */
const MIN_W = 480, MIN_H = 300, MAX_W = 1400, MAX_H = 420, PAD_X = 96, PAD_Y = 50, NW = 96, NH = 40;

/** 按角色分层排（只算实际有设备的层）。**不用力导向布局**：网络层次是已知的，
 * 力导向每次刷新位置都不一样，人记不住哪个点是哪台。 */
function place(devices: Dev[], W: number, H: number) {
  const drawW = Math.min(W, MAX_W), x0 = (W - drawW) / 2;
  const pos: Record<string, { x: number; y: number }> = {};
  const rows = ROWS.filter((r) => devices.some((d) => d.role === r));
  const layerY: Record<string, number> = {};
  rows.forEach((role, r) => {
    const y = rows.length === 1 ? H / 2 : PAD_Y + (r * (H - 2 * PAD_Y)) / (rows.length - 1);
    layerY[role] = y;
    const row = devices.filter((d) => d.role === role);
    row.forEach((d, i) => { pos[d.name] = { x: x0 + PAD_X + ((i + 0.5) * (drawW - PAD_X - 24)) / row.length, y }; });
  });
  const band = rows.length > 1 ? Math.min(96, (H - 2 * PAD_Y) / (rows.length - 1) - 12) : 64;
  return { pos, rows, layerY, x0, drawW, band };
}

export default function Topology() {
  const t = useT();
  const lang = useLang();
  const na = (s?: string) => (s && s.trim() ? s : t("topology.na"));
  const [d, setD] = useState<Data | null>(null);
  const [sel, setSel] = useState("");
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [box, setBox] = useState({ w: 660, h: MIN_H });
  // 画布容器的实际尺寸：卡片在网格里跟右边详情栏等高，宽度随窗口变
  const ro = useRef<ResizeObserver | null>(null);
  const boxRef = useCallback((el: HTMLDivElement | null) => {
    ro.current?.disconnect();
    if (!el) return;
    ro.current = new ResizeObserver(([e]) => {
      const { width, height } = e.contentRect;
      setBox({ w: Math.max(MIN_W, Math.round(width)), h: Math.min(MAX_H, Math.max(MIN_H, Math.round(height))) });
    });
    ro.current.observe(el);
  }, []);
  const refresh = (force = false) => {
    setBusy(true); setFailed(false);
    load<Data>("topology", force ? "/api/topology?refresh=1" : "/api/topology").then((x) => {
      setD(x);
      // 默认选中故障最多的设备，右侧详情不空着（已经选过就不动）。
      setSel((cur) => {
        if (cur) return cur;
        const worst = [...x.devices].sort((a, b) => b.incident_count - a.incident_count)[0];
        return worst && worst.incident_count > 0 ? worst.name : "";
      });
    }).catch(() => setFailed(true)).finally(() => setBusy(false));
  };
  useEffect(() => refresh(), []);
  if (!d) return failed
    ? <div className="mx-auto max-w-[520px] rounded-xl border border-rose-200 bg-rose-50 p-5 text-sm text-rose-700">
        {t("topology.loadFailed")}
        <button onClick={() => refresh(true)} className="ml-3 rounded-md border border-rose-300 bg-white px-3 py-1 text-rose-700 hover:bg-rose-100">{t("topology.reload")}</button>
      </div>
    : <Loading />;
  const W = box.w, H = box.h;
  const { pos, rows, layerY, x0, drawW, band } = place(d.devices, W, H);
  const by = Object.fromEntries(d.devices.map((x) => [x.name, x]));
  const cur = by[sel];

  const managed = d.devices.filter((x) => x.managed);
  return (
    <div className="page-wrap space-y-5">
      <p className="flex flex-wrap items-center gap-x-1 text-[0.8125rem] leading-relaxed text-slate-600">
        {fill(t("topology.summary.line"), { src: d.source === "netbox" ? t("topology.summary.sourceNetbox") : t("topology.summary.sourceFile"), h: d.window_hours })}
        <HelpTip text={t("topology.summary.help")} />
        {d.netbox_error && (
          d.netbox_error.includes("先用")
            ? <span className="text-amber-600">{t("topology.netboxWarn")}</span>
            : <span className="text-rose-600">{t("topology.netboxFail")}{d.netbox_error}</span>
        )}
        {d.cached_seconds != null && <span className="ml-3 text-xs text-slate-400">{fill(t("topology.cachedLine"), { age: d.cached_seconds < 60 ? `${d.cached_seconds} ${t("topology.seconds")}` : `${Math.round(d.cached_seconds / 60)} ${t("topology.minutes")}` })}</span>}
        <button onClick={() => refresh(true)} disabled={busy} className="ml-3 h-7 rounded-[8px] border border-line bg-white px-2.5 text-xs text-slate-700 hover:border-slate-400 disabled:opacity-50">
          {busy ? t("topology.loading") : t("topology.reload")}
        </button>
      </p>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px] 2xl:grid-cols-[minmax(0,1fr)_400px]">
        <Card className="flex flex-col overflow-hidden">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-line px-5 py-3 text-xs text-dim">
            <span className="text-[0.9375rem] font-semibold text-slate-900">{t("topology.graph.title")}</span>
            <span className="ml-auto flex items-center gap-1.5"><span className="inline-block h-3 w-3 rounded-[3px] border-[1.5px] border-rose-500 bg-rose-50" />{t("topology.legend.incident")}</span>
            <span className="flex items-center gap-1.5"><span className="inline-block h-3 w-3 rounded-[3px] border-[1.5px] border-dashed border-amber-500" />{t("topology.legend.singleHomed")}</span>
            <span className="flex items-center gap-1.5"><span className="inline-block h-3 w-3 rounded-[3px] border-[1.5px] border-dashed border-slate-400" />{t("topology.legend.unmanaged")}</span>
          </div>
          <div className="relative flex min-h-[300px] flex-1 items-center overflow-x-auto">
          <div ref={boxRef} className="absolute inset-x-5 inset-y-3" />
          <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="relative mx-5 my-3 block shrink-0">
            {rows.map((r) => (
              <g key={r}>
                <rect x={x0} y={layerY[r] - band / 2} width={drawW} height={band} rx={8} fill="#f7f8fa" />
                <text x={x0 + 14} y={layerY[r] + 4} className="fill-slate-500 text-[0.75rem]">{t(LAYER_KEY[r])}</text>
              </g>
            ))}
            {d.links.map((l, i) => {
              const a = pos[l.a], b = pos[l.b];
              if (!a || !b) return null;
              const hot = sel && (l.a === sel || l.b === sel);
              return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                stroke={hot ? "#1f5fbf" : "#c3c9d2"}
                strokeWidth={hot ? 2.4 : 1.4} strokeDasharray={l.managed ? undefined : "4 4"} />;
            })}
            {d.devices.map((x) => {
              const p = pos[x.name];
              if (!p) return null;
              const bad = x.incident_count > 0;
              const on = sel === x.name;
              return (
                <g key={x.name} onClick={() => setSel(on ? "" : x.name)} className="cursor-pointer">
                  {on && <rect x={p.x - NW / 2 - 4} y={p.y - NH / 2 - 4} width={NW + 8} height={NH + 8} rx={11} fill="none" stroke="#d6e4f7" strokeWidth={3} />}
                  {/* 单上联：边框本身改成橙色虚线，不再外套一圈虚线框 */}
                  <rect x={p.x - NW / 2} y={p.y - NH / 2} width={NW} height={NH} rx={8}
                    fill={!x.managed ? "#f7f8fa" : bad ? "#fbedeb" : "#ffffff"}
                    stroke={bad ? "#c42b2b" : x.single_homed ? "#b26205" : on ? "#1f5fbf" : !x.managed ? "#9aa5b1" : "#c3c9d2"}
                    strokeWidth={bad || on ? 2 : 1.5} strokeDasharray={!x.managed || x.single_homed ? "4 3" : undefined} />
                  <text x={p.x} y={p.y - 2} textAnchor="middle" className="fill-slate-900 text-[0.8125rem] font-semibold">{x.name}</text>
                  <text x={p.x} y={p.y + 12} textAnchor="middle" className="fill-slate-500 font-mono text-[0.625rem]">{x.managed ? na(x.mgmt_ip) : t("topology.layer.unmanaged")}</text>
                  {bad && (
                    <g>
                      <rect x={p.x + NW / 2 - 9} y={p.y - NH / 2 - 9} width={18} height={18} rx={9} fill="#c42b2b" />
                      <text x={p.x + NW / 2} y={p.y - NH / 2 + 4} textAnchor="middle" className="fill-white text-[0.6875rem] font-semibold">{x.incident_count}</text>
                    </g>
                  )}
                </g>
              );
            })}
          </svg>
          </div>
        </Card>

        <Card className="p-5 text-sm">
          {cur ? (
            <div>
              <div className="mb-4 flex items-start gap-2">
                <div>
                  <div className="text-[1.125rem] font-semibold text-slate-900">{cur.name}</div>
                  <div className="text-xs text-dim">{t(LAYER_KEY[cur.role]) || cur.role_cn} · {t("topology.detail.from")} {cur.inventory_source}</div>
                </div>
                {cur.incident_count > 0 && <Badge className="ml-auto bg-rose-50 text-rose-700 ring-rose-200">{t("topology.detail.incidentBadge")} {cur.incident_count} {t("topology.detail.incidentBadgeSuffix")}</Badge>}
              </div>
              {cur.managed ? (
                <>
                  <dl className="grid grid-cols-[84px_minmax(0,1fr)] gap-x-3 gap-y-2 text-[0.8125rem]">
                    {([
                      ["topology.detail.model", cur.model, ""],
                      ["topology.detail.serial", cur.serial, "id"],
                      ["topology.detail.mgmtIp", cur.mgmt_ip, ""],
                      ["topology.detail.site", cur.site, ""],
                      ["topology.detail.rack", cur.rack, ""],
                      ["topology.detail.platform", cur.platform, ""],
                      ["topology.detail.softwareVersion", cur.software_version, ""],
                      ["topology.detail.zabbixHost", cur.zabbix_host, ""],
                    ] as [DictKey, string, string][]).map(([k, v, m]) => (
                      <div key={k} className="contents">
                        <dt className="text-dim">{t(k)}</dt>
                        <dd data-mask={m || undefined} className="break-words text-slate-800">{na(v)}</dd>
                      </div>
                    ))}
                  </dl>
                  <div className="mt-4 border-t border-line pt-3">
                    <div className="mb-2 text-[0.8125rem] font-medium text-slate-900">{t("topology.detail.interfacesTitle")}</div>
                    <div className="max-h-44 space-y-1.5 overflow-auto pr-1 text-[0.8125rem]">
                      {(cur.interfaces.length ? cur.interfaces : [{ name: "", peer: "", peer_interface: "", description: "" }]).map((it, i) => (
                        <div key={`${it.name}-${i}`} className="grid grid-cols-[132px_minmax(0,1fr)] gap-2">
                          <span className="font-mono text-xs break-all text-slate-500">{na(it.name)}</span>
                          <span className="text-slate-700">
                            {it.peer ? `${it.peer} ${it.peer_interface || t("topology.na")}` : t("topology.detail.peerNotRecorded")}
                            {it.description && ` · ${it.description}`}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                </>
              ) : (
                <div className="rounded-[10px] border border-dashed border-slate-300 bg-slate-50 px-3 py-2 text-sm text-slate-600">
                  {t("topology.detail.notInInventory")}
                </div>
              )}
              {cur.incident_count > 0 && (
                <div className="mt-4 rounded-[10px] bg-slate-50 px-3.5 py-3 text-[0.8125rem] leading-relaxed text-slate-800">
                  <div className="text-xs text-dim">{t("topology.detail.lastIncident")} {ts(cur.latest_incident)}</div>
                  <div className="mt-0.5"><AiText text={oneLine(cur.latest_root_cause, 60)} /></div>
                </div>
              )}
              {cur.managed && (
                <button type="button" onClick={() => goTab(`incidents?device=${encodeURIComponent(cur.name)}&view=history`)}
                  className="mt-3 flex h-9 w-full items-center justify-between rounded-[8px] border border-line px-3 text-[0.8125rem] font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50">
                  {t("topology.detail.viewHistory")}
                  <span className="text-brand">→</span>
                </button>
              )}
            </div>
          ) : (
            <Empty title={t("topology.detail.empty.title")} />
          )}
        </Card>
      </div>

      <Card className="overflow-hidden">
        <div className="border-b border-line px-5 py-3.5 text-[0.9375rem] font-semibold text-slate-900">{t("topology.table.title")} <span className="ml-1.5 text-[0.8125rem] font-normal text-dim">{t("topology.table.countPrefix")} {managed.length} {t("topology.table.countSuffix")}</span></div>
        <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] text-[0.8125rem]">
          <thead className="border-b border-line text-xs text-slate-500">
            <tr>{(["topology.table.col.device", "topology.table.col.layer", "topology.table.col.mgmtIp", "topology.table.col.zabbixHost", "topology.table.col.uplinks", "topology.table.col.incidents"] as DictKey[]).map((h) => (
              <th key={h} className="px-5 py-2.5 text-left font-medium">{t(h)}</th>))}</tr>
          </thead>
          <tbody className="divide-y divide-line">
            {managed.map((x) => (
              <tr key={x.name} onClick={() => setSel(x.name)}
                className={cn("cursor-pointer transition hover:bg-slate-50", sel === x.name && "!bg-brand/[0.06]")}>
                <td className="px-5 py-2.5 font-medium text-slate-900">{x.name}</td>
                <td className="px-5 py-2.5 text-dim">{t(LAYER_KEY[x.role]) || x.role_cn}</td>
                <td className="px-5 py-2.5 font-mono text-xs">{na(x.mgmt_ip)}</td>
                <td className="px-5 py-2.5 font-mono text-xs text-dim">{na(x.zabbix_host)}</td>
                <td className="px-5 py-2.5 text-xs">
                  {x.uplinks.join(lang === "zh" ? "、" : ", ") || "—"}
                  {x.single_homed && <Badge className="ml-2 bg-amber-50 text-amber-700 ring-amber-200" title={t("topology.table.singleHomedTitle")}>{t("topology.table.singleHomedBadge")}</Badge>}
                </td>
                <td className="px-5 py-2.5 text-xs">
                  {x.incident_count > 0 ? <Badge className="bg-rose-50 text-rose-700 ring-rose-200">{x.incident_count} {t("unit.times")}</Badge> : <span className="text-dim">{t("topology.table.none")}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </Card>
    </div>
  );
}
