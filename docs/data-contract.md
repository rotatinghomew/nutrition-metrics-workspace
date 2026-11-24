# 业务资料协议

规格、地区日可售、成交行和退款各有自己的粒度。金额单位分，日期结束边界不包含当日。sale_code是小票原码，spec_id是最初导入暂用的对应规格；legacy0仍须结合code_links解释，不能当作已确认唯一映射。专题归属v1为单属展示，v2为多属且登记了份额。该区别是来源定义，不预写查询方式。

所有数字、身份和业务来件均为合成。记录的稳定编号只在对应表内唯一，跨表关联以明确字段为准。布尔值为原来源明确回报，空字符串引用表示没有已知对应。金额如有均用整数分，比例为0至1，其他单位在字段中注明。

## specifications

可售规格的有效版本。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| family | string | 商品家族 |
| code | string | 销售使用的商品码 |
| valid_from | string | 规格有效起日 |
| valid_to | string | 有效结束日，不包含该日 |
| package_g | number | 一件净质量克 |

## availability

地区日可售情况，未列日期是无回报而非不可售。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| spec_id | string | 规格 |
| region | string | 地区 |
| date | string | 销售日 |
| sellable | boolean | 明确可售状态 |
| known_at | string | 该回报的获知时点 |

引用关系：spec_id 对应 specifications.id。

## nutrition

资料员收到的营养来源数据。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| spec_id | string | 规格 |
| valid_from | string | 资料适用起日 |
| valid_to | string | 资料适用结束日 |
| known_at | string | 资料收到时间 |
| basis | string | per100g或perServing |
| basis_g | number | 每份实际克数；per100g时为100 |
| sugar_g | nullable_number | 该基准下的总糖克数；null为缺失 |
| status | string | 来源状态：confirmed或pending |

引用关系：spec_id 对应 specifications.id。

## sales

小票商品行事实，金额单位分。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| receipt | string | 收银小票编号 |
| household | string | 本用途匿名家庭键 |
| spec_id | string | 商品规格 |
| region | string | 成交地区 |
| sold_at | string | 成交时点 |
| known_at | string | 资料可用时点 |
| units | number | 购买件数 |
| amount_minor | number | 实际成交金额分 |
| duplicate_of | string | 确认为重复报送的原行；空串表示未确认重复 |
| sale_code | string | 小票实际商品码；legacy0的映射仍有争议，不能因暂用spec_id就消除原始歧义 |

引用关系：spec_id 对应 specifications.id。

## returns

按原小票行登记的退款。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| sale_id | string | 原成交行 |
| occurred_at | string | 实际退款时点 |
| known_at | string | 收到退款资料时点 |
| amount_minor | number | 本笔退款金额分 |

引用关系：sale_id 对应 sales.id。

## topic_memberships

专题负责人给出的单属和多属原始关系。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| spec_id | string | 规格 |
| topic | string | 专题 |
| version | string | 归属版本 |
| share | number | 该版本登记的分配份额；多属展示关系可为1 |
| valid_from | string | 关系开始日期 |
| valid_to | string | 关系结束日期 |
| known_at | string | 关系获知时点 |

引用关系：spec_id 对应 specifications.id。

## code_links

换码复核来件，允许尚有冲突的映射。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| old_code | string | 旧码 |
| new_spec | string | 可能的现规格 |
| from_date | string | 适用开始日 |
| to_date | string | 适用结束日 |
| known_at | string | 收到时间 |
| confirmed | boolean | 该映射是否已确认 |

引用关系：new_spec 对应 specifications.id。

## report_agreements

收件方已有的订阅约定，不是查询实现。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| recipient | string | 本地收件方 |
| metric_name | string | 原约定的度量名称 |
| attribution_version | string | 锁定的专题归属版 |
| cutoff | string | 原约定的事实截止时间 |
| period_start | string | 经营期起日 |
| period_end | string | 经营期结束日 |
| confirmed | boolean | 收件方是否已确认此约定 |
| timezone | string | 收件方约定时区 |
| scheduled_at | string | 约定的下一次到期出报时点 |
| recurrence | string | 本约定为按月订阅 |
| delivery_key | string | 收件方识别同一期通知的订阅业务键 |
