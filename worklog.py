#!/usr/bin/env python3
"""
claude-code-worklog — Claude Code での作業を、プロジェクトごと・日ごとの時間にする。

  python3 worklog.py install            # フックを ~/.claude/settings.json に入れる
  python3 worklog.py report             # 今月の作業を CSV で出す（作業のまとまりごと）
  python3 worklog.py report --by day    # 日 × プロジェクトの合計
  python3 worklog.py log --date 2026-11-10   # その日に打ったコマンド（伏せ字済み）

読むもの
  - Claude Code のセッションの記録（~/.claude/projects/**/*.jsonl）。何もしなくても残っている
  - このフックの記録（~/.claude-worklog/log/YYYY-MM-DD.jsonl）。セッションの記録は既定で 30 日で
    消えるため、フックで時刻とコマンドだけを残しておく

決まり
  - 前の記録から 15 分を超えてあいたら、別の作業にする（--gap で変えられる）
  - 時間は、まとまりの最初の記録から最後の記録まで。1 件だけなら 1 分
  - コマンドの出力は残さない。パスワード・トークンらしいものは書き込む前に伏せる
  - 外には何も送らない。標準ライブラリだけで動く

MIT License. https://github.com/moldspoon/claude-code-worklog
"""
import argparse
import csv
import glob
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone

__version__ = "0.1.0"

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get("CLAUDE_WORKLOG_HOME") or os.path.join(HOME, ".claude-worklog")
LOG_DIR = os.path.join(DATA_DIR, "log")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
HOOK_EVENTS = ("PostToolUse", "PostToolUseFailure", "UserPromptSubmit")

# ---------------------------------------------------------------- 伏せ字

# (パターン, 置き換え) を上から順にかける
REDACTIONS = [
    # 秘密鍵の本体
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "***PRIVATE KEY***"),
    # URL に埋め込んだ認証情報 user:pass@host
    (re.compile(r"(\w+://[^/\s:@]+):[^@\s/]+@"), r"\1:***@"),
    # Authorization: Bearer xxx / Basic xxx
    (re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)[^\s'\"]+"), r"\1***"),
    # mysql -pXXXX
    (re.compile(r"(\s-p)(?!\s)[^\s'\"]+"), r"\1***"),
    # --password=XXXX / DB_PASSWORD=XXXX / token: XXXX / API_KEY=XXXX など
    (re.compile(r"(?i)((?:--)?[\w-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)[\w-]*\s*[=:]\s*)(['\"]?)[^\s'\"]+\2"), r"\1\2***\2"),
    # 形の決まったキー
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]{8,}"), r"\1***"),
    (re.compile(r"\b((?:sk|rk|pk)_(?:live|test)_)[A-Za-z0-9]{8,}"), r"\1***"),
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{20,}"), r"\1***"),
    (re.compile(r"\b(github_pat_)[A-Za-z0-9_]{20,}"), r"\1***"),
    (re.compile(r"\b(xox[abprs]-)[A-Za-z0-9-]{10,}"), r"\1***"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "AIza***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "***JWT***"),
    # Webhook の URL は URL そのものが鍵
    (re.compile(r"(hooks\.slack\.com/services/)[^\s'\"]+"), r"\1***"),
    (re.compile(r"(discord(?:app)?\.com/api/webhooks/)[^\s'\"]+"), r"\1***"),
    (re.compile(r"(api\.vercel\.com/v1/integrations/deploy/[^/\s]+/)[^\s'\"]+"), r"\1***"),
    # 英字と数字が混ざった 40 文字以上の塊（16 進だけのハッシュは残す）
    (re.compile(r"(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/=_-]*[g-zG-Z])(?=[A-Za-z0-9+/=_-]*\d)[A-Za-z0-9+/=_-]{40,}"), "***"),
]


def redact(text):
    for pat, rep in REDACTIONS:
        text = pat.sub(rep, text)
    return text


# ---------------------------------------------------------------- フック

