// 使用客户端原生持久化协议保存分会话浏览记录，不参与机器人的模型上下文。
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { join } from "node:path";
import { mkdir, writeFile, rename, readdir, readFile, unlink } from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { imageBlocks, mirrorImages } from "./dsh_attachments.mjs";

const require = createRequire(process.env.QQBOT_DSH_PACKAGE + "/package.json");
const load = name => import(pathToFileURL(require.resolve(name)).href);
const { Context } = await load("@deepseek-ai/cordis");
const persistence = await load("@deepseek-ai/dsh-session-persistence-jsonl");
const { Session, KNOWN_SESSION_EVENT_TYPES } = await load("@deepseek-ai/dsh-session");
const { assertReleasedV4Relationships } = await load("@deepseek-ai/dsh-session-format-v3-to-v4");
const { createUserMessage } = await load("@deepseek-ai/dsh-llm");

const types = new Set(["turn/start", "turn/end", "step/start", "step/end", "assistant/message", "tool/call", "tool/result"]);
const queuePath = (home, cwd) => join(home, "qqbot-pending", createHash("sha256").update(cwd.toLowerCase()).digest("hex").slice(0, 16));

export async function saveView(view, events, projections) {
  const folder = queuePath(view.home, view.cwd);
  await mkdir(folder, {recursive: true});
  const record = randomUUID();
  const path = join(folder, `${Date.now()}-${record}.json`);
  const images = events.filter(e => e.type === "user/message").flatMap(e => e.data.content || []).filter(p => p.type === "image");
  // 持续执行会话的回合编号从当前累计值开始，镜像按自身顺序续接。
  const firstTurn = events.find(e => e.type === "turn/start")?.data.turn || 1;
  const normalized = events.filter(e => types.has(e.type)).map(event => {
    const copy = structuredClone(event);
    if (typeof copy.data.turn === "number") copy.data.turn -= firstTurn - 1;
    return copy;
  });
  const payload = {view: {...view, record, images}, events: normalized};
  await writeFile(path + ".tmp", JSON.stringify(payload), {flag: "wx"});
  await rename(path + ".tmp", path);
  return await syncViews(view.home, view.cwd, projections);
}

export async function syncViews(home, cwd, projections = []) {
  const folder = queuePath(home, cwd);
  let files;
  try { files = await readdir(folder); } catch (error) { if (error.code === "ENOENT") return {synced: 0, busy: [], failures: []}; throw error; }
  const mapping = await readMapping(home, cwd);
  const blocked = new Set();
  const report = {synced: 0, busy: [], failures: []};
  for (const file of files.filter(f => /^\d+-[a-f0-9-]+\.json$/.test(f)).sort()) {
    const path = join(folder, file);
    try {
      const {view, events} = JSON.parse(await readFile(path, "utf8"));
      if (view.home !== home || view.cwd !== cwd || blocked.has(view.id)) continue;
      try {
        await mirrorImages(view.attachmentRoot, home, [...view.images, ...imageBlocks(events)]);
        await appendView({...view, id: mapping[view.id] || view.id}, events, projections);
      }
      catch (error) {
        blocked.add(view.id);
        // 客户端正常持有写锁时等待，不将占用伪装成保存损坏。
        const target = ["SessionAlreadyOwnedError", "SessionAlreadyExistsError"].includes(error.name)
          ? report.busy : report.failures;
        target.push({id: view.id, name: error.name});
        continue;
      }
      await unlink(path);
      report.synced++;
    } catch (error) { if (error.code !== "ENOENT") throw error; }
  }
  return report;
}

async function appendView(view, events, projections) {
  const ctx = new Context();
  let writer, plugin;
  try {
    plugin = ctx.plugin(persistence, {root: join(view.home, "sessions")});
    await plugin.await();
    const storage = ctx.sessionPersistence;
    for (let attempt = 0; ; attempt++) {
      try {
        try { writer = await storage.open(view.id, "write"); }
        catch (error) {
          if (error.name !== "SessionPersistenceNotFoundError") throw error;
          writer = await storage.create({version: 4, id: view.id, createdAt: Date.now(), cwd: view.cwd, isSeeded: false});
        }
        // 首次写入即取得跨进程锁，避免同群并发创建两个版本。
        await writer.flush();
        break;
      } catch (error) {
        await writer?.close(); writer = undefined;
        if (attempt >= 2 || !["SessionAlreadyOwnedError", "SessionAlreadyExistsError"].includes(error.name)) throw error;
        await delay(100);
      }
    }
    const saved = await writer.read();
    if (saved.events.some(e => e.type === "user/message" && e.data.source?.record === view.record)) return;
    const session = Session.create(view.id, saved.events, writer.header, writer.inheritedEventCount, projections);
    const offset = saved.events.length;
    session.append("session/title", {title: view.title, messageSeqs: [], source: {kind: "user"}});
    const input = [view.user, "【浏览记录：模型生成结果，不代表 QQ 发送成功；在客户端续聊不会发送到 QQ。】"];
    session.append("user/message", createUserMessage({content: [{type: "text", text: input.join("\n\n")}, ...view.images], source: {kind: "user", record: view.record}}), {surfaceOp: "append"});
    const turns = saved.events.filter(e => e.type === "turn/start").length;
    for (const event of events) {
      if (!types.has(event.type)) continue;
      const data = structuredClone(event.data);
      if (typeof data.turn === "number") data.turn += turns;
      session.append(event.type, data, event.surfaceOp === undefined ? undefined : {surfaceOp: "append"});
    }
    const all = session.snapshotEvents();
    assertReleasedV4Relationships({header: writer.header, events: all, inheritedEventCount: writer.inheritedEventCount}, new Set(KNOWN_SESSION_EVENT_TYPES));
    await writer.append(all.slice(offset));
    await writer.flush();
  } finally {
    await writer?.close();
    await plugin?.dispose();
  }
}

const mappingPath = (home, cwd) => join(home, "qqbot-views", createHash("sha256").update(cwd.toLowerCase()).digest("hex").slice(0, 16) + ".json");
async function readMapping(home, cwd) {
  try { return JSON.parse(await readFile(mappingPath(home, cwd), "utf8")); }
  catch (error) { if (error.code === "ENOENT") return {}; throw error; }
}

// 显式重建浏览副本，保留原记录和机器人的连续执行上下文。
export async function recoverViews(home, cwd, ids) {
  const mapping = await readMapping(home, cwd);
  const ctx = new Context();
  const plugin = ctx.plugin(persistence, {root: join(home, "sessions")});
  await plugin.await();
  try {
    for (const logical of ids) {
      let reader, writer;
      try {
        reader = await ctx.sessionPersistence.open(mapping[logical] || logical, "read");
        const saved = await reader.read();
        const id = randomUUID();
        writer = await ctx.sessionPersistence.create({...reader.header, id, cwd, createdAt: Date.now()},
          {inheritedEventCount: reader.inheritedEventCount});
        assertReleasedV4Relationships({header: writer.header, events: saved.events,
          inheritedEventCount: reader.inheritedEventCount}, new Set(KNOWN_SESSION_EVENT_TYPES));
        await writer.append(saved.events);
        await writer.flush();
        mapping[logical] = id;
        const path = mappingPath(home, cwd);
        await mkdir(join(home, "qqbot-views"), {recursive: true});
        const temporary = path + "." + randomUUID() + ".tmp";
        await writeFile(temporary, JSON.stringify(mapping), {flag: "wx"});
        await rename(temporary, path);
      } finally { await writer?.close(); await reader?.close(); }
    }
  } finally { await plugin.dispose(); }
  return {mapping, report: await syncViews(home, cwd)};
}
