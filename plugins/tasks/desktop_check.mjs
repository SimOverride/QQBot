// 只检查桥接实际依赖的模块及导出，不读取个人配置、不请求模型、不修改安装。
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const require = createRequire(process.env.QQBOT_DSH_PACKAGE + "/package.json");
const required = {
  "@deepseek-ai/cordis": {Context: "function"},
  "@deepseek-ai/dsh-agent": {installModelSelection: "function"},
  "@deepseek-ai/dsh-llm": {createUserMessage: "function"},
  "@deepseek-ai/dsh-session": {Session: "function", SessionId: "function", KNOWN_SESSION_EVENT_TYPES: "object"},
  "@deepseek-ai/dsh-session-persistence-jsonl": {},
  "@deepseek-ai/dsh-session-format-v3-to-v4": {assertReleasedV4Relationships: "function"},
  "@deepseek-ai/dsh-attachment-local": {LocalAttachmentStore: "function", readImageFile: "function", commitPreparedImageFile: "function"},
};
const missing = [];
for (const [name, exports] of Object.entries(required)) {
  let module;
  try { module = await import(pathToFileURL(require.resolve(name)).href); }
  catch { missing.push(name); continue; }
  for (const [key, type] of Object.entries(exports)) {
    if (typeof module[key] !== type) missing.push(`${name}:${key}`);
  }
  if (name === "@deepseek-ai/dsh-session" && typeof module.Session?.create !== "function") missing.push(`${name}:Session.create`);
  if (name === "@deepseek-ai/dsh-session-persistence-jsonl"
      && typeof module.apply !== "function" && typeof module.default !== "function"
      && typeof module.default?.apply !== "function") missing.push(`${name}:plugin`);
}
process.stdout.write("QQBOT_DSH_CHECK " + JSON.stringify({missing}) + "\n");
process.exitCode = missing.length ? 1 : 0;