def hook(stdin=sys.stdin):
    """Claude Code のフックから呼ばれる。時刻・フォルダ・道具の名前と、Bash なら伏せたコマンドを 1 行追記する。
    質問の文面・ファイルの中身・コマンドの出力は書かない。何があっても作業を止めない"""
    try:
        data = json.load(stdin)
        event = data.get("hook_event_name") or ""
        tool = data.get("tool_name") or ("prompt" if event == "UserPromptSubmit" else "")
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "session": data.get("session_id"),
            "cwd": data.get("cwd"),
            "tool": tool,
        }
        if tool == "Bash":
            record["command"] = redact((data.get("tool_input") or {}).get("command") or "")
            record["ok"] = event != "PostToolUseFailure"
        os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(LOG_DIR, date.today().isoformat() + ".jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return 0


def install(dry_run=False, uninstall=False):
    """~/.claude/settings.json にフックを足す（外すときは --uninstall）。元のファイルは .bak に残す"""
    script = os.path.abspath(__file__)
    command = f'"{sys.executable}" "{script}" hook'
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in HOOK_EVENTS:
        entries = hooks.get(event, [])
        mine = [e for e in entries if any("worklog.py" in (h.get("command") or "") and " hook" in (h.get("command") or "") for h in e.get("hooks", []))]
        if uninstall:
            if mine:
                hooks[event] = [e for e in entries if e not in mine]
                if not hooks[event]:
                    del hooks[event]
                changed = True
            continue
        if mine:
            continue
        entry = {"hooks": [{"type": "command", "command": command}]}
        if event != "UserPromptSubmit":
            entry["matcher"] = "*"
        hooks[event] = entries + [entry]
        changed = True
    if not hooks:
        settings.pop("hooks", None)
    if not changed:
        print("変更はありません（" + ("フックは入っていません" if uninstall else "フックはもう入っています") + "）。")
        return 0
    text = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    if dry_run:
        print(text)
        return 0
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, SETTINGS + ".bak")
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        f.write(text)
    if uninstall:
        print(f"フックを外しました: {SETTINGS}（元のファイルは settings.json.bak）")
    else:
        print(f"フックを入れました: {SETTINGS}（元のファイルは settings.json.bak）")
        print(f"記録の置き場所: {LOG_DIR}")
        print("開いている Claude Code のセッションには、/hooks を開くか再起動すると効きます。")
    return 0


# ---------------------------------------------------------------- 記録を読む

def parse_ts(ts):
    """ISO 8601 の文字列を、手元の時刻帯の datetime にする（Z や +09:00 付き、どちらも）"""
    if not isinstance(ts, str) or len(ts) < 19:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def read_transcripts(since, until):
    """Claude Code のセッションの記録から、やり取りの時刻と Bash のコマンドを読む。
    返すのは {session: [(datetime, cwd, command or None, ok)]}"""
    out = {}
    lo = datetime.combine(since, datetime.min.time()).timestamp()
    for path in glob.glob(os.path.join(PROJECTS_DIR, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < lo:
                continue
            uses, failed = [], set()
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if '"timestamp"' not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("type") not in ("user", "assistant") or not r.get("cwd"):
                        continue
                    dt = parse_ts(r.get("timestamp"))
                    if not dt or not (since <= dt.date() <= until):
                        continue
                    session = r.get("sessionId") or path
                    events = out.setdefault(session, [])
                    events.append((dt, r["cwd"], None, True))
                    content = (r.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_use" and b.get("name") == "Bash":
                            uses.append((session, dt, r["cwd"], (b.get("input") or {}).get("command") or "", b.get("id")))
                        elif b.get("type") == "tool_result" and b.get("is_error"):
                            failed.add(b.get("tool_use_id"))
            for session, dt, cwd, command, use_id in uses:
                out[session].append((dt, cwd, redact(command), use_id not in failed))
        except (OSError, UnicodeDecodeError):
            continue
    return out


def read_hook_log(since, until):
    out = {}
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "*.jsonl"))):
        day = os.path.basename(path)[:10]
        if not (since.isoformat() <= day <= until.isoformat()):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                dt = parse_ts(r.get("ts"))
                if not dt or not r.get("cwd"):
                    continue
                command = r.get("command") if r.get("tool") == "Bash" else None
                out.setdefault(r.get("session") or "hook", []).append((dt, r["cwd"], command, r.get("ok", True)))
    return out


