# Accounting Buddy 系统手册

> 变更记录与踩坑经验见 [CHANGELOG.md](CHANGELOG.md)。

## 概述

**Accounting Buddy** 是一个自动记账工具，用于监听招商银行（CMB）发来的交易通知邮件，自动解析其中的交易信息，并通过 API 录入到记账系统（BudgetPilot）中。

### 架构总览

```
招商银行邮件通知 ──IMAP──→ email_fetcher ──→ email_parser ──→ category_mapper ──→ api_client ──→ BudgetPilot API
                         (按 UID 拉取新邮件)  (正则提取交易)      (商户分类)          (HTTP POST)      (按 externalId 幂等写入)
```

运行方式：systemd timer 每 5 分钟触发一次 oneshot 服务。

---

## 实现细节

### 项目结构

```
accounting_buddy/
├── main.py                # 主调度器：编排完整流水线
├── email_fetcher.py       # IMAP 邮件拉取与去重
├── email_parser.py        # 邮件正则解析 → 结构化交易记录
├── category_mapper.py     # 商户名 → 账本分类 ID 映射
├── api_client.py          # BudgetPilot API 封装（含重试/幂等）
├── config.yaml            # 运行时配置（发件人、映射表、间隔）
├── .env                   # 敏感凭证（邮箱密码、API Key）
├── state.json             # 已处理邮件 UID 追踪（增量去重）
├── accounting-buddy.service  # systemd 服务定义
├── accounting-buddy.timer    # systemd 定时器定义
└── venv/                  # Python 虚拟环境
```

### 1. 邮件拉取（`email_fetcher.py`）

**协议**：IMAPS（SSL，端口 993），连接 `imap.163.com`。

**流程**：
1. 从 `state.json` 加载上次处理的最大 UID
2. 连接 IMAP，登录，选择邮箱文件夹
3. 搜索 UID 范围 `(last_uid+1):*`，**不再依赖 `UNSEEN`**——手机/网页先读过的邮件也会被处理
4. 批量 `FETCH RFC822` 获取完整邮件内容
5. 逐封解析：解码 Subject/From（支持 GBK/UTF-8 编码头），提取 HTML 或 plain text 正文，并读取 `Date` 头作为交易日期/时间的参考
6. 非发件人邮件在客户端过滤（163 的 IMAP `OR` 搜索不可用，统一走客户端过滤）

**首次运行**：`state.json` 中没有该文件夹记录时，游标会初始化到当前最新 UID 并直接返回，**不会重放整个邮箱**。需要导入历史邮件时显式运行 `--full-sync`。

**可靠投递**：拉取阶段**不标记已读、不推进状态**。`main.py` 只有在某封邮件的全部交易都入账成功（或服务端幂等命中）后，才调用 `mark_processed()` 标记 `\Seen` 并推进 `state.json`。API 失败/网络异常的邮件会留在 UID 范围内，下一轮自动重试；服务端 `externalId` 幂等保证重试不会产生重复账目。

**全量同步**：`--full-sync` 搜索全部邮件，交易以“待确认”状态写入（不动余额），不标记已读、不更新 state。

### 2. 邮件解析（`email_parser.py`）

支持 6 种邮件格式：

| 格式 | 识别特征 | 类型 | 说明 |
|------|----------|------|------|
| 标准消费 | `您账户...于...在...支付...元，余额...` | expense | 最常见交易通知 |
| 资金归集 | `资金归集执行成功...尾号...转账...` | transfer | 内部转账 |
| 工资入账 | `...入账...人民币...元，余额...` | income | 收入类 |
| 扣款/保险 | `...扣款人民币...商户：...余额...` | expense | 保险扣款等 |
| 自动转账 | `...自动转账执行成功（收款人...）` | expense | 对外转账 |
| 转账汇款 / 实时转至他行 | 同上 + `收款人` | expense | 对外转账 |
| 收到本行转入 / 他行实时转入 | `转入人民币...付方...` | income | 转入 |
| 信用卡还款（带目标卡） | `向尾号为...的信用卡还款` | transfer | 源卡 → 目标信用卡 |
| 信用卡还款交易（无目标卡） | `信用卡还款交易人民币...` | expense | 无目标卡信息 |
| 银联入账 / 银联扣款 | `银联入账/扣款人民币...` | income / expense | 云闪付等 |
| 通用入账 | `入账<金额>元，余额...` | income | 补贴、转账等 |
| Apple Pay / 线上支付 | `（设备卡号...）于...线上支付...` | expense | 含线上免密支付 |
| 信用卡账单 | HTML 含 `消费明细` + `尾号` | expense | 多交易 HTML 表格，支持退货/退款（记收入） |
| 资产划转 | 大额存单到期、证券保证金转出入、自助购汇 | transfer | 源/目标使用 `INVEST` 特殊账户 |
| 港币柜台取现 | `柜台取款港币...` | - | 邮件只有港币金额，暂不自动入账（告警） |

