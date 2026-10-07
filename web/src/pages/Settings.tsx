import { useCallback, useEffect, useMemo, useState } from "react";
import { cn } from "../lib/api";
import { Badge, Card, CardHead, Empty, Icon, Loading, PATH, SectionTitle } from "../lib/ui";
import { AiInline, AiText } from "../lib/aitext";
import { call } from "../lib/sopui";
import { fill, useT, type DictKey } from "../lib/i18n";
import { maskText, useMask } from "../lib/mask";

/** 系统设置页：把散落在 `.env` 里的连接配置 + 运行参数搬到网页上，可看可改。
 *
 * 真源就是仓库根的 `.env` 文件，后端每次调用都重新读它——保存了立刻生效，不用重启后端
 * （两个例外：拓扑缓存 TTL、定时任务间隔，后端已经把这两条写进对应字段的说明文字里了）。
 *
 * 敏感字段（密码/密钥/token）这页从来看不到真实值：后端只给一个 `configured` 布尔值，
 * 输入框永远留空，只有真的填了新值才会在保存时覆盖旧值。
 */

type SourceKind = "dotenv" | "os_environ" | "env_override" | "unset";

type SettingItem = {
  key: string;
  label: string;
  group: "connection" | "runtime";
  subgroup: string;
  description: string;
  sensitive: boolean;
  numeric: boolean;
  switch: boolean;
  source: SourceKind;
  configured?: boolean;
  value?: string;
};

type LlmRoute = {
  configured: boolean;
  vendor_label: string;
  model: string;
  has_backup: boolean;
  note: string;
  route_provider?: string;
  route_transport?: string;
  route_warnings?: string[];
  route_error?: string;
};

type Data = { connection: SettingItem[]; runtime: SettingItem[]; llm_route: LlmRoute };

type TestTarget = "zabbix" | "netbox" | "llm";
type TestResult = { busy: boolean; ok?: boolean; message?: string; elapsed_ms?: number };

const CONNECTION_CARDS: { key: string; titleKey: DictKey; icon: string; testTarget?: TestTarget }[] = [
  { key: "zabbix", titleKey: "settings.card.zabbix", icon: PATH.bell, testTarget: "zabbix" },
  { key: "llm", titleKey: "settings.card.llm", icon: PATH.spark, testTarget: "llm" },
  { key: "feishu", titleKey: "settings.card.feishu", icon: PATH.send },
  { key: "device", titleKey: "settings.card.device", icon: PATH.terminal },
];

const RUNTIME_GROUPS: { key: string; titleKey: DictKey; noteKey?: DictKey }[] = [
  { key: "budget", titleKey: "settings.runtime.budget.title", noteKey: "settings.runtime.budget.note" },
  { key: "loop", titleKey: "settings.runtime.loop.title", noteKey: "settings.runtime.loop.note" },
  { key: "chat", titleKey: "settings.runtime.chat.title", noteKey: "settings.runtime.chat.note" },
  { key: "topology", titleKey: "settings.runtime.topology.title", noteKey: "settings.runtime.topology.note" },
  { key: "sop", titleKey: "settings.runtime.sop.title", noteKey: "settings.runtime.sop.note" },
  { key: "schedule", titleKey: "settings.runtime.schedule.title", noteKey: "settings.runtime.schedule.note" },
];

const SOURCE_LABEL: Record<SourceKind, { textKey: DictKey; tone: string }> = {
  dotenv: { textKey: "settings.source.dotenv", tone: "bg-slate-100 text-slate-600 ring-slate-200" },
  os_environ: { textKey: "settings.source.osEnviron", tone: "bg-white text-slate-600 ring-slate-300" },
  env_override: { textKey: "settings.source.envOverride", tone: "bg-amber-50 text-amber-700 ring-amber-200" },
  unset: { textKey: "settings.source.unset", tone: "bg-slate-50 text-slate-400 ring-slate-200" },
};

