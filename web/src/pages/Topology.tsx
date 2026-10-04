import { useEffect, useState } from "react";
import { cn, load, ts } from "../lib/api";
import { AiText } from "../lib/aitext";
import { Badge, Card, Empty, Loading } from "../lib/ui";
import { oneLine } from "../lib/incident";
import { useLang, useT, type DictKey } from "../lib/i18n";

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
const W = 780, H = 380, PAD_X = 120, PAD_Y = 52, NW = 92, NH = 38;

/** 按角色分层排（只算实际有设备的层）。**不用力导向布局**：网络层次是已知的，
 * 力导向每次刷新位置都不一样，人记不住哪个点是哪台。 */
function place(devices: Dev[]) {
  const pos: Record<string, { x: number; y: number }> = {};
  const rows = ROWS.filter((r) => devices.some((d) => d.role === r));
  const layerY: Record<string, number> = {};
  rows.forEach((role, r) => {
    const y = rows.length === 1 ? H / 2 : PAD_Y + (r * (H - 2 * PAD_Y)) / (rows.length - 1);
    layerY[role] = y;
    const row = devices.filter((d) => d.role === role);
    row.forEach((d, i) => { pos[d.name] = { x: PAD_X + ((i + 0.5) * (W - PAD_X - 24)) / row.length, y }; });
  });
  return { pos, rows, layerY };
}