**数据结构**（`Transaction`）：

```python
@dataclass
class Transaction:
    account_suffix: str       # 卡号后4位
    date: str                 # YYYY-MM-DD
    time: str                 # HH:MM:SS
    merchant: str             # 商户名
    amount: float
    balance: float | None
    is_credit_card: bool
    raw_text: str
    tx_type: int              # 1=支出, 2=收入, 3=转账
    target_account_suffix: str # 转账目标卡尾号
```

**解析策略**：先通过 HTML 纯文本判断格式（含 `消费明细` → 信用卡，含 `您账户`+`余额` → 普通通知），再分派到对应解析函数。多个正则并行扫描同一文本，因为一封邮件可能包含多条记录（如资金归集 + 普通消费）。

**日期处理**：月/日统一以邮件 `Date` 头为基准解析；若天真日期晚于邮件日期，则回退一年（处理跨年补录）。自动转账正文没有时间，直接使用邮件自身的日期和时间。API 的 `note` 上限 200 字符、`merchantName` 上限 50 字符，`to_api_payload` 会截断；空的 `transactionTime` 不会发送。

**公积金处理**：招行不会为公积金卡（3416）的入金单独发邮件，只有每月 6 日的“资金归集 3416→0611”。配置 `provident_fund_card: '3416'` 后，遇到从该卡发出的资金归集，会**先补一笔同额收入**（商户「公积金入账」、分类「公积金」、幂等键 `cmb:<uid>:pf:<idx>`），再记转账。这样资金归集有余额可扣，收入也会进入报表。注意：如果以后收到真正的入金邮件，需要避免重复。

### 3. 分类（`category_mapper.py` + `merchant_rules.yaml` + `llm_classifier.py`）

**分类顺序（命中即止）**：

1. **学习到的二级分类**：商户表里明确关联到子分类（如 `餐饮/外卖`）时优先采用。
2. **别名规则命中的二级分类**（`merchant_rules.yaml`）：规则可以写完整路径，如 `餐饮/外卖`、`交通/打车`、`固定支出/房租`、`专项支出/旅行`。启动时从 BudgetPilot 分类树建立 `一级/二级 → ID` 路径表，规则返回子分类 ID；旧的一级商户映射不会阻止细分。
3. **学习到的一级分类**：商户表里只有一级分类时作为兜底。
4. **别名规则的一级分类**：规则只写一级时作为兜底。
5. **分类名/关键词匹配**（旧逻辑，长词优先）。
6. **本地 Ollama 兜底**：前面都失败时，让模型在现有分类里选一个。置信度低于 `llm_min_confidence` 的不落库，写入 `llm_review.csv`；结果缓存在 `llm_cache.json`，同一商户不重复调用。
7. 仍无结果 → `fallback_expense_category_id` / `fallback_income_category_id`。

规则支持 `exclude` 排除项，例如「北京大学」排除「口腔/医院」，避免长 pattern 误分。旅行、保险、学费、大额购物已从普通分类拆到固定/专项支出，规则表以完整路径为准。

**商户聚合联动**：BudgetPilot 会把支付渠道原始名称（如 `支付宝-北京三快在线科技有限公司`）归一化为品牌（`美团`），原始名称保存在商户别名表，并在 `/api/v1/merchants` 的 `aliases` 字段返回。采集端 `build_merchant_map` 会把聚合名和所有原始名称都加入学习映射，因此在网页上修正一次分类，后续同样原始名称的邮件仍能命中。

**自动补全映射**：每次运行只为 `category_mapping` 中缺失的分类生成 ID；通讯、订阅服务、人情往来、退款、报销、公积金等新分类会自动加入。

**历史重分类**：