export default function Settings() {
  const t = useT();
  const [data, setData] = useState<Data | null>(null);
  const [err, setErr] = useState("");
  const [dirty, setDirty] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState("");
  const [toastTone, setToastTone] = useState<"ok" | "err">("ok");
  const [tests, setTests] = useState<Record<TestTarget, TestResult>>({
    zabbix: { busy: false }, netbox: { busy: false }, llm: { busy: false },
  });

  const refresh = useCallback(() => {
    fetch("/api/settings")
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then((d: Data) => { setData(d); setErr(""); })
      .catch(() => setErr(t("settings.loadFailed")));
  }, []);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => { if (!toast) return; const t = setTimeout(() => setToast(""), 6000); return () => clearTimeout(t); }, [toast]);

  const byGroupThenSub = useMemo(() => {
    if (!data) return { connection: {} as Record<string, SettingItem[]>, runtime: {} as Record<string, SettingItem[]> };
    const bucket = (rows: SettingItem[]) => rows.reduce<Record<string, SettingItem[]>>((acc, x) => {
      (acc[x.subgroup] ??= []).push(x); return acc;
    }, {});
    return { connection: bucket(data.connection), runtime: bucket(data.runtime) };
  }, [data]);

  const setField = (key: string, value: string) => setDirty((d) => ({ ...d, [key]: value }));

  const save = async () => {
    if (Object.keys(dirty).length === 0) { setToast(t("settings.noChanges")); setToastTone("ok"); return; }
    setSaving(true);
    const { ok, data: resp } = await call<{ ok: boolean; message?: string; rejected?: { key: string; reason: string }[] }>(
      "POST", "/api/settings", { updates: dirty },
    );
    setSaving(false);
    if (ok && resp.ok) {
      setToast(fill(t("settings.savedTemplate"), { n: Object.keys(dirty).length }));
      setToastTone("ok");
      setDirty({});
      refresh();
    } else {
      const reasons = (resp.rejected || []).map((r) => `${r.key}：${r.reason}`).join("；");
      setToast(reasons || resp.message || t("settings.saveFailed"));
      setToastTone("err");
    }
  };

  const runTest = async (target: TestTarget) => {
    setTests((t) => ({ ...t, [target]: { busy: true } }));
    const { data: resp } = await call<{ ok: boolean; message: string; elapsed_ms: number }>("POST", "/api/settings/test", { target });
    setTests((t) => ({ ...t, [target]: { busy: false, ok: resp.ok, message: resp.message, elapsed_ms: resp.elapsed_ms } }));
  };

  if (err) return <Card><Empty title={err} /></Card>;
  if (!data) return <Loading />;

  return (
    <div className="mx-auto max-w-[1280px] space-y-5 pb-20">
      <div className="rounded-[3px] border border-amber-200 border-l-2 border-l-amber-500 bg-amber-50 px-4 py-2.5 text-[13px] text-amber-900">
        <b>{t("settings.banner")}</b>{t("settings.bannerSep")}{t("settings.bannerSuffix")}
      </div>

      {toast && (
        <div className={cn("rounded-[3px] px-4 py-2 text-sm ring-1", toastTone === "ok" ? "bg-emerald-50 text-emerald-700 ring-emerald-200" : "bg-rose-50 text-rose-700 ring-rose-200")}>
          {toast}
        </div>
      )}

      <section>
        <SectionTitle title={t("settings.connection.title")} note={t("settings.connection.note")} />
        <div className="grid grid-cols-2 gap-4">
          {CONNECTION_CARDS.map((c) => (
            <ConnCard key={c.key} title={t(c.titleKey)} icon={c.icon} items={byGroupThenSub.connection[c.key] || []}
              dirty={dirty} onChange={setField}
              testTarget={c.testTarget} testResult={c.testTarget ? tests[c.testTarget] : undefined}
              onTest={c.testTarget ? () => runTest(c.testTarget!) : undefined}
              llmRoute={c.key === "llm" ? data.llm_route : undefined} />
          ))}
        </div>
      </section>

      <section>
        <SectionTitle title={t("settings.runtime.title")} note={t("settings.runtime.note")} />
        <div className="grid grid-cols-2 gap-4">
          {RUNTIME_GROUPS.map((g) => {
            const items = byGroupThenSub.runtime[g.key] || [];
            if (items.length === 0) return null;
            return (
              <Card key={g.key} className="overflow-hidden">
                <CardHead title={t(g.titleKey)} note={g.noteKey ? t(g.noteKey) : undefined} />
                <div className="divide-y divide-line">
                  {items.map((x) => <RuntimeRow key={x.key} item={x} dirty={dirty} onChange={setField} />)}
                </div>
              </Card>
            );
          })}
        </div>
      </section>

      <div className="fixed right-0 bottom-0 left-52 z-10 border-t border-line bg-white px-6 py-2.5">
        <div className="mx-auto flex max-w-[1280px] items-center gap-3">
          <span className="text-xs text-dim">
            {Object.keys(dirty).length === 0 ? t("settings.noUnsaved") : fill(t("settings.unsavedTemplate"), { n: Object.keys(dirty).length })}
          </span>
          <button onClick={save} disabled={saving || Object.keys(dirty).length === 0}
            className="ml-auto rounded-[3px] bg-brand px-4 py-1.5 text-[13px] font-medium text-white transition hover:bg-[#0b4a63] disabled:cursor-not-allowed disabled:opacity-40">
            {saving ? t("settings.saving") : t("settings.save")}
          </button>
        </div>
      </div>
    </div>
  );
}

