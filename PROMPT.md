# 懒人部署：把下面这段直接丢给 DSH

不用自己敲命令。把**整段**复制给你的 AI 助手（DSH / Claude Code / Cursor 都行），它会一步步问你、然后装好。

---

```text
我想在本地部署 https://github.com/JosephCross167/bili-pet-whale 这个项目（一只自动刷 B 站的赛博宠物）。

你负责从头装好，规则：

1. 先检查环境：Windows、Python 3.11+、git、Edge 浏览器。缺什么告诉我怎么装。
2. 克隆到一个我指定的目录，创建 .venv，装 requirements.txt，再装 playwright 的 msedge。
3. 然后停下来问我这几项（不要猜，也不要自己编）：
   - 宠物账号的 UID 和昵称
   - 我自己的 B 站 UID
   - 收日报的邮箱
   - DeepSeek API key
   - （可选）163 邮箱和授权码；我说不填就跳过，并把 [mail].enabled 设成 false
   拿到后写进 secrets.toml（从 secrets.example.toml 复制），并确认 secrets.toml 在 .gitignore 里。
4. 跑 `python -m bili.auth login` 让我扫码登录宠物号，登录后跑 `python -m bili.auth check` 确认。
5. 跑 `python scripts/build_taste.py` 生成我的口味画像（用我的收藏夹）。
6. 问我"宠物自己喜欢什么"（几个 tag + 权重），按 README 的格式写 state/self_taste.json。
7. 跑 `python runner.py --force --no-say` 出一份日报，把日报前 40 行贴给我看，让我确认：
   - 视频归属（我的 / 它自己的 / 中立）判断对不对
   - 有没有明显跑偏
8. 我确认后再启动常驻：`python runner.py --daemon`，
   并帮我配开机自启（scripts/install-task.ps1，需要管理员权限；没权限就用 start-supervisor.vbs）。

约束：
- 不要修改 README 里写的"安全边界"（白名单、额度、暂停熔断）。
- 任何会真的发评论/私信/投币的操作，先问我。
- 每一步做完告诉我结果，出错就停下来问，不要连着猜三步。
- 不要用 PowerShell 重写中文文本文件（会乱码），改文件用编辑工具。

目标：装完后它每天自己刷视频，按我的口味分享，偶尔评论，我能用私信指挥它。
```

---

## 为什么这样写

| 设计 | 原因 |
|---|---|
| 明确"停下来问" | 密钥和 UID 不能猜，猜错会绑到别人账号 |
| 先 `--no-say` 试跑 | 写操作不可逆，先看它分类对不对 |
| 限定"不要改安全边界" | 白名单和额度是整个项目的地基 |
| 禁止 PowerShell 改文本 | Windows 下 UTF-8 会被写成 GBK，实测毁过文件 |

## 我这边（作者）跑一次大概要多久

| 步骤 | 时间 |
|---|---|
| 装环境 | 3~5 分钟 |
| 登录扫码 | 1 分钟 |
| 建画像 | 1~3 分钟（看你收藏夹多大） |
| 试跑一场 | 2 分钟 |
| 合计 | **约 10 分钟** |

## 常见卡住的地方

| 卡在哪 | 原因 |
|---|---|
| `playwright install` 很慢 | 用系统 Edge 省掉 150MB 下载：代码里 `channel="msedge"` 已经这么做了 |
| 登录后 check 不通过 | cookie 存到 `state/cookies.json`，检查是否被沙箱拦了写权限 |
| 画像全 0 分 | 收藏夹是空的，或者 `build_taste.py` 没跑完 |
| 日报里全是"中立" | `self_taste.json` 没建，或 `min_score_*` 设太高 |
| 评论发了看不到 | 新号正常现象，B 站会影子隐藏，日报里会标 |
