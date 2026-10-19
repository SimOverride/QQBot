// 在临时目录验证客户端原生会话追加和跨进程读取，不调用模型。
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { saveView, syncViews, recoverViews } from "../plugins/assistant/dsh_view.mjs";
const require = createRequire(process.env.QQBOT_DSH_PACKAGE + "/package.json");
const load = name => import(pathToFileURL(require.resolve(name)).href);
const [mode, home, id] = process.argv.slice(2);
if (mode === "save") {
  await saveView({home, cwd: process.cwd(), id, title: "QQBot 测试", user: "虚构测试消息"}, [], []);
} else {
  const { Context } = await load("@deepseek-ai/cordis");
  const persistence = await load("@deepseek-ai/dsh-session-persistence-jsonl");
  const ctx = new Context();
  const plugin = ctx.plugin(persistence, {root: home + "/sessions"});
  await plugin.await();
  let handle = await ctx.sessionPersistence.open(id, ["locked", "recover"].includes(mode) ? "write" : "read");
  try {
    if (mode === "recover") {
      const before = (await handle.read()).events.filter(e => e.type === "user/message").length;
      await saveView({home, cwd: process.cwd(), id, title: "QQBot 测试", user: "重建前"}, [], []);
      const result = await recoverViews(home, process.cwd(), [id]);
      if (result.report.synced !== 1) throw new Error("占用队列未恢复");
      await saveView({home, cwd: process.cwd(), id, title: "QQBot 测试", user: "重建后"}, [], []);
      await syncViews(home, process.cwd());
      await handle.close();
      handle = await ctx.sessionPersistence.open(result.mapping[id], "read");
      if ((await handle.read()).events.filter(e => e.type === "user/message").length !== before + 2) throw new Error("重建映射错误");
    }
    if (mode === "locked") {
      await saveView({home, cwd: process.cwd(), id, title: "QQBot 测试", user: "占用期间的虚构消息"}, [], []);
      const before = (await handle.read()).events.filter(e => e.type === "user/message").length;
      await handle.close();
      await syncViews(home, process.cwd());
      await syncViews(home, process.cwd());
      handle = await ctx.sessionPersistence.open(id, "read");
      const after = (await handle.read()).events.filter(e => e.type === "user/message").length;
      if (after !== before + 1) throw new Error("队列补写丢失或重复");
    }
    const listed = await ctx.sessionPersistence.list();
    if (!listed.some(item => item.header.id === handle.header.id)) throw new Error("原生会话列表未包含记录");
    const {events} = await handle.read();
    console.log(JSON.stringify({users: events.filter(e => e.type === "user/message").length,
      assistants: events.filter(e => e.type === "assistant/message").length,
      tools: events.filter(e => e.type === "tool/result").length}));
  } finally { await handle.close(); await plugin.dispose(); }
}
