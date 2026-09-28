# 本地用电监测与功率限额服务（FastAPI）

纯本地、可复现的站点用电监测服务：接收多站点设备的实时遥测，做单位/字段/权限/
迟到校验、幂等入库、站点功率限额与分级降载（含滞回恢复），并按时间窗口提供
功率曲线与累计能耗。不依赖任何外部系统。

## 能力一览

- **遥测上报**：功率/电压/电流，支持 W/kW、V/mV、A/mA 自动换算为 SI；
  字段缺失、单位非法、量纲不匹配、NaN/Inf、明显越界给出**可区分拒绝码**。
- **幂等与迟到**：idempotency_key 重放首次结论；同设备同测量时刻相同值不重复
  计数、不同值报冲突；迟到超窗拒绝，窗口内迟到按测量时刻有序归属。
- **权限边界**：全局 / 站点 / 设备三级 API Key，越权与未认证可区分。
- **预算与降载**：超限按优先级依次关闭可中断负载；恢复需功率严格低于
  `limit × restore_ratio`，形成滞回带防抖动；全程在一把锁内串行化，
  并发上报不会击穿预算或重复降载。
- **统计口径**：功率曲线缺数据桶不补零（empty_buckets 显式列出）；能耗用
  梯形积分，缺口段不当零，显式返回 covered/gap 秒数与 complete 标志；
  重复/乱序数据不影响结论，重复查询结果稳定。

## 快速开始

```bash
uv sync
uv run pytest                      # 运行全部 60+ 用例
uv run uvicorn main:app --reload   # 启动（内置确定性演示数据）
# 打开 http://127.0.0.1:8000/docs
```

演示站点 site-a（上限 1000W，恢复比例 0.95），密钥：
`admin-key`（全局）、`site-a-key`（站点）、`dev-fixed-key`（仅 fixed 设备）。

## 代码结构

```
power_budget/
  models.py       枚举与数据类（含单位表、SI 换算、拒绝码）
  clock.py        可注入时钟（SystemClock / FakeClock）
  validation.py   字段/单位/量纲/范围校验
  permissions.py  API Key 认证与设备授权
  storage.py      内存存储（幂等表、有序去重样本、降载状态、可重入锁）
  budget.py       帧功率、目标降载集、分级降载与滞回恢复
  stats.py        去重排序、分桶曲线、梯形积分能耗
  service.py      门面：ingest / ingest_frame / 查询（锁内串行化）
  seed.py         确定性演示数据
  api.py          FastAPI 路由与应用工厂
tests/            分场景测试（含并发、迟到、滞回、口径、HTTP）
docs/
  DESIGN.md           单位/时间/预算/幂等/权限口径（权威约定）
  LOCAL_VALIDATION.md 本地验证与复现步骤
```

## 文档

- 口径与规则：[docs/DESIGN.md](docs/DESIGN.md)
- 本地验证：[docs/LOCAL_VALIDATION.md](docs/LOCAL_VALIDATION.md)
