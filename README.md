# Accounting Buddy 系统手册

## 概述

**Accounting Buddy** 是一个自动记账工具，用于监听招商银行（CMB）发来的交易通知邮件，自动解析其中的交易信息，并通过 API 录入到记账系统（BudgetPilot）中。

### 架构总览

```
招商银行邮件通知 ──IMAP──→ email_fetcher ──→ email_parser ──→ category_mapper ──→ api_client ──→ BudgetPilot API
                         (拉取未读邮件)      (正则提取交易)      (商户分类)          (HTTP POST)      (写入账本)
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
3. 搜索 `UNSEEN` 且 `FROM` 匹配任一招行发件人的邮件（使用嵌套 `OR` IMAP 搜索）
4. 批量 `FETCH RFC822` 获取完整邮件内容
5. 逐封解析：解码 Subject/From（支持 GBK/UTF-8 编码头），提取 HTML 或 plain text 正文
6. 非发件人邮件二次过滤
7. 对非全量同步邮件标记 `\Seen` 并更新 `state.json`

**增量机制**：`state.json` 以 `"user:folder": last_uid` 格式记录进度，每次只处理 UID 大于上次最大值的邮件。全量同步（`--full-sync`）时忽略 state，下载所有邮件但不标记已读、不更新 state。

**多 OR 搜索修复**：3+ 个发件人时构建嵌套 OR 树：
```
OR(OR(FROM A, FROM B), OR(FROM C, FROM D))
```
避免 IMAP `OR` 只能接受两个操作数的限制。

### 2. 邮件解析（`email_parser.py`）

支持 6 种邮件格式：

| 格式 | 识别特征 | 类型 | 说明 |
|------|----------|------|------|
| 标准消费 | `您账户...于...在...支付...元，余额...` | expense | 最常见交易通知 |
| 资金归集 | `资金归集执行成功...尾号...转账...` | transfer | 内部转账 |
| 工资入账 | `...入账...人民币...元，余额...` | income | 收入类 |
| 扣款/保险 | `...扣款人民币...商户：...余额...` | expense | 保险扣款等 |
| 自动转账 | `...自动转账执行成功（收款人...）` | expense | 对外转账 |
| 信用卡账单 | HTML 含 `消费明细` + `尾号` | expense | 多交易 HTML 表格 |

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

### 3. 分类映射（`category_mapper.py`）

**两层映射**：

1. **API 自动建表**（首次运行）：从 BudgetPilot API 拉取分类树，遍历所有叶子节点，用 `DEFAULT_KEYWORDS` 中的关键词匹配，自动生成 `category_mapping` 写入 `config.yaml`。
2. **运行时分类**：`CategoryMapper.classify(merchant_name)` 依次尝试：
   - 精确匹配分类名（`category_mapping` 的 key）
   - 关键词子串匹配（关键词 → 分类 ID）
   - 返回 `None` → 回退到 "其他支出/收入"

**默认关键词表**：

| 分类 | 关键词示例 |
|------|-----------|
| 餐饮 | 餐厅、外卖、美团、肯德基、星巴克、咖啡... |
| 交通 | 地铁、滴滴、打车、高德、加油、高铁... |
| 购物 | 淘宝、京东、拼多多、超市、天猫... |
| 娱乐 | 电影、游戏、B站、网易云... |
| 医疗 | 医院、药店、诊所、体检... |
| 通讯 | 话费、联通、移动、电信... |
| 水电 | 水电、物业、燃气、供暖... |
| 工资 | 工资、薪资、薪酬... |
| 转账 | 转账、转帐、汇款... |

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

**重试与幂等**：`create_transaction_with_retry` 最多重试 2 次（间隔 3 秒）：
- **超时**：等待后重试
- **重复交易（code 10004）**：自动查询同账户同金额同日期的交易，返回已有记录
- **其他错误**：直接抛出 `APIError`

**连接管理**：`__del__` 和 `close()` 确保 `httpx.Client` 被正确关闭。

### 5. 主调度器（`main.py`）

完整执行流程：

```
load_env() → load_config() → APIClient()
         ↓
build_initial_mapping()  # 首次运行自动建分类映射
         ↓
CategoryMapper() → fetch_emails()  # 拉取邮件
         ↓
for each email:
    parse_email() → for each tx:
        lookup account_id (config.yaml account_mapping)
        lookup category_id (CategoryMapper.classify)
        dry_run? → print only
        → api.create_transaction_with_retry()
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
  '8338': 631
  '0611': 629
  # ...

# 分类名 → BudgetPilot 分类 ID（首次运行自动填充）
category_mapping:
  餐饮: 271
  交通: 270
  # ...

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
| 邮箱文件夹变更 | 删除 `state.json` 或运行 `--full-sync` |
| 部分邮件遗漏 | 手动运行 `--full-sync` |
| 想从头开始 | 删除 `state.json`，下次运行自动重建 |

### 常见问题

**Q: 没有拉取到邮件？**
- 检查 IMAP 授权码是否过期（163 邮箱）
- 确认邮件确实是 `UNSEEN`（已读邮件不会触发增量同步）
- 查看 `journalctl` 日志确认发件人匹配

**Q: 解析失败 "Unknown email format"？**
- 银行邮件格式可能更新，检查 `email_parser.py` 的正则是否匹配
- 用 `--dry-run -v` 查看邮件原文确认格式

**Q: 分类不对？**
- 编辑 `config.yaml` 的 `category_mapping` 直接指定分类名 → ID
- 或在 `category_mapper.py` 的 `DEFAULT_KEYWORDS` 中添加关键词

**Q: API 返回 10004 重复？**
- 已内置幂等处理，会自动查找已有交易并跳过
- 确认没有手动创建过完全相同金额/日期的交易

---

## 依赖

| 包 | 用途 |
|----|------|
| imapclient>=3.0 | IMAP 邮件拉取 |
| beautifulsoup4>=4.12 | HTML 解析 |
| httpx>=0.27 | HTTP API 调用 |
| pyyaml>=6.0 | YAML 配置读写 |
| python-dotenv>=1.0 | `.env` 环境变量加载 |
