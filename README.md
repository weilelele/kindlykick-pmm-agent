# 踢屁股专家 · Feishu PM Agent

> 一个跑在飞书群里的 AI 项目管理 Bot，自动整理任务、跟踪进展、定时催单。
> An AI-powered project management bot for Feishu (Lark) groups.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/template/feishu-pm-agent)

---

## 核心气质：踢屁股 + 彩虹屁

这个 Bot 有两个必须同时存在的特质：

「踢屁股」— 催进度、盯截止日、对拖延零容忍，必要时直接点名施压。

「彩虹屁」— 真心看见每个人的努力。不是泛泛的"加油"，而是具体的认可：
"你今天搞定了 XXX，这个挺难的，干得漂亮。"

每次有人汇报进展，Bot 会先给彩虹屁，再记录任务。每日播报里有专门的「今日彩虹屁」板块，点名表扬当天有贡献的成员。让团队感受到：努力是被看见的，不只是一台完成任务的机器。

---

## 功能亮点

| 功能 | 说明 |
|------|------|
| 智能意图识别 | 发任何消息给 Bot，Claude 自动判断是会议记录、进展汇报、还是任务分配 |
| 会议记录解析 | 发飞书文档链接 / 图片 / 文字 → 自动提取任务、分配负责人 |
| 进展追踪 | 群里 @Bot 汇报 → 自动关联任务并更新状态 |
| 批量操作 | "这几个都分配给 Sam" / "都完成了可以清掉" → Bot 直接执行 |
| 定时播报 | 每天 10:00 早间鼓励 + 18:00 晚间催单 |
| 成员自动注册 | 成员首次 @Bot 即自动登记，无需手动添加 |

---

## 架构

```
飞书群组 / 私聊
    │
    ▼  Webhook POST /webhook/event
FastAPI  (Railway / 任意云服务器)
    ├── handlers.py      消息路由
    │       └── classify_message()  ← Claude 判断意图
    ├── ai_client.py     Claude API（Haiku 路由 + Sonnet 播报）
    ├── database.py      Supabase（任务 / 成员 / 进展）
    ├── feishu_client.py 飞书消息发送
    └── scheduler.py     APScheduler 定时播报
```

**模型策略（省钱）**
- Claude Haiku：意图分类、任务提取、问答（每次对话触发）
- Claude Sonnet：仅用于早晚播报生成（每天 2 次）

---

## 快速开始

### 前置条件

