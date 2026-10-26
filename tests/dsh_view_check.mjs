// 在临时目录验证客户端原生会话追加和跨进程读取，不调用模型。
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
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
      const report = await saveView({home, cwd: process.cwd(), id, title: "QQBot 测试", user: "占用期间的虚构消息"}, [], []);
      if (report.busy.length !== 1 || report.failures.length || report.synced) throw new Error("占用未归类为等待");
      // 真实子进程持锁时，补写 CLI 也应返回正常等待状态，队列仍可稍后恢复。
      const cli = spawnSync(process.execPath, ["--expose-internals",
        fileURLToPath(new URL("../plugins/assistant/dsh_view_sync.mjs", import.meta.url)),
        home, process.cwd()], {env: process.env, encoding: "utf8", timeout: 10000});
      const line = cli.stdout?.split(/\r?\n/).find(value => value.startsWith("QQBOT_VIEW_SYNC "));
      if (cli.status !== 0 || !line || JSON.parse(line.slice(16)).busy.length !== 1) throw new Error("占用 CLI 错报失败");
      const before = (await handle.read()).events.filter(e => e.type === "user/message").length;
      await handle.close();
      await syncViews(home, process.cwd());
      await syncViews(home, process.cwd());
      handle = await ctx.sessionPersistence.open(id, "read");
      const after = (await handle.read()).events.filter(e => e.type === "user/message").length;
      if (after !== before + 1) throw new Error("队列补写丢失或重复");
    }
    if (mode === "invalid") {
      const report = await saveView({home, cwd: process.cwd(), id, title: "QQBot 测试", user: "无效事件测试"},
        [{type: "turn/end", data: {turn: 999}}], []);
      if (report.busy.length || report.failures.length !== 1) throw new Error("无效事件被误判为正常占用");
      const cli = spawnSync(process.execPath, ["--expose-internals",
        fileURLToPath(new URL("../plugins/assistant/dsh_view_sync.mjs", import.meta.url)),
        home, process.cwd()], {env: process.env, encoding: "utf8", timeout: 10000});
      if (cli.status !== 2) throw new Error("真实失败未报告异常状态");
    }
    const listed = await ctx.sessionPersistence.list();
    if (!listed.some(item => item.header.id === handle.header.id)) throw new Error("原生会话列表未包含记录");
    const {events} = await handle.read();
    console.log(JSON.stringify({users: events.filter(e => e.type === "user/message").length,
      assistants: events.filter(e => e.type === "assistant/message").length,
      tools: events.filter(e => e.type === "tool/result").length}));
  } finally { await handle.close(); await plugin.dispose(); }
}
