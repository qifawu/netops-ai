import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "../lib/api";
import { Badge, Card, CardHead, Empty, Icon, Loading, PATH, SectionTitle } from "../lib/ui";
import { fill, useT, type DictKey } from "../lib/i18n";
import {
  TONE_CLASS, fetchOverview, fmtBytes, hashParam, sectionOf, shortTitle, versionTone,
  type KbDoc, type KbExpression, type KbHit, type KbOverview, type KbSearch,
} from "../lib/kb";

/** 后端 `api/kb.py::FAMILY_ORDER` 是固定 8 项分类枚举（不是 AI 自由生成文本），
 *  OSPF/BGP/SNMP/STP 本来就是英文缩写两边一样，只有中文命名的 4 项需要映射。 */
const FAMILY_KEY: Record<string, DictKey> = {
  接口: "kb.family.interface",
  重启: "kb.family.restart",
  日志: "kb.family.log",
  其他: "kb.family.other",
};

const SAMPLES = [
  "%OSPF-5-ADJCHG Dead timer expired",
  "BGP Idle (Admin)",
  "Last reload reason",
  "OSPF 邻居 hello 计时器 不一致",
  "%LINK-3-UPDOWN",
];

const ICON_BOOK = "M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3V4Zm0 13a3 3 0 0 1 3-3h11M9 8h6";
const ICON_COPY = "M9 9h10v11H9V9Zm-4 6V4h10";
const ICON_LINK = "M14 4h6v6m0-6-9 9M10 6H5v13h13v-5";

export default function Knowledge() {
  const t = useT();
  const [ov, setOv] = useState<KbOverview | null | undefined>(undefined);
  const [q, setQ] = useState(() => hashParam("q"));
  const [res, setRes] = useState<KbSearch | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [filter, setFilter] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { fetchOverview(true).then(setOv); }, []);

  async function run(text: string) {
    const q = text.trim();
    if (!q) return;
    setQ(q); setBusy(true); setErr("");
    try {
      const r = await fetch(`/api/kb/search?q=${encodeURIComponent(q)}&limit=8`);
      if (!r.ok) throw new Error();
      setRes((await r.json()) as KbSearch);
    } catch {
      setErr(t("kb.playground.searchFailed"));
    } finally { setBusy(false); }
  }
  // 从对话页的「参考文档」标签跳过来时，#knowledge?q=… 带着搜索词，进页面就搜一次。
  useEffect(() => { const t = hashParam("q"); if (t) run(t); /* eslint-disable-next-line */ }, []);

  if (ov === undefined) return <Loading />;

  const docs = ov?.documents ?? [];
  const n159 = docs.filter((d) => versionTone(d.version) === "match").length;

  return (
    <div className="mx-auto max-w-[1180px] space-y-5">
      <p className="text-sm leading-relaxed text-slate-600">
        {t("kb.intro")}
        <b className="font-semibold text-slate-800">{t("kb.introBold")}</b>。
      </p>

      <div className="grid grid-cols-3 gap-4">
        <Stat label={t("kb.stat.docCount")} value={ov ? String(ov.doc_count) : "—"} note={ov ? fill(t("kb.stat.docCount.noteTemplate"), {
          size: fmtBytes(ov.db_size_bytes),
          modified: ov.db_modified ? fill(t("kb.stat.docCount.modifiedTemplate"), { date: new Date(ov.db_modified * 1000).toLocaleDateString("zh-CN") }) : "",
        }) : t("kb.stat.docCount.emptyNote")} />
        <Stat label={t("kb.stat.chunkCount")} value={ov ? ov.chunk_count.toLocaleString() : "—"} note={t("kb.stat.chunkCount.note")} />
        <Stat label={t("kb.stat.searchMethod")} value={t("kb.stat.searchMethod.value")} note={t("kb.stat.searchMethod.note")} accent />
      </div>

      {/* 检索试验台：这页最重要的东西，放在最显眼的位置。 */}
      <Card className="overflow-hidden">
        <CardHead title={t("kb.playground.title")} note={t("kb.playground.note")} icon={<Icon d={PATH.search} className="text-slate-500" />} />
        <div className="space-y-3 px-5 py-4">
          <div className="flex gap-2">
            <input ref={inputRef} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && run(q)}
              placeholder={t("kb.playground.placeholder")}
              className="flex-1 rounded-xl border border-line bg-white px-4 py-2.5 text-sm outline-none transition focus:border-brand focus:ring-2 focus:ring-brand/15" />
            <button onClick={() => run(q)} disabled={busy || !q.trim()}
              className="rounded-[3px] bg-brand px-5 text-[13px] font-medium text-white transition hover:bg-[#0b4a63] disabled:opacity-40">
              {busy ? t("kb.playground.searching") : t("kb.playground.search")}
            </button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-dim">{t("kb.playground.tryPrefix")}</span>
            {SAMPLES.map((s) => (
              <button key={s} onClick={() => run(s)}
                className="rounded-[3px] border border-line bg-white px-3 py-1 font-mono text-xs text-slate-600 transition hover:border-brand/40 hover:text-brand">{s}</button>
            ))}
          </div>

          {err && <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{err}</div>}
          {res && !err && (
            <div className="space-y-3 border-t border-line pt-3">
              <ExprLine ex={res.expression} count={res.results.length} />
              {res.results.length === 0
                ? <NoHit q={res.q} onPick={run} />
                : <div className="space-y-2.5">{res.results.map((h, i) => <ResultCard key={i} hit={h} tokens={highlightTokens(res.expression)} />)}</div>}
            </div>
          )}
        </div>
      </Card>

      <div>
        <SectionTitle title={t("kb.docList.title")} right={
          <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder={t("kb.docList.filterPlaceholder")}
            className="w-64 rounded-lg border border-line bg-white px-3 py-1.5 text-sm outline-none transition focus:border-brand focus:ring-2 focus:ring-brand/15" />
        }
          note={ov && ov.doc_count > 0 ? fill(t("kb.docList.noteTemplate"), {
            total: ov.doc_count,
            v159: n159 ? fill(t("kb.docList.hasV159"), { n: n159 }) : t("kb.docList.noV159"),
          }) : undefined} />
        {docs.length === 0
          ? <Card><Empty title={t("kb.docList.empty.title")} hint={t("kb.docList.empty.hint")} /></Card>
          : <DocList docs={docs} filter={filter} />}
      </div>
    </div>
  );
}

