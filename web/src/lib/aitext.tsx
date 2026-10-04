import { useEffect, useState } from "react";
import { plain } from "./api";
import { useLang } from "./i18n";
import Inline from "./Inline";
import { call } from "./sopui";

/** AI 生成的自由文本（root_cause/理由/回答）按需翻译。跟 `i18n.ts` 的静态字典是两回事——
 *  那份字典管前端写死的 UI 文案，编译期就知道两种写法；这里是模型自己写的中文，界面切到
 *  英文时得现调一次后端 `/api/translate`（内部有磁盘缓存，同一段文本只会真的调一次模型）。
 *
 *  **绝不要**把设备/日志的逐字证据（evidence 的 source、counter_evidence）传进这个 hook——
 *  那些是审计取证用的原文，改一个字都不行，必须原样展示，不管界面是什么语言。
 */

const memCache = new Map<string, string>();

/** 故障列表一页能有几百条，切到英文那一刻全部同时发请求会把浏览器的并发连接占满，
 *  连当前正在看的详情页那几条反而要排在后面、迟迟出不来。低优先级（列表预览）走这个队列
 *  限流；详情页（用户正盯着看的那几条）不进队列，直接发，不被列表请求插队。 */
const LOW_PRIORITY_LIMIT = 3;
let lowPriorityInFlight = 0;
const lowPriorityQueue: (() => void)[] = [];

function runLowPriority(fn: () => void) {
  if (lowPriorityInFlight < LOW_PRIORITY_LIMIT) {
    lowPriorityInFlight++;
    fn();
  } else {
    lowPriorityQueue.push(fn);
  }
}

function lowPriorityDone() {
  lowPriorityInFlight--;
  const next = lowPriorityQueue.shift();
  if (next) {
    lowPriorityInFlight++;
    next();
  }
}

/** 中文界面直接原样返回，不发请求。英文界面：先显示原文（不空白/不转圈占位，避免布局跳动），
 *  翻译到达后原地替换；同一段文本切换页面/语言来回跳不会重复请求。
 *  `lowPriority`：用在数量多、非当前焦点的地方（比如故障列表几百条预览标题）。 */
export function useTranslatedText(text: string, opts?: { lowPriority?: boolean }): string {
  const lang = useLang();
  const lowPriority = opts?.lowPriority ?? false;
  const [out, setOut] = useState(text);

  useEffect(() => {
    if (lang === "zh" || !text.trim()) {
      setOut(text);
      return;
    }
    const cacheKey = `en:${text}`;
    const cached = memCache.get(cacheKey);
    if (cached !== undefined) {
      setOut(cached);
      return;
    }
    setOut(text);
    let cancelled = false;
    const run = () => {
      call<{ translated?: string }>("POST", "/api/translate", { text, lang: "en" }).then((r) => {
        if (lowPriority) lowPriorityDone();
        if (cancelled) return;
        if (r.ok && r.data.translated) {
          memCache.set(cacheKey, r.data.translated);
          setOut(r.data.translated);
        }
        // 翻译失败：留着原文（中文），总比空白或报错字样强
      });
    };
    if (lowPriority) runLowPriority(run);
    else run();
    return () => {
      cancelled = true;
    };
  }, [lang, text, lowPriority]);

  return out;
}

/** 组件形式，方便在 `.map()` 循环里用——直接调 `useTranslatedText` 会违反 Hooks 规则，
 *  包一层组件就没问题（循环里渲染组件是合法的，循环里直接调 hook 不合法）。
 *  过一遍 `plain()` 兜底剥掉 `**`/`` ` ``——这个组件用在不该出现行内标记的地方（标题、
 *  徽标文字），模型翻译时偶尔会凭习惯加回markdown标记，交给 `AiInline` 才该保留。 */
export function AiText({ text, lowPriority }: { text: string; lowPriority?: boolean }) {
  return <>{plain(useTranslatedText(text, { lowPriority }))}</>;
}

/** 同上，但过一遍 `Inline`（`**粗体**`/`` `代码` `` 行内标记）——模型写句子时常带这些标记，
 *  纯替换成 `AiText` 会漏掉高亮。翻译 prompt 里已要求保留这两种标记原样，只翻标记内的文字。 */
export function AiInline({ text, lowPriority }: { text: string; lowPriority?: boolean }) {
  return <Inline text={useTranslatedText(text, { lowPriority })} />;
}
