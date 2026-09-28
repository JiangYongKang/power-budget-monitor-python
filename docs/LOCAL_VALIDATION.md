# 本地验证指南

不接任何外部服务，所有数据均可在本机复现。

## 1. 环境准备

需要 uv（仓库已带 uv.lock）。Python 版本要求见 pyproject.toml（>=3.14）。

```bash
uv sync                       # 安装运行 + 开发依赖（fastapi/pytest/httpx）
```

## 2. 跑测试

```bash
uv run pytest                 # 全部用例
uv run pytest -q              # 简洁输出
uv run pytest tests/test_budget.py -v          # 只看预算/滞回
uv run pytest tests/test_concurrency.py -v     # 只看并发
```

测试组织：

| 文件 | 覆盖点 |
|---|---|
| tests/test_validation.py | 单位换算、量纲匹配、越界/NaN、时间戳解析（拒绝码可区分） |
| tests/test_ingest.py | 幂等重放、同刻去重/冲突、迟到/超前、乱序、权限、VxI 推导 |
| tests/test_budget.py | 分级降载顺序、不重复降载、预算不击穿、滞回恢复、临界不抖动 |
| tests/test_concurrency.py | 同帧多设备并发、重复并发提交、结论一致 |
| tests/test_stats_queries.py | 曲线分桶/空桶、梯形能耗、缺口不补零、跨天、重复查询稳定 |
| tests/test_api.py | HTTP 状态码、批量帧（含 207 部分拒绝）、预算/曲线/能耗接口 |

测试日志会打印三段信息，方便定位是“拒绝 / 降载 / 统计口径”哪类问题：

```
[预算帧 00:00] INPUT={...} -> basis=当前帧 1500W > 1000W ... final_shed=[load-1,load-2] measured=800W
ingest REJECT idem=... reason=value_out_of_range detail=... payload={...}
[并发-收敛] final_shed=(load-1,load-2) measured=800.0 basis=...
```

想在终端看到这些判定日志：

```bash
uv run pytest tests/test_budget.py -v --log-cli-level=INFO
```

## 3. 启动服务（内置可复现演示数据）

```bash
uv run uvicorn main:app --host 127.0.0.1 --port 8000
# 或：uv run python main.py
```

服务启动时用 FakeClock 灌入一段确定性遥测（见 power_budget/seed.py），
每次启动数据完全一致。交互式文档：http://127.0.0.1:8000/docs 。

## 4. 手动演练（也可用 /docs Swagger）

以下示例在 Swagger UI 或 curl 中执行，鉴权头为 X-API-Key。

1. 查看当前降载状态与最近一次判定依据：
   GET /api/v1/sites/site-a/budget
2. 单位非法上报 -> 422 且 reason=unknown_unit：
   POST /api/v1/telemetry，readings 中 unit 传 Pa。
3. 越权上报 -> 401：用 dev-fixed-key 写 load-1。
4. 原子批量帧 -> POST /api/v1/telemetry/frame，reports 放同一 ts 的
   fixed=600W、load-1=0.45kW、load-2=250W、load-3=200W，
   返回的每条结果带同一 decision：final_shed=[load-1,load-2]，
   measured_power_w=800。
5. 功率曲线：GET /api/v1/sites/site-a/curve?start=...&end=...&align=1m，
   缺数据桶在 empty_buckets 中列出，不补零。
6. 累计能耗：GET /api/v1/devices/fixed/energy?start=...&end=...，
   返回 energy_wh / covered_seconds / gap_seconds / complete。

批量帧请求体示例：

```json
{
  "reports": [
    {"idempotency_key": "f-fixed", "device_id": "fixed",
     "ts": "2026-01-01T00:10:00Z",
     "readings": [{"metric": "power", "unit": "W", "value": 600}]},
    {"idempotency_key": "f-l1", "device_id": "load-1",
     "ts": "2026-01-01T00:10:00Z",
     "readings": [{"metric": "power", "unit": "kW", "value": 0.45}]},
    {"idempotency_key": "f-l2", "device_id": "load-2",
     "ts": "2026-01-01T00:10:00Z",
     "readings": [{"metric": "power", "unit": "W", "value": 250}]},
    {"idempotency_key": "f-l3", "device_id": "load-3",
     "ts": "2026-01-01T00:10:00Z",
     "readings": [{"metric": "power", "unit": "W", "value": 200}]}
  ]
}
```

## 5. 确定性与无外部依赖

- 演示时钟从固定时刻 2026-01-01T00:00:00Z 开始（FakeClock），
  不访问系统墙钟、不发网络请求；
- 数据全部在内存（Store），重启即从同一脚本重建，结果可重复；
- 统计是去重排序 + 梯形积分的纯计算，同一批数据重复查询结果一致。