function Stat({ label, value, note, accent }: { label: string; value: string; note: string; accent?: boolean }) {
  return (
    <Card className="px-5 py-4">
      <div className="text-xs text-dim">{label}</div>
      <div className={cn("num mt-1 text-[22px] leading-tight font-semibold", accent ? "text-brand" : "text-slate-900")}>{value}</div>
      <div className="mt-1 text-xs leading-relaxed text-dim">{note}</div>
    </Card>
  );
}

/** 要高亮的词：AND 词表（含中文映射出的英文词），去掉太短的。 */
const highlightTokens = (ex: KbExpression) => ex.tokens.filter((t) => t.length >= 2);

function ExprLine({ ex, count }: { ex: KbExpression; count: number }) {
  const t = useT();
  if (!ex.tokens.length) return null;
  return (
    <div className="space-y-1.5 text-xs leading-relaxed text-dim">
      <div>
        {count > 0 ? fill(t("kb.expr.hitTemplate"), { n: count }) : t("kb.expr.noHit")}
        {ex.match_all_required && t("kb.expr.matchAllRequired")}
        {ex.used_or ? t("kb.expr.usedOr") : ""}
        {ex.zh_mapped.length > 0 && <>{t("kb.expr.zhMappedPrefix")}<b className="font-medium text-slate-700">{ex.zh_mapped.join("、")}</b></>}
        。
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <span>{t("kb.expr.terms")}</span>
        {ex.tokens.map((tok) => (
          <code key={tok} className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">{tok}</code>
        ))}
      </div>
    </div>
  );
}

