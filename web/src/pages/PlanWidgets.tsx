import { useState, type FormEvent, type ReactNode } from "react";
import { cn } from "../lib/api";
import { Badge, Icon, PATH } from "../lib/ui";
import { fill, useT, type DictKey } from "../lib/i18n";
import { Btn } from "../lib/sopui";

/** 对话气泡里的交互式选择（设备范围 / 周期 / 检查项）和提示泡。
 *  选择提交后由后端确定性地写进草案（不调模型）；只有最新的那个气泡能操作，旧的变成只读摘要。
 *  键盘：Tab 在选项间移动，空格勾选，Enter 提交（每个气泡都是一个 form）。 */

export type PickerDevice = { name: string; role: string; host: string; neighbors: number };
export type Widget = {
  id: string; type: "device_picker" | "schedule_picker" | "check_picker"; prompt?: string;
  groups?: { role: string; label: string; devices: PickerDevice[] }[];
  selected?: string[] | { every_minutes?: number; daily_at?: string };
  options?: number[]; daily?: boolean; hint?: string;
  items?: { id: string; title: string; what: string; command: string; kind: string; roles: string[] }[];
  recommended?: string[];
};
export type Hint = { id: string; text: string };
export type Selection = { widget_id: string; type: Widget["type"]; value: Record<string, unknown> };

const box = "rounded-[3px] border border-line bg-white";

/** 气泡外框：标题一行 + 内容。 */
function Frame({ title, children, testid }: { title: string; children: ReactNode; testid: string }) {
  return (
    <div className={cn(box, "mt-2")} data-testid={testid}>
      <div className="flex items-center gap-2 border-b border-line bg-[#f6f7f9] px-3 py-1.5 text-xs font-semibold text-slate-700">
        <Icon d={PATH.list} className="h-3.5 w-3.5 text-slate-500" />{title}
      </div>
      {children}
    </div>
  );
}

