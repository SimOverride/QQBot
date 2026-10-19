// 补写浏览记录，显式恢复时保留旧副本。
import { syncViews, recoverViews } from "./dsh_view.mjs";
try {
  const [home, cwd, mode, ...ids] = process.argv.slice(2);
  const result = mode === "recover" ? await recoverViews(home, cwd, ids) : await syncViews(home, cwd);
  console.log(JSON.stringify(result));
  if ((result.report || result).failures.length) process.exitCode = 2;
} catch (error) { console.log(JSON.stringify({error: error.name})); process.exitCode = 1; }