function NoHit({ q, onPick }: { q: string; onPick: (s: string) => void }) {
  const t = useT();
  return (
    <div className="rounded-xl border border-dashed border-line bg-slate-50/60 px-5 py-5 text-sm">
      <div className="font-medium text-slate-700">{fill(t("kb.noHit.titleTemplate"), { q })}</div>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-xs leading-relaxed text-slate-600">
        <li>{t("kb.noHit.tip1")}</li>
        <li>{t("kb.noHit.tip2Prefix")} <code className="rounded bg-slate-100 px-1 font-mono">%OSPF-5-ADJCHG</code>、<code className="rounded bg-slate-100 px-1 font-mono">%LINK-3-UPDOWN</code>。</li>
        <li>{t("kb.noHit.tip3")}</li>
      </ul>
      <div className="mt-3 flex flex-wrap gap-2">
        {SAMPLES.slice(0, 3).map((s) => (
          <button key={s} onClick={() => onPick(s)} className="rounded-[3px] border border-line bg-white px-3 py-1 font-mono text-xs text-slate-600 hover:border-brand/40 hover:text-brand">{s}</button>
        ))}
      </div>
    </div>
  );
}

/** 命中词高亮。只做纯文本切分，不用 dangerouslySetInnerHTML（片段里可能有 < > 之类）。 */
function Highlight({ text, tokens }: { text: string; tokens: string[] }) {
  const parts = useMemo(() => {
    if (!tokens.length) return [text];
    const esc = [...tokens].sort((a, b) => b.length - a.length).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    return text.split(new RegExp(`(${esc.join("|")})`, "gi"));
  }, [text, tokens]);
  const set = useMemo(() => new Set(tokens.map((t) => t.toLowerCase())), [tokens]);
  return <>{parts.map((p, i) => (set.has(p.toLowerCase()) ? <mark key={i} className="rounded bg-amber-100 px-0.5 text-slate-900">{p}</mark> : <span key={i}>{p}</span>))}</>;
}

function KindBadge({ kind }: { kind: KbHit["kind"] }) {
  const t = useT();
  return kind === "lab_reference"
    ? <Badge className="bg-amber-50 text-amber-700 ring-amber-200" title={t("kb.kindBadge.labReferenceTitle")}>{t("kb.kindBadge.labReference")}</Badge>
    : <Badge className="bg-indigo-50 text-indigo-700 ring-indigo-200">{t("kb.kindBadge.official")}</Badge>;
}

function VersionBadge({ version }: { version: string }) {
  const t = useT();
  if (!version) return <Badge className={TONE_CLASS.none}>{t("kb.versionBadge.unregistered")}</Badge>;
  // 版本文字照抄来源登记，长就换行，不截断
  return <Badge title={version} className={cn(TONE_CLASS[versionTone(version)], "max-w-[34rem] !shrink rounded-lg py-1 text-left leading-snug whitespace-normal")}>{version}</Badge>;
}

