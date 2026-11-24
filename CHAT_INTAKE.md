# 聊天记账（Telegram）

把不发邮件的消费通知转发给一个 Telegram bot，后端在本机解析、先发预览，确认后才写入 BudgetPilot。

支持：

- 文字短格式：`午饭 35 微信`
- 自然语言：`今天在星巴克花了 32 块，用的支付宝`
- 银行短信：`您尾号1234的卡11月22日18:30消费人民币128.00元，余额5000.00元`
- 支付成功截图、账单截图（可带说明文字）
- 按钮确认：确认入账 / 标为待确认 / 取消 / 修改内容
- `/undo` 撤销上一条已入账的消息

所有识别都在本机完成：图片用 RapidOCR，文字结构化用本地 Ollama（默认 `qwen2.5-7b`），不调用云端模型。

---

## 1. 首次配置

### 1.1 创建 bot

1. 在 Telegram 搜索 **@BotFather**；
2. 发送 `/newbot`，按提示取一个名字和用户名（用户名必须以 `bot` 结尾）；
3. 复制返回的 token，形如 `123456789:AA...`。

### 1.2 写 `.env`

```bash
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_ALLOWED_CHAT_IDS=
```

`TELEGRAM_ALLOWED_CHAT_IDS` 先用空值，下一步用来获取自己的 chat id。

### 1.3 安装依赖

```bash
cd ~/accounting_buddy
./venv/bin/pip install -e .
```

新增依赖是 `rapidocr-onnxruntime`（CPU 推理，模型随包安装，约 100MB）。

### 1.4 拿到自己的 chat id

前台运行：

```bash
./venv/bin/python intake_daemon.py
```

然后在 Telegram 里给 bot 发 `/start`。日志会出现：

```
Rejected message from chat 123456789; add it to TELEGRAM_ALLOWED_CHAT_IDS
```

把这个数字填回 `.env`：

```bash
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

多个 chat 用英文逗号分隔。按 `Ctrl+C` 退出前台进程。

> 白名单为空时所有消息都会被拒绝，这是有意的：bot 用户名是公开的，不能默认让任何人记账。

如果要在群聊里使用，先在 BotFather 里 `/setprivacy` 选择该 bot 并关闭隐私模式，重新把 bot 拉进群；否则 bot 只能看到 `/` 开头的命令，看不到普通消息。

### 1.5 安装常驻服务

```bash
mkdir -p ~/.config/systemd/user
ln -sf ~/accounting_buddy/accounting-intake.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now accounting-intake
journalctl --user -u accounting-intake -f
```

（本机已经建好 symlink 并 `daemon-reload`，填完 token 和白名单后只需 `systemctl --user enable --now accounting-intake`。）

服务只做长轮询，不监听端口，不需要公网 IP 或反向代理。

---

## 2. 配置项

`config.yaml`：

```yaml
intake:
  use_llm: true          # 关闭后只用规则和兜底正则，适合离线调试
  llm_timeout: 120
  default_account_id: 1  # 没识别出账户时用这个
  account_aliases:       # 补充特殊叫法；银行简称/钱包名会自动识别
    微信: 1
    支付宝: 1
    现金: 5
    尾号1234: 2
  account_refresh_seconds: 300  # 账户列表刷新间隔，新增账户无需重启
  duplicate_check: true  # 预览时查前后一天同账户同金额
  duplicate_lookback_days: 1
  confirm_ttl_minutes: 30  # 预览等待确认的时长，过期作废
