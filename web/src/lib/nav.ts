/** 跨页跳转：总览里点一条故障，直接落到「告警与结论」并选中它。
 *  App 只在挂载时读一次 location.hash，所以用一个事件通知它切页，被点的故障 ID 先存在这里。 */
let pending = "";
export const takePending = () => { const p = pending; pending = ""; return p; };
export function goTab(tab: string, incidentId = "") {
  pending = incidentId;
  window.dispatchEvent(new CustomEvent("netops:nav", { detail: tab }));
}
