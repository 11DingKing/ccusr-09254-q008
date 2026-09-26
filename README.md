# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 补录公平队列

学期末补录高峰时，`/api/queue` 提供持久化工作队列（表 `queue_items` / `queue_rules`）：

- **评分与老化**：`score = 毕业权重*毕业标志 + 材料权重*材料齐全标志 + 老化因子*已等待小时数`。毕业与材料齐全者优先；老化因子让普通请求随等待时间逐渐前移，不会无限等待。
- **规则可版本化**：`POST /api/queue/rules` 发布新版本并自动停用旧版本，历史版本保留在 `queue_rules` 中；每次排序都读取当前激活规则即时重算（纯函数见 `app/queue/scoring.py`）。
- **租约认领**：`POST /api/queue/claim` 返回随机 `lease_token`；续租、退回、完成都必须携带 token。租约到期后条目自动回到可认领集合，其他工作者可回收，防止请求因工作者宕机而永久占用。
- **退回补证**：`POST /api/queue/items/{id}/return` 不重置 `effective_since`，重新入队后等待时间自首次入队连续累计。
- **确定性重建**：优先级完全由持久化字段（属性、`effective_since`、id）和当前规则推导，排序键为 `(-score, effective_since, id)` 全序确定；进程重启后用全新会话重建的顺序完全一致。
- **并发安全**：认领采用「读候选 → 释放快照 → 条件 UPDATE」流程，`UPDATE ... WHERE (等待中 或 租约已到期)` 的 `rowcount` 唯一决定赢家；SQLite 开启 WAL + busy_timeout，多线程下同一条目不会被重复处理。

### 队列 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/queue/items` | 入队（毕业/材料标志、自定义 payload） |
| POST | `/api/queue/claim` | 认领评分最高条目（空队列返回 204），返回 token |
| POST | `/api/queue/items/{id}/renew` | 续租（须带 token，过期 token 返回 409） |
| POST | `/api/queue/items/{id}/return` | 退回补证并保留原等待时间 |
| POST | `/api/queue/items/{id}/complete` | 完成（终态） |
| GET | `/api/queue/items` | 按当前规则查看优先级（可按状态过滤） |
| GET | `/api/queue/rules` | 查看所有规则版本 |
| GET | `/api/queue/stats` | 统计：等待/处理中/过期租约/已完成及下一目标 |

默认规则（毕业 100、材料 50、老化 10 分/小时，租约 300 秒）在首次使用时自动创建；默认参数可通过发布新规则覆盖。测试通过可冻结、可推进的注入式时钟（`app/queue/clock.py`）模拟时间流逝，覆盖并发认领（16 线程压力场景）、时钟推进、规则切换、退回保序、租约回收和重启重建。
