// 用确定性假模型驱动正式桥接协议，不调用 API、不读写真实聊天。
import { apply } from "../plugins/assistant/dsh_bridge.mjs";

const mode = process.argv[2];
const hooks = new Map(), registered = new Map();
let sessionEvent, prompt;
const agentCtx = {
  on(name, callback) {
    if (!hooks.has(name)) hooks.set(name, []);
    hooks.get(name).push(callback);
    return () => {};
  },
  tools: {
    restrict() {}, presentAs() {}, guard() {},
    register(fn) { registered.set(fn.name, fn); return () => registered.delete(fn.name); },
  },
  systemPrompt: {
    suppressRuntimeContext() {}, variable(_name, value) { prompt = value; }, section() {},
  },
};
const agent = {
  ctx: agentCtx,
  session: {id: "test", snapshotEvents: () => []},
  followup() {}, cancel() {},
  async whenIdle() {
    try {
      for (let index = 0; index < 3; index++) {
        // 最后注册的钩子属于正式桥接；真实模型仅在其放行后才会运行。
        await hooks.get("agent/pre-step").at(-1)({}, async () => {});
        if (index === 2 || mode !== "tokens") {
          if (registered.size) throw new Error("收尾仍暴露工具");
          if (mode === "tokens" && !prompt().includes("工具已禁用")) throw new Error("缺少收尾指令");
          if (mode === "repeat") {
            await hooks.get("agent/pre-step").at(-1)({}, async () => {});
            throw new Error("重复收尾未阻止");
          }
          sessionEvent(agent.session, {type: "assistant/message", data: {message: {content: [{type: "text", text: "根据已有结果完成收尾"}]}}});
          sessionEvent(agent.session, {type: "turn/end", data: {reason: {kind: "completed"}}});
          return;
        }
        const fn = registered.get("probe");
        if (!fn) throw new Error("查询工具缺失");
        sessionEvent(agent.session, {type: "assistant/message", data: {
          message: {content: []}, usage: {totalTokens: index === 0 ? 62350 : 62552},
        }});
        await fn.execute({}, {callId: `call-${index}`, signal: new AbortController().signal});
      }
    } catch (error) {
      // 模拟原生回合将异常包装为 UNKNOWN，验证桥接仍保留受控错误码。
      sessionEvent(agent.session, {type: "turn/end", data: {reason: {kind: "error", error: {message: error.message, code: "UNKNOWN"}}}});
    }
  },
};
apply({
  get(name) { return name === "loader" ? {await: async () => {}} : () => process.exit(0); },
  agentDefaultModel: {currentSelection: () => ({provider: "test", model: "test"})},
  agents: {async create(options) { options.setup(agentCtx); return {agent}; }},
  sessions: {flush: async () => {}},
  on(_name, callback) { sessionEvent = callback; },
});
