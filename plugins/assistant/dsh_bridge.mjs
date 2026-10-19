// DSH 聊天回合仅注册当前会话的业务工具，不加载个人工具权限。
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";
import { saveView } from "./dsh_view.mjs";

const require = createRequire(process.env.QQBOT_DSH_PACKAGE + "/package.json");
const load = name => import(pathToFileURL(require.resolve(name)).href);
const { installModelSelection } = await load("@deepseek-ai/dsh-agent");
const { createUserMessage } = await load("@deepseek-ai/dsh-llm");
export const name = "qqbot-chat";
export const inject = ["agents", "sessions", "tools", "systemPrompt", "attachments", "agentDefaultModel"];
const output = (type, data = {}) => process.stdout.write("QQBOT_CHAT " + JSON.stringify({type, ...data}) + "\n");

export function apply(ctx) {
  let handle, snapshot, disposers = [], text = "", reason, step = 0;
  const pending = new Map();
  let calls = [], flushCalls;
  function enqueue(call) {
    calls.push(call);
    if (!flushCalls) flushCalls = setTimeout(() => {
      output("calls", {calls}); calls = []; flushCalls = undefined;
    }, 0);
  }
  const lines = createInterface({input: process.stdin});
  async function content(messages, history = false) {
    const blocks = [];
    for (const [index, message] of messages.entries()) {
      if (history) blocks.push({type: "text", text: index === messages.length - 1
        ? "【当前请求】" : `【历史消息 ${index + 1}，角色：${message.role === "assistant" ? "机器人" : "用户"}】`});
      const parts = typeof message.content === "string" ? [{type: "text", text: message.content}] : message.content || [];
      for (const part of parts) {
        if (part.type === "text" || part.type === "input_text") blocks.push({type: "text", text: part.text});
        else if (part.type === "image_url" || part.type === "input_image") {
          const url = typeof part.image_url === "string" ? part.image_url : part.image_url.url;
          const match = /^data:(image\/[a-z]+);base64,(.*)$/s.exec(url);
          if (!match) throw new Error("图片必须由后台验证并下载");
          const attachment = await ctx.attachments.saveImage({mediaType: match[1], data: Buffer.from(match[2], "base64")});
          blocks.push({type: "image", attachment});
        }
      }
    }
    return blocks;
  }
  function configure(agentCtx) {
    for (const dispose of disposers) dispose();
    disposers = [];
    for (const fn of snapshot.functions) {
      disposers.push(agentCtx.tools.register({
        ...fn,
        output: {schema: {type: "string"}, render: (_args, value) => JSON.parse(value)},
        execute: async (args, execution) => {
          return await new Promise((resolve, reject) => {
            const abort = () => { pending.delete(execution.callId); reject(new Error("回合已取消")); };
            execution.signal.addEventListener("abort", abort, {once: true});
            pending.set(execution.callId, {resolve: value => {
              execution.signal.removeEventListener("abort", abort); resolve(value);
            }});
            enqueue({call_id: execution.callId, name: fn.name, arguments: JSON.stringify(args)});
          });
        },
      }));
    }
  }
  snapshot = {functions: [], system: "", max_steps: 1};
  const ready = (async () => {
    await ctx.get("loader")?.await();
    const selection = ctx.agentDefaultModel.currentSelection();
    const options = {
      sessionId: process.env.QQBOT_SESSION_ID || `qqbot-chat-${randomUUID()}`,
      meta: {cwd: process.env.QQBOT_RUNTIME_CWD || process.cwd()},
      agentOptions: {provider: selection.provider, model: selection.model, maxTokens: Number(process.env.QQBOT_OUTPUT_TOKENS)},
      setup: agentCtx => {
        installModelSelection(agentCtx, {current: selection, assembled: undefined});
        agentCtx.tools.restrict({allow: []});
        agentCtx.tools.presentAs("native");
        agentCtx.tools.guard(exec => snapshot.functions.some(f => f.name === exec.name) ? undefined : "当前会话不允许此工具");
        agentCtx.systemPrompt.suppressRuntimeContext();
        agentCtx.systemPrompt.variable("qqbot_prompt", () => snapshot.system);
        agentCtx.systemPrompt.section({name: "qqbot", order: 0, complete: true, text: "{{qqbot_prompt}}"});
        configure(agentCtx);
        agentCtx.on("agent/pre-step", async (_payload, next) => {
          if (++step > snapshot.max_steps) throw new Error("聊天步骤预算已用完");
          if (snapshot.budget && (snapshot.budget.tokens <= 0 || snapshot.budget.seconds <= 0 || snapshot.budget.cost_exhausted)) {
            throw new Error("本轮共享资源预算已用完");
          }
          configure(agentCtx);
          return next();
        });
      },
    };
    try {
      handle = process.env.QQBOT_SESSION_ID
        ? await ctx.agents.resume({...options, resumeSessionId: options.sessionId})
        : await ctx.agents.create(options);
    } catch (error) {
      if (error.name !== "SessionPersistenceNotFoundError") throw error;
      handle = await ctx.agents.create(options);
    }
    ctx.on("session/event", (session, event) => {
      if (session.id !== handle.agent.session.id) return;
      if (event.type === "assistant/message") {
        text = (event.data.message.content || []).filter(p => p.type === "text").map(p => p.text).join("\n");
        output("usage", {usage: event.data.usage});
      }
      if (event.type === "turn/end") reason = event.data.reason;
    });
  })();
  ready.catch(error => output("error", {message: String(error)}));
  async function start(item) {
    await ready;
    snapshot = item;
    const eventOffset = handle.agent.session.snapshotEvents().length;
    configure(handle.agent.ctx);
    handle.agent.followup(createUserMessage({content: await content(item.messages, true), source: {kind: "user"}}));
    await handle.agent.whenIdle();
    if (reason?.kind !== "completed") throw new Error("聊天回合未正常结束：" + JSON.stringify(reason));
    await ctx.sessions.flush(handle.agent.session);
    if (item.view) {
      try { await saveView(item.view, handle.agent.session.snapshotEvents().slice(eventOffset), ctx.sessions.messageProjections); }
      catch { output("view_error"); }
    }
    output("final", {text});
  }
  let serial = Promise.resolve();
  lines.on("line", line => {
    serial = serial.then(async () => {
      const item = JSON.parse(line);
      if (item.type === "start") { start(item).catch(error => output("error", {message: String(error)})); return; }
      if (item.type === "result") {
        snapshot = {...snapshot, ...item};
        for (const result of item.results) {
          if (!pending.has(result.id)) throw new Error("工具结果编号不匹配");
          const value = await content(result.messages);
          const waiting = pending.get(result.id); pending.delete(result.id);
          waiting.resolve(JSON.stringify(value));
        }
      }
    }).catch(error => output("error", {message: String(error)}));
  });
  lines.on("close", () => { handle?.agent.cancel({kind: "user"}); ctx.get("appExit")(0); });
}
