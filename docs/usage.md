# 指标平台使用说明

本应用在只读资料目录 `data/` 之上提供指标登记、定义校核、交互查询、影响预览、
订阅确认与到期出报六类可操作功能。所有本地状态写入 `local-state/`（已在 `.gitignore`），
原始 JSONL 资料永不被修改。

## 1. 启动与初始化

```bash
# 命令行（无需网络，仅用 Python 标准库 + 项目已固定依赖）
python3 -m app.cli seed          # 首次：写入 13 个内建指标并导入既有订阅约定
python3 -m app.cli catalog       # 查看登记册

# 本地 Web 应用（默认 http://127.0.0.1:8080，空状态时自动 seed）
python3 -m app.web --port 8080
```

测试：`python3 -m unittest discover -s tests -v`（31 个用例，覆盖资料层与全部业务规则）。

## 2. 指标登记（分析员）

每条指标必须显式登记：**分母、分子、时间归属、单位、去重对象、可展开维度、可加性类别**。

| 字段 | 取值与含义 |
| --- | --- |
| `kind` | `base` 基础度量 / `derived` 表达式派生 / `cohort_window` 跨期窗口 |
| `time_basis` | `operating` 经营期（按原成交窗口）/ `collection` 回款期（按实际发生日），二者不能混算 |
| `additivity` | `additive` 可加 / `semi_additive` 去重聚合、禁止沿专题相加 / `ratio` 比率，只能重算不能平均 |
| `dedup_object` | `spec` / `receipt_row` / `household` / `household_first` |
| `requires_attribution` | `single`（v1 单属）/ `multi_share`（v2 获批份额）/ `null`（不按专题时） |
| `eligible_dims` | `region`、`family`、`spec`、`topic` 的子集，业务端越权展开会得到具体原因 |

派生表达式（`app/dsl.py`）只支持数字、已登记指标引用、四则运算与白名单函数 `safe_div`；
分母为 0 或依赖不可计算时返回"不可计算"，不会伪造 0。

### 新增一个指标（CLI 与 Web 等价）

```bash
cat > /tmp/my_metric.json <<'JSON'
{
  "code": "net_sales_share_r0",
  "name": "r0地区净销售额占比",
  "owner": "销售部门",
  "kind": "derived",
  "description": "r0 地区经营期净额 / 全地区经营期净额",
  "unit": "ratio",
  "time_basis": "operating",
  "dedup_object": "receipt_row",
  "numerator": "r0 地区 net_sales_amount",
  "denominator": "全地区 net_sales_amount",
  "eligible_dims": ["region", "family"],
  "additivity": "ratio",
  "expression": "safe_div(net_sales_amount, gross_sales_amount)"
}
JSON
python3 -m app.cli register --file /tmp/my_metric.json     # 草稿
python3 -m app.cli validate --metric net_sales_share_r0   # 单独校核
python3 -m app.cli confirm --metric net_sales_share_r0 --actor 张三  # 确认后才可查询/订阅
```

校核会拒绝：循环依赖、经营期×回款期混搭、引用草稿/不存在指标、比率套比率、
依赖不支持的维度、互相冲突的专题归属方式。

## 3. 交互查询（业务人员）

```bash
python3 -m app.cli query --metric sugar_documented_spec_ratio \
  --mode replay --cutoff 2026-03-10T00:00:00+08:00 \
  --start 2026-02-01 --end 2026-03-02 --group-by region

# 专题必须显式选版本，两种归属可并存但一次查询不能暗换
python3 -m app.cli query --metric documented_sales_amount_share \
  --cutoff 2026-03-22T00:00:00+08:00 --start 2026-01-31 --end 2026-03-02 \
  --group-by topic --attribution v2
```

返回结构：`rows`（每个分组的值与不确定区）+ `audit`（血缘、口径、截止规则、剔除/歧义计数）。
Web 端点击「这个数字在数什么？」可展开同样的审计信息。

* **不可计算而非空表**：例如回款期指标请求 `topic` 维度时，返回
  `blocked_reason`，说明该指标未登记专题归属、已授权维度是什么。
* **不确定区**：`legacy0` 换码冲突（两个未确认候选规格）与营养区间不唯一的对象
  进入 `uncertain_value`，缺项不当零；Web 端以橙色单列。
* **两种事实截止规则**：
  - `replay` 复原旧报告：只用 cutoff 前**已确认**资料；
  - `restate` 重述同一经营期：采用 cutoff 前到达的后补确认事实（pending 转 confirmed）。

## 4. 换版与影响预览

修改分母、归属方式等口径字段时，先预览再确认：

