# 考勤跨夜班与加班分段核算

面向制造企业薪酬核算员，处理采信、判定、核算、复核与归档。

## 启动

```bash
docker compose up --build -d
```

访问 http://localhost:18252 。初次启动自动运行脏样例，可切换 clean.csv 与 legacy.csv。顶部“运行批次”会重新计算，同源输入复用批次。

## 业务模块

- 打卡采信：`/m1_ingest`、`/api/m1_ingest`
- 班次日历：`/m2_mapping`、`/api/m2_mapping`
- 跨夜配对：`/m3_versions`、`/api/m3_versions`
- 缺卡复核：`/m4_segments`、`/api/m4_segments`
- 工时切段：`/m5_calculate`、`/api/m5_calculate`
- 薪资对账：`/m6_reconcile`、`/api/m6_reconcile`
- 周期归档：`/m7_archive`、`/api/m7_archive`
- 付款月结：`/m8_monthly`、`/api/m8_monthly`（付款登记 `POST /api/settlements/<run_id>/payment`，台账 `GET /api/settlements`）

## 月薪加班基数裁决

月薪员工加班基数 = **月结生效时人事快照月薪 ÷ 21.75 ÷ 8（即 ÷174 小时）**，与自然日和实际排班无关；月薪固定部分只按入离职在职自然日折算。这样少排班不会压低基数，排班补录也改不动已复核月结。REGULAR 工时由月薪覆盖不重复计，仅 1.5/2/3 倍片段收加成；计时员工按打卡时薪逐片段计，与 M5 金额一致。完整口径见 `docs/DATA-DICTIONARY.md`。

入离职、排班或规则变化产生新批次，同期间旧月结自动作废留档（原件不删）；新结果算失败时旧版保留；同源重跑复用 run_id，付款登记幂等不重复付款；页面与导出只呈现有效月结。`sample_data/monthly.csv` 配 `sample_data/roster.json` 为混合批次样例（计时、全月月薪、月中入职、月中离职、无班次新员工，含跨夜越假日）。

## 核心规则

跨夜按班次归属日配对，休息区间先扣除，再按法定日期和工时阈值切分倍率。完整数值、边界和异常定义见 `docs/DATA-DICTIONARY.md`。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

原始样例在 `sample_data/`。测试在 `tests/`。封存文件是单份CSV，含运行清单、计算证据、隔离原因与对账。复核需提交当前版本，陈旧复核返回409。

## 数据与限制

SQLite保存在records命名卷，普通重启与构建不会清除。确认可丢弃本项目数据后，运行 `docker compose down -v` 再启动可重置。单文件上限2MB；不接入外部业务系统；历史规则由本项目样例定义，不代表实际行业标准。
