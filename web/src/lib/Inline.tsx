import { Fragment } from "react";

/** 行内 Markdown。模型写的短句字段（根因、下一步、证据、判定依据）会夹
 *  `**强调**` 和 `` `接口名` ``，当纯文本塞进 DOM 就是满屏星号。
 *
 *  **不用 Md 那套。** 那个会包出 `<p>` 和标题的外边距，这些字段是行内的，
 *  嵌在带色小框和句子中间。这里只认这两种标记，别的原样留着——
 *  一句话字段里出现三级标题本身就该露出来，不该被悄悄吃掉。 */
const TOKEN = /\*\*([^*]+)\*\*|`([^`]+)`/g;

export default function Inline({ text }: { text: string }) {
  const out: React.ReactNode[] = [];
  let last = 0;
  for (const m of (text || "").matchAll(TOKEN)) {
    if (m.index! > last) out.push(text.slice(last, m.index));
    out.push(
      m[1] != null
        ? <strong key={m.index} className="font-semibold">{m[1]}</strong>
        : <code key={m.index} className="rounded bg-black/5 px-1 py-0.5 font-mono text-[0.9em]">{m[2]}</code>,
    );
    last = m.index! + m[0].length;
  }
  out.push(text?.slice(last) ?? "");
  return <Fragment>{out}</Fragment>;
}
