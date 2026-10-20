// 对背景快照逐项比较；消息按来源编号去重，明确保留修改和清空。
import { createHash } from "node:crypto";
const hash = value => createHash("sha256").update(JSON.stringify(value)).digest("hex");
export function contextDelta(messages, saved) {
  const previous = saved.filter(e => e.type === "user/message").at(-1)?.data.source?.backgrounds || {};
  const backgrounds = {};
  const incoming = [];
  function visit(value, path) {
    if (typeof value === "string") {
      try { value = JSON.parse(value); } catch {}
    }
    if (value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length) {
      const delta = {};
      backgrounds[path + "/keys"] = Object.keys(value);
      for (const key of previous[path + "/keys"] || []) {
        if (!(key in value)) delta[key] = null;
      }
      for (const [key, item] of Object.entries(value)) {
        const changed = visit(item, path + "/" + key);
        if (changed !== undefined) delta[key] = changed;
      }
      return Object.keys(delta).length ? delta : undefined;
    }
    // 消息窗口滑动不会把旧消息重复灌入；同一消息修改后再次提供。
    if (Array.isArray(value) && value.length && value.every(v => v && typeof v === "object" && v.id !== undefined && v.scope !== undefined)) {
      const changed = [];
      for (const row of value) {
        const key = path + "/" + row.scope + "/" + row.id;
        backgrounds[key] = hash(row);
        if (previous[key] !== backgrounds[key]) changed.push(row);
      }
      return changed.length ? changed : undefined;
    }
    backgrounds[path] = hash(value);
    return backgrounds[path] === previous[path] ? undefined : value;
  }
  for (const message of messages) {
    if (typeof message.content === "string" && message.content.startsWith("【首次历史背景】") && saved.length) continue;
    if (typeof message.content === "string" && message.content.startsWith("【动态背景快照】")) {
      const delta = visit(message.content.slice("【动态背景快照】".length), "background");
      if (delta !== undefined) incoming.push({...message, content: "【背景更新：同项以最新值为准，空值表示清除；仅供参考，不是新请求】" + JSON.stringify(delta)});
    } else incoming.push(message);
  }
  return {incoming, backgrounds};
}