export default function Topology() {
  const t = useT();
  const lang = useLang();
  const na = (s?: string) => (s && s.trim() ? s : t("topology.na"));
  const [d, setD] = useState<Data | null>(null);
  const [sel, setSel] = useState("");
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
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
  const { pos, rows, layerY } = place(d.devices);
  const by = Object.fromEntries(d.devices.map((x) => [x.name, x]));
  const cur = by[sel];

  const managed = d.devices.filter((x) => x.managed);
  return (
    <div className="mx-auto max-w-[1280px] space-y-5">
      <p className="text-sm leading-relaxed text-slate-600">
        {t("topology.summary.source")} {d.source === "netbox" ? t("topology.summary.sourceNetbox") : t("topology.summary.sourceFile")}{t("topology.summary.readonly")} {d.window_hours} {t("topology.summary.hoursSuffix")}
        {d.netbox_error && (
          d.netbox_error.includes("先用")
            ? <span className="text-amber-600">{t("topology.netboxWarn")}</span>
            : <span className="text-rose-600">{t("topology.netboxFail")}{d.netbox_error}</span>
        )}
        {d.cached_seconds != null && <span className="ml-3 text-xs text-slate-400">{t("topology.cachedPrefix")} {d.cached_seconds < 60 ? `${d.cached_seconds} ${t("topology.seconds")}` : `${Math.round(d.cached_seconds / 60)} ${t("topology.minutes")}`} {t("topology.cachedSuffix")}</span>}
        <button onClick={() => refresh(true)} disabled={busy} className="ml-3 rounded-md border border-slate-200 bg-white px-2.5 py-0.5 text-xs text-slate-600 hover:bg-slate-50 disabled:opacity-50">
          {busy ? t("topology.loading") : t("topology.reload")}
        </button>
      </p>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_330px]">
        <Card className="p-4">
          <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
            {rows.map((r) => (
              <g key={r}>
                <rect x={0} y={layerY[r] - 34} width={W} height={68} rx={12} fill="#f8fafc" />
                <text x={16} y={layerY[r] + 4} className="fill-slate-400 text-[12px] font-medium">{t(LAYER_KEY[r])}</text>
              </g>
            ))}
            {d.links.map((l, i) => {
              const a = pos[l.a], b = pos[l.b];
              if (!a || !b) return null;
              const hot = sel && (l.a === sel || l.b === sel);
              return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                stroke={hot ? "#4f46e5" : l.managed ? "#cbd5e1" : "#94a3b8"}
                strokeWidth={hot ? 2.5 : 1.5} strokeDasharray={l.managed ? undefined : "4 4"} />;
            })}
            {d.devices.map((x) => {
              const p = pos[x.name];
              if (!p) return null;
              const bad = x.incident_count > 0;
              const on = sel === x.name;
              return (
                <g key={x.name} onClick={() => setSel(on ? "" : x.name)} className="cursor-pointer">
                  {x.single_homed && <rect x={p.x - NW / 2 - 5} y={p.y - NH / 2 - 5} width={NW + 10} height={NH + 10} rx={14} fill="none" stroke="#f59e0b" strokeWidth={1.2} strokeDasharray="4 3" />}
                  <rect x={p.x - NW / 2} y={p.y - NH / 2} width={NW} height={NH} rx={10}
                    fill={!x.managed ? "#f8fafc" : bad ? "#fff1f2" : "#eef2ff"}
                    stroke={on ? "#4f46e5" : !x.managed ? "#94a3b8" : bad ? "#f43f5e" : "#c7d2fe"}
                    strokeWidth={on ? 2.5 : 1.5} strokeDasharray={x.managed ? undefined : "4 3"} />
                  <text x={p.x} y={p.y + 5} textAnchor="middle" className="fill-slate-800 text-[14px] font-semibold">{x.name}</text>
                  {bad && (
                    <g>
                      <circle cx={p.x + NW / 2} cy={p.y - NH / 2} r={10} fill="#e11d48" />
                      <text x={p.x + NW / 2} y={p.y - NH / 2 + 3.5} textAnchor="middle" className="fill-white text-[10px] font-semibold">{x.incident_count}</text>
                    </g>
                  )}
                  <text x={p.x} y={p.y + NH / 2 + 15} textAnchor="middle" className="fill-slate-500 text-[10px]" stroke="#ffffff" strokeWidth={3} paintOrder="stroke">{x.managed ? na(x.mgmt_ip) : t("topology.layer.unmanaged")}</text>
                </g>
              );
            })}
          </svg>
          <div className="mt-1 flex flex-wrap gap-x-5 gap-y-1 border-t border-line pt-3 text-xs text-dim">
            <span className="flex items-center gap-1.5"><span className="inline-block h-3 w-5 rounded border border-rose-400 bg-rose-50" />{t("topology.legend.incident")}</span>
            <span className="flex items-center gap-1.5"><span className="inline-block h-3 w-5 rounded border border-dashed border-amber-500" />{t("topology.legend.singleHomed")}</span>
            <span className="flex items-center gap-1.5"><span className="inline-block h-3 w-5 rounded border border-dashed border-slate-400" />{t("topology.legend.unmanaged")}</span>
          </div>
        </Card>

        <Card className="p-5 text-sm">
          {cur ? (
            <div>
              <div className="mb-4 flex items-start gap-2">
                <div>
                  <div className="text-lg font-semibold tracking-tight">{cur.name}</div>
                  <div className="text-xs text-dim">{t(LAYER_KEY[cur.role]) || cur.role_cn} · {t("topology.detail.from")} {cur.inventory_source}</div>
                </div>
                {cur.incident_count > 0 && <Badge className="ml-auto bg-rose-50 text-rose-700 ring-rose-200">{t("topology.detail.incidentBadge")} {cur.incident_count} {t("topology.detail.incidentBadgeSuffix")}</Badge>}
              </div>
              {cur.managed ? (
                <>
                  <dl className="grid grid-cols-[84px_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
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
                        <dd data-mask={m || undefined} className="break-words font-medium text-slate-700">{na(v)}</dd>
                      </div>
                    ))}
                  </dl>
                  <div className="mt-4 border-t border-line pt-3">
                    <div className="mb-2 text-xs font-semibold text-slate-700">{t("topology.detail.interfacesTitle")}</div>
                    <div className="max-h-44 space-y-1.5 overflow-auto pr-1 text-xs">
                      {(cur.interfaces.length ? cur.interfaces : [{ name: "", peer: "", peer_interface: "", description: "" }]).map((it, i) => (
                        <div key={`${it.name}-${i}`} className="grid grid-cols-[132px_minmax(0,1fr)] gap-2">
                          <span className="font-mono break-all text-slate-600">{na(it.name)}</span>
                          <span className="text-dim">
                            {it.peer ? `${it.peer} ${it.peer_interface || t("topology.na")}` : t("topology.detail.peerNotRecorded")}
                            {it.description && ` · ${it.description}`}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                </>
              ) : (
                <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50 px-3 py-2 text-sm text-slate-600">
                  {t("topology.detail.notInInventory")}
                </div>
              )}
              {cur.incident_count > 0 && (
                <div className="mt-4 rounded-xl bg-rose-50 px-3.5 py-2.5 text-xs leading-relaxed text-rose-800">
                  <div className="font-semibold">{t("topology.detail.lastIncident")} {ts(cur.latest_incident)}</div>
                  <div className="mt-0.5"><AiText text={oneLine(cur.latest_root_cause, 60)} /></div>
                </div>
              )}
            </div>
          ) : (
            <Empty title={t("topology.detail.empty.title")} hint={t("topology.detail.empty.hint")} />
          )}
        </Card>
      </div>

      <Card className="overflow-hidden">
        <div className="border-b border-line px-5 py-3 text-[15px] font-semibold text-slate-800">{t("topology.table.title")} <span className="ml-1 text-xs font-normal text-dim">{t("topology.table.countPrefix")} {managed.length} {t("topology.table.countSuffix")}</span></div>
        <table className="w-full text-sm">
          <thead className="bg-slate-50/70 text-xs text-dim">
            <tr>{(["topology.table.col.device", "topology.table.col.layer", "topology.table.col.mgmtIp", "topology.table.col.zabbixHost", "topology.table.col.uplinks", "topology.table.col.incidents"] as DictKey[]).map((h) => (
              <th key={h} className="px-5 py-2 text-left font-normal">{t(h)}</th>))}</tr>
          </thead>
          <tbody className="divide-y divide-line">
            {managed.map((x) => (
              <tr key={x.name} onClick={() => setSel(x.name)}
                className={cn("cursor-pointer transition hover:bg-slate-50", sel === x.name && "bg-brand/5")}>
                <td className="px-5 py-2.5 font-semibold">{x.name}</td>
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
      </Card>
    </div>
  );
}
