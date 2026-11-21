# 变更记录

## 2026-11-21 — 邮件投递可靠性 + 招行格式补全 + 三层分类重构

本次改动与 `~/budgetpilot`（BudgetPilot 账本）配套上线，涉及两边的接口、数据库和服务配置。本文记录改动内容、数据快照和踩坑经验，供后续排查/升级参考。

---

### 1. 邮件投递模型（`email_fetcher.py` + `main.py`）

**之前的问题**

- 只扫 `UNSEEN`：手机上先读过的招行邮件永久漏账。
- 拉取阶段就标记 `\Seen` 并推进 `state.json`：API 失败或进程崩溃后邮件不会重试，直接丢账。
- 首封邮件失败时，后续邮件仍被处理，游标语义混乱。
- 非招行邮件会让游标永远不推进，每轮重复下载。

**现在的行为**

- 按 UID 范围增量扫描 `(last_uid+1):*`，不再依赖 `UNSEEN`。
- 首次运行（`state.json` 无记录）把游标初始化到当前最新 UID，**不重放历史**；历史导入用 `--full-sync`。
- 拉取阶段不标已读、不写 state；`main.py` 只在某封邮件的全部交易都成功（或服务端幂等命中）后调用 `mark_processed()` 标记 `\Seen` 并推进游标。
- 失败邮件留在 UID 范围内，下一轮自动重试。
- 非招行邮件也会推进游标，避免重复下载。
- `--full-sync` 搜索全部邮件，交易以待确认状态写入，不标已读、不更新 state。
- `--dry-run` 不再强制全量，只预览增量；历史预览用 `--full-sync --dry-run`。
- 交易级幂等键：`externalId = cmb:<邮件UID>:<交易序号>`；公积金推算收入用 `cmb:<UID>:pf:<序号>`，重试/重放不会重复入账。

---

### 2. 支持的招行邮件格式（`email_parser.py`）

在原有 6 种格式基础上补齐：

| 格式 | 识别特征 | 记账方式 |
|------|----------|----------|
| 转账汇款 / 实时转至他行 | `转账汇款人民币…收款人…` | 支出 |
| 收到本行转入 / 他行实时转入 | `转入人民币…付方…` | 收入 |
| 信用卡还款（带目标卡） | `向尾号为…的信用卡还款` | 转出卡 → 目标信用卡转账 |
| 信用卡还款交易（无目标卡） | `信用卡还款交易人民币…` | 支出 |
| 银联入账 / 银联扣款 | `银联入账/扣款人民币…` | 收入 / 支出 |
| 通用入账 | `入账<金额>元，余额…` | 收入 |
| Apple Pay / 线上支付 | `（设备卡号…）…线上支付…` | 支出 |
| 信用卡退货/退款 | 账单里 `CNY -金额 尾号X 退货/退款` | 收入（退款分类） |
| 大额存单到期 | `大额存单于…到期转入活期，本金…利息…` | 本金：投资账户→银行卡转账；利息：收入 |
| 银行活期转证券保证金 | `银行活期转证券保证金人民币…` | 银行卡 → 投资账户转账 |
| 证券保证金转银行活期 | `证券保证金转银行活期人民币…` | 投资账户 → 银行卡转账 |
| 自助购汇 | `自助购汇，购汇港币现汇…人民币减少…` | 银行卡 → 投资账户转账 |
| 港币柜台取现 | `柜台取款港币…` | **不自动入账**（邮件只有 HKD 金额，保留告警） |

**日期与字段修正**

- 月/日统一以邮件 `Date` 头为基准；天真日期晚于邮件日期时回退一年，处理跨年补录。
- 自动转账没有正文时间，使用邮件自身日期时间，不再用 `now()`。
- BudgetPilot 限制：`note ≤ 200`、`merchantName ≤ 50`、`transactionTime` 必须是 `HH:mm:ss`；payload 统一截断，空时间不发送。
- 修复 MIME 头解码（`=?utf-8?B?...`）和 `email.message` 子模块未显式导入导致的 ImportError。

---

### 3. 公积金收入（`main.py: build_entries`）

**发现的问题**