- [飞书开放平台](https://open.feishu.cn) 账号（需要能创建企业自建应用）
- [Supabase](https://supabase.com) 免费账号
- [Anthropic API Key](https://console.anthropic.com)
- [Railway](https://railway.com) 账号（或其他支持 Docker 的平台）

---

### 第一步：Supabase 建表

1. 新建 Supabase 项目
2. 进入 **SQL Editor**，粘贴并运行 `schema.sql` 的全部内容
3. 记录：
   - **Project URL**：`https://xxxxxx.supabase.co`
   - **service_role key**（Settings → API → service_role）

---

### 第二步：飞书开放平台配置

进入 [open.feishu.cn](https://open.feishu.cn) → 创建**企业自建应用**。

#### 2.1 开启机器人能力
「添加应用能力」→「机器人」

#### 2.2 申请权限（权限管理）

| 权限标识 | 用途 |
|----------|------|
| `im:message` | 读取消息内容 |
| `im:message:send_as_bot` | 发送消息 |
| `im:chat.member:read` | 读取群成员（用于成员同步） |
| `contact:user.base:readonly` | 获取用户姓名（自动注册成员） |
| `docx:document:readonly` | 读取飞书文档内容 |
| `drive:drive:readonly` | 访问飞书云文档 |
| `im:resource` | 下载图片 / 文件 |

#### 2.3 配置事件订阅
「事件订阅」→ 添加事件：`im.message.receive_v1`

请求网址先填占位符，部署完成后回来更新：
```
https://your-app.railway.app/webhook/event
```

记录「验证 Token」备用。

#### 2.4 获取各项 ID

| 变量 | 获取方式 |
|------|----------|
| `FEISHU_APP_ID` | 应用凭证与基础信息页 |
| `FEISHU_APP_SECRET` | 同上 |
| `FEISHU_VERIFICATION_TOKEN` | 事件订阅页 |
| `OWNER_OPEN_ID` | 让自己在群里发一条消息，从 webhook payload 的 `event.sender.sender_id.open_id` 获取 |
| `GROUP_CHAT_ID` | 同上，取 `event.message.chat_id` |
| `BOT_OPEN_ID` | 把 Bot 加群后，从任意消息的 `mentions` 中找到 Bot 的 open_id |

---

### 第三步：部署

#### 方式 A：Railway 一键部署（推荐）

点击顶部「Deploy on Railway」按钮，按提示填入环境变量。

#### 方式 B：手动部署

```bash
git clone https://github.com/weilelele/caihongpi-tipigu-zhuanjia.git
cd caihongpi-tipigu-zhuanjia

cp .env.example .env   # 编辑填入所有变量

# 本地调试（需要 ngrok 转发）
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8080

# 另一个终端
ngrok http 8080
# 把 ngrok URL + /webhook/event 填到飞书事件订阅
```

部署到任意支持 Docker 的平台（Railway / Render / Fly.io）均可。

---

### 第四步：初始化

部署成功后：

1. 把 Bot 加入群聊
2. **Bot 会自动注册成员**：任何成员在群里 @Bot 发消息后，Bot 会通过飞书 API 查询姓名并自动登记
3. 也可以手动添加：和 Bot 私聊发 `/添加成员 姓名 open_id`

---

## 使用说明

### 任何人都可以（@Bot 在群里）

```
@踢屁股专家 今天把登录模块写完了
@踢屁股专家 这个需求有点卡，在等设计稿
@踢屁股专家 这几个任务都完成了，可以清掉
```

Bot 会自动判断是进展汇报、批量完成还是问询。

### 管理员私聊 Bot

| 操作 | 方式 |
|------|------|
| 解析会议记录 | 发飞书文档链接 / 图片截图 / 文字纪要 |
| 分配任务 | `这几个都分配给小明`（附上任务列表） |
| 查询任务 | `/状态` |
| 标记完成 | `/完成 任务ID前缀` |
| 查看成员 | `/成员` |
| 手动播报 | `/播报 morning` 或 `/播报 evening` |

---

## 环境变量说明

复制 `.env.example` 为 `.env` 并填写：

```bash
cp .env.example .env
```

| 变量 | 必填 | 说明 |
|------|------|------|
| `FEISHU_APP_ID` | ✅ | 飞书应用 ID |
| `FEISHU_APP_SECRET` | ✅ | 飞书应用 Secret |
| `FEISHU_VERIFICATION_TOKEN` | ✅ | 事件订阅验证 Token |
| `FEISHU_ENCRYPT_KEY` | 可选 | 事件加密 Key，留空不加密 |
| `ANTHROPIC_API_KEY` | ✅ | Claude API Key |
| `SUPABASE_URL` | ✅ | Supabase 项目 URL |
| `SUPABASE_SERVICE_KEY` | ✅ | Supabase service_role key |
| `OWNER_OPEN_ID` | ✅ | 管理员飞书 open_id |
| `GROUP_CHAT_ID` | ✅ | 目标群的 chat_id |
| `BOT_OPEN_ID` | ✅ | Bot 自身的 open_id |

---

## 文件结构

```
caihongpi-tipigu-zhuanjia/
├── main.py            FastAPI 入口 + Webhook 接收
├── config.py          环境变量（pydantic-settings）
├── handlers.py        消息路由与业务逻辑
├── ai_client.py       Claude API 封装
├── feishu_client.py   飞书消息 / 文档 / 用户 API
├── database.py        Supabase CRUD
├── scheduler.py       定时播报（APScheduler）
├── schema.sql         Supabase 建表 SQL
├── requirements.txt
├── Dockerfile
├── railway.toml
└── .env.example
```

---

## License

MIT
