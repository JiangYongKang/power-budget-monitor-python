# 本地用电监测与功率限额服务

纯本地、无外部依赖的 FastAPI 服务：设备遥测上报（单位归一化 + 校验 + 鉴权 + 幂等）、
站点/回路功率预算与分级降载（滞回）、时间窗口功率曲线与累计能耗统计。

## 快速开始

```bash
uv sync
uv run uvicorn main:app --reload          # 启动服务（默认 http://127.0.0.1:8000）
uv run pytest                              # 运行全部测试（20 个，含并发/滞回场景）
```

启动后访问 `/docs` 可交互调用。典型流程：

```bash
# 1. 注册设备（每台设备一个 owner_token）
curl -X POST localhost:8000/devices -H 'content-type: application/json' -d '{
  "device_id":"ac","site_id":"s1","circuit_id":"c1",
  "load_class":"interruptible","shed_priority":1,"owner_token":"tok"}'

# 2. 配置站点预算：超限 1000W，恢复 600W（滞回带 600~1000W）
curl -X POST localhost:8000/budgets -H 'content-type: application/json' -d '{
  "scope":"site","scope_id":"s1","limit_w":1000,"restore_w":600}'

# 3. 上报遥测（功率必填，电压/电流可选；report_id 为幂等键）
curl -X POST localhost:8000/telemetry -H 'content-type: application/json' -d '{
  "device_id":"ac","timestamp":"2026-09-28T12:00:00Z",
  "power":1.5,"power_unit":"kW","report_id":"r1","token":"tok"}'

# 4. 查询降载状态 / 功率曲线 / 累计能耗
curl localhost:8000/load-state/ac
curl 'localhost:8000/stats/power?scope=site&scope_id=s1&start=2026-09-28T00:00:00Z&end=2026-09-29T00:00:00Z&bucket_seconds=3600'
curl 'localhost:8000/stats/energy?scope=site&scope_id=s1&start=2026-09-28T00:00:00Z&end=2026-09-29T00:00:00Z'
```

## 文档

- [docs/DESIGN.md](docs/DESIGN.md)：单位与时间口径、预算与降载规则、幂等与迟到数据约定、
  权限边界、本地验证方式。

## 目录结构

```
app/models.py      数据模型与拒绝原因枚举
app/units.py       单位归一化（W/V/A）
app/validation.py  字段/单位/量程/时间校验
app/auth.py        token 鉴权
app/store.py       线程安全内存存储（幂等去重、按时间戳归属）
app/budget.py      预算判定、分级降载与滞回恢复
app/stats.py       分桶功率与梯形积分能耗
app/api.py         FastAPI 路由
tests/             20 个测试：校验、幂等、迟到乱序、并发、临界与滞回、端到端
```
