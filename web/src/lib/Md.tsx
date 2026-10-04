import { marked } from "marked";
import DOMPurify from "dompurify";

// 关掉 `~文字~` 删除线：回答里 `~4.9h`、`~2 Mbps` 这种「约」的写法太常见，
// 成对出现会把整段中间的文字画上删除线（真数据上验出来的）。
marked.use({ tokenizer: { del() { return undefined; } } });

/** 模型的回答是 Markdown，不渲染就满屏 `##` 和 `**`。
 *
 *  **必须过一遍 DOMPurify。** 这段文字里混着设备回显和 Zabbix 原文，
 *  是从网络另一头拿回来的内容，不能当可信 HTML 直接塞进 DOM。 */
export default function Md({ text }: { text: string }) {
  const html = DOMPurify.sanitize(marked.parse(text, { async: false, breaks: true }) as string);
  return (
    <div
      className="prose-netops text-sm leading-relaxed [&_code]:rounded [&_code]:bg-slate-100 [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[0.85em] [&_h2]:mt-3 [&_h2]:mb-1.5 [&_h2]:text-[15px] [&_h2]:font-semibold [&_h3]:mt-2.5 [&_h3]:mb-1 [&_h3]:font-semibold [&_li]:my-0.5 [&_ol]:my-1.5 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-1.5 [&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:bg-term [&_pre]:p-3 [&_pre_code]:bg-transparent [&_pre_code]:text-slate-200 [&_strong]:font-semibold [&_table]:my-2 [&_table]:w-full [&_table]:text-xs [&_td]:border [&_td]:border-line [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-line [&_th]:bg-slate-50 [&_th]:px-2 [&_th]:py-1 [&_ul]:my-1.5 [&_ul]:list-disc [&_ul]:pl-5"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
