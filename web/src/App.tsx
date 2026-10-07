import { Component, useEffect, useMemo, useState, type FC, type ReactNode } from "react";
import { cn, load } from "./lib/api";
import Overview from "./pages/Overview";
import Incidents from "./pages/Incidents";
import Inspection from "./pages/Inspection";
import Topology from "./pages/Topology";
import Audit from "./pages/Audit";
import Settings from "./pages/Settings";
import { setMask, useMask } from "./lib/mask";
import { LangContext, fill, loadStoredLang, storeLang, translate, useLang, useSetLang, useT, type DictKey, type Lang } from "./lib/i18n";
import { AuthGate, UserMenu, memberNavItems, useMember } from "./member";

type Item = { key: string; labelKey: DictKey; descKey: DictKey; page: FC };
type Status = { items: { key: string; label: string; ok: boolean; note?: string }[] };

/** `/api/status` 的 4 项 `label` 是后端拼的中文（`dashboard.py::build_status`），但每项
 *  只有 1~2 种固定形状（外加 llm/schedule 两项嵌了模型名/分钟数），不是 AI 自由文本，
 *  没必要为它专门调翻译接口——按 `key` + 原文形状精确匹配重新拼一遍。`note`（hover 提示）
 *  是各任务执行结果拼接的自由文本，这里不处理，原样显示。翻不出的形状原样透传，不会崩。 */
function statusLabel(t: (k: DictKey) => string, key: string, label: string): string {
  if (key === "backend") return t("status.backend.running");
  if (key === "topology") return label.includes("NetBox") ? t("status.topology.netbox") : t("status.topology.local");
  if (key === "llm") {
    if (label === "模型没配") return t("status.llm.notConfigured");
    return fill(t("status.llm.modelTemplate"), { model: label.replace(/^模型\s*/, "") });
  }
  if (key === "schedule") {
    if (label === "定时任务未开") return t("status.schedule.off");
    const m = label.match(/(\d+)/);
    if (m) return fill(t("status.schedule.everyMinutesTemplate"), { n: m[1] });
  }
  return label;
}
/** 每项一个图标。**不引图标库**：七个 path 比一个依赖便宜，
 * 而且 YESLAB 那套用的是 emoji，我们用线性图标反而更像产品。 */
const ICON: Record<string, string> = {
  overview: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6V11h-6v9Zm0-16v5h6V4h-6Z",
  incidents: "M12 3 2 20h20L12 3Zm0 5v6m0 3v1",
  chat: "M4 5h16v11H8l-4 3V5Z",
  playbooks: "M5 4h10l4 4v12H5V4Zm10 0v4h4M8 12h8M8 16h5",
  inspection: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 4 4",
  topology: "M12 3v5M6 21v-5m12 5v-5M4 16h4v5H4v-5Zm12 0h4v5h-4v-5ZM10 8h4v5h-4V8Z",
  audit: "M5 4h14v16H5V4Zm3 12v-3m4 3V9m4 7v-5",
  cost: "M12 3v18m4-14H10a3 3 0 0 0 0 6h4a3 3 0 0 1 0 6H8",
  users: "M16 11a3 3 0 1 0-6 0 3 3 0 0 0 6 0ZM5 20a7 7 0 0 1 14 0M19 8v4m2-2h-4",
  settings: "M12 3v3m0 12v3m9-9h-3M6 12H3m14.5-6.5-2 2m-9 9-2 2m0-13 2 2m9 9 2 2M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z",
};

/** 侧边栏图标统一单色线条，不再按类型上色：彩色图标是装饰，
 *  真正"出事了要看"的信号由页面里的状态色（红 / 橙 / 绿）承担。 */

const NAV: { groupKey: DictKey; items: Item[] }[] = [
  { groupKey: "nav.group.home", items: [{ key: "overview", labelKey: "nav.overview", descKey: "nav.overview.desc", page: Overview }] },
  { groupKey: "nav.group.ops", items: [
    { key: "incidents", labelKey: "nav.incidents", descKey: "nav.incidents.desc", page: Incidents },
    { key: "inspection", labelKey: "nav.inspection", descKey: "nav.inspection.desc", page: Inspection },
  ]},
  { groupKey: "nav.group.assets", items: [
    { key: "topology", labelKey: "nav.topology", descKey: "nav.topology.desc", page: Topology },
  ] },
  { groupKey: "nav.group.admin", items: [
    { key: "audit", labelKey: "nav.audit", descKey: "nav.audit.desc", page: Audit },
    { key: "settings", labelKey: "nav.settings", descKey: "nav.settings.desc", page: Settings },
  ] },
];

