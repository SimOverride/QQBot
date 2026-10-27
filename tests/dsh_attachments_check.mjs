// 使用原生附件与会话服务验证换目录、旧图恢复和浏览补写，不调用模型。
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { syncBuiltinESMExports, createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { join, resolve } from "node:path";
import { deflateSync } from "node:zlib";

const root = process.argv[2], home = join(root, "client"), runtime = join(root, "runtime");
const require = createRequire(process.env.QQBOT_DSH_PACKAGE + "/package.json");
const load = name => import(pathToFileURL(require.resolve(name)).href);
const { Context } = await load("@deepseek-ai/cordis");
const { LocalAttachmentStore } = await load("@deepseek-ai/dsh-attachment-local");
const { imageBlocks, installImageFallback, mirrorImages } = await import("../plugins/assistant/dsh_attachments.mjs");
const { saveView, syncViews } = await import("../plugins/assistant/dsh_view.mjs");
const persistence = await load("@deepseek-ai/dsh-session-persistence-jsonl");

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const value of bytes) {
    crc ^= value;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function chunk(type, data) {
  const body = Buffer.concat([Buffer.from(type), data]);
  const length = Buffer.alloc(4), checksum = Buffer.alloc(4);
  length.writeUInt32BE(data.length); checksum.writeUInt32BE(crc32(body));
  return Buffer.concat([length, body, checksum]);
}
function image(value) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(1, 0); header.writeUInt32BE(1, 4); header[8] = 8; header[9] = 2;
  return {mediaType: "image/png", data: Buffer.concat([
    Buffer.from("89504e470d0a1a0a", "hex"), chunk("IHDR", header),
    chunk("IDAT", deflateSync(Buffer.from([0, value, 0, 0]))), chunk("IEND", Buffer.alloc(0)),
  ])};
}
const ctx = new Context(), oldCtx = new Context();
const plugin = ctx.plugin(LocalAttachmentStore, {dshHome: runtime});
const oldPlugin = oldCtx.plugin(LocalAttachmentStore, {dshHome: home});
await Promise.all([plugin.await(), oldPlugin.await()]);
const store = ctx.attachments;
const previous = await store.saveImage(image(10));
await mirrorImages(store.root, home, [{attachment: previous}]);
await fs.unlink(store.imageHostPath(previous));

// 精确复现客户端根目录 chmod 被拒绝，普通文件写入仍然可用。
const chmod = fs.chmod;
let denied = 0;
fs.chmod = async (path, ...args) => {
  if (resolve(String(path)) === resolve(home)) {
    denied++;
    throw Object.assign(new Error("测试目录权限拒绝"), {code: "EPERM"});
  }
  return chmod(path, ...args);
};
syncBuiltinESMExports();
let viewPlugin;
try {
  await assert.rejects(oldCtx.attachments.saveImage(image(20)), {code: "EPERM"});
  const current = await store.saveImage(image(20));
  installImageFallback(store, home);
  assert.equal((await store.readImage(previous)).ref.attachmentId, previous.attachmentId);
  // 已迁入执行目录后读取不再依赖客户端旧图。
  await fs.unlink(oldCtx.attachments.imageHostPath(previous));
  await store.readImage(previous);
  const blocks = imageBlocks([
    {type: "user/message", data: {content: [{type: "image", attachment: previous}]}},
    {type: "tool/result", data: {message: {content: [{type: "image", attachment: current}]}}},
  ]);
  assert.equal(blocks.length, 2);
  const deniedBefore = denied;
  await Promise.all([mirrorImages(store.root, home, blocks), mirrorImages(store.root, home, blocks)]);
  assert.equal(denied, deniedBefore);
  await oldCtx.attachments.readImage(current);

  const pending = await store.saveImage(image(30));
  const object = store.imageHostPath(pending), bytes = await fs.readFile(object);
  await fs.unlink(object);
  const view = {home, cwd: root, id: "attachments-test", title: "附件测试", user: "虚构消息", attachmentRoot: store.root};
  const event = {type: "user/message", data: {content: [{type: "image", attachment: pending}]}};
  const report = await saveView(view, [event], []);
  assert.equal(report.failures.length, 1);
  assert.equal(report.synced, 0);
  await fs.writeFile(object, bytes);
  assert.equal((await syncViews(home, root)).synced, 1);
  assert.equal((await syncViews(home, root)).synced, 0);
  await oldCtx.attachments.readImage(pending);
  viewPlugin = oldCtx.plugin(persistence, {root: join(home, "sessions")});
  await viewPlugin.await();
  const reader = await oldCtx.sessionPersistence.open(view.id, "read");
  try {
    const {events} = await reader.read();
    assert.equal(imageBlocks(events).length, 1);
    assert.equal(imageBlocks(events)[0].attachment.attachmentId, pending.attachmentId);
  } finally { await reader.close(); }
  // 损坏对象不得覆盖或被当作成功同步。
  await fs.writeFile(oldCtx.attachments.imageHostPath(current), Buffer.from("损坏"));
  await assert.rejects(mirrorImages(store.root, home, [{attachment: current}]), {code: "ATTACHMENT_CORRUPT"});
  console.log("原生图片换目录、旧引用恢复、并发去重、失败补写恢复及摘要校验通过");
} finally {
  fs.chmod = chmod; syncBuiltinESMExports();
  await viewPlugin?.dispose();
  await oldPlugin.dispose(); await plugin.dispose();
}