```bash
cat > /tmp/changes.json <<'JSON'
{"denominator": "可售规格数（修订：分母说明收紧）"}
JSON
python3 -m app.cli revise --metric sugar_documented_spec_ratio \
  --file /tmp/changes.json --note "分母说明收紧" --actor 李四 \
  --cutoff 2026-03-22T00:00:00+08:00 --start 2026-01-31 --end 2026-03-02
# 预览给出：变更字段、是否口径改变、受影响订阅、同一事实截面下新旧值与 delta、阻断项
python3 -m app.cli confirm-revision --metric sugar_documented_spec_ratio \
  --file /tmp/changes.json --note "分母说明收紧" --actor 李四
```

确认后：旧版本文件保留且可继续查询；锁定旧版的定时订阅自动转 `renewal_required`，
**不会**悄悄使用新口径；收件方可在 Web「订阅确认」页或 CLI 续约：

```bash
python3 -m app.cli confirm-renewal --subscription imp-sub-a --actor product-team --version 2
# 不指定 --version 则升级到当前最新版；也可显式选择继续锁定旧版
```

### 事实改变 vs 口径改变（差异桥）

```bash
python3 -m app.cli bridge --metric sugar_documented_spec_ratio --baseline-version 1 \
  --prior-cutoff 2026-03-07T00:00:00+08:00 --current-cutoff 2026-04-21T00:00:00+08:00 \
  --start 2026-01-31 --end 2026-03-02
```

输出 A（旧口径×旧事实）、B（旧口径×新事实）、C（新口径×新事实）：
事实效应 = B−A，口径效应 = C−B，且校验两段之和等于总差异。

### 资料修订与口径换版分开解释

资料被证实错误时，不改指标定义，而是给订阅挂修订通知：

```bash
# Web: 订阅确认页 → 资料修订提示；或直接调用 API
curl -X POST http://127.0.0.1:8080/api/subscriptions/revision-notice \
  -d '{"subscription_id":"imp-sub-a","notice":"营养资料 n0-1 来源错误，2026-02 段不能保证原含义"}'
```

订阅状态保持 `active`，但通知随报告送达；能保证原含义的订阅照发，
不能保证的由收件方在重新确认时决定。

## 5. 到期出报与本地收件箱

```bash
python3 -m app.cli due --now 2026-04-08T09:00:00+08:00   # 幂等：重复执行不产生第二份通知
python3 -m app.cli inbox                                  # 查看本地收件箱
```

报告保证：

1. **单一事实截面**：一份报告内所有指标共用同一 cutoff 与模式，不会出现分子新、分母旧；
2. **版本锁定**：报告章节标注实际使用的指标版本，续约前始终是订阅锁定版；
3. **跨归属拒绝混入**：回款期指标不能放进经营期报告，对应章节返回阻断原因；
4. **幂等键**：`delivery_key + 期间` 决定通知文件名，重复运行只记一条 `duplicate_notice`。

## 6. 加入新销售期 / 新资料

应用不硬编码期间，加入新月份只需两步：

1. 把新的 `sales / returns / availability / nutrition / topic_memberships` 行
   按 `docs/data-contract.md` 追加到 `data/*.jsonl`（保持字段、时区、半开区间约定）；
2. 查询/订阅时直接使用新的 `period_start/period_end` 与更晚的 `cutoff`，
   无需改任何 SQL 或代码。先跑 `python3 -m materials` 确认资料格式。

新事实若带来新的歧义（如新的未确认换码链接），系统会自动把受影响成交行
隔离到不确定区，并在 `audit.fact_context` 中给出计数与 cutoff 说明。

## 7. 关键业务规则到实现的对照

| 规则 | 实现位置 |
| --- | --- |
| 成交事实不与按日规格资料直接连接、不放大金额 | `app/measures.py` 全部在规格/receipt 安全粒度预聚合 |
| 家庭人数不沿专题分项相加 | `additivity=semi_additive`，家庭集合去重，UI 明确标注 |
| 百分比不平均门店平均值 | 比率分组重算分子/分母，`RATIO_OF_RATIO` 校核拦截 |
| 单属 v1 / 多属份额 v2 并存且不暗换 | 查询必须显式传 `attribution_version`，`app/checks.py` 强制 |
| 跨月退款按原成交窗口看净额；回款按发生日 | `net_sales_amount`(operating) 与 `collection_refund_amount`(collection) 独立 |
| 复购只认走完观察窗的首购家庭；删失不计零 | `app/measures.py::_repurchase_measure`，敏感性区间单列 |
| 重复票据 ≠ 第二次真实购买 | context 剔除 `duplicate_of` 行；复购按家庭购买日集合判定（见 `tests/fixtures.py`） |
| 换码/区间不唯一显示为不确定 | `app/context.py::_detect_ambiguity`，结果分 `value`/`uncertain_value` |
| 经营期与回款期不能相除 | `TIME_BASIS_MISMATCH` 校核 + 报告截面二次拦截 |
| 换版须预览+确认，旧订阅不被静默升级 | `app/versions.py` + `app/subscriptions.py::mark_renewals` |
| 出报幂等、截面一致 | `app/subscriptions.py::run_due/_generate_report` |
