import type { ReactNode } from "react";
import { cn } from "./api";

/** SOP 编辑相关页面共用的小件：请求封装、按钮、提示条、diff 展示。 */

export async function call<T>(method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE", url: string, body?: unknown): Promise<{ ok: boolean; status: number; data: T & { message?: string } }> {
  try {
    const r = await fetch(url, { method, headers: body ? { "content-type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
    const data = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, data };
  } catch {
    // 不在这里塞死一句中文兜底文案——留空，每个调用点自己已经有更贴切的失败提示
    // （"保存失败"/"审核失败"这种），用它们自己的 `r.data.message || t(...)` 兜底，
    // 这里硬塞一句非空的中文反而会把那些提示全部盖住，不随界面语言切换。
    return { ok: false, status: 0, data: {} as T & { message?: string } };
  }
}

export function Notice({ tone, children }: { tone: "indigo" | "amber" | "rose" | "emerald"; children: ReactNode }) {
  const cls = {
    indigo: "bg-indigo-50 text-indigo-800 ring-indigo-200",
    amber: "bg-amber-50 text-amber-800 ring-amber-200",
    rose: "bg-rose-50 text-rose-800 ring-rose-200",
    emerald: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  }[tone];
  return <div className={cn("rounded-xl px-4 py-3 text-[13px] leading-relaxed ring-1", cls)}>{children}</div>;
}

export function Btn({ children, onClick, tone = "plain", busy, disabled, small, title }: {
  children: ReactNode; onClick?: () => void; tone?: "plain" | "brand" | "ok" | "danger"; busy?: boolean; disabled?: boolean; small?: boolean; title?: string;
}) {
  const t = {
    plain: "bg-white text-slate-700 ring-line hover:bg-slate-50",
    brand: "bg-brand text-white ring-brand hover:bg-indigo-600",
    ok: "bg-emerald-600 text-white ring-emerald-600 hover:bg-emerald-700",
    danger: "bg-white text-rose-600 ring-rose-200 hover:bg-rose-50",
  }[tone];
  return (
    <button onClick={onClick} disabled={disabled} title={title}
      className={cn("inline-flex items-center gap-1.5 rounded-lg font-medium ring-1 ring-inset transition disabled:cursor-not-allowed disabled:opacity-45", small ? "px-2.5 py-1 text-xs" : "px-4 py-2 text-sm", t)}>
      {busy && <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />}
      {children}
    </button>
  );
}

export function DiffView({ diff, maxH = "max-h-72" }: { diff: string; maxH?: string }) {
  return (
    <pre className={cn("overflow-auto border-t border-line bg-white py-2 font-mono text-[12px] leading-5", maxH)}>
      {diff.split("\n").map((l, i) => {
        const cls = l.startsWith("+++") || l.startsWith("---") ? "text-slate-400"
          : l.startsWith("+") ? "bg-emerald-50 text-emerald-800"
            : l.startsWith("-") ? "bg-rose-50 text-rose-800"
              : l.startsWith("@@") ? "text-indigo-500" : "text-slate-500";
        return <div key={i} className={cn("whitespace-pre px-4", cls)}>{l || " "}</div>;
      })}
    </pre>
  );
}