def load(since, until, transcripts=True):
    """両方を読み、同じセッションはセッションの記録の方を使う（フックの記録と二重にしない）"""
    sessions = read_transcripts(since, until) if transcripts else {}
    for session, events in read_hook_log(since, until).items():
        if session not in sessions:
            sessions[session] = events
    return sessions


# ---------------------------------------------------------------- プロジェクトの見分け方

_root_cache = {}


def project_of(cwd, depth):
    """作業したフォルダを、プロジェクトのフォルダに丸める。
    上にたどって .git があればそこ（git worktree も .git ファイルで見つかる）。
    無ければ、ホームの下の depth 段目まで（~/dev/foo など）。フォルダがもう無いときも同じ"""
    if cwd in _root_cache:
        return _root_cache[cwd]
    p = os.path.normpath(cwd)
    root = None
    cur = p
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            root = cur
            break
        parent = os.path.dirname(cur)
        if parent == cur or cur == HOME:
            break
        cur = parent
    if root is None or root == HOME:
        if p.startswith(HOME + os.sep):
            parts = p[len(HOME) + 1:].split(os.sep)
            root = os.path.join(HOME, *parts[:depth])
        else:
            root = p
    _root_cache[cwd] = root
    return root


def short(path):
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + os.sep) else path


# ---------------------------------------------------------------- まとめる

def blocks(sessions, gap_minutes, depth):
    """プロジェクトごとに時刻順に並べ、gap を超えてあいたら区切る。
    返すのは [{project, start, end, minutes, commands, failed, sessions}]"""
    by_project = {}
    for session, events in sessions.items():
        for dt, cwd, command, ok in events:
            by_project.setdefault(project_of(cwd, depth), []).append((dt, session, command, ok))
    gap = timedelta(minutes=gap_minutes)
    out = []
    for project, events in by_project.items():
        events.sort(key=lambda e: e[0])
        cur = None
        for dt, session, command, ok in events:
            if cur is None or dt - cur["end"] > gap or dt.date() != cur["start"].date():
                cur = {"project": project, "start": dt, "end": dt, "commands": 0, "failed": 0, "sessions": set()}
                out.append(cur)
            cur["end"] = dt
            cur["sessions"].add(session)
            if command:
                cur["commands"] += 1
                cur["failed"] += 0 if ok else 1
    for b in out:
        b["minutes"] = max(1, round((b["end"] - b["start"]).total_seconds() / 60))
    out.sort(key=lambda b: (b["start"], b["project"]))
    return out


def month_range(text):
    y, m = map(int, text.split("-"))
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def period(args):
    today = date.today()
    if getattr(args, "date", None):
        d = date.fromisoformat(args.date)
        return d, d
    if args.month:
        return month_range(args.month)
    if args.since or args.until:
        since = date.fromisoformat(args.since) if args.since else today - timedelta(days=30)
        until = date.fromisoformat(args.until) if args.until else today
        return since, until
    if args.days:
        return today - timedelta(days=args.days - 1), today
    return today.replace(day=1), today


def hm(minutes):
    return f"{minutes // 60}:{minutes % 60:02d}"


def report(args):
    since, until = period(args)
    rows = blocks(load(since, until, not args.no_transcripts), args.gap, args.depth)
    if args.project:
        rows = [b for b in rows if args.project in (os.path.basename(b["project"]), short(b["project"]), b["project"])]
    out = open(args.output, "w", encoding="utf-8-sig" if args.bom else "utf-8", newline="") if args.output else sys.stdout
    if not args.output and args.bom:
        out.write("﻿")
    w = csv.writer(out)
    if args.by == "block":
        w.writerow(["date", "project", "path", "start", "end", "minutes", "hours", "commands", "failed", "sessions"])
        for b in rows:
            w.writerow([b["start"].date().isoformat(), os.path.basename(b["project"]), short(b["project"]),
                        b["start"].strftime("%H:%M"), b["end"].strftime("%H:%M"), b["minutes"], hm(b["minutes"]),
                        b["commands"], b["failed"], len(b["sessions"])])
    else:
        keyf = (lambda b: (b["start"].date().isoformat(), b["project"])) if args.by == "day" else (lambda b: (b["project"],))
        agg = {}
        for b in rows:
            a = agg.setdefault(keyf(b), {"minutes": 0, "blocks": 0, "commands": 0, "days": set()})
            a["minutes"] += b["minutes"]
            a["blocks"] += 1
            a["commands"] += b["commands"]
            a["days"].add(b["start"].date())
        if args.by == "day":
            w.writerow(["date", "project", "path", "minutes", "hours", "blocks", "commands"])
            for (d, p), a in sorted(agg.items()):
                w.writerow([d, os.path.basename(p), short(p), a["minutes"], hm(a["minutes"]), a["blocks"], a["commands"]])
        else:
            w.writerow(["project", "path", "minutes", "hours", "days", "blocks", "commands"])
            for (p,), a in sorted(agg.items(), key=lambda kv: -kv[1]["minutes"]):
                w.writerow([os.path.basename(p), short(p), a["minutes"], hm(a["minutes"]), len(a["days"]), a["blocks"], a["commands"]])
    if args.output:
        out.close()
        total = sum(b["minutes"] for b in rows)
        print(f"{args.output} に書き出しました（{since}〜{until}・{len(rows)} 件・計 {hm(total)}）", file=sys.stderr)
    return 0