```

- 账户 ID 可以在 BudgetPilot 的「账户」页面或 `GET /api/v1/accounts` 查到。
- `default_account_id` 不填时依次退回顶层 `default_account_id`、第一个启用账户。
- 不配置 `account_aliases` 也能用：账户名里的银行简称（汇丰、招行、中行、工行…）和钱包名（微信、支付宝）会自动匹配。
- 账户列表在服务启动时加载，之后每 `account_refresh_seconds` 秒刷新一次；消息落回默认账户时会立即强制刷新一次。网页新增账户后不用重启 intake。

---

## 3. 入账规则

### 3.1 账户

按顺序匹配：

1. `account_aliases` 命中（显式配置的特殊叫法）；
2. 带标签的卡号尾号：`尾号1234`、`卡号1234`、`(1234)`、完整卡号的后四位；支持 3 位尾号（如 `尾号066`，部分香港账户）；
3. 账户显示名、银行简称或钱包名出现在消息里：`汇丰` / `匯豐` / `HSBC` 可匹配「汇丰中国(5555/066)」，`微信` 可匹配「微信余额」；同一家银行有多张卡时，再用消息里的尾号缩小范围；
4. 独立的 4 位数字（排除金额、日期后）匹配账户尾号；
5. 复用邮件链路的 `account_mapping` 尾号映射；
6. `intake.default_account_id` / `default_account_id`；
7. 第一个启用账户。

尾号匹配会排除金额（`1000.00`）、日期（`2026-11`）等数字；如果多个账户共用同一个尾号，会用消息里出现的银行名（招商/中国银行/工商/建设/浦发/汇丰…）区分。

微信/支付宝的支付通知不会说明底层银行卡，所以通常需要把「微信」「支付宝」都指向你实际使用的账户，或者单独建一个电子钱包账户。账户显示名建议保留尾号，例如「招商银行储蓄卡(1234)」，这样聊天里只写尾号也能匹配。

### 3.2 分类

复用邮件链路：

1. 商户表里学习到的二级分类；
2. `merchant_rules.yaml` 的别名规则；
3. 学习到的一级分类；
4. 规则的一级分类；
5. **消息里直接出现的分类名**，最长优先，并校验收支类型；
6. 分类名/关键词；
7. 本地 LLM 兜底；
8. `fallback_expense_category_id` / `fallback_income_category_id`。

第 5 步是为聊天场景加的：说明或截图里写了「房租收入」「工资」「餐饮」这类分类名时，直接命中，不交给模型猜。例如说明是「房租收入」，会命中分类「房租收入」，而不会落到「其他收入」。

在 BudgetPilot 网页上修正一次某个商户的分类，后续聊天记账同样会生效。

### 3.3 预览确认

每条消息**只生成预览，不写账本**。预览里有四个按钮：

| 按钮 | 行为 |
|------|------|
| ✅ 确认入账 | 创建交易，`isConfirmed=true`，立即影响余额和预算 |
| 🕓 标为待确认 | 创建交易，`isConfirmed=false`，不影响余额，稍后可在网页确认 |
| ✏️ 修改内容 | 作废本条预览，下一条消息按修正后的内容重新解析 |
| ❌ 取消 | 丢弃，不写账本 |

- 预览默认 30 分钟有效（`intake.confirm_ttl_minutes`），过期按钮提示「已失效」；
- 按钮一直没点，就不会有任何交易；
- 每条 chat 最多保留 5 条待确认预览，超出时丢弃最旧的；
- 服务重启后预览仍在，按钮继续有效；
- 双击同一按钮只会入账一次，第二次提示「已处理」；
- 如果前后一天内、同账户、同金额、同类型已有交易，预览里会提示疑似重复的交易 ID，但不阻止你确认。

文字命令等价于按钮：

- `/confirm`：确认当前 chat 最新一条预览
- `/cancel`：取消最新一条预览
- `/edit 35 星巴克`：按给定内容重新解析；不带内容时进入「请发送修正内容」状态

「修改内容」或 `/edit` 不会改变幂等键：修正后的交易仍使用原始消息的 `externalId`。

### 3.4 幂等

每条消息生成确定性的 `externalId`：

```
chat:telegram:{chat_id}:{message_id}
一条消息解析出多笔时追加 :0、:1……
```

Telegram 重投、服务重启、API 超时重试都不会重复入账。

### 3.5 日期

- 消息里能识别出日期就用消息里的日期（支持 `今天`、`昨天`、`11月22日`、`2026-11-22`）；
- 识别不出就用当天；
- 银行短信里的时间会写入交易时间。

### 3.6 识别分工

一笔聊天记账由三层共同完成，不是全交给大模型：

| 环节 | 实现 | 负责什么 |
|------|------|----------|
| 读图 | RapidOCR（本地 OCR） | 像素 → 文字：金额、卡号尾号、日期时间、银行名 |
| 结构化 | qwen2.5-7b（本地 Ollama） | 文字 → 交易：收支方向、金额、日期时间、商户，以及置信度 |
| 账户/分类 | 确定性规则 | 尾号 → 账户；分类名/商户规则 → 分类；重复检测 |
| 入账 | 代码 | 幂等 externalId、预览确认、`/undo` |

账户和分类**不交给模型猜**，原因：

- 模型看不到 BudgetPilot 的账户列表和分类树，无法可靠映射；
- 尾号、分类名是确定性信息，用规则匹配可解释、可测试；
- 模型只负责自由文本里“哪个数字是金额、是收入还是支出”这类它擅长的部分。

如果 Ollama 不可用，简单文字仍能靠规则和保守正则入账；图片仍能 OCR，但复杂排版的结构化效果会下降。

---

## 4. 图片识别

- 引擎：RapidOCR（PP-OCR 模型 + onnxruntime，CPU）；
- 支持 Telegram 照片和以图片形式发送的文件（PNG/JPEG 等，≤20MB）；
- 识别出的文字和图片说明一起交给抽取器，一张账单有多条交易时全部提取；
- 图片不会上传到任何第三方。

截图越干净识别越准。建议只截「付款成功」那一屏，避免把聊天背景、余额和广告一起截进去。

---

## 5. 回复与撤销

收到消息后先回预览：

```
待确认 · 聊天记账

