// 后端起着就用真接口，没起就退回 fixture（真实记录导出来的，字段名一致）。
export async function load<T>(name: string, apiPath?: string): Promise<T> {
  if (apiPath) {
    try {
      const r = await fetch(apiPath);
      if (r.ok) return (await r.json()) as T;
    } catch {}
  }
  return (await (await fetch(`fixtures/${name}.json`)).json()) as T;
}

export const cn = (...xs: (string | false | undefined)[]) => xs.filter(Boolean).join(" ");
export const ts = (s: number) =>
  new Date(s * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });

/** 列表里只显示人看的那半句。结论原文在详情页**一个字不改**，
 *  这里砍掉的只是「，见 undistinguishable_candidates」这类给程序看的指针。 */
export const brief = (t: string) => (t || "（无结论）").replace(/[，,]\s*见\s*\w+\s*$/, "");

/** 去掉一行摘要里的 Markdown 标记。剧本描述是网工在 YAML 里手写的，
 *  常带 `**强调**`，列表里只要纯文字，上整套渲染器不值当。 */
export const plain = (t: string) => (t || "").replace(/\*\*(.+?)\*\*/g, "$1").replace(/`(.+?)`/g, "$1");
