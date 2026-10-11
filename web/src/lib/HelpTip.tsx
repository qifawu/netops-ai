import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

/** 「胶囊问号」：一个小的「?」按钮，悬停 / 键盘聚焦时在旁边弹出说明；触屏点一下切换，Esc 关闭。
 *  只给确实有用、但不该常驻页面的说明用（页面上不放「这块是干什么的」这种解释性文字）。
 *  `text` 传已经翻译好的文案。浮层自动避开屏幕左右和下边缘。 */
export default function HelpTip({ text }: { text: string }) {
  const id = useId();
  const btn = useRef<HTMLButtonElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number }>({ left: 0, top: 0 });

  useLayoutEffect(() => {
    if (!open || !btn.current || !tip.current) return;
    const b = btn.current.getBoundingClientRect();
    const w = tip.current.offsetWidth;
    const h = tip.current.offsetHeight;
    const margin = 8;
    let left = b.left + b.width / 2 - w / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - w - margin));
    let top = b.bottom + 6;
    if (top + h > window.innerHeight - margin) top = Math.max(margin, b.top - h - 6);  // 下面放不下就放上面
    setPos({ left, top });
  }, [open, text]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    const onScroll = () => setOpen(false);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("scroll", onScroll, true); };
  }, [open]);

  return (
    <span className="relative inline-flex align-middle">
      <button ref={btn} type="button" aria-label="?" aria-describedby={open ? id : undefined} aria-expanded={open}
        onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)} onBlur={() => setOpen(false)}
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); setOpen((x) => !x); }}
        className="inline-flex h-[15px] min-w-[15px] items-center justify-center rounded-full border border-slate-400 px-[3px] text-[10px] leading-none font-semibold text-slate-500 transition hover:border-brand hover:text-brand focus-visible:border-brand focus-visible:text-brand focus-visible:outline-none"
        data-testid="help-tip">
        ?
      </button>
      {open && (
        <div ref={tip} id={id} role="tooltip" style={{ position: "fixed", left: pos.left, top: pos.top }}
          className="z-50 max-w-[320px] rounded-[2px] border border-line bg-card px-2.5 py-1.5 text-left text-xs leading-relaxed font-normal whitespace-normal text-slate-700 shadow-md">
          {text}
        </div>
      )}
    </span>
  );
}
