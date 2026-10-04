import { useEffect, useState } from "react";
import { cn } from "../lib/api";
import { Btn, Notice, call } from "../lib/sopui";
import { Card, CardHead } from "../lib/ui";
import { fill, useT } from "../lib/i18n";

/** 巡检参数表单：阈值 + 范围。字段、范围、说明都来自后端 `/api/inspection/config` 的 schema，
 *  前端不写死任何阈值；校验以后端为准（错误原样显示在表单上方）。写的文件固定是仓库根的 inspection.yaml。 */

type Param = { key: string; kind: string; min: number; max: number; unit: string; label: string; help: string };
type Schema = { name: string; label: string; help: string; params: Param[] };
export type InspectionConfig = {
  file_exists: boolean; file: string; error: string;
  values: { detectors: Record<string, Record<string, number>>; scope: Record<string, string[] | number> };
  defaults: { detectors: Record<string, Record<string, number>>; scope: Record<string, string[] | number> };
  schema: Schema[];
  scope_limits: { lookback_days: { min: number; max: number } };
};

const SCOPE_LISTS = [
  ["include_hosts", "inspection.cfg.includeHosts"],
  ["exclude_hosts", "inspection.cfg.excludeHosts"],
  ["include_item_keys", "inspection.cfg.includeKeys"],
  ["exclude_item_keys", "inspection.cfg.excludeKeys"],
] as const;

const lines = (v: unknown) => (Array.isArray(v) ? (v as string[]).join("\n") : "");

