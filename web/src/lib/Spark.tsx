/** 巡检页的小曲线：后端已经降采样到 ≤60 点，这里只负责画。自绘 SVG，不引图表库。 */
export default function Spark({ series, className }: { series: number[][] | null | undefined; className?: string }) {
  if (!series || series.length < 2) return null;
  const W = 220, H = 44, P = 3;
  const xs = series.map((p) => p[0]), ys = series.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sx = (x: number) => P + ((x - x0) / (x1 - x0 || 1)) * (W - 2 * P);
  const sy = (y: number) => H - P - ((y - y0) / (y1 - y0 || 1)) * (H - 2 * P);
  const pts = series.map((p) => `${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(" ");
  const last = series[series.length - 1];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className={className ?? "h-11 w-[220px]"} role="img"
      aria-label={`min ${y0} max ${y1}`}>
      <title>{`${series.length} points · min ${y0} · max ${y1}`}</title>
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={sx(last[0])} cy={sy(last[1])} r="2.2" fill="currentColor" />
    </svg>
  );
}