招行不会为公积金卡（3416）的入金单独发邮件，只有每月 6 日的“资金归集 3416→0611”。系统原来只看到转账，所以收入为 0。

**方案**

- 配置 `provident_fund_card: '3416'`。
- 遇到从该卡发出的资金归集，先补一笔同额收入（商户“公积金入账”、分类“公积金”、幂等键 `cmb:<uid>:pf:<idx>`），再记转账。
- 这样 3416 有余额可扣，收入也进入报表；每月“收入 + 转出”自动对平。
- 历史 9 个月已补，合计与 3416 全部转出金额一致。
- 注意：如果以后真的收到入金邮件，需要关闭推算或做去重，避免重复。

---

### 4. 资产划转与投资账户

- `config.yaml` 新增 `investment_account_id`。
- 解析器用特殊后缀 `INVEST` 表示投资账户；`main.py` 通过配置映射到真实账户 ID。
- 大额存单本金、证券保证金转出入、自助购汇均记为转账；存单利息记为收入。
- 港币柜台取现只给 HKD 金额，未自动入账，仅告警；本次历史中的一笔按前一日购汇成本人工折算。

---

### 5. 三层分类体系

分类顺序（命中即止）：

1. **学习映射**：启动时拉取 BudgetPilot 商户表，`商户名/别名 → 分类`；只信任非“其他”分类。网页里修改交易分类时，后端会同步更新关联商户的默认分类。
2. **别名规则**（`merchant_rules.yaml`）：清洗支付渠道前缀、公司后缀、`快捷支付` 等，再按品牌/法律主体匹配；同时参考邮件备注，按 pattern 长度和交易类型过滤。
3. **分类名/关键词匹配**：旧逻辑，长词优先。
4. **本地 Ollama 兜底**（`llm_classifier.py`）：前三层失败时调用；结果缓存 `llm_cache.json`；置信度低于 `llm_min_confidence` 写 `llm_review.csv`，不落库；“特约商户、扫二维码付款、收到本行转入、银联入账”等无信息文本主动跳过。
5. 仍无结果 → `fallback_expense_category_id` / `fallback_income_category_id`。

**历史重分类**

```bash
cd /home/gubin/accounting_buddy
./venv/bin/python reclassify.py --dry-run     # 预览
./venv/bin/python reclassify.py --no-llm      # 只用规则/学习/关键词
./venv/bin/python reclassify.py               # 含 Ollama 兜底
```

只处理当前仍为“其他支出/其他收入”的交易，通过 BudgetPilot 的批量接口 `POST /api/v1/transactions/reclassify` 只改 `category_id`，不动余额、预算和预警。

**新增分类**

- 通讯
- 订阅服务
- 人情往来
- 退款（收入）
- 公积金（收入，之前已建）
- 报销（使用系统已有分类）

**结果快照（2026-11-21，仅计数）**

| 分类 | 前 | 后 |
|------|---:|---:|
| 其他支出 | 859 | 15 |
| 其他收入 | 86 | 9 |
| 餐饮 | 62 | 499 |
| 购物 | 71 | 273 |
| 交通 | 140 | 178 |
| 人情往来 | — | 48 |
| 订阅服务 | — | 42 |
| 医疗 | 11 | 35 |
| 通讯 | — | 21 |
| 娱乐 | — | 13 |
| 住房 | 0 | 5 |
| 退款（收入） | — | 77 |

剩余 15 笔“其他支出”是 8 笔“特约商户”和 7 笔“扫二维码付款”，邮件没有可用商户信息，未强行猜测；剩余 9 笔“其他收入”是转账类资金流入，建议后续单独改成转账类型。

---

### 6. 配置项（`config.yaml`）

```yaml
account_mapping: {...}
category_mapping: {...}
fallback_expense_category_id: 188
fallback_income_category_id: 179
investment_account_id: 6
provident_fund_card: '3416'

llm_enabled: true
ollama_url: http://127.0.0.1:11434
ollama_model: qwen2.5-7b
llm_min_confidence: 0.8
llm_timeout: 120
ollama_disable_thinking: false
```

注意：程序在映射变化时会用 `yaml.dump` 重写 `config.yaml`，**注释不会被保留**；配置说明以 README/本文为准。

---

