import { useCallback, useEffect, useState } from "react";
import { cn, ts } from "../lib/api";
import { Btn } from "../lib/sopui";
import { Badge, Card, CardHead, Empty, Loading } from "../lib/ui";
import { fill, useT, type DictKey } from "../lib/i18n";

type Draft = {
  id: string; created_at: string; channel: "email" | "feishu"; kind: string; to: string[]; subject: string; body: string;
  attachments: string[]; status: "pending" | "sent" | "discarded" | "failed"; approved_by: string; sent_at: string; error: string;
};
type Filter = "pending" | "sent" | "failed" | "discarded" | "all";

const STATUS_KEY: Record<Draft["status"], DictKey> = {
  pending: "rep.status.pending", sent: "rep.status.sent", discarded: "rep.status.discarded", failed: "rep.status.failed",
};
const STATUS_TONE: Record<Draft["status"], string> = {
  pending: "bg-amber-50 text-amber-700 ring-amber-200", sent: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  discarded: "bg-slate-100 text-slate-500 ring-slate-200", failed: "bg-rose-50 text-rose-700 ring-rose-200",
};
const FILTERS: [Filter, DictKey][] = [
  ["pending", "rep.status.pending"], ["sent", "rep.status.sent"], ["failed", "rep.status.failed"], ["discarded", "rep.status.discarded"], ["all", "rep.filter.all"],
];
const KINDS: [string, DictKey][] = [["daily", "rep.kind.daily"], ["weekly", "rep.kind.weekly"], ["monthly", "rep.kind.monthly"]];

const linkCls = "inline-flex h-8 items-center rounded-[8px] bg-white px-3 text-[0.8125rem] font-medium text-slate-700 ring-1 ring-inset ring-[#c3cad2] transition hover:bg-slate-50 hover:ring-slate-400";
const inputCls = "h-8 rounded-[8px] border border-line bg-white px-2.5 text-[0.8125rem] text-slate-800 outline-none focus:border-slate-400";

async function call(method: string, url: string, body?: unknown): Promise<{ ok: boolean; data: any }> {
  try {
    const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
    return { ok: r.ok, data: await r.json().catch(() => ({})) };
  } catch {
    return { ok: false, data: { message: "network error" } };
  }
}