```bash
./venv/bin/python reclassify.py --dry-run                      # 预览「其他」分类
./venv/bin/python reclassify.py --no-llm                       # 只修「其他」分类
./venv/bin/python reclassify.py --dry-run --refine-subcategories  # 预览一级→二级细分
./venv/bin/python reclassify.py --no-llm --refine-subcategories   # 执行细分
```

- 默认只处理「其他支出/其他收入」；
- `--refine-subcategories` 额外把一级分类交易细分到子类；只允许落到子分类，规则明确要求的跨大类（如盒马/山姆：购物→餐饮/食材）会放行并统计；
- 全部通过批量接口只改 `category_id`，不动余额、预算和预警。

**LLM 配置**（`config.yaml`）：

```yaml
llm_enabled: true
ollama_url: http://127.0.0.1:11434
ollama_model: qwen2.5-7b        # 3B 模型实测不可靠，不推荐
llm_min_confidence: 0.8
llm_timeout: 120
```

> Ollama 官方源慢时，可从 ModelScope 下载 GGUF 后本地导入：
> ```bash
> curl -L -o qwen2.5-7b-instruct-q3_k_m.gguf \
>   "https://modelscope.cn/models/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/master/qwen2.5-7b-instruct-q3_k_m.gguf"
> printf 'FROM %s\nPARAMETER temperature 0\n' "$PWD/qwen2.5-7b-instruct-q3_k_m.gguf" > Modelfile
> ollama create qwen2.5-7b -f Modelfile
> ```

**默认关键词表**：

| 分类 | 关键词示例 |
|------|-----------|
| 餐饮 | 餐厅、外卖、美团、肯德基、星巴克、咖啡... |
| 交通 | 地铁、滴滴、打车、高德、加油、高铁... |
| 住房 | 房租、房贷、物业、水费、电费、燃气... |
| 购物 | 淘宝、京东、拼多多、超市、天猫... |
| 娱乐 | 电影、游戏、B站、网易云... |
| 医疗 | 医院、药店、诊所、体检... |
| 通讯 | 话费、联通、移动、电信、宽带 |
| 订阅服务 | 订阅、云服务、阿里云、火山引擎、OpenAI、DeepSeek |
| 人情往来 | 红包、礼金、随礼、群收款 |
| 退款 | 退货、退款、退票、冲正 |
| 工资 | 工资、薪资、薪酬、奖金、补贴 |
| 投资 | 利息、股息、理财、基金、证券 |
| 公积金 | 公积金、住房补贴 |
| 报销 | 报销 |
| 转账 | 转账、转帐、汇款、还款 |

**固定支出与专项支出**：

- `固定支出`（一级，不纳入日常预算）：`房租`、`房贷`、`保险`。
  - 房租按「贝壳租房 / 贝壳找房」识别；保险按「大都会人寿 / 保险」识别。
  - 房贷目前没有历史交易，保留空分类。
- `专项支出`（一级，按专项预算/年度储备管理）：`旅行`、`学费`、`大额购物`。
  - 旅行：携程、飞猪、12306/中国铁路、航旅纵横、机场、航空公司、文旅/旅游类商户。
  - 学费：北京大学软件与微电子学院等。
  - 大额购物：周生生/珠宝/黄金；历史大额服饰按单笔人工归入。
- 规则优先级仍然是「学习到的二级 > 规则二级 > 学习到的一级 > 规则一级」，商户表已同步到新分类，避免旧映射把房租/保险/旅行拉回住房、医疗、交通。

**支付渠道与交易方式标签**（`annotations.py`）：

- 支付渠道从商户名/备注前缀识别，写入 `extFields.channel`：支付宝、微信支付、京东支付、Apple Pay、银联、抖音支付、程支付；无法识别的交易不写。
- 交易方式写入 `tags`：`内部转账`（所有转账）、`信用卡还款`、`资金归集`、`自动转账`、`自动扣款`（排除银联主动扣款）。
- 新邮件在创建交易时自动携带这些字段；历史数据用 `annotate.py` 回填：
  ```bash
  ./venv/bin/python annotate.py --dry-run
  ./venv/bin/python annotate.py
  ```

### 4. API 客户端（`api_client.py`）

封装 BudgetPilot REST API：