function SourceBadge({ source }: { source: SourceKind }) {
  const t = useT();
  const s = SOURCE_LABEL[source];
  return <Badge className={s.tone} title={t("settings.source.title")}>{t(s.textKey)}</Badge>;
}

function ConnCard({ title, icon, items, dirty, onChange, testTarget, testResult, onTest, llmRoute }: {
  title: string; icon: string; items: SettingItem[]; dirty: Record<string, string>;
  onChange: (key: string, value: string) => void;
  testTarget?: TestTarget; testResult?: TestResult; onTest?: () => void; llmRoute?: LlmRoute;
}) {
  const t = useT();
  if (items.length === 0) return null;
  const feishu = title === t("settings.card.feishu");
  const configuredCount = items.filter((x) => (x.sensitive ? x.configured : !!x.value)).length;
  return (
    <Card className="overflow-hidden">
      <CardHead title={title} icon={<Icon d={icon} className="text-slate-500" />}
        note={feishu ? fill(t("settings.configuredRatioTemplate"), { done: configuredCount, total: items.length }) : undefined}
        right={onTest && (
          <button onClick={onTest} disabled={testResult?.busy}
            className="rounded-[3px] border border-[#c3cad2] bg-white px-2 py-[3px] text-xs font-medium text-slate-700 transition hover:border-brand hover:text-brand disabled:opacity-50">
            {testResult?.busy ? t("settings.testing") : t("settings.testConnection")}
          </button>
        )} />
      {testTarget && testResult && testResult.ok !== undefined && (
        <div className={cn("px-5 py-2 text-xs", testResult.ok ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-700")}>
          {testResult.ok ? "✓" : "✗"} {testResult.message}（{testResult.elapsed_ms} ms）
        </div>
      )}
      {feishu && (
        <div className="px-5 pt-3 text-xs text-dim">
          {t("settings.feishuNoTest")}
        </div>
      )}
      {llmRoute && llmRoute.configured && (
        <div className="mx-5 mt-3 rounded-[3px] border border-line border-l-2 border-l-brand bg-slate-50 px-3 py-2 text-xs text-slate-800">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium">{t("settings.llmRoute.vendorLabel")}{llmRoute.vendor_label}</span>
            <span className="text-slate-400">·</span>
            <span>{t("settings.llmRoute.modelLabel")} {llmRoute.model}</span>
            {llmRoute.route_transport && (
              <Badge className="bg-white text-slate-600 ring-slate-300">{llmRoute.route_transport}</Badge>
            )}
          </div>
          <div className="mt-1 text-slate-600">
            {fill(t("settings.llmRoute.noBackup"), { note: llmRoute.note })}
          </div>
          {llmRoute.route_warnings && llmRoute.route_warnings.length > 0 && (
            <div className="mt-1 text-amber-700">⚠ {llmRoute.route_warnings.join("；")}</div>
          )}
        </div>
      )}
      {llmRoute && !llmRoute.configured && (
        <div className="mx-5 mt-3 rounded-[3px] bg-slate-50 px-3 py-2 text-xs text-dim ring-1 ring-slate-100">
          {t("settings.llmRoute.notConfigured")}
        </div>
      )}
      <div className="divide-y divide-line">
        {items.map((x) => <FieldRow key={x.key} item={x} dirty={dirty} onChange={onChange} />)}
      </div>
    </Card>
  );
}

function FieldRow({ item, dirty, onChange }: { item: SettingItem; dirty: Record<string, string>; onChange: (key: string, value: string) => void }) {
  const t = useT();
  const touched = item.key in dirty;
  const displayValue = touched ? dirty[item.key] : (item.value ?? "");
  const masked = useMask();
  const shown = masked ? maskText(displayValue) : displayValue;
  return (
    <div className="px-4 py-2.5">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-slate-700"><AiText text={item.label} lowPriority /></span>
        <code className="font-mono text-[11px] text-dim">{item.key}</code>
        {item.sensitive && !item.configured && <Badge className="bg-slate-50 text-slate-400 ring-slate-200">{t("settings.field.notConfigured")}</Badge>}
        {touched && <Badge className="bg-brand/10 text-brand ring-brand/20">{t("settings.field.pendingSave")}</Badge>}
      </div>
      <p className="mt-0.5 text-xs leading-relaxed text-dim"><AiInline text={item.description} lowPriority /></p>
      <div className="mt-1.5 flex items-center gap-2">
        {item.sensitive ? (
          <input type="password" value={displayValue} onChange={(e) => onChange(item.key, e.target.value)}
            placeholder={item.configured ? t("settings.field.sensitivePlaceholderConfigured") : t("settings.field.sensitivePlaceholderUnset")}
            className="w-full rounded-[3px] border border-[#c3cad2] bg-white px-3 py-1.5 text-sm outline-none transition focus:border-brand focus:ring-1 focus:ring-brand/30" />
        ) : (
          <input value={shown} readOnly={masked} onChange={(e) => onChange(item.key, e.target.value)}
            className="w-full rounded-[3px] border border-[#c3cad2] bg-white px-3 py-1.5 text-sm outline-none transition focus:border-brand focus:ring-1 focus:ring-brand/30" />
        )}
      </div>
    </div>
  );
}

function RuntimeRow({ item, dirty, onChange }: { item: SettingItem; dirty: Record<string, string>; onChange: (key: string, value: string) => void }) {
  const t = useT();
  const touched = item.key in dirty;
  const displayValue = touched ? dirty[item.key] : (item.value ?? "");
  const masked = useMask();
  const shown = masked ? maskText(displayValue) : displayValue;
  return (
    <div className="grid grid-cols-[1fr_auto] items-start gap-3 px-4 py-2.5">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-slate-700"><AiText text={item.label} lowPriority /></span>
          <code className="font-mono text-[11px] text-dim">{item.key}</code>
          <SourceBadge source={item.source} />
          {touched && <Badge className="bg-brand/10 text-brand ring-brand/20">{t("settings.field.pendingSave")}</Badge>}
        </div>
        <p className="mt-0.5 text-xs leading-relaxed text-dim"><AiInline text={item.description} lowPriority /></p>
      </div>
      <div className="w-40 shrink-0">
        {item.switch ? (
          <select value={displayValue} onChange={(e) => onChange(item.key, e.target.value)}
            className="w-full rounded-[3px] border border-[#c3cad2] bg-white px-2 py-1.5 text-sm outline-none focus:border-brand focus:ring-1 focus:ring-brand/30">
            <option value="">{t("settings.field.useCodeDefault")}</option>
            <option value="on">on</option>
            <option value="off">off</option>
          </select>
        ) : (
          <input type={item.numeric ? "number" : "text"} value={shown} readOnly={masked} onChange={(e) => onChange(item.key, e.target.value)}
            placeholder={item.numeric ? t("settings.field.numericPlaceholder") : ""}
            className="w-full rounded-[3px] border border-[#c3cad2] bg-white px-2 py-1.5 text-sm outline-none focus:border-brand focus:ring-1 focus:ring-brand/30" />
        )}
      </div>
    </div>
  );
}