export default function InspectionConfigForm({ cfg, onSaved, onRun }: {
  cfg: InspectionConfig; onSaved: (c: InspectionConfig) => void; onRun: () => Promise<void>;
}) {
  const t = useT();
  const [det, setDet] = useState<Record<string, Record<string, string>>>({});
  const [lists, setLists] = useState<Record<string, string>>({});
  const [lookback, setLookback] = useState("7");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  // 后端值变了（保存后回读 / 外部改了文件）就重置表单
  useEffect(() => {
    const d: Record<string, Record<string, string>> = {};
    for (const [name, params] of Object.entries(cfg.values.detectors)) {
      d[name] = Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)]));
    }
    setDet(d);
    setLists(Object.fromEntries(SCOPE_LISTS.map(([k]) => [k, lines(cfg.values.scope[k])])));
    setLookback(String(cfg.values.scope.lookback_days));
  }, [cfg]);

  const dirty = (() => {
    for (const [name, params] of Object.entries(cfg.values.detectors))
      for (const [k, v] of Object.entries(params)) if (det[name]?.[k] !== String(v)) return true;
    for (const [k] of SCOPE_LISTS) if ((lists[k] ?? "") !== lines(cfg.values.scope[k])) return true;
    return lookback !== String(cfg.values.scope.lookback_days);
  })();

  const buildBody = () => {
    const detectors: Record<string, Record<string, number>> = {};
    for (const [name, params] of Object.entries(det)) {
      detectors[name] = {};
      for (const [k, v] of Object.entries(params)) detectors[name][k] = v.trim() === "" ? NaN : Number(v);
    }
    const scope: Record<string, unknown> = { lookback_days: Number(lookback) };
    for (const [k] of SCOPE_LISTS) scope[k] = (lists[k] ?? "").split("\n").map((x) => x.trim()).filter(Boolean);
    return { detectors, scope };
  };

  const save = async (rerun: boolean) => {
    setErr(""); setMsg("");
    const body = buildBody();
    // JSON 里没有 NaN；空输入或非数字当成"写错了"，在这里拦下，不发一个会被序列化成 null 的请求
    if (Object.values(body.detectors).some((p) => Object.values(p).some((v) => Number.isNaN(v))) || Number.isNaN(body.scope.lookback_days as number)) {
      setErr(t("inspection.cfg.notNumber")); return;
    }
    setBusy(true);
    const r = await call<InspectionConfig>("PUT", "/api/inspection/config", body);
    if (!r.ok) { setBusy(false); setErr(r.data.message || t("inspection.cfg.saveFailed")); return; }
    onSaved(r.data);
    if (rerun) { setMsg(t("inspection.cfg.rerunning")); await onRun(); setMsg(t("inspection.cfg.rerunDone")); }
    else setMsg(t("inspection.cfg.saved"));
    setBusy(false);
  };

  const inputCls = "w-28 rounded-lg border border-line bg-white px-2.5 py-1.5 font-mono text-sm tabular-nums focus:border-brand focus:outline-none";
  return (
    <Card>
      <CardHead title={t("inspection.cfg.title")}
        note={cfg.file_exists ? fill(t("inspection.cfg.fileExists"), { file: cfg.file }) : fill(t("inspection.cfg.fileMissing"), { file: cfg.file })} />
      <div className="space-y-5 px-5 py-4">
        {cfg.error && <Notice tone="rose">{fill(t("inspection.cfg.fileBroken"), { err: cfg.error })}</Notice>}
        {err && <Notice tone="rose">{err}</Notice>}
        {msg && !err && <Notice tone="emerald">{msg}</Notice>}
        <div className="grid grid-cols-3 gap-4">
          {cfg.schema.map((s) => (
            <div key={s.name} className="rounded-xl border border-line p-4">
              <div className="text-sm font-semibold text-slate-800">{s.label}</div>
              <div className="mt-1 text-xs leading-relaxed text-dim">{s.help}</div>
              <div className="mt-3 space-y-2.5">
                {s.params.map((p) => {
                  const def = cfg.defaults.detectors[s.name]?.[p.key];
                  const cur = det[s.name]?.[p.key] ?? "";
                  return (
                    <label key={p.key} className="block text-xs" title={p.help}>
                      <span className="flex items-baseline justify-between gap-2">
                        <span className="font-medium text-slate-700">{p.label}</span>
                        <span className="text-dim">{p.unit}</span>
                      </span>
                      <span className="mt-1 flex items-center gap-2">
                        <input data-testid={`cfg-${s.name}-${p.key}`} value={cur} inputMode="decimal"
                          onChange={(e) => setDet((d) => ({ ...d, [s.name]: { ...d[s.name], [p.key]: e.target.value } }))}
                          className={cn(inputCls, cur !== String(def) && "border-amber-300 bg-amber-50/50")} />
                        <span className="text-dim">{cur !== String(def) ? fill(t("inspection.cfg.defaultIs"), { v: String(def) }) : t("inspection.cfg.isDefault")}</span>
                      </span>
                      <span className="mt-0.5 block text-[11px] leading-snug text-dim">{p.help}</span>
                    </label>
                  );
                })}
              </div>
            </div>
          ))}
        </div>

        <div className="rounded-xl border border-line p-4">
          <div className="text-sm font-semibold text-slate-800">{t("inspection.cfg.scopeTitle")}</div>
          <div className="mt-1 text-xs text-dim">{t("inspection.cfg.scopeHint")}</div>
          <div className="mt-3 grid grid-cols-4 gap-3">
            {SCOPE_LISTS.map(([k, labelKey]) => (
              <label key={k} className="block text-xs">
                <span className="font-medium text-slate-700">{t(labelKey)}</span>
                <textarea data-testid={`cfg-scope-${k}`} rows={3} value={lists[k] ?? ""} onChange={(e) => setLists((l) => ({ ...l, [k]: e.target.value }))}
                  className="mt-1 w-full rounded-lg border border-line bg-white px-2.5 py-1.5 font-mono text-xs focus:border-brand focus:outline-none" placeholder="*" />
              </label>
            ))}
          </div>
          <label className="mt-3 flex items-center gap-2 text-xs">
            <span className="font-medium text-slate-700">{t("inspection.cfg.lookback")}</span>
            <input data-testid="cfg-scope-lookback_days" value={lookback} onChange={(e) => setLookback(e.target.value)} className={inputCls} />
            <span className="text-dim">{fill(t("inspection.cfg.lookbackRange"), { min: cfg.scope_limits.lookback_days.min, max: cfg.scope_limits.lookback_days.max })}</span>
          </label>
        </div>

        <div className="flex items-center gap-3">
          <Btn tone="brand" busy={busy} disabled={busy || (!dirty && cfg.file_exists)} onClick={() => save(true)}>{t("inspection.cfg.saveRun")}</Btn>
          <Btn disabled={busy || !dirty} onClick={() => save(false)}>{t("inspection.cfg.saveOnly")}</Btn>
          <span className="text-xs text-dim">{t("inspection.cfg.footer")}</span>
        </div>
      </div>
    </Card>
  );
}
