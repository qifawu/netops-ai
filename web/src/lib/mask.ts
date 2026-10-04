import { useSyncExternalStore } from "react";

/** 演示模式（对外展示脱敏）。
 *
 *  用一个全局 MutationObserver 直接改页面里的文本节点，不逐页处理：
 *  - IPv4（含 OSPF Router ID 这类点分十进制标识）→ 文档保留地址 192.0.2.N，同一个真实地址永远对应同一个占位，
 *    这样「V1 的地址」和「V2 的地址」在一页里还是能区分开；
 *  - eventid / 指纹 / 序列号 → 只留后 2 位，前面补 *；
 *  - MAC 地址 → 抹掉。
 *  设备名（V1-vios 这类是实验通用名）不动。
 *
 *  开启方式：URL 带 `?mask=1`（写入 localStorage），或顶栏的「演示模式」开关。`?mask=0` 关掉。
 *  关闭时把改过的节点还原成原文（只还原自己改过、且之后没被 React 重写过的节点）。 */
const KEY = "netops.mask";
const listeners = new Set<() => void>();

export const isMask = () => {
  try { return localStorage.getItem(KEY) === "1"; } catch { return false; }
};

const IPV4 = /(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?!\d|\.\d)/g;
const MAC = /\b(?:[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}|(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2})\b/gi;
const HASH_EVENT = /#(\d{5,})/g;
const EVENT_KV = /(eventid[=:：\s]*)(\d{5,})/gi;
const ALERT_LIST = /((?:告警|事件)\s*)((?:\d{5,}[、,，\s]*)+)/g;
const CHAT_ID = /(对话\s*)(\d{6,})/g;

const ipMap = new Map<string, string>();
const fakeIp = (real: string) => {
  let v = ipMap.get(real);
  if (!v) {
    const n = ipMap.size;
    v = n < 240 ? `192.0.2.${n + 10}` : `198.51.100.${(n % 240) + 10}`;
    ipMap.set(real, v);
  }
  return v;
};
const tail = (s: string) => "***" + s.slice(-2);

export function maskText(s: string, asId = false): string {
  if (!s) return s;
  if (asId) return s.replace(/[0-9a-zA-Z]{5,}/g, (m) => tail(m));
  return s
    .replace(IPV4, (m) => (m.startsWith("255.") || m.startsWith("0.0.0.") ? m : fakeIp(m)))
    .replace(MAC, "xxxx.xxxx.xxxx")
    .replace(HASH_EVENT, (_, d) => "#" + tail(d))
    .replace(EVENT_KV, (_, k, d) => k + tail(d))
    .replace(ALERT_LIST, (_, k, list: string) => k + list.replace(/\d{5,}/g, (d) => tail(d)))
    .replace(CHAT_ID, (_, k, d) => k + tail(d));
}

type Rec = { orig: string; masked: string };
const done = new Map<Text, Rec>();
let obs: MutationObserver | null = null;
const SKIP = new Set(["SCRIPT", "STYLE", "TEXTAREA", "INPUT"]);

function maskNode(t: Text) {
  const p = t.parentElement;
  if (!p || SKIP.has(p.tagName)) return;
  const raw = t.data;
  const rec = done.get(t);
  if (rec && rec.masked === raw) return;
  const id = p.closest("[data-mask]")?.getAttribute("data-mask") === "id";
  const m = maskText(raw, id);
  if (m !== raw) { t.data = m; done.set(t, { orig: raw, masked: m }); }
}

function walk(root: Node) {
  if (root.nodeType === Node.TEXT_NODE) { maskNode(root as Text); return; }
  if (root.nodeType !== Node.ELEMENT_NODE) return;
  const el = root as Element;
  maskAttrs(el);
  const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) maskNode(n as Text);
  el.querySelectorAll("[title]").forEach(maskAttrs);
}

function maskAttrs(el: Element) {
  const t = el.getAttribute?.("title");
  if (!t) return;
  const m = maskText(t);
  if (m !== t) { el.setAttribute("data-title-orig", t); el.setAttribute("title", m); }
}

function start() {
  if (obs) return;
  walk(document.body);
  obs = new MutationObserver((records) => {
    obs!.disconnect();
    for (const r of records) {
      if (r.type === "characterData") maskNode(r.target as Text);
      else if (r.type === "attributes") maskAttrs(r.target as Element);
      else r.addedNodes.forEach(walk);
    }
    if (done.size > 6000) for (const k of done.keys()) if (!k.isConnected) done.delete(k);
    observe();
  });
  observe();
}

function observe() {
  obs?.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["title"] });
}

function stop() {
  obs?.disconnect();
  obs = null;
  for (const [t, rec] of done) if (t.isConnected && t.data === rec.masked) t.data = rec.orig;
  done.clear();
  document.querySelectorAll("[data-title-orig]").forEach((el) => {
    el.setAttribute("title", el.getAttribute("data-title-orig") || "");
    el.removeAttribute("data-title-orig");
  });
}

export function setMask(on: boolean) {
  try { on ? localStorage.setItem(KEY, "1") : localStorage.removeItem(KEY); } catch {}
  on ? start() : stop();
  listeners.forEach((f) => f());
}

/** 页面挂载后调用一次：读 URL 参数 / localStorage，决定要不要开。 */
export function initMask() {
  const q = new URLSearchParams(location.search).get("mask");
  if (q === "1") try { localStorage.setItem(KEY, "1"); } catch {}
  else if (q === "0") try { localStorage.removeItem(KEY); } catch {}
  if (isMask()) start();
}

export function useMask() {
  return useSyncExternalStore((f) => { listeners.add(f); return () => { listeners.delete(f); }; }, isMask);
}