function ResultCard({ hit, tokens }: { hit: KbHit; tokens: string[] }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  const title = shortTitle(hit.title);
  const sec = sectionOf(hit.heading_path);
  const cite = `${title}${sec ? " › " + sec : ""}${hit.url ? " — " + hit.url : ""}`;
  async function copy() {
    try { await navigator.clipboard.writeText(cite); } catch {
      const ta = document.createElement("textarea"); ta.value = cite; document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); document.body.removeChild(ta);
    }
    setCopied(true); setTimeout(() => setCopied(false), 1500);
  }
  return (
    <div className="rounded-xl border border-line bg-white px-4 py-3 transition hover:border-brand/30">
      <div className="flex items-start gap-2">
        <Icon d={ICON_BOOK} className="mt-0.5 h-4 w-4 text-brand" />
        <div className="min-w-0 flex-1 text-sm leading-snug">
          <span className="font-semibold text-slate-800">{title}</span>
          {sec && <span className="text-slate-500"> › {sec}</span>}
        </div>
      </div>
      <p className="mt-2 max-h-44 overflow-y-auto rounded-lg bg-slate-50 px-3 py-2 text-[13px] leading-relaxed break-words whitespace-pre-line text-slate-700">
        <Highlight text={hit.snippet.replace(/\n\s*\n+/g, "\n").trim()} tokens={tokens} />
      </p>
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <KindBadge kind={hit.kind} />
        <VersionBadge version={hit.version} />
        <span className="ml-auto flex items-center gap-2">
          {hit.url
            ? <a href={hit.url} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-xs text-brand hover:underline"><Icon d={ICON_LINK} className="h-3.5 w-3.5" />{t("kb.result.sourceLink")}</a>
            : <span className="text-xs text-dim">{hit.kind === "lab_reference" ? t("kb.result.noExternalLink") : t("kb.result.linkNotRegistered")}</span>}
          <button onClick={copy} className="flex items-center gap-1 rounded-lg border border-line px-2.5 py-1 text-xs font-medium text-slate-600 transition hover:bg-slate-50">
            <Icon d={copied ? PATH.check : ICON_COPY} className="h-3.5 w-3.5" />{copied ? t("kb.result.copied") : t("kb.result.copyCitation")}
          </button>
        </span>
      </div>
    </div>
  );
}

function DocList({ docs, filter }: { docs: KbDoc[]; filter: string }) {
  const t = useT();
  const f = filter.trim().toLowerCase();
  const shown = f ? docs.filter((d) => `${d.title} ${d.family} ${d.version} ${d.kind === "lab_reference" ? "自行整理" : "官方"}`.toLowerCase().includes(f)) : docs;
  const groups = useMemo(() => {
    const m = new Map<string, KbDoc[]>();
    for (const d of shown) m.set(d.family, [...(m.get(d.family) ?? []), d]);
    return [...m.entries()];
  }, [shown]);
  if (!shown.length) return <Card><Empty title={t("kb.docList.filteredEmpty.title")} hint={t("kb.docList.filteredEmpty.hint")} /></Card>;
  return (
    <div className="space-y-4">
      {groups.map(([fam, list]) => (
        <Card key={fam} className="overflow-hidden">
          <CardHead title={(FAMILY_KEY[fam] && t(FAMILY_KEY[fam])) || fam} note={fill(t("kb.docList.groupNoteTemplate"), { count: list.length, chunks: list.reduce((a, d) => a + d.chunks, 0).toLocaleString() })} />
          <table className="w-full table-fixed text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-dim">
                <th className="px-5 py-2 font-medium">{t("kb.docList.col.title")}</th>
                <th className="w-[230px] px-3 py-2 font-medium">{t("kb.docList.col.version")}</th>
                <th className="w-[64px] px-3 py-2 text-right font-medium">{t("kb.docList.col.chunks")}</th>
                <th className="w-[92px] px-3 py-2 font-medium">{t("kb.docList.col.fetched")}</th>
                <th className="w-[84px] px-5 py-2 font-medium">{t("kb.docList.col.source")}</th>
              </tr>
            </thead>
            <tbody>
              {list.map((d) => (
                <tr key={d.source} className="border-b border-line/70 align-top last:border-0">
                  <td className="px-5 py-2.5">
                    <div className="leading-snug font-medium text-slate-800" title={d.title}>{shortTitle(d.title)}</div>
                    {d.kind === "lab_reference" && <div className="mt-1"><KindBadge kind={d.kind} /></div>}
                  </td>
                  <td className="px-3 py-2.5"><VersionBadge version={d.version} /></td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-slate-700">{d.chunks}</td>
                  <td className="px-3 py-2.5 text-xs text-slate-500 tabular-nums">{d.fetched || "—"}</td>
                  <td className="px-5 py-2.5">
                    {d.url
                      ? <a href={d.url} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-xs text-brand hover:underline"><Icon d={ICON_LINK} className="h-3.5 w-3.5" />{t("kb.docList.open")}</a>
                      : <span className="text-xs text-dim">{d.kind === "lab_reference" ? t("kb.docList.inRepo") : "—"}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      ))}
    </div>
  );
}