def show_log(args):
    """その日（または期間）に打ったコマンドを、時刻順に出す。稼働報告の「やったこと」を書くときの手がかり用"""
    since, until = period(args)
    rows = []
    for session, events in load(since, until, not args.no_transcripts).items():
        for dt, cwd, command, ok in events:
            if not command:
                continue
            p = project_of(cwd, args.depth)
            if args.project and args.project not in (os.path.basename(p), short(p), p):
                continue
            rows.append((dt, os.path.basename(p), command, ok))
    for dt, p, command, ok in sorted(rows, key=lambda r: r[0]):
        c = " ".join(command.split())
        c = c if len(c) <= 200 else c[:200] + "…"
        print(f"{dt.strftime('%m-%d %H:%M')}  {p:<20} {c}{'' if ok else '  (失敗)'}")
    return 0


# ---------------------------------------------------------------- 入口

def main(argv=None):
    ap = argparse.ArgumentParser(prog="worklog.py", description="Claude Code での作業を、プロジェクトごと・日ごとの時間にする")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("hook", help="（Claude Code のフックから呼ばれる）")

    p = sub.add_parser("install", help="フックを ~/.claude/settings.json に入れる")
    p.add_argument("--dry-run", action="store_true", help="書き込まずに、書き込む中身を出す")
    p.add_argument("--uninstall", action="store_true", help="入れたフックを外す")

    def common(p):
        p.add_argument("--month", help="YYYY-MM（既定は今月）")
        p.add_argument("--since", help="YYYY-MM-DD")
        p.add_argument("--until", help="YYYY-MM-DD")
        p.add_argument("--days", type=int, help="今日を含む直近の日数")
        p.add_argument("--project", help="このプロジェクトだけ（フォルダ名か ~/ からのパス）")
        p.add_argument("--depth", type=int, default=2, help="git の無いフォルダを、ホームの下の何段目で丸めるか（既定 2 = ~/dev/foo）")
        p.add_argument("--no-transcripts", action="store_true", help="セッションの記録を読まず、フックの記録だけを使う")

    p = sub.add_parser("report", help="作業時間を CSV で出す")
    common(p)
    p.add_argument("--by", choices=["block", "day", "project"], default="block", help="まとめ方（既定 block = 作業のまとまりごと）")
    p.add_argument("--gap", type=int, default=15, help="何分を超えてあいたら別の作業にするか（既定 15）")
    p.add_argument("-o", "--output", help="書き出すファイル（既定は画面）")
    p.add_argument("--bom", action="store_true", help="Excel で文字化けしないよう、先頭に BOM を付ける")

    p = sub.add_parser("log", help="打ったコマンドを時刻順に出す（伏せ字済み）")
    common(p)
    p.add_argument("--date", help="YYYY-MM-DD（その日だけ）")

    args = ap.parse_args(argv)
    if args.cmd == "hook":
        return hook()
    if args.cmd == "install":
        return install(args.dry_run, args.uninstall)
    if args.cmd == "report":
        return report(args)
    if args.cmd == "log":
        if not (args.date or args.month or args.since or args.until or args.days):
            args.date = date.today().isoformat()
        return show_log(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