| 方法 | API 端点 | 说明 |
|------|----------|------|
| `get_accounts()` | `GET /accounts` | 获取账户列表 |
| `get_categories()` | `GET /categories/tree` | 获取分类树 |
| `create_transaction()` | `POST /transactions` | 创建交易 |
| `get_transactions()` | `GET /transactions` | 查询交易 |
| `delete_transaction()` | `DELETE /transactions/:id` | 删除交易 |
| `confirm_transaction()` | `POST /transactions/:id/confirm` | 确认交易 |
| `update_transaction()` | `PUT /transactions/:id` | 更新交易 |

**重试与幂等**：`create_transaction_with_retry` 最多重试 2 次（间隔 3 秒），覆盖超时、连接失败、读取中断等瞬态网络错误。每笔交易携带稳定幂等键 `cmb:<邮件UID>:<序号>`：
- BudgetPilot 侧对同一用户下相同 `externalId` 只创建一次，重复请求直接返回已有交易（`code=0`），因此超时重试、状态回退后的重放都不会重复入账。
- `code=10004` 现在只表示**名称重复**（账户/商户等），不再被当作交易重复处理。
- 其他业务错误直接抛出 `APIError`，该邮件保持未读，下一轮重试。

**连接管理**：`__del__` 和 `close()` 确保 `httpx.Client` 被正确关闭。

### 5. 主调度器（`main.py`）

完整执行流程：

```
load_env() → load_config() → APIClient()
         ↓
build_initial_mapping()  # 补全缺失的分类映射 + 分类类型表
         ↓
CategoryMapper() → fetch_emails()  # 按 UID 范围拉取，不标已读
         ↓
for each email (UID 升序):
    parse_email(ref_date=邮件日期) → for each tx:
        account_id = account_mapping[suffix]   # INVEST → investment_account_id；未映射用 default_account_id，否则报错
        category_id = mapper.classify(merchant, tx_type)
        external_id = "cmb:<uid>:<idx>"
        dry_run? → print only
        → api.create_transaction_with_retry()
    email 全部成功 → 记录 UID
         ↓
mark_processed(): 标记成功邮件 \Seen，推进 state 到连续成功前缀
         ↓
log summary
```

### 6. 定时运行（systemd）

**Timer**：
- 启动后 30 秒首次执行
- 之后每 5 分钟执行一次
- 精度 10 秒
- `Persistent=true`：错过执行时间后（如关机）下次启动立即补跑

**Service**：
- `Type=oneshot`：运行完退出
- 输出到 journal（`journalctl -u accounting-buddy` 查看日志）

---

## 使用指南

### 环境准备

```bash
# 1. 创建虚拟环境（如果不存在）
python3 -m venv venv

# 2. 安装依赖
./venv/bin/pip install -e .
# 或手动安装
./venv/bin/pip install imapclient beautifulsoup4 httpx pyyaml python-dotenv
```

### 配置

**1. 邮箱配置（`.env`）**

```ini
EMAIL_HOST=imap.163.com
EMAIL_PORT=993
EMAIL_USER=your_email@163.com
EMAIL_PASSWORD=your_app_password
```

> 163 邮箱需开启 IMAP 并生成授权码，不是登录密码。

**2. API 配置（`.env`）**

```ini
API_BASE_URL=http://127.0.0.1:6060/api/v1
API_KEY=your-api-key
```

**3. 业务配置（`config.yaml`）**

```yaml
# 招行发件人列表（IMAP 搜索会匹配包含这些地址的邮件）
cmb_senders:
  - 95555@message.cmbchina.com
  - 95555@cmbchina.com
  - ccsvc@message.cmbchina.com

# 监听的邮箱文件夹
email_folder: INBOX

# 卡号后4位 → BudgetPilot 账户 ID
account_mapping:
  '8338': 1
  '0611': 2
  '6611': 2
  '3416': 3
  '0062': 4
  '0886': 5

# 分类名 → BudgetPilot 分类 ID（缺失项每次运行自动补全）
category_mapping:
  餐饮: 163
  交通: 165
  住房: 169
  购物: 173
  医疗: 177
  教育: 181
  娱乐: 183
  水电: 215
  工资: 161
  转账: 189
  投资: 171

# 未映射卡号时的兜底账户；不配置则未映射卡号会记错误并跳过（推荐显式配置全部卡号）
# default_account_id: 637

# 分类器无法识别商户时的兜底分类
fallback_expense_category_id: 188   # 其他支出
fallback_income_category_id: 179    # 其他收入

# 大额存单/证券保证金/购汇等资产划转使用的投资账户 ID
# 解析器用特殊后缀 INVEST 表示该账户
investment_account_id: 6

# 公积金卡：该卡的“资金归集”会先补一笔同额收入（分类：公积金）
provident_fund_card: '3416'

# 本地 Ollama 兜底（规则/学习/关键词都未命中时才调用）
llm_enabled: true
ollama_url: http://127.0.0.1:11434
ollama_model: qwen2.5:7b
llm_min_confidence: 0.8
llm_timeout: 60

# Timer 间隔（仅文档用，实际由 systemd timer 控制）
interval_minutes: 5
```

