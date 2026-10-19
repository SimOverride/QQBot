// 后台补写客户端占用期间留下的记录，不启动模型或加载个人插件。
import { syncViews } from "./dsh_view.mjs";
try { await syncViews(process.argv[2], process.argv[3]); }
catch { process.exitCode = 1; }