export default function Reports() {
  const t = useT();
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [filter, setFilter] = useState<Filter>("pending");
  const [open, setOpen] = useState<string | null>(null);
  const [approver, setApprover] = useState(() => localStorage.getItem("netops.approver") || "");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [kind, setKind] = useState("weekly");
  const [channel, setChannel] = useState<"email" | "feishu">("email");
  const [to, setTo] = useState("");

  const refresh = useCallback(async () => {
    const r = await call("GET", "/api/outbox");
    setDrafts(r.ok ? (r.data.drafts as Draft[]) : []);
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  const createDraft = async () => {
    setBusy("create"); setMsg(null);
    const r = await call("POST", `/api/reports/traffic/${kind}/draft`, { channel, to: to.split(/[,，;；\s]+/).filter(Boolean) });
    setBusy(null);
    setMsg({ ok: r.ok, text: r.ok ? fill(t("rep.created"), { subject: r.data.subject }) : String(r.data.message || r.data.detail || "failed") });
    if (r.ok) { setFilter("pending"); void refresh(); }
  };
  const act = async (d: Draft, action: "approve" | "discard") => {
    if (action === "approve" && !approver.trim()) { setMsg({ ok: false, text: t("rep.needApprover") }); return; }
    if (action === "approve" && !window.confirm(fill(t("rep.confirmSend"), { to: d.to.join(", ") || d.channel }))) return;
    localStorage.setItem("netops.approver", approver.trim());
    setBusy(d.id); setMsg(null);
    const r = await call("POST", `/api/outbox/${d.id}/${action}`, { approver: approver.trim() });
    setBusy(null);
    setMsg({ ok: r.ok, text: r.ok ? t(action === "approve" ? "rep.sentOk" : "rep.discardedOk") : String(r.data.message || r.data.error || "failed") });
    void refresh();
  };

  const rows = (drafts ?? []).filter((d) => filter === "all" || d.status === filter);
  const count = (f: Filter) => (drafts ?? []).filter((d) => f === "all" || d.status === f).length;

  return (
    <div className="space-y-4">
      <Card>
        <CardHead title={t("rep.reports.title")} />
        <div className="divide-y divide-line">
          <div className="flex flex-wrap items-center gap-3 px-5 py-3.5">
            <div className="w-40 text-sm font-medium text-slate-900">{t("rep.traffic.title")}</div>
            <div className="flex flex-wrap gap-2">
              {KINDS.map(([k, label]) => <a key={k} className={linkCls} href={`/api/reports/traffic/${k}`}>{t(label)} PDF</a>)}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3 px-5 py-3.5">
            <div className="w-40 text-sm font-medium text-slate-900">{t("rep.cve.title")}</div>
            <a className={linkCls} href="/api/reports/cve" target="_blank" rel="noreferrer">{t("rep.open")}</a>
            <span className="text-xs text-dim">{t("rep.slow")}</span>
          </div>
          <div className="flex flex-wrap items-center gap-3 px-5 py-3.5">
            <div className="w-40 text-sm font-medium text-slate-900">{t("rep.topo.title")}</div>
            <a className={linkCls} href="/api/reports/topology" target="_blank" rel="noreferrer">{t("rep.open")}</a>
            <span className="text-xs text-dim">{t("rep.slow")}</span>
          </div>
        </div>
      </Card>

      <Card>
        <CardHead title={t("rep.summary.title")} />
        <div className="flex flex-wrap items-center gap-2 px-5 py-4">
          <select className={inputCls} value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map(([k, label]) => <option key={k} value={k}>{t(label)}</option>)}
          </select>
          <select className={inputCls} value={channel} onChange={(e) => setChannel(e.target.value as "email" | "feishu")}>
            <option value="email">{t("rep.channel.email")}</option>
            <option value="feishu">{t("rep.channel.feishu")}</option>
          </select>
          {channel === "email" && (
            <input className={cn(inputCls, "w-72")} value={to} onChange={(e) => setTo(e.target.value)} placeholder={t("rep.to.placeholder")} />
          )}
          <Btn tone="brand" busy={busy === "create"} disabled={busy !== null || (channel === "email" && !to.trim())} onClick={createDraft}>{t("rep.create")}</Btn>
        </div>
      </Card>

      <Card>
        <CardHead title={t("rep.outbox.title")} right={
          <div className="flex gap-1">
            {FILTERS.map(([f, label]) => (
              <button key={f} type="button" onClick={() => setFilter(f)}
                className={cn("h-7 rounded-[6px] px-2.5 text-xs transition", filter === f ? "bg-slate-900 font-medium text-white" : "text-slate-600 hover:bg-slate-100")}>
                {t(label)} <span className="num opacity-70">{count(f)}</span>
              </button>
            ))}
          </div>
        } />
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-3">
          <label className="text-xs text-slate-500" htmlFor="approver">{t("rep.approver")}</label>
          <input id="approver" className={cn(inputCls, "w-40")} value={approver} onChange={(e) => setApprover(e.target.value)} />
          {msg && <span className={cn("text-[0.8125rem]", msg.ok ? "text-emerald-600" : "text-rose-600")}>{msg.text}</span>}
        </div>
        {drafts === null ? <div className="p-5"><Loading /></div> : rows.length === 0 ? <Empty title={t("rep.empty")} /> : (
          <ul className="divide-y divide-line">
            {rows.map((d) => (
              <li key={d.id} className="px-5 py-3.5">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <button type="button" className="min-w-0 text-left text-sm font-medium text-slate-900 hover:underline" onClick={() => setOpen(open === d.id ? null : d.id)}>
                    {d.subject}
                  </button>
                  <Badge className={STATUS_TONE[d.status]}>{t(STATUS_KEY[d.status])}</Badge>
                  <span className="text-xs text-dim">{d.channel === "email" ? t("rep.channel.email") : t("rep.channel.feishu")}{d.to.length > 0 && ` → ${d.to.join(", ")}`}</span>
                  <span className="num ml-auto text-xs text-dim">{ts(Date.parse(d.created_at) / 1000)}</span>
                </div>
                {d.status === "sent" && <div className="mt-1 text-xs text-dim">{fill(t("rep.sentBy"), { who: d.approved_by })}</div>}
                {d.error && <div className="mt-1 text-xs text-rose-600">{d.error}</div>}
                {open === d.id && (
                  <div className="mt-3 space-y-2">
                    <pre className="max-h-72 overflow-auto rounded-[8px] bg-slate-50 px-4 py-3 text-[0.8125rem] leading-relaxed whitespace-pre-wrap text-slate-800">{d.body}</pre>
                    {d.attachments.length > 0 && <div className="text-xs text-dim">{t("rep.attachments")}：{d.attachments.join("、")}</div>}
                  </div>
                )}
                {(d.status === "pending" || d.status === "failed") && (
                  <div className="mt-3 flex gap-2">
                    <Btn tone="brand" small busy={busy === d.id} disabled={busy !== null} onClick={() => act(d, "approve")}>{t(d.status === "failed" ? "rep.retry" : "rep.approve")}</Btn>
                    {d.status === "pending" && <Btn small disabled={busy !== null} onClick={() => act(d, "discard")}>{t("rep.discard")}</Btn>}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
