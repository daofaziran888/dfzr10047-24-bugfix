# 解构工坊 · 异构文件解析与字段抽取编排平台

面向异构文件投递、格式探测、编码归一、解析器路由、抽取模板版本、字段解析、坏行隔离、结构嗅探、嵌套展平、取值转型、单位归一、输出契约、结果投递、解析水位与重解析编排的数据采集接入平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8004/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5177/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8004`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 文件投递 | `file_intake` | 投递批次 | 批次编号、来源渠道、原始文件名 |
| 格式探测 | `format_probe` | 探测结论 | 探测编号、关联批次、魔数特征 |
| 编码归一 | `encoding_norm` | 转码记录 | 转码编号、关联批次、原始编码 |
| 编码恢复 | `encoding_recovery` | 失败编码记录 | 恢复编号、关联批次、原始编码、原错误 |
| 解析路由 | `parser_route` | 路由决策 | 决策编号、关联批次、推断格式 |
| 抽取模板 | `extract_tmpl` | 模板版本 | 模板编号、模板名称、适用格式 |
| 字段解析 | `field_parse` | 解析字段 | 解析编号、关联批次、命中模板 |
| 坏行隔离 | `row_quarantine` | 隔离行 | 隔离编号、关联批次、原始行号 |
| 解析批次 | `parse_batch` | 批次记录 | 批次记录编号、关联投递批次、解析器标识 |
| 结构嗅探 | `schema_sniff` | 结构推断 | 嗅探编号、关联批次、样本行数 |
| 分隔规则 | `delimiter_rule` | 规则条目 | 规则编号、适用格式、分隔符 |
| 嵌套展平 | `nested_flatten` | 展平路径 | 展平编号、关联模板、原始路径 |
| 取值转型 | `value_cast` | 转型规则 | 转型编号、目标类型、来源类型 |
| 单位归一 | `unit_normalize` | 归一系数 | 归一编号、字段名称、原始单位 |
| 输出契约 | `output_contract` | 契约条目 | 契约编号、关联批次、输出列集合 |
| 结果投递 | `result_deliver` | 投递记录 | 投递记录编号、关联契约、目标位置 |
| 解析水位 | `parse_watermark` | 水位读数 | 水位编号、来源渠道、已解析至位点 |
| 重解析任务 | `reparse_task` | 重解析单 | 重解析编号、关联批次、重解析原因 |
| 重复指纹 | `dup_fingerprint` | 指纹记录 | 指纹编号、关联批次、内容指纹 |
| 抽样复核 | `sample_review` | 复核样本 | 样本编号、关联批次、抽样比例 |
| 解析指标 | `parse_metric` | 指标读数 | 指标读数编号、统计窗口、来源渠道 |
| 模板差异 | `tmpl_diff` | 差异条目 | 差异编号、旧模板版本、新模板版本 |
| 投递审计 | `intake_audit` | 审计条目 | 审计编号、关联对象、操作类型 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 编码恢复

`unknown-charset` 之类无法识别的声明不再让登记抛错丢记录，处理方式与真正空附件分开：

- 无法识别的声明 → 写一条 `无法转码` 转码记录，并把原字节、当时的声明和原错误转成
  **失败编码记录**（`encoding_recovery` 表），状态 `待恢复`，可打开失败详情。
- 真正空附件（0 字节）→ 核定成 `已核定（空附件）` 的合法空输入，不产生失败记录，
  字段解析可读入但没有字段。
- 失败详情保留当时的编码声明、原字节 HEX、原错误与候选解码；历史报告不会被后来的更正改写。
- 对同一字节提交两种结果不同的更正时，服务端在**受理回执**里说明本次受理了哪一条：
  - `继续核对`：本次受理生效，页面据此继续核对并确认恢复；
  - `另发恢复`：本次未采用，原受理保持不变，按回执打开另发的恢复单。
  - 渠道更正优先于人工选码；被覆盖的人工选码另发恢复，编码仍可单独核对。
- 确认恢复必须带上本次回执编号；重复确认只写入一次字段（按恢复编号 + 行号 + 字段名去重）。
- 字段解析读入资格由编码归一核验：需要已核定的转码输出，且该批次原失败编码记录已恢复；
  未知编码不会被记成无损转码，也不会流入字段解析。转码结论同步写入处理台账（`intake_audit`）。

接口（固定路径，注册在 `/api/{module}` 之前）：

```text
GET  /api/encoding-recovery/failures                 失败编码记录清单
GET  /api/encoding-recovery/failures/{no}            失败详情（原字节、候选、回执、字段、资格）
POST /api/encoding-recovery/attachments              登记附件（空字节 = 合法空附件）
POST /api/encoding-recovery/failures/{no}/corrections 受理更正，返回回执
POST /api/encoding-recovery/failures/{no}/confirm    按回执确认恢复（幂等）
POST /api/encoding-recovery/failures/{no}/retry      转码失败重试
GET  /api/encoding-recovery/eligibility/{batch}      读入资格
GET  /api/encoding-recovery/attachments/{batch}      按批次分别查验未知编码与空附件
POST /api/encoding-recovery/field-parse              字段解析登记（带读入资格核验）
```

前端在 `编码归一` 工作台下方渲染 `EncodingRecoveryPanel.vue`，提供附件登记、失败清单、
失败详情（原字节/声明/候选解码）、更正与回执、确认恢复与字段核对。

领域：数据工程
子方向：数据采集、爬虫与RPA