支出  CNY 35.00
商户：星巴克咖啡
分类：饮品
账户：微信钱包
日期：2026-11-23 12:30
识别把握不高（65%）

确认后才会写入账本。
预览 30 分钟内有效。
```

图片消息会额外附一小段 OCR 原文，方便核对。点「确认入账」后，原消息被编辑为：

```
✅ 已入账

支出  CNY 35.00
商户：星巴克咖啡
分类：饮品
账户：微信钱包
日期：2026-11-23 12:30
交易：#1927

回复 /undo 可撤销本次记账
```

- `/undo` 或「撤销」：删除当前 chat **上一条已入账消息**产生的所有交易；
- 待确认的交易不计入「上一条已入账」，需要在网页确认或单独处理；
- 多次 `/undo` 会继续往前找上一条；
- 删除交易由 BudgetPilot 处理，余额和预算会同步回滚。

---

## 6. 隐私与安全

- bot token 只放在 `.env`（已被 `.gitignore` 忽略），不要提交；
- `TELEGRAM_ALLOWED_CHAT_IDS` 是唯一准入条件，其他 chat 的消息直接丢弃并记录 chat id；
- Telegram bot 消息经过 Telegram 服务器，不是端到端加密；不要发送完整卡号、密码或验证码；
- OCR 和 LLM 都在本机，截图和文本不会出网；
- `intake_state.json` 保存 offset、待确认预览的解析结果和交易 ID；不保存图片，预览过期后自动清理；该文件已被 `.gitignore` 忽略，不要提交。

---

## 7. 已知限制

- 不支持转账类周期/内部转账；转账请用网页或邮件链路。
- 账户是推断出来的。账户名里的银行简称、钱包名和尾号会自动匹配；同一家银行有多张卡时建议在账户名里保留尾号，仍无法区分时再用 `account_aliases` 显式指定。
- 重复检测只覆盖「已有交易 → 聊天入账」方向。如果先聊天记了一笔、之后招行又发来同一笔的邮件，邮件链路仍会再记一笔；收到邮件后请核对，必要时 `/undo` 撤销聊天那笔。银行会发邮件的卡消费，优先让邮件链路自动处理。
- OCR 对复杂排版、深色模式截图、艺术字可能出错，记错后直接 `/undo`。
- 本地 7B 模型在 CPU 上单条约 3–15 秒；图片多一步 OCR，再加一次点击确认。
- 一条消息最多取 20 笔交易；多条交易只能整体确认，不能只勾其中一条。
- 预览 30 分钟后失效，需要重新发送；服务停机期间过期的预览不会自动入账。
- Telegram 在某些网络环境需要代理；`api.telegram.org` 不可达时服务会每 5 秒重试并写日志。
- 同一个 bot token 同时只能有一个 `getUpdates` 消费者；不要让 OpenClaw 和 intake 同时轮询同一个 bot（会报 `409 Conflict`）。

---

## 8. 排障

```bash
# 服务状态与日志
systemctl --user status accounting-intake
journalctl --user -u accounting-intake -n 100 --no-pager

# 手动跑一轮
./venv/bin/python intake_daemon.py --once --verbose

# 检查 Ollama
ollama list
curl -s http://127.0.0.1:11434/api/tags | head

# 检查 OCR
./venv/bin/python -c "from intake.ocr import available; print(available())"
```

常见错误：

| 现象 | 原因 |
|------|------|
| `409 Conflict` | 同一 token 有另一个轮询者（OpenClaw/另一个 intake） |
| `Unauthorized` | token 错误或被 BotFather 重置 |
| 一直 `Rejected message from chat ...` | chat id 没加进白名单，或服务没重启 |
| `没识别出交易` | 消息没有金额；或 Ollama 未启动、模型名不对 |
| `图片识别失败` | RapidOCR 未安装，或图片格式不支持 |
| 按钮提示「预览已失效」 | 超过 30 分钟，或已被确认/取消；重新发送即可 |
| 点按钮没反应 | intake 服务没运行；或消息超过 48 小时无法编辑，看日志 |
| `libGL.so.1` 找不到 | 安装 `opencv-python-headless` 或系统 libGL |

---

## 9. 测试

```bash
./venv/bin/python -m unittest tests.test_intake_extractor tests.test_intake_service tests.test_intake_telegram tests.test_intake_daemon -v
```

覆盖：短格式解析、日期归一化、LLM JSON 解析、退款纠正、账户别名、幂等 externalId、重复检测、多笔交易、预览不写库、确认/待确认/取消、双击幂等、修改后重解析、预览过期、undo、Telegram update/callback 解析、offset 和草稿持久化。