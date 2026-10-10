// 对接本机 dsh 0.1.0-rc.7，不加载个人配置或向机器人转发内部推理。
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";

const require = createRequire(process.env.QQBOT_DSH_PACKAGE + "/package.json");
const load = (name) => import(pathToFileURL(require.resolve(name)).href);
const { installModelSelection } = await load("@deepseek-ai/dsh-agent");
const { createUserMessage } = await load("@deepseek-ai/dsh-llm");
const { SessionId } = await load("@deepseek-ai/dsh-session");
export const name = "qqbot-task-bridge";
export const inject = ["agentDefaultModel", "agents", "sessions"];

const output = (type, data = {}) => process.stdout.write("QQBOT_EVENT " + JSON.stringify({type, data}) + "\n");
const visibleText = (message) => (message?.content || []).map(p =>
  p.type === "text" ? p.text : p.type === "tool-result" ? visibleText(p) : ""
).filter(Boolean).join("\n").slice(0, 12000);

export function apply(ctx) {
  let agent;
  let serial = Promise.resolve();
  let revision = 0;
  let reason;
  let steps = 0;
  let closing = false;
  const calls = new Map();
  const ready = (async () => {
    await ctx.get("loader")?.await();
    const selection = ctx.agentDefaultModel.currentSelection();
    const handle = await ctx.agents.create({
      sessionId: SessionId(`qqbot-${randomUUID()}`),
      meta: {cwd: process.cwd()},
      agentOptions: {provider: selection.provider, model: selection.model},
      setup: agentCtx => { installModelSelection(agentCtx, {current: selection, assembled: undefined}); },
    });
    agent = handle.agent;
    ctx.on("session/event", (session, event) => {
      if (session.id !== agent.session.id) return;
      const d = event.data;
      if (event.type === "assistant/message") output(event.type, {text: visibleText(d.message), usage: d.usage});
      else if (event.type === "tool/call") {
        calls.set(d.callId, Date.now());
        output(event.type, {name: d.name, call_id: d.callId, arguments: d.arguments?.slice(0, 6000)});
      }
      else if (event.type === "tool/result") {
        const id = d.message?.source?.callId;
        output(event.type, {call_id: id, elapsed_ms: calls.has(id) ? Date.now() - calls.get(id) : null,
          text: visibleText(d.message), error: d.error});
        calls.delete(id);
      }
      else if (event.type === "todo/write") output(event.type, d);
      else if (event.type === "turn/end") { reason = d.reason; output(event.type, {reason}); }
      else if (event.type === "step/start") {
        output(event.type, {step: ++steps});
        if (steps > Number(process.env.QQBOT_MAX_STEPS)) agent.cancel({kind: "user"});
      }
    });
    output("ready", {session: agent.session.id, model: selection.model});
  })();
  ready.catch(error => { output("error", {message: "dsh 初始化失败：" + error.stack}); ctx.get("appExit")(1); });
  const lines = createInterface({input: process.stdin});
  lines.on("line", line => {
    serial = serial.then(async () => {
      await ready;
      const item = JSON.parse(line);
      if (item.type === "close") {
        await ctx.sessions.flush(agent.session);
        closing = true;
        lines.close();
        process.stdin.destroy();
        ctx.get("appExit")(0);
        return;
      }
      if (item.type !== "prompt" && item.type !== "supplement") return;
      const own = ++revision;
      reason = undefined;
      const message = createUserMessage({content: [{type: "text", text: item.text}], source: {kind: "user"}});
      if (item.type === "supplement") agent.steer(message); else agent.followup(message);
      output("accepted", {id: item.id});
      agent.whenIdle().then(async () => {
        await ctx.sessions.flush(agent.session);
        if (own === revision) output("idle", {revision: own, reason});
      }).catch(() => output("error", {message: "dsh 执行异常"}));
    }).catch(() => output("error", {message: "dsh 输入处理失败"}));
  });
  lines.on("close", () => { if (!closing) ctx.get("appExit")(1); });
}