function Shell() {
  const [status, setStatus] = useState<Status | null>(null);
  const [clock, setClock] = useState(() => new Date().toLocaleTimeString("zh-CN", { hour12: false }));
  useEffect(() => { load<Status>("status", "/api/status").then(setStatus).catch(() => {}); }, []);
  useEffect(() => {
    const t = setInterval(() => setClock(new Date().toLocaleTimeString("zh-CN", { hour12: false })), 1000);
    return () => clearInterval(t);
  }, []);
  // hash 可以带参数（`#knowledge?q=…`），选页只看问号前面那段
  const [tab, setTab] = useState(location.hash.slice(1).split("?")[0] || "overview");
  const mask = useMask();
  useEffect(() => {
    const on = (e: Event) => { const k = (e as CustomEvent<string>).detail; setTab(k.split("?")[0]); location.hash = k; };
    window.addEventListener("netops:nav", on);
    return () => window.removeEventListener("netops:nav", on);
  }, []);
  const member = useMember();
  // 扩展入口注入的页面：按 `group` 放进对应分组，`after` 指定排在哪一项后面。
  // 公开版的入口是直通桩，返回空数组，侧边栏就只有上面 NAV 里写死的那些。
  const nav = useMemo(() => {
    const groups = NAV.map((g) => ({ ...g, items: [...g.items] }));
    for (const extra of memberNavItems(member.user)) {
      const g = groups.find((x) => x.groupKey === extra.group) ?? groups[groups.length - 1];
      const at = g.items.findIndex((x) => x.key === extra.after);
      g.items.splice(at >= 0 ? at + 1 : g.items.length, 0, extra);
    }
    return groups;
  }, [member.user]);
  const all = nav.flatMap((g) => g.items);
  const cur = all.find((n) => n.key === tab) ?? all[0];
  const Page = cur.page;
  const today = new Date().toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });

  const lang = useLang();
  const setLang = useSetLang();
  const t = useT();

  return (
    <div className="flex min-h-screen">
      <aside className="w-52 shrink-0 bg-nav text-slate-300">
        <div className="sticky top-0 flex h-screen flex-col overflow-y-auto">
        <div className="flex h-[52px] items-center gap-2 border-b border-white/10 px-4">
          <svg viewBox="0 0 24 24" className="h-[18px] w-[18px] shrink-0 text-[#6fb3cf]" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 3v5M6 21v-5m12 5v-5M4 16h4v5H4v-5Zm12 0h4v5h-4v-5ZM10 8h4v5h-4V8Z" />
          </svg>
          <div className="min-w-0 leading-tight">
            <div className="font-mono text-[14px] font-semibold tracking-tight text-white">netops-ai</div>
            <div className="line-clamp-2 text-[10.5px] leading-[13px] text-slate-400">{t("app.tagline")}</div>
          </div>
        </div>
        <nav className="flex-1 py-2">
        {nav.map((g) => (
          <div key={g.groupKey} className="pb-1">
            <div className="px-4 pt-3 pb-1 text-[10.5px] font-semibold tracking-wider text-slate-500 uppercase">{t(g.groupKey)}</div>
            {g.items.map((n) => (
              <button key={n.key} onClick={() => { setTab(n.key); location.hash = n.key; }}
                className={cn("relative flex h-8 w-full items-center gap-2.5 px-4 text-left text-[13px] transition-colors",
                  tab === n.key
                    ? "bg-white/[0.08] font-medium text-white"
                    : "text-slate-300 hover:bg-white/[0.04] hover:text-white")}>
                {tab === n.key && <span className="absolute inset-y-0 left-0 w-[3px] bg-[#4ba3c7]" />}
                <svg viewBox="0 0 24 24" className={cn("h-4 w-4 shrink-0", tab === n.key ? "text-[#8cc4dc]" : "text-slate-400")}
                     fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d={ICON[n.key] ?? ICON.overview} />
                </svg>
                {t(n.labelKey)}
              </button>
            ))}
          </div>
        ))}
        </nav>
        {/* **状态点必须是真探出来的。** 常亮的绿点第一次出事的时候还是绿的，
            那比没有更坏——所以这几颗各对应 `/api/status` 里一次真实判断。
            label 经 `statusLabel()` 按形状翻译；`note`（hover 提示）是任务执行结果拼的
            自由文本，原样显示不翻。放在侧边栏底部常驻，像网管软件的状态栏，不挤顶栏。 */}
        {status && status.items.length > 0 && (
          <div className="space-y-1 border-t border-white/10 px-4 py-3">
            {status.items.map((x) => (
              <div key={x.key} title={x.note || ""} className="flex items-start gap-2 text-[11px] leading-[15px] text-slate-400">
                <span className={cn("mt-[4px] inline-block h-[7px] w-[7px] shrink-0 rounded-full", x.ok ? "bg-[#3fb95f]" : "bg-[#e0902a]")} />
                <span className="min-w-0">{statusLabel(t, x.key, x.label)}</span>
              </div>
            ))}
          </div>
        )}
        </div>
      </aside>

      <div className="min-w-0 flex-1">
        <header className="sticky top-0 z-20 flex h-[52px] items-center justify-between gap-4 border-b border-line bg-card px-6">
          <div className="min-w-0 flex-1 leading-tight">
            <div className="text-[15px] font-semibold text-slate-900">{t(cur.labelKey)}</div>
            <div className="line-clamp-1 text-[11.5px] text-dim" title={t(cur.descKey)}>{t(cur.descKey)}</div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {/* 只读是这套东西最该被一眼看到的约束：顶栏常驻一个小标签，不再做成彩色大按钮。 */}
            <span title={t("app.readonlyBadge")}
              className="flex items-center gap-1.5 rounded-[2px] border border-brand/40 bg-brand/[0.06] px-2 py-[3px] text-[11px] font-semibold tracking-wide whitespace-nowrap text-brand">
              <svg viewBox="0 0 24 24" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 11V8a6 6 0 1 1 12 0v3M5 11h14v10H5V11Z" /></svg>
              {t("app.readonlyBadge")}
            </span>
            <button onClick={() => setLang(lang === "zh" ? "en" : "zh")} title="Switch language / 切换语言"
              className="rounded-[2px] border border-line px-2 py-[3px] text-[11px] font-medium whitespace-nowrap text-slate-600 transition hover:border-slate-400 hover:text-slate-900">
              {lang === "zh" ? "EN" : "中文"}
            </button>
            <button onClick={() => setMask(!mask)} title={t("app.demoMode.title")}
              className={cn("flex items-center gap-1.5 rounded-[2px] border px-2 py-[3px] text-[11px] font-medium whitespace-nowrap transition",
                mask ? "border-brand/50 text-brand" : "border-line text-slate-600 hover:border-slate-400")}>
              <span className={cn("relative inline-block h-3 w-5 rounded-[2px] transition", mask ? "bg-brand" : "bg-slate-300")}>
                <span className={cn("absolute top-[2px] h-2 w-2 rounded-[1px] bg-white transition-all", mask ? "left-[10px]" : "left-[2px]")} />
              </span>
              {t("app.demoMode")}
            </button>
            <div className="ml-2 border-l border-line pl-3 text-right leading-tight">
              <div className="num text-[13px] font-semibold text-slate-800">{clock}</div>
              <div className="num text-[10.5px] text-dim">{today}</div>
            </div>
            <UserMenu />
          </div>
        </header>
        <main className="p-5"><Boundary key={tab} crashedText={t("app.pageCrashed")}><Page /></Boundary></main>
      </div>
    </div>
  );
}

