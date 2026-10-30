---
name: wb-usage
description: 生成并打开 WorkBuddy 本地用量看板 —— 积分与 token 双维度（credits/tokens 一键切换）、每日堆叠柱、24 小时分布、会话排行、上下文占用百分比、13 周活动热力图（daily/cumulative）。数据全部来自本机 ~/.workbuddy/workbuddy.db 与 audit-log，不上传。当用户说「看用量」「用量面板」「/wb-usage」「我这个月花了多少积分」「哪个会话最费」「上下文快满了没」「像 Codex 那样的 usage 面板」时使用。
---

# WorkBuddy 用量看板

一条命令出图：读本地库 → 聚合 → 生成单文件 HTML → 打开。

```bash
python ~/.workbuddy/skills/wb-usage/wb_usage.py --open
```

- 输出：`~/wb-usage/dashboard.html`（单文件，无外部依赖，断网可看）
- 换目录：`--out <dir>` 或环境变量 `WB_USAGE_OUT`
- 只要数据不要图：不加 `--open`，脚本会打印总积分/请求数/会话数
- 传进资料库：`--publish`。首次在个人空间新建页面「WorkBuddy 用量」，之后按 `~/wb-usage/library.json` 里记下的 `node_block_id` 原地覆盖同一页面（版本号递增，侧栏只有一个条目）。走 WorkBuddy 本机 IPC，**WorkBuddy 必须开着**。
- 定时/无窗口运行：加 `--log <文件>`，输出追加写进日志（配 `pythonw.exe` 用）
- 主题：配色取自 WorkBuddy 的设计 token，默认跟 WorkBuddy 外观设置（浅色/深色/跟随系统），页面右上角也能切；`--theme light|dark|system` 强制指定
- 试看：`--demo` 用假数据生成，不读本机、不写历史

## 能点什么

面板是纯手写 SVG + 原生 JS，没有 CDN，界面是中文：

- `7d / 30d / all` 区间切换（改区间会重算指标、柱状、小时分布、表格）
- `credits / tokens` 维度切换：整页（指标、柱状、小时、表格、热力图）在积分和 token 之间切
- `session / model` 分组切换（堆叠柱按会话或按模型拆）
- 图例点击 → 隐藏/显示某个会话；超过 7 个的自动并入灰色「其他」
- 柱子 hover → 当天总分 + Top 会话明细；热力图格子 hover → 当天积分
- 表头点击 → 按积分/请求/上下文排序，再点反向
- 行点击 → 展开该会话的每个 request（时间 + requestId 前 8 位 + 积分）
- 热力图 `daily / cumulative` 切换（cumulative 从窗口起点累加，hover 同时显示当天增量）

样式与交互都在 `template.html`，`wb_usage.py` 只做采集和数据注入（占位符是 `__DATA__`）。
改版式只动 template，不用碰 Python。

## 数据从哪来

| 来源 | 字段 | 用途 |
|---|---|---|
| `workbuddy.db` → `session_usage.credit_json` | `{"<requestId>": 积分}` | **唯一的请求级用量**，看板全靠它 |
| `workbuddy.db` → `session_usage` | `used` / `size` | 上下文占用，`used/size` 即百分比 |
| `workbuddy.db` → `sessions` | `title` `custom_title` `model` `cwd` | 会话名、模型归因 |
| `audit-log/*.jsonl` | `requestId` → `timestamp` | 给每个请求补时间戳，做日/小时聚合 |
| `projects/*/*.jsonl` | `message.usage` + `providerData.conversationRequestId` | **token 用量**：input / output / cache_read / total |

token 的 join 键是 `providerData.conversationRequestId`（去掉横线小写），**不是**行的 `id`
（那是 messageId，对不上）。一轮对话会发多次模型调用，所以同一个 requestId 的 token 要累加。
`total_tokens = input + output`，其中 input 已包含 cache_read；真实新读入 = input − cache_read。

约 1/4 的 requestId 在 audit-log 里查不到，脚本会回落到该会话的 `updated_at`，
所以那部分的日归属可能有偏差 —— 但积分总数不受影响。

## 已知边界（别指望它做这些）

- **没有 5 小时 / 每周滚动额度**：WorkBuddy 是套餐 + 积分制，做不出 Codex 那种 limit 百分比。
- token 只有**轮次级累加值**，不是按模型调用逐条展示；且只有本地留存的会话记录才有
  （`projects/*.jsonl` 删了就没了），所以 token 覆盖可能低于积分覆盖。跑完会打印覆盖率。
- **`session_usage` 每个会话只留最后一次快照**，没有逐日历史。历史靠
  `~/wb-usage/history/YYYY-MM-DD.json` 逐次累积（按 requestId 去重合并，
  旧的不会被覆盖）。想要连续曲线，就得定期跑一次 —— Windows 用仓库里的 `schedule.ps1`，macOS/Linux 用 cron（见 README）。
- 首次跑只有几天数据，热力图会很空，正常。

## 怎么看死活

```bash
# 生成了没、多少条
python ~/.workbuddy/skills/wb-usage/wb_usage.py

# 历史攒了多少天
ls ~/wb-usage/history/

# 直接查库（确认数据源本身没坏）
sqlite3 ~/.workbuddy/workbuddy.db "select count(*) from session_usage;"
```

脚本跑完会打印两行：

```
总积分 514.29 · 152 请求 · 26 会话 · 6 天（2026-10-22 → 2026-10-30）
tokens 236,299,186（152/152 请求带 token 记录）
```

四个数跟上一次比只增不减，突然变小说明库被重置或 `--out` 指到了空目录。
括号里是 token 覆盖率，掉到 0 多半是 `projects/` 目录被清过。

## 怎么回滚

```bash
rm -rf ~/.workbuddy/skills/wb-usage        # 技能本身
rm -rf ~/wb-usage               # 产物与历史
```

只删产物、保留技能，就只删第二行 —— 但历史一起没了，下次从零开始攒。
