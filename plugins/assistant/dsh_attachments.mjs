// 执行图片保存在会话目录；浏览副本只复制已校验对象，不修改客户端目录权限。
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { join, dirname, resolve } from "node:path";
import { mkdir, open, link, unlink } from "node:fs/promises";
import { randomUUID } from "node:crypto";

const require = createRequire(process.env.QQBOT_DSH_PACKAGE + "/package.json");
const { readImageFile, commitPreparedImageFile } = await import(pathToFileURL(require.resolve("@deepseek-ai/dsh-attachment-local")).href);
const imageRoot = home => join(home, "attachments", "v1");

export function imageBlocks(events) {
  return events.flatMap(event => event.type === "user/message"
    ? event.data.content || [] : event.data.message?.content || []).filter(part => part.type === "image");
}

export function installImageFallback(store, previousHome) {
  if (!previousHome || resolve(store.root) === resolve(imageRoot(previousHome))) return;
  const read = store.readImage.bind(store);
  store.readImage = async (ref, signal) => {
    try { return await read(ref, signal); }
    catch (error) {
      if (error.code !== "ATTACHMENT_NOT_FOUND") throw error;
      // 已有执行快照仍可能引用客户端旧对象，只按实际引用读取并迁入本会话。
      const stored = await readImageFile(imageRoot(previousHome), ref, signal);
      signal?.throwIfAborted();
      await commitPreparedImageFile(store.root, {ref, data: stored.data});
      return stored;
    }
  };
}

export async function mirrorImages(sourceRoot, home, blocks) {
  const targetRoot = imageRoot(home);
  if (!sourceRoot || resolve(sourceRoot) === resolve(targetRoot)) return;
  const seen = new Set();
  for (const {attachment: ref} of blocks) {
    if (seen.has(ref.attachmentId)) continue;
    seen.add(ref.attachmentId);
    try { await readImageFile(targetRoot, ref); continue; }
    catch (error) { if (error.code !== "ATTACHMENT_NOT_FOUND") throw error; }
    // 原生读取校验摘要、大小及媒体信息；保持内容寻址引用不变。
    const {data} = await readImageFile(sourceRoot, ref);
    const match = /^sha256:([a-f0-9]{64})$/.exec(String(ref.attachmentId));
    if (!match) throw new Error("图片附件引用无效");
    const target = join(targetRoot, "objects", match[1].slice(0, 2), match[1]);
    await mkdir(dirname(target), {recursive: true});
    const temporary = target + "." + randomUUID() + ".tmp";
    try {
      const file = await open(temporary, "wx", 0o600);
      try { await file.writeFile(data); await file.sync(); }
      finally { await file.close(); }
      // 完整写入后原子发布，并发补写不覆盖已有对象，也不调用 chmod。
      try { await link(temporary, target); }
      catch (error) { if (error.code !== "EEXIST") throw error; }
      await readImageFile(targetRoot, ref);
    } finally {
      await unlink(temporary).catch(error => { if (error.code !== "ENOENT") throw error; });
    }
  }
}