export default function App() {
  const [lang, setLangState] = useState<Lang>(loadStoredLang);
  const setLang = (l: Lang) => { setLangState(l); storeLang(l); };
  return (
    <LangContext.Provider value={{ lang, setLang }}>
      {/* 登录门禁。没启用时直通，Shell 照常渲染。数据请求都在 Shell 里，登录之后才发。 */}
      <AuthGate><Shell /></AuthGate>
    </LangContext.Provider>
  );
}

/** **一页崩了不该白掉整个控制台。**

撞到：`/api/inspection` 返回的形状少了 `groups`，巡检页 `map` 到
undefined 抛异常，React 没有边界就把整棵树卸载了——**看到的是全白页，
不是"巡检页坏了"**，连侧边栏都没了，根本看不出是哪一页的问题。
`key={tab}` 是为了切页时重置错误状态，否则崩过一次之后换页还是错误页。 */
class Boundary extends Component<{ children: ReactNode; crashedText: string }, { err: Error | null }> {
  state = { err: null as Error | null };
  static getDerivedStateFromError(err: Error) { return { err }; }
  render() {
    if (!this.state.err) return this.props.children;
    return (
      <div className="rounded-[4px] border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">
        <div className="font-medium">{this.props.crashedText}</div>
        <pre className="mt-2 whitespace-pre-wrap font-mono text-xs">{String(this.state.err)}</pre>
      </div>
    );
  }
}
