"""python3 -m unittest discover tests"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date

TMP = tempfile.mkdtemp()
os.environ["CLAUDE_WORKLOG_HOME"] = os.path.join(TMP, "worklog")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import worklog  # noqa: E402

PROJ = os.path.join(TMP, "work", "shop-site")


def write_transcript(session, entries):
    d = os.path.join(worklog.PROJECTS_DIR, "-work-shop-site")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, session + ".jsonl"), "w") as f:
        for ts, kind, content in entries:
            f.write(json.dumps({"type": kind, "timestamp": ts, "cwd": PROJ, "sessionId": session,
                                "message": {"content": content}}) + "\n")


class RedactTest(unittest.TestCase):
    def test_secrets(self):
        cases = {
            "mysql -u app -pS3cretPass db": "mysql -u app -p*** db",
            "curl -H 'Authorization: Bearer abc.def' https://x": "curl -H 'Authorization: Bearer ***' https://x",
            "DB_PASSWORD=hunter2 php artisan migrate": "DB_PASSWORD=*** php artisan migrate",
            "git clone https://me:tok123@example.com/r.git": "git clone https://me:***@example.com/r.git",
            "aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE": "aws configure set aws_access_key_id AKIA***",
            "export OPENAI=sk-proj-abcdefghijklmnop": "export OPENAI=sk-proj***",
            "curl -X POST https://hooks.slack.com/services/T000/B000/XXXX": "curl -X POST https://hooks.slack.com/services/***",
        }
        for src, want in cases.items():
            self.assertEqual(worklog.redact(src), want, src)

    def test_no_false_positives(self):
        for src in ("find . -perm 644 -print", "ssh -p2222 host", "git log -p",
                    "cd /home/user/projects/client2025/backend/app"):
            self.assertEqual(worklog.redact(src), src)

    def test_keeps_hashes(self):
        sha = "a" * 40
        self.assertEqual(worklog.redact("git checkout " + sha), "git checkout " + sha)


class ReportTest(unittest.TestCase):
    def setUp(self):
        os.makedirs(os.path.join(PROJ, ".git"), exist_ok=True)
        write_transcript("s1", [
            ("2026-11-10T00:00:00.000Z", "user", "質問"),
            ("2026-11-10T00:05:00.000Z", "assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "php artisan test"}}]),
            ("2026-11-10T00:06:00.000Z", "user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True}]),
            ("2026-11-10T00:20:00.000Z", "assistant", "回答"),
            # 40 分あいたので別の作業
            ("2026-11-10T01:00:00.000Z", "user", "続き"),
            ("2026-11-10T01:10:00.000Z", "assistant", "回答"),
        ])
        # フックの記録のうち、セッションの記録がある s1 は捨て、ない s0 だけを使う
        os.makedirs(worklog.LOG_DIR, exist_ok=True)
        with open(os.path.join(worklog.LOG_DIR, "2026-11-10.jsonl"), "w") as f:
            for ts in ("2026-11-10T00:00:00+00:00", "2026-11-10T09:00:00+00:00"):
                f.write(json.dumps({"ts": ts, "session": "s1", "cwd": PROJ, "tool": "Bash", "command": "ls"}) + "\n")
            for ts in ("2026-11-10T05:00:00+00:00", "2026-11-10T05:30:00+00:00"):
                f.write(json.dumps({"ts": ts, "session": "s0", "cwd": PROJ, "tool": "Bash", "command": "ls", "ok": True}) + "\n")

    def run_report(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            worklog.main(["report", "--since", "2026-11-09", "--until", "2026-11-11", *args])
        return buf.getvalue().strip().splitlines()

    def test_blocks(self):
        rows = self.run_report()
        self.assertEqual(rows[0].split(",")[:6], ["date", "project", "path", "start", "end", "minutes"])
        body = [r.split(",") for r in rows[1:]]
        self.assertEqual([r[5] for r in body], ["20", "10", "1", "1"])
        self.assertEqual(body[0][1], "shop-site")
        self.assertEqual(body[0][7:9], ["1", "1"])  # コマンド 1 件・失敗 1 件

    def test_by_project(self):
        rows = self.run_report("--by", "project")
        self.assertEqual(rows[1].split(",")[2], "32")

    def test_hook_writes_no_output(self):
        stdin = io.StringIO(json.dumps({"hook_event_name": "PostToolUse", "session_id": "s9", "cwd": PROJ, "tool_name": "Bash",
                                        "tool_input": {"command": "TOKEN=abcdef123 deploy"}, "tool_response": {"stdout": "顧客の住所"}}))
        worklog.hook(stdin)
        path = os.path.join(worklog.LOG_DIR, date.today().isoformat() + ".jsonl")
        with open(path, encoding="utf-8") as f:
            last = json.loads(f.read().splitlines()[-1])
        self.assertEqual(last["command"], "TOKEN=*** deploy")
        self.assertNotIn("住所", json.dumps(last, ensure_ascii=False))

    def test_hook_never_raises(self):
        self.assertEqual(worklog.hook(io.StringIO("not json")), 0)


if __name__ == "__main__":
    unittest.main()
