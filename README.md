# gridlab 多采样率遥测对齐与缺口修复

环境监测站的采样流按 1 / 5 / 15 分钟三种采样率混在一起，设备时钟漂移 2–90 秒，
报表按 15 分钟汇总。本服务把原始采样采信、校正到 UTC、按 1 分钟网格对齐、
按声明策略修复小缺口、算 15 分钟窗口覆盖度，并给出可复核的四账与导出。

## 功能模块

| 模块 | 动作 | 入口 |
|---|---|---|
| M1 采样采信 | 导入自带脏样例，逐行诊断编码、分段、列数与原始行号 | `POST /api/samples/import`、页面 `/samples` |
| M2 时基契约 | 查传感器规格、采样率、量程与设备偏移区间 | `GET /api/timebase` |
| M3 质量隔离 | 看原因码分布与隔离行，导出隔离 CSV | `GET /api/quarantine`、页面 `/quality` |
| M4 网格对齐 | 1 分钟网格、同分钟去重、15 分钟窗口覆盖 | `GET /api/grid`、`POST /api/grid`、页面 `/grid` |
| M5 缺口修复 | 缺口段、补齐值、窗口覆盖 | `GET /api/gaps`、`POST /api/gaps/repair`、页面 `/gaps` |
| M6 批次与对账 | 批次清单、四账、差异归因 | `GET /api/batches`、`GET /api/reconcile`、页面 `/batches` |
| M7 导出 | 网格 / 采纳 / 隔离 CSV、运行清单、单元格证据 | `GET /api/export/*` |
| M8 夜班滚动发布 | 补批增量采信、15 分钟窗口版本化发布、迟到覆盖/补齐/合计、五账与导出 | `POST /api/publish/*`、页面 `/publish` |

## 核心规则

- 真值时间 = 采样时刻 − 设备偏移；偏移表按 `[effective_from, effective_to)` 解析，命中两段即判歧义并隔离。
- 同一传感器同一分钟重复：先按质量位 `good > suspect > bad` 择一，并列取 `ingest_seq` 较大者，再并列取原始行号较大者。
- 1 分钟桶为 `[t, t+1min)`，15 分钟窗口右端开；端点 `00:15:00` 归右窗口。
- 缺口 ≤3 桶按传感器声明策略补齐（`hold` 取前值、`linear` 按桶距等分）并标 `FILLED`；>3 桶记 `GAP` 不插值；`linear` 缺后继锚点记 `EDGE_NO_ANCHOR`。
- 窗口覆盖度 = 实有桶数 / 应有桶数（应有按采样率派生），低于 `0.8` 标 `PARTIAL`。
- 迟到 = 桶早于「已见最大桶 − 15 分钟」，仍采纳，只标 `LATE_ARRIVAL`。

## M8 夜班滚动发布规则

值班节奏是每 10 分钟交付一次 15 分钟窗口，但迟到记录可能下一批才到。M8 在 M1–M5 的
采信、时基、质量与补齐原语之上，提供一套**只追加、内容寻址、原子切换**的发布账：

- **原始记录不丢**：每个补批的原始行进 `pub_raw`，只追加；补批幂等键取原始字节 sha256，
  同一补批重放命中唯一键，复用而不新增（原始行、版本都不会翻倍）。
- **每次发布看得见采样来源**：窗口逐格快照 `pub_window_cells` 带 `source_batch / source_row /
  arrival_seq`，值班员可从任一发布格追到补批与原始行。
- **迟到补批的三类影响可量化**：新窗口 `NEW`；已发布窗口被实测修订 `REVISED`，逐格区分
  「补齐格被实测覆盖（FILLED→OBSERVED）」「缺口被补齐/填实（GAP→…）」，并给出实有/补齐/
  缺口增量与实有合计 `sum_delta`；指纹不变记 `UNCHANGED`。
- **同一迟到补批重跑不新增版本**：窗口版本按业务指纹（逐格桶/状态/值/原因 + 覆盖率，不含
  来源批次）内容寻址，指纹相同即复用同一 `version_id`；发布时间线仍可留痕，但版本表不增长。
- **发布中断保留旧版**：版本写入、当前指针切换、发布登记在同一事务；提交前任一步失败整体
  回滚，`pub_window_current` 指针与版本表回到上一版（测试用 `service.FAIL_BEFORE_COMMIT` 验证）。
- **窗口锚点隔离**：补齐锚点只取「当前窗口及其左侧（已发布侧）」观测，未来窗口的桶永远不会
  反向改写已发布窗口；迟到数据只有真正落在窗口内才会修订它。
- **五账对得上**：原始账（原始=采信+重复+隔离）、网格账（应有=实有+补齐+缺口）、数值账
  （已发布窗口去重采信合计=窗口实有合计，补齐不回流）、窗口账（发布数=指针数）、版本账
  （无孤儿指针/悬挂快照）。Decimal 求和，禁止 SQL `SUM` 走浮点；窗口结果、采样记录、导出
  CSV/清单三处合计同源。
- 自带夜班样例 `sample_data/night_shift/`：首批 A、二批 B、迟到补批 C（回填 + 覆盖）。

## 定义与已知不覆盖

字段、单位、精度、空值三态、去重键与空态结论见 `docs/DATA-DICTIONARY.md`；
陷阱登记、反例与不覆盖清单见 `docs/DEPTH.md`。

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18203/api/health
```

浏览器打开 `http://127.0.0.1:18203/`，点「导入样例」。

## 演示步骤

1. 首页选「主样例（脏）」与「设备偏移表」，点「导入样例」。
2. `/samples` 看编码回退、分段、列映射与偏移区间。
3. `/quality` 看 7 类原因码与隔离行，点「隔离 CSV」下载。
4. `/grid` 看 110 个网格点的实有 / 补齐 / 缺口分布，点任意单元格看证据。
5. `/gaps` 看缺口段与 14 个窗口的覆盖度。
6. `/batches` 看四账与四个导出文件。

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

## 目录

```
app/main.py config.py errors.py reasons.py manifest.py store.py web.py
app/modules/m1_intake … m7_export
app/modules/m8_publish  夜班滚动发布（增量采信/窗口版本/原子发布/五账/导出）
app/templates  base.html + 7 页
app/static     theme.css layout.css components.css app.js table.js grid.js publish.js
docs/          DEPTH.md DATA-DICTIONARY.md RUNBOOK.md
sample_data/   11 个自带样例 + 1 个偏移表 + night_shift/ 3 个夜班补批
tests/         每模块 1 个测试文件 + e2e + invariants + m8_publish
```