### 7. 测试与验证

```bash
cd /home/gubin/accounting_buddy
./venv/bin/python -m unittest discover -s tests -t . -v
```

- 当前 35 个用例全部通过。
- 覆盖：跨年日期、各邮件格式、字段截断、空时间、公积金推算、资产划转、规则匹配、名称清洗、学习映射、LLM 兜底顺序、UID 拉取/游标/标记已读。
- `llm_cache.json`、`llm_review.csv` 已加入 `.gitignore`（含商户名，不应提交）。
- `.env` 权限已改为 600。

---

### 8. 经验与坑

1. **招行邮件里的商户是法律主体名**：`北京三快在线科技有限公司`=美团、`上海拉扎斯信息科技有限公司`=饿了么、`金拱门`=麦当劳、`中国铁路网络有限公司`=12306。靠品牌关键词永远匹配不到，必须有“法律主体/别名 → 品牌 → 分类”的规则层。
2. **银行通知不完整**：公积金入金没有邮件；港币取现只有港币金额；大额存单到期只给本金和利息。能用现有邮件推断的（归集金额=入金）就推断，不能可靠折算的（HKD）宁可告警不记账。
3. **163 IMAP**：`OR` 搜索不可靠，统一拉取后客户端过滤；必须发 IMAP ID 命令；按 UID 而不是 `UNSEEN` 扫描，否则手读邮件会漏。
4. **BudgetPilot 的错误码**：业务错误 HTTP 仍返回 200，必须看 `code`；`10004` 是“名称已存在”，不是交易重复；交易幂等靠 `externalId`。
5. **字段硬限制**：`note` 200、`merchantName` 50、`transactionTime` `HH:mm:ss`。历史工资/转账邮件正文当 note 会超长，必须截断；自动转账的空时间会导致反序列化失败。
6. **确认历史不要走逐笔 API**：借记卡余额为 0 时会报 20003。要么先设余额/初始余额，要么用“只改分类”的批量接口或数据库批量确认后再设置余额。
7. **首次运行不会回放历史**：游标初始化到最新 UID。需要历史时显式 `--full-sync`，且 `--full-sync` 写入的是待确认交易，不动余额。
8. **Ollama 模型下载**：官方源可能被限速到几百 KB/s；ModelScope 国内镜像可到 20–35 MB/s。下载 GGUF 后用 `ollama create` 导入：
   ```bash
   curl -L -o qwen2.5-7b-instruct-q3_k_m.gguf \
     "https://modelscope.cn/models/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/master/qwen2.5-7b-instruct-q3_k_m.gguf"
   printf 'FROM %s\nPARAMETER temperature 0\nPARAMETER num_ctx 4096\n' "$PWD/qwen2.5-7b-instruct-q3_k_m.gguf" > Modelfile
   ollama create qwen2.5-7b -f Modelfile
   ```
9. **小模型不可用**：`qwen-3b-edge` 会把美团判成“金融/住宿”，置信度还不能信；7B Q3_K_M 在 CPU 上每商户约 4–7 秒，分类质量可用于兜底。Qwen3 系列如果出现只输出思考过程/空 JSON，可尝试 `think: false`。
10. **LLM 只做兜底**：不要让它覆盖规则和用户修正；结果要缓存、低置信度进 review，通用“特约商户/二维码付款”直接跳过。
11. **配置注释会丢**：`save_config()` 用 `yaml.dump` 重写文件，注释不会保留，重要说明放 README/CHANGELOG。
12. **导入顺序**：升级 BudgetPilot 时必须先执行 `external_id_migration.sql` 再上新代码；否则交易查询/插入会因未知字段失败。

---

### 9. 相关文件

- `email_fetcher.py`：UID 增量、状态游标、标记已读
- `email_parser.py`：格式解析、日期/字段处理
- `main.py`：交易编排、公积金推算、INVEST 映射、分类调用
- `category_mapper.py`：三层分类、名称清洗、学习映射
- `merchant_rules.yaml`：别名规则库
- `llm_classifier.py`：Ollama 兜底、缓存、review
- `reclassify.py`：历史重分类
- `tests/`：35 个单元测试
- `README.md`：当前行为与配置说明