#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 本地用量看板（交互版）。

数据来源（全部只读）：
  ~/.workbuddy/workbuddy.db
    session_usage(session_id, used, size, updated_at, credit_json)
      credit_json = {"<requestId>": 积分}   ← 唯一的请求级用量来源
    sessions(id, title, custom_title, model, cwd, last_activity_at)
  ~/.workbuddy/audit-log/*.jsonl
    {requestId, sessionId, timestamp, ...}   ← 给 requestId 补时间戳

局限（写在这里，免得下次又去翻）：
  - session_usage 每个会话只留最后一次快照，没有逐日历史；
    所以每次跑都会把当次全量记录 union 进 history/YYYY-MM-DD.json，
    历史靠这个目录自己攒。
  - 积分不是 token，WorkBuddy 没有 input/output/cache 的拆分，
    也没有 5 小时/每周滚动额度。
  - 约 1/4 的 requestId 在 audit-log 里找不到，回落到会话的 updated_at。

用法：
  python wb_usage.py                # 生成并打印路径
  python wb_usage.py --open         # 生成后直接打开
  python wb_usage.py --out <dir>    # 换输出目录（默认 ~/wb-usage，或环境变量 WB_USAGE_OUT）
  python wb_usage.py --publish      # 传进 WorkBuddy 资料库（需要 WorkBuddy 开着）
"""

import argparse
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import webbrowser
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template.html")
HOME = os.path.expanduser("~")
WB_DIR = os.path.join(HOME, ".workbuddy")
DB = os.path.join(WB_DIR, "workbuddy.db")
AUDIT_GLOB = os.path.join(HOME, ".workbuddy", "audit-log", "*.jsonl")
CONV_GLOB = os.path.join(HOME, ".workbuddy", "projects", "*", "*.jsonl")
DEFAULT_OUT = os.environ.get("WB_USAGE_OUT") or os.path.join(HOME, "wb-usage")


# ---------------------------------------------------------------- 采集

def load_tokens():
    """requestId -> (input, output, cache_read, total)，按轮次累加。

    WorkBuddy 把每条消息的 usage 写在会话记录里（projects/<proj>/<sid>.jsonl），
    行类型是 message / function_call，message.usage 形如
      {input_tokens, output_tokens, total_tokens, cache_read_input_tokens}
    真正能和 credit_json 对上的键是 providerData.conversationRequestId
    （行的 id 是 messageId，对不上）。一轮对话会发多次模型调用，
    所以同一个 conversationRequestId 要把 token 累加起来。
    """
    acc = {}
    for path in glob.glob(CONV_GLOB):
        try:
            fh = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                u = msg.get("usage")
                pd = d.get("providerData")
                rid = pd.get("conversationRequestId") if isinstance(pd, dict) else None
                if not isinstance(u, dict) or not rid:
                    continue
                try:
                    ti = int(u.get("input_tokens") or 0)
                    to = int(u.get("output_tokens") or 0)
                    tc = int(u.get("cache_read_input_tokens") or 0)
                    tt = int(u.get("total_tokens") or (ti + to))
                except (TypeError, ValueError):
                    continue
                rid = str(rid).replace("-", "").lower()
                a = acc.get(rid)
                if a is None:
                    acc[rid] = [ti, to, tc, tt]
                else:
                    a[0] += ti; a[1] += to; a[2] += tc; a[3] += tt
    return {k: tuple(v) for k, v in acc.items()}


def load_audit_timestamps():
    """requestId -> 最早时间戳(ms)。同一请求在 audit-log 里可能有多条事件。"""
    ts = {}
    for path in glob.glob(AUDIT_GLOB):
        try:
            fh = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"requestId"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                rid, t = d.get("requestId"), d.get("timestamp")
                if rid and t:
                    ts[rid] = min(t, ts.get(rid, t))
    return ts


def collect():
    if not os.path.exists(DB):
        sys.exit("找不到 %s —— WorkBuddy 还没在这台机器上产生用量。" % DB)

    # 复制到临时目录再读：SQLite 只要看到 -wal/-shm 就会尝试写它们，
    # 而 ~/.workbuddy 在沙箱里是只读的，直接开会失败。复制一份就绕开了。
    tmp = tempfile.mkdtemp(prefix="wb-usage-")
    try:
        local = os.path.join(tmp, "wb.db")
        shutil.copy2(DB, local)
        for suffix in ("-wal", "-shm"):
            src = DB + suffix
            if os.path.exists(src):
                shutil.copy2(src, local + suffix)
        con = sqlite3.connect(local)
        try:
            return _read(con)
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read(con):
    sessions = {}
    for sid, title, custom, model, cwd in con.execute(
        "select id, title, custom_title, model, cwd from sessions"
    ):
        sessions[sid] = {
            "title": (custom or title or "(未命名)").strip(),
            "model": model or "",
            "cwd": cwd or "",
        }

    audit = load_audit_timestamps()
    tokens = load_tokens()
    records = {}
    ctx = {}
    for sid, used, size, updated, credit_json in con.execute(
        "select session_id, used, size, updated_at, credit_json from session_usage"
    ):
        ctx[sid] = (used or 0, size or 0, updated or 0)
        if not credit_json:
            continue
        try:
            credits = json.loads(credit_json)
        except ValueError:
            continue
        for rid, credit in credits.items():
            if not isinstance(credit, (int, float)):
                continue
            ti, to, tc, tt = tokens.get(str(rid).replace("-", "").lower(), (0, 0, 0, 0))
            records[rid] = {
                "rid": rid,
                "sid": sid,
                "credit": float(credit),
                "ts": audit.get(rid) or updated or 0,
                "tin": ti,
                "tout": to,
                "tcache": tc,
                "ttok": tt,
            }
    return records, ctx, sessions


def merge_history(records, out_dir):
    """把历史快照并进来，再写回今天这份。请求级记录只增不改。"""
    hist_dir = os.path.join(out_dir, "history")
    os.makedirs(hist_dir, exist_ok=True)
    merged = {}
    for path in sorted(glob.glob(os.path.join(hist_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                for rec in json.load(fh).get("records", []):
                    merged[rec["rid"]] = rec
        except (OSError, ValueError, KeyError):
            continue
    for rid, rec in records.items():
        merged[rid] = rec  # 新数据覆盖旧的（时间戳可能更准）

    today = os.path.join(hist_dir, datetime.now().strftime("%Y-%m-%d") + ".json")
    with open(today, "w", encoding="utf-8") as fh:
        json.dump(
            {"generated_at": datetime.now().isoformat(timespec="seconds"),
             "records": sorted(merged.values(), key=lambda r: r["ts"])},
            fh, ensure_ascii=False, separators=(",", ":"),
        )
    return merged


def summarize(records):
    daily = defaultdict(float)
    for rec in records.values():
        if rec["ts"]:
            daily[datetime.fromtimestamp(rec["ts"] / 1000).strftime("%Y-%m-%d")] += rec["credit"]
    return {
        "total": sum(r["credit"] for r in records.values()),
        "tok": sum(r.get("ttok", 0) for r in records.values()),
        "tok_hit": sum(1 for r in records.values() if r.get("ttok")),
        "requests": len(records),
        "sessions": len({r["sid"] for r in records.values()}),
        "days": len(daily),
        "first": min(daily) if daily else "-",
        "last": max(daily) if daily else "-",
    }


# ---------------------------------------------------------------- 渲染

def render(records, ctx, sessions, out_dir):
    days = sorted({
        datetime.fromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d")
        for r in records.values() if r["ts"]
    })
    sids = {r["sid"] for r in records.values()}
    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "span": [days[0] if days else "-", days[-1] if days else "-", len(days)],
        # 压缩成数组：[requestId, sessionId, 积分, 毫秒时间戳, in, out, cache, total]
        "records": [[r["rid"], r["sid"], round(r["credit"], 4), r["ts"],
                     r.get("tin", 0), r.get("tout", 0), r.get("tcache", 0),
                     r.get("ttok", 0)]
                    for r in sorted(records.values(), key=lambda x: x["ts"])],
        "sessions": {
            sid: {
                "t": sessions.get(sid, {}).get("title", "(未命名)"),
                "m": sessions.get(sid, {}).get("model", ""),
                "c": sessions.get(sid, {}).get("cwd", ""),
                "u": ctx.get(sid, (0, 0, 0))[0],
                "s": ctx.get(sid, (0, 0, 0))[1],
            }
            for sid in sids
        },
    }

    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()
    html = html.replace("__DATA__", json.dumps(payload, ensure_ascii=False,
                                               separators=(",", ":")))

    path = os.path.join(out_dir, "dashboard.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


LIBRARY_PAGE_NAME = "WorkBuddy 用量.html"


def _library_skill_dir():
    """最新版本的内置资料库技能目录（WorkBuddy 升级后版本号会变）。"""

    root = os.path.join(WB_DIR, "plugins", "cache", "workbuddy-builtin", "skill-library")
    dirs = [d for d in glob.glob(os.path.join(root, "*"))
            if os.path.isfile(os.path.join(d, "page", "import_html.py"))]
    if not dirs:
        sys.exit("找不到 WorkBuddy 资料库技能（plugins/cache/workbuddy-builtin/skill-library）。")
    return max(dirs, key=os.path.getmtime)


def publish(path, out_dir):
    """把看板传进资料库。首次新建页面，之后按记下的 node_block_id 原地覆盖，侧栏只有一个条目。
    走 WorkBuddy 本机 IPC 通道，所以 WorkBuddy 必须开着。"""
    import subprocess
    skill = _library_skill_dir()
    state_file = os.path.join(out_dir, "library.json")
    state = {}
    if os.path.isfile(state_file):
        with open(state_file, encoding="utf-8") as fh:
            state = json.load(fh)

    cmd = [sys.executable, os.path.join(skill, "page", "import_html.py"), path]
    if state.get("node_block_id"):
        cmd += ["--node-block-id", state["node_block_id"]]
    else:
        cmd += ["--file-name", LIBRARY_PAGE_NAME]
    env = dict(os.environ, WORKBUDDY_CONFIG_DIR=WB_DIR, CODEBUDDY_SKILL_DIR=skill,
               PYTHONIOENCODING="utf-8")
    res = subprocess.run(cmd, cwd=skill, env=env, capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=180)
    line = next((l for l in res.stdout.splitlines() if l.startswith("KS_IMPORT_OK ")), None)
    if not line:
        sys.exit("传进资料库失败：%s" % (res.stdout.strip() or res.stderr.strip())[-500:])
    info = json.loads(line[len("KS_IMPORT_OK "):])
    state.update({k: info.get(k) for k in ("node_block_id", "url", "publish_url") if info.get(k)})
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(state_file, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    print("资料库：%s" % (state.get("url") or state["node_block_id"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--publish", action="store_true",
                    help="生成后传进资料库（首次新建，之后原地更新同一页面）")
    ap.add_argument("--log", help="输出追加写进这个文件（给无窗口的定时任务用）")
    args = ap.parse_args()

    if args.log:
        sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8", buffering=1)
        print("---- %s" % datetime.now().isoformat(timespec="seconds"))

    os.makedirs(args.out, exist_ok=True)
    records, ctx, sessions = collect()
    if not records:
        sys.exit("没有找到任何用量记录。")

    merged = merge_history(records, args.out)
    s = summarize(merged)
    path = render(merged, ctx, sessions, args.out)

    print("总积分 %.2f · %d 请求 · %d 会话 · %d 天（%s → %s）" %
          (s["total"], s["requests"], s["sessions"], s["days"], s["first"], s["last"]))
    print("tokens %s（%d/%d 请求带 token 记录）" %
          ("{:,}".format(s["tok"]), s["tok_hit"], s["requests"]))
    print(path)
    if args.publish:
        publish(path, args.out)
    if args.open:
        webbrowser.open("file:///" + os.path.abspath(path).replace("\\", "/"))


if __name__ == "__main__":
    main()