### 手动运行

```bash
cd /home/gubin/accounting_buddy

# 正常运行
./venv/bin/python main.py

# 模拟运行（只打印，不创建交易）
./venv/bin/python main.py --dry-run

# 详细日志
./venv/bin/python main.py -v

# 全量同步（处理所有历史邮件，不标记已读）
./venv/bin/python main.py --full-sync

# 组合使用
./venv/bin/python main.py --dry-run -v --full-sync
```

### systemd 管理

```bash
# 安装（创建软链接）
systemctl --user enable accounting-buddy.timer
systemctl --user start accounting-buddy.timer

# 查看状态
systemctl --user status accounting-buddy.timer
systemctl --user status accounting-buddy.service

# 手动触发一次
systemctl --user start accounting-buddy.service

# 查看日志
journalctl --user -u accounting-buddy -f

# 停止定时任务
systemctl --user stop accounting-buddy.timer
```

### 增量状态管理

`state.json` 记录了上次处理的最大邮件 UID。以下情况需要处理：

| 场景 | 操作 |
|------|------|
| 邮箱文件夹变更 | 删除该项 state 记录 |
| 部分邮件遗漏 | 手动运行 `--full-sync`（以待确认交易写入，核对后确认） |
| 想从头开始 | 删除 `state.json` 后运行 `--full-sync`；仅删除 state 会把游标重置到当前最新 UID，不会回放历史 |
| 历史邮件预览 | `./venv/bin/python main.py --full-sync --dry-run -v` |

### 常见问题

**Q: 没有拉取到邮件？**
- 检查 IMAP 授权码是否过期（163 邮箱）
- 首次运行会把 `state.json` 游标初始化到当前最新 UID，**不会处理历史邮件**；需要历史数据请运行 `--full-sync`
- 已读邮件也会被增量扫描（按 UID 范围而非 `UNSEEN`），若仍没有，检查 `journalctl` 日志确认发件人匹配

**Q: 解析失败 "Unknown email format"？**
- 银行邮件格式可能更新，检查 `email_parser.py` 的正则是否匹配
- 用 `--dry-run -v` 查看邮件原文确认格式

**Q: 分类不对？**
- 编辑 `config.yaml` 的 `category_mapping` 直接指定分类名 → ID
- 或在 `category_mapper.py` 的 `DEFAULT_KEYWORDS` 中添加关键词

**Q: 幂等键会不会重复？**
- 幂等键格式为 `cmb:<邮件UID>:<交易序号>`，由 IMAP UID 和邮件内序号组成，稳定且唯一
- 邮件失败重试、`state.json` 回退、重复消费同一封邮件都由 BudgetPilot 的 `externalId` 去重，不会重复入账
- 若升级前已有历史交易被重复写入，需要人工清理；新版本不会再产生重复

**Q: 邮件处理失败后会丢吗？**
- 不会。失败的邮件不会被标记已读，`state.json` 也不会越过它，下一轮定时任务会自动重试

---

## 测试

```bash
cd /home/gubin/accounting_buddy
./venv/bin/python -m unittest discover -s tests -t . -v
```

覆盖日期解析（含跨年）、6 种邮件格式、字段截断与空时间处理、分类映射补全及类型过滤。

---

## 依赖

| 包 | 用途 |
|----|------|
| imapclient>=3.0 | IMAP 邮件拉取 |
| beautifulsoup4>=4.12 | HTML 解析 |
| httpx>=0.27 | HTTP API 调用 |
| pyyaml>=6.0 | YAML 配置读写 |
| python-dotenv>=1.0 | `.env` 环境变量加载 |