export function DevicePicker({ w, onSubmit }: { w: Widget; onSubmit: (s: Selection, summary: string) => void }) {
  const t = useT();
  const groups = w.groups ?? [];
  const [sel, setSel] = useState<Set<string>>(new Set((w.selected as string[]) ?? []));
  const toggle = (n: string) => setSel((s) => { const x = new Set(s); if (x.has(n)) x.delete(n); else x.add(n); return x; });
  const addGroup = (names: string[]) => setSel((s) => new Set([...s, ...names]));
  const clearGroup = (names: string[]) => setSel((s) => new Set([...s].filter((n) => !names.includes(n))));
  const all = groups.flatMap((g) => g.devices.map((d) => d.name));
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (!sel.size) return;
    onSubmit({ widget_id: w.id, type: "device_picker", value: { devices: all.filter((n) => sel.has(n)) } }, deviceSummary(t, groups, sel));
  };
  return (
    <Frame title={t("plans.widget.devices.title")} testid="widget-device">
      <form onSubmit={submit} className="space-y-2 px-3 py-2">
        <div className="flex flex-wrap gap-1.5" data-testid="device-quick">
          {groups.map((g) => (
            <button type="button" key={g.role} onClick={() => addGroup(g.devices.map((d) => d.name))}
              className="rounded-[2px] border border-line px-2 py-[2px] text-xs text-slate-700 transition hover:border-brand hover:text-brand focus-visible:outline focus-visible:outline-brand">
              {fill(t("plans.widget.allOf"), { group: g.label })}
            </button>
          ))}
          <button type="button" onClick={() => addGroup(all)}
            className="rounded-[2px] border border-line px-2 py-[2px] text-xs text-slate-700 transition hover:border-brand hover:text-brand focus-visible:outline focus-visible:outline-brand">
            {t("plans.widget.allDevices")}
          </button>
        </div>
        {groups.map((g) => {
          const names = g.devices.map((d) => d.name);
          const n = names.filter((x) => sel.has(x)).length;
          return (
            <fieldset key={g.role} className="border-t border-line pt-1.5">
              <legend className="sr-only">{g.label}</legend>
              <div className="mb-1 flex items-center gap-2 text-xs">
                <span className="font-semibold text-slate-700">{g.label}</span>
                <span className="num text-dim">{n}/{names.length}</span>
                <button type="button" onClick={() => addGroup(names)} className="ml-auto text-brand hover:underline">{t("plans.widget.selectAll")}</button>
                <button type="button" onClick={() => clearGroup(names)} className="text-slate-500 hover:underline">{t("plans.widget.clear")}</button>
              </div>
              <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-1">
                {g.devices.map((d) => (
                  <label key={d.name} className={cn("flex cursor-pointer items-center gap-2 rounded-[2px] border px-2 py-1 text-xs transition",
                    sel.has(d.name) ? "border-brand/60 bg-steel-50" : "border-line hover:border-slate-400")}>
                    <input type="checkbox" checked={sel.has(d.name)} onChange={() => toggle(d.name)} className="accent-[#0f5c7a]"
                      data-testid={`pick-${d.name}`} />
                    <span className="min-w-0">
                      <span className="block font-mono text-[12px] font-semibold text-slate-800">{d.name}</span>
                      <span className="block truncate text-[11px] text-dim">
                        {fill(t("plans.widget.neighbors"), { n: d.neighbors })} · <span className="num">{d.host}</span>
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>
          );
        })}
        <div className="flex items-center gap-3 border-t border-line pt-2">
          <span className="text-xs text-dim">{fill(t("plans.widget.selectedCount"), { n: sel.size })}</span>
          <span className="ml-auto"><Btn small tone="brand" disabled={!sel.size}>{t("plans.widget.confirmDevices")}</Btn></span>
        </div>
      </form>
    </Frame>
  );
}

export function deviceSummary(t: (k: DictKey) => string, groups: NonNullable<Widget["groups"]>, sel: Set<string>) {
  const parts = groups.map((g) => [g.label, g.devices.filter((d) => sel.has(d.name)).length] as const).filter(([, n]) => n > 0);
  return t("plans.widget.selectedPrefix") + parts.map(([l, n]) => `${l} ${n}`).join(t("plans.widget.sep"));
}

export function ScheduleChips({ w, onSubmit, label }: { w: Widget; onSubmit: (s: Selection, summary: string) => void; label: (s: { every_minutes?: number; daily_at?: string }) => string }) {
  const t = useT();
  const cur = (w.selected ?? {}) as { every_minutes?: number; daily_at?: string };
  const [daily, setDaily] = useState(!!cur.daily_at);
  const [time, setTime] = useState(cur.daily_at || "08:00");
  const go = (v: { every_minutes?: number; daily_at?: string }) =>
    onSubmit({ widget_id: w.id, type: "schedule_picker", value: v }, t("plans.widget.schedulePrefix") + label(v));
  const chip = (on: boolean) => cn("rounded-[2px] border px-2.5 py-1 text-xs font-medium transition focus-visible:outline focus-visible:outline-brand",
    on ? "border-brand bg-brand text-white" : "border-line bg-white text-slate-700 hover:border-brand hover:text-brand");
  return (
    <Frame title={t("plans.widget.schedule.title")} testid="widget-schedule">
      <form onSubmit={(e) => { e.preventDefault(); if (daily) go({ daily_at: time }); }} className="space-y-2 px-3 py-2">
        <div className="flex flex-wrap items-center gap-1.5" role="group">
          {(w.options ?? [15, 60, 360]).map((m) => (
            <button type="button" key={m} aria-pressed={cur.every_minutes === m} className={chip(cur.every_minutes === m)}
              onClick={() => go({ every_minutes: m })} data-testid={`chip-${m}`}>{label({ every_minutes: m })}</button>
          ))}
          {w.daily !== false && (
            <button type="button" aria-pressed={daily} className={chip(daily)} onClick={() => setDaily(!daily)} data-testid="chip-daily">
              {t("plans.sched.daily")}
            </button>
          )}
          {daily && (
            <>
              <input type="time" value={time} onChange={(e) => setTime(e.target.value)} aria-label={t("plans.sched.daily")}
                className="rounded-[3px] border border-line px-1.5 py-[3px] font-mono text-[12px] focus:border-brand focus:outline-none" />
              <Btn small tone="brand" disabled={!time}>{t("plans.widget.confirmDaily")}</Btn>
            </>
          )}
        </div>
        {w.hint && <div className="border-l-2 border-l-brand/40 pl-2 text-[11.5px] leading-relaxed text-slate-600">{w.hint}</div>}
      </form>
    </Frame>
  );
}

export function CheckPicker({ w, onSubmit }: { w: Widget; onSubmit: (s: Selection, summary: string) => void }) {
  const t = useT();
  const items = w.items ?? [];
  const [sel, setSel] = useState<Set<string>>(new Set((w.selected as string[]) ?? []));
  const [open, setOpen] = useState<string>("");
  const rec = new Set(w.recommended ?? []);
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (!sel.size) return;
    const ids = items.map((i) => i.id).filter((id) => sel.has(id));
    onSubmit({ widget_id: w.id, type: "check_picker", value: { templates: ids } },
      t("plans.widget.checksPrefix") + items.filter((i) => sel.has(i.id)).map((i) => i.title).join(t("plans.widget.sep")));
  };
  return (
    <Frame title={t("plans.widget.checks.title")} testid="widget-checks">
      <form onSubmit={submit} className="px-3 py-2">
        <div className="divide-y divide-line">
          {items.map((it) => (
            <div key={it.id} className="py-1.5">
              <label className="flex cursor-pointer items-start gap-2">
                <input type="checkbox" className="mt-[3px] accent-[#0f5c7a]" checked={sel.has(it.id)} data-testid={`check-${it.id}`}
                  onChange={() => setSel((s) => { const x = new Set(s); if (x.has(it.id)) x.delete(it.id); else x.add(it.id); return x; })} />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-1.5 text-[13px] font-medium text-slate-800">
                    {it.title}
                    {rec.has(it.id) && <Badge className="bg-white text-brand ring-brand/40">{t("plans.widget.recommended")}</Badge>}
                  </span>
                  <span className="block text-xs leading-relaxed text-slate-600">{it.what}</span>
                </span>
                <button type="button" onClick={(e) => { e.preventDefault(); setOpen(open === it.id ? "" : it.id); }} aria-expanded={open === it.id}
                  className="shrink-0 text-[11px] text-slate-500 hover:text-brand">{t("plans.widget.showCommand")}</button>
              </label>
              {open === it.id && <div className="term mt-1 ml-6 overflow-x-auto px-2 py-1 text-[11.5px] whitespace-pre">{it.command}</div>}
            </div>
          ))}
        </div>
        <div className="flex items-center gap-3 border-t border-line pt-2">
          <span className="text-xs text-dim">{fill(t("plans.widget.checkCount"), { n: sel.size })}</span>
          <span className="ml-auto"><Btn small tone="brand" disabled={!sel.size}>{t("plans.widget.confirmChecks")}</Btn></span>
        </div>
      </form>
    </Frame>
  );
}

/** 已提交 / 已过期的气泡：只读的一行摘要，提交过的带「修改」（重新打开一个新的）。 */
export function WidgetSummary({ text, expired, onEdit }: { text: string; expired?: boolean; onEdit?: () => void }) {
  const t = useT();
  return (
    <div className={cn(box, "mt-2 flex items-center gap-2 px-3 py-1.5 text-xs", expired ? "text-dim" : "text-slate-700")} data-testid="widget-summary">
      <Icon d={expired ? PATH.clock : PATH.check} className={cn("h-3.5 w-3.5", expired ? "text-slate-400" : "text-ok")} />
      <span className="min-w-0 flex-1">{expired ? t("plans.widget.expired") : text}</span>
      {onEdit && <button type="button" onClick={onEdit} className="shrink-0 text-brand hover:underline">{t("plans.widget.edit")}</button>}
    </div>
  );
}

/** 提示泡：比建议卡轻（浅灰底、小字、左侧细色条），不是按钮，可以关掉。 */
export function Hints({ hints, onClose }: { hints: Hint[]; onClose: (id: string) => void }) {
  const t = useT();
  if (!hints.length) return null;
  return (
    <div className="mt-1.5 space-y-1" data-testid="plan-hints">
      {hints.map((h) => (
        <div key={h.id} className="flex items-start gap-2 border-l-2 border-l-slate-400 bg-[#f1f3f5] px-2.5 py-1 text-[11.5px] leading-relaxed text-slate-600">
          <span className="min-w-0 flex-1">{h.text}</span>
          <button type="button" aria-label={t("plans.hint.close")} title={t("plans.hint.close")} onClick={() => onClose(h.id)}
            className="shrink-0 px-0.5 leading-none text-slate-400 hover:text-slate-700">×</button>
        </div>
      ))}
    </div>
  );
}
