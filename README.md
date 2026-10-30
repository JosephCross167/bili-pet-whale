# bili-pet-whale

一只住在 B 站账号里的赛博宠物：自己刷推荐流、按你的口味挑视频、会评论、会私信、会撒娇，也会拒绝你。

**不是做号，是养宠物。**

默认人格是一只爱吃白饭的小鲸鱼，但**人格、口味、行为阈值全部可改**——改完就是你的宠物。

---

## 它能干什么

| 能力 | 说明 |
|---|---|
| **看视频** | 每轮刷自己的推荐流 → 规则粗筛 → AI 细筛 → 打分 |
| **分享** | 按你的口味匹配分挑视频私信给你；够分才发 |
| **评论** | 走浏览器 DOM 发评论，可带 @、可同步动态 |
| **互动** | 点赞 / 收藏 / 投币（只对它自己觉得有意思的） |
| **私信** | 只回你一个人；支持指令（点赞、收藏、找视频、统计、传话） |
| **回复** | 你回它的评论，它在楼中楼里回你 |
| **@ 处理** | 你 @ 它，按私信标准聊；陌生人 @ 它，按陌生人规则（只回一次、只回问句） |
| **指定 @ 某人** | 你下指令让它去某条视频下 @ 指定 UID，一次性、24 小时过期 |
| **养推荐流** | 每天真播几条按口味搜来的视频，把推荐流拉回你的兴趣区 |
| **情绪** | 0~100，会累会记仇，每天回归；低情绪时会拒绝或撒娇 |

## 安全边界（结构性的，改配置也破不了）

1. **单白名单**：私信 / @ 只认 `admin_uid` 一个人
2. **两级额度**：日常预算 + 硬上限（特批也不破）
3. **暂停 / 熔断**：暂停后不主动做任何事；一次 412 当天全停
4. **去重**：同一条私信只执行一次；@ 授权一次性
5. **不主动搭讪**：陌生人只回问句，每人每天 1 次、每天共 3 次

---

## 你要改的四样东西

| 文件 | 是什么 | 不改会怎样 |
|---|---|---|
| `persona.md` | **人格**：说话风格、自称、脾气、口头禅 | 它会一直是一只爱吃白饭的小鲸鱼 |
| `config.toml` | **行为阈值**：评论概率、每天上限、分享概率、静默天数 | 用默认值也能跑，但节奏是别人的 |
| `state/self_taste.json` | **宠物自己的口味**：它觉得什么有意思（tag → 权重） | 没有它，它不会"自己喜欢"任何东西 |
| 口味画像（`state/`） | **你的口味**：从你的收藏夹抽 tag 算权重 | 没有它，它不会挑给你的视频 |

### 1. 人格 `persona.md`

直接改文字就行。改完重启 daemon 生效。`scenarios.md` 是配套的对话样例，可以一起改。

### 2. 行为 `config.toml`

常改的几项：

```toml
[guard.daily_budget]      # 每天最多做几次
comment = [0, 10]         # 评论 0~10 条
dm      = [0, 30]         # 私信

[taste]
min_score_ours = 1.0      # 你的口味匹配到多少分才 @ 你
min_score_self = 1.5      # 它自己多喜欢才互动

[speak]
comment_chance = 0.5      # 每轮发评论的概率（骰子）
share_chance   = 0.55     # 每轮分享的概率
```

### 3. 宠物自己的口味 `state/self_taste.json`

```json
{
  "tokens": {
    "deepseek": 6.0,
    "claude": 5.0,
    "大肥鱼": 4.0,
    "整活": 1.0
  }
}
```

权重是相对值，越大越喜欢。可以手写，也可以不建——没有这个文件，它就对什么都不特别上心。

### 4. 你的口味画像

从你的 B 站收藏夹学：

```powershell
.\.venv\Scripts\python.exe scripts\build_taste.py
```

它会抽样收藏夹、取 tag、算权重，写到 `state/taste_<你的UID>.json`。
**跑一次就够**，之后想更新再跑。

---

## 必须补充的东西

`secrets.toml`（复制 `secrets.example.toml` 改）。少一项就跑不起来，会明确告诉你缺什么：

| 配置 | 从哪来 |
|---|---|
| `[account] uid` | 宠物号的 UID |
| `[targets] admin_uid` | 你的 UID（唯一白名单） |
| `[targets] report_mail` | 收日报的邮箱 |
| `[llm] api_key` | DeepSeek API key |
| `[mail] username/password` | 163 邮箱 + 授权码（不想要邮件可以只填前四项，`[mail].enabled=false`） |

**`secrets.toml` 已被 `.gitignore` 忽略**，不会进仓库。

---

## 快速开始

```powershell
# 1. 装环境
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\playwright.exe install msedge

# 2. 填 secrets
copy secrets.example.toml secrets.toml
notepad secrets.toml

# 3. 登录宠物号（扫码）
.\.venv\Scripts\python.exe -m bili.auth login

# 4. 学你的口味
.\.venv\Scripts\python.exe scripts\build_taste.py

# 5. 试跑一场（不发任何东西）
.\.venv\Scripts\python.exe runner.py --force --no-say

# 6. 正式常驻
.\.venv\Scripts\python.exe runner.py --daemon
```

> 第一次跑建议用 `--no-say`，看完日报再开写操作。

## 长期运行

```powershell
# 注册开机自启（需要管理员权限）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-task.ps1
```

没有管理员权限时，用 `scripts\start-supervisor.vbs` 放进启动文件夹（每 5 分钟自检一次）。

## 已知限制

| 限制 | 说明 |
|---|---|
| 楼中楼对新评论无效 | B 站索引延迟，刚发的评论几分钟内定位不到；失败就不发 |
| API 投币需人机验证 | 只能走浏览器点击，每次固定扣 2 枚 |
| API 删除评论假成功 | `code:0` 但不删，只能用浏览器 DOM（`comment_dom.delete_comment`） |
| AI 摘要覆盖约 50% | 一半视频读不到内容，只能靠标题 + 标签 |
| 分区名常为空 | 用内置 tid→名称表兜底 |

## License

MIT。人格随便改，改完就是你的宠物。
