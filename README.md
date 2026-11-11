# claude-code-worklog

Claude Code で作業した時間を、**プロジェクトごと・日ごと**の CSV にする小さなツールです。
受託や保守の稼働報告・工数の集計に、そのまま使えます。

```
$ python3 worklog.py report --by day --month 2026-11
date,project,path,minutes,hours,blocks,commands
2026-11-04,shop-site,~/dev/shop-site,95,1:35,3,41
2026-11-04,client-api,~/work/client-api,30,0:30,1,12
2026-11-05,shop-site,~/dev/shop-site,140,2:20,4,63
```

- Python 3.9 以上の標準ライブラリだけで動きます（`pip install` は不要）
- **外には何も送りません。** 読むのも書くのも手元のファイルだけです
- コマンドの出力は残しません。パスワードやトークンらしい値は、書き込む前に伏せます

## 使い方

```sh
git clone https://github.com/moldspoon/claude-code-worklog.git ~/.claude-worklog-src
cd ~/.claude-worklog-src

python3 worklog.py report                 # 今月の作業（まとまりごと）
python3 worklog.py report --by day        # 日 × プロジェクトの合計
python3 worklog.py report --by project    # プロジェクトごとの合計
python3 worklog.py report --month 2026-10 --project shop-site -o 2026-10.csv --bom
python3 worklog.py log --date 2026-11-10  # その日に打ったコマンド（伏せ字済み）
```

フックを入れなくても、`report` はすぐ使えます。Claude Code が自分で残しているセッションの記録
（`~/.claude/projects`）を読むからです。

### フックを入れる（おすすめ）

セッションの記録は、Claude Code の既定の設定では **30 日で消えます**。月末に先月分を集計したいときに足りなくなるので、
フックで時刻とコマンドだけを別に残しておきます。

```sh
python3 worklog.py install            # ~/.claude/settings.json にフックを足す（元のファイルは .bak に残る）
python3 worklog.py install --dry-run  # 書き込む中身を見るだけ
python3 worklog.py install --uninstall
```

記録は `~/.claude-worklog/log/YYYY-MM-DD.jsonl` に、自分だけが読める権限（600）で残ります。
1 行に入るのは、時刻・セッションの ID・作業したフォルダ・道具の名前と、Bash なら伏せたコマンドだけです。
質問の文面・ファイルの中身・コマンドの出力は書きません。

手で入れる場合は、`~/.claude/settings.json` に次を足してください（パスは置き場所に合わせて）。

```json
{
  "hooks": {
    "PostToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "python3 /path/to/worklog.py hook" }] }],
    "PostToolUseFailure": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "python3 /path/to/worklog.py hook" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 /path/to/worklog.py hook" }] }]
  }
}
```

## 時間の数え方

決まった計算だけで出します。同じ記録からは、いつ集計しても同じ時間になります。

- 記録を**プロジェクトごと**に時刻順に並べ、前の記録から **15 分を超えてあいたら**別の作業にします（`--gap 30` などで変更可）
- 1 つの作業の時間は、最初の記録から最後の記録まで。記録が 1 件だけなら 1 分
- 記録には、コマンドのほかに**やり取りの時刻**も入ります。コードを読ませたり相談したりしている時間も数えるためです
- 日をまたぐ作業は、日付で区切ります
- 別々のプロジェクトで同時にセッションを開いていると、それぞれに時間が付きます（合計は実時間より長くなります）

### プロジェクトの見分け方

作業したフォルダから上にたどり、`.git` があるフォルダを 1 つのプロジェクトにします。
無ければ、ホームの下の 2 段目（`~/dev/foo` など）に丸めます（`--depth` で変更可）。

## 伏せる値

フックの記録に書き込む前と、`log` で表示する前に伏せます。

- `mysql -pXXXX`、`--password=`、`DB_PASSWORD=`、`API_KEY=`、`token:` などの値
- `Authorization: Bearer` / `Basic` のあとの値、URL に埋め込んだ `user:pass@`
- AWS のアクセスキー、`sk-…`、`sk_live_…`、GitHub・Slack のトークン、Google の API キー、JWT、秘密鍵の本体
- Slack・Discord の Webhook の URL
- 英字と数字が混ざった 40 文字以上の塊（16 進だけの git のハッシュは残します）

パターンで拾うので、**取りこぼしはあり得ます。** 記録のファイルを人に渡す前には中身を確かめてください。
取りこぼした形を見つけたら Issue で教えてください（値そのものは書かずに）。

## 稼働報告から請求まで回すなら

この CSV を、顧客ごとの保守台帳と月末の請求書まで自動でつなげるサービスも作っています。
→ [メンテノート](https://mentenote.com/?from=github)（保守を請け負う開発会社向け・運営：モルドスプーン株式会社）

## English

A tiny, dependency-free Python script that turns your Claude Code sessions into a per-project, per-day timesheet (CSV).
It reads Claude Code's own transcripts (`~/.claude/projects`) and, optionally, a hook log that keeps timestamps and
redacted commands beyond the 30-day transcript retention. Nothing is sent anywhere; command output is never stored.

```sh
python3 worklog.py install      # add hooks to ~/.claude/settings.json (backup is kept)
python3 worklog.py report --by day --month 2026-11
```

## License

MIT
