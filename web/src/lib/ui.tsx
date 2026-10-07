import type { ReactNode } from "react";
import { cn } from "./api";

/** 页面里共用的几块积木。间距 / 字号 / 圆角统一在这里定，各页别再自己发明。
 *  字号层级：页面主标题 20 / 卡片标题 15 / 正文 14 / 辅助说明 12。 */

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("rounded-[4px] border border-line bg-card", className)}>{children}</div>;
}

export function CardHead({ title, note, right, icon }: { title: string; note?: string; right?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex min-h-[40px] items-center gap-2 border-b border-line bg-[#f6f7f9] px-4 py-2">
      {icon && <span className="flex shrink-0 text-slate-500 [&>svg]:!text-slate-500">{icon}</span>}
      <span className="text-[13px] font-semibold tracking-wide text-slate-800">{title}</span>
      {note && <span className="text-xs text-dim">{note}</span>}
      {right && <span className="ml-auto">{right}</span>}
    </div>
  );
}

export function Badge({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={cn("inline-flex shrink-0 items-center gap-1 rounded-[2px] px-1.5 py-px text-[11px] leading-[16px] font-medium whitespace-nowrap ring-1 ring-inset", className)}>
      {children}
    </span>
  );
}

export function SectionTitle({ title, note, right }: { title: string; note?: string; right?: ReactNode }) {
  return (
    <div className="mb-3 flex items-end gap-3">
      <div>
        <h2 className="text-[13px] font-semibold tracking-wide text-slate-800">{title}</h2>
        {note && <p className="mt-0.5 text-xs text-dim">{note}</p>}
      </div>
      {right && <div className="ml-auto">{right}</div>}
    </div>
  );
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center gap-1 px-6 py-10 text-center">
      <svg viewBox="0 0 24 24" className="mb-1 h-8 w-8 text-slate-300" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 7h16v11H4V7Zm0 0 3-3h10l3 3M9 12h6" />
      </svg>
      <div className="text-sm font-medium text-slate-600">{title}</div>
      {hint && <div className="text-xs text-dim">{hint}</div>}
    </div>
  );
}

export function Loading() {
  return (
    <div className="space-y-3">
      <div className="h-24 animate-pulse rounded-[4px] bg-slate-200/70" />
      <div className="grid grid-cols-4 gap-3">
        {[0, 1, 2, 3].map((k) => <div key={k} className="h-24 animate-pulse rounded-[4px] bg-slate-200/70" />)}
      </div>
    </div>
  );
}

export function Icon({ d, className }: { d: string; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={cn("h-4 w-4 shrink-0", className)} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}

export const PATH = {
  bell: "M6 16v-5a6 6 0 1 1 12 0v5l1.5 2h-15L6 16Zm4 4h4",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 4 4",
  target: "M12 3v4m0 10v4M3 12h4m10 0h4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z",
  send: "M21 3 10 14M21 3l-7 18-4-7-7-4 18-7Z",
  shield: "M12 3 4 6v6c0 4.5 3.2 7.5 8 9 4.8-1.5 8-4.5 8-9V6l-8-3Zm-3 9 2 2 4-4",
  check: "M5 12.5l4.5 4.5L19 7.5",
  chevron: "M9 6l6 6-6 6",
  spark: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z",
  clock: "M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z",
  list: "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01",
  terminal: "M4 5h16v14H4V5Zm3 4 3 3-3 3m5 0h4",
  trash: "M4 6h16M9 6V4h6v2m-8 0 1 14h10l1-14M10 10v6m4-6v6",
};
