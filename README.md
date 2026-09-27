# 公共交通中断改道发布服务

一个仅使用 Python 标准库实现的线路、站点、班次、施工绕行和无障碍变化发布服务。方案按草稿、复核、批准、发布流转；路径计算会应用停运、跳站、绕行和无障碍限制。草稿版本还可以登记换乘保障，发布时把每笔保障判断写入快照。

## 运行

```bash
python app.py --init
python app.py --port 8010
```

打开 <http://127.0.0.1:8010>。`--init` 会导入两条示例线路、六个站点，以及 L1 的 23:50 和 L2 的 24:00 两个跨日班次。数据库默认是 `transit_disruption.db`，可用 `--db` 或 `TRANSIT_DB` 修改。

## 业务能力

- 基础数据导入会一次性检查线路、站点经纬度、连续站序、重复站点、站间行驶时间和班次时间。错误批次写入 `import_errors` 后整体拒绝，不留下半批数据。
- 中断事件可以包含 `stop_closure`、`skip_stop`、`detour`、`accessibility_change`，可以设置服务日分钟窗口。
- 路径使用 Dijkstra 算法比较基线与方案版本；跳站时车辆可继续通过，但乘客不能在跳站上下车，经过省略路段的行驶时间会计入下一段。
- 班次时间以服务日零点起算，允许超过 1440 分钟。例如 1430 分发车、21 分钟到达会显示为次日 `00:21`。
- 修改只允许发生在草稿版本；创建新版本会复制父版本变更和换乘保障，已发布快照继续保留。
- 发布在一个 SQLite 事务内写入方案快照和 SHA-256，旧发布版本不会被覆盖。
- 换乘保障在草稿版本登记换乘站、接驳线路、步行分钟和最少留乘分钟。版本路径经过换乘站时，按"预计到达 + 步行"可赶上的最晚接驳班次计算留乘分钟，不足即标记 `pending_adjustment`（待调整）并给出 `shortfall_minutes`（差几分钟）。
- 发布时把每笔保障判断写入快照：以末班到达换乘站（不含接驳线路本身）为基准，跳站省略时间累计、绕行按更晚到达顺延。之后基础时刻变化只影响新版本的重算，已发布版本仍显示当时的结论。
- 资料（SQLite 存取）、计算（模块级纯函数 `adjusted_cumulative` / `trip_arrival_at_stop` / `judge_transfer`）和页面（`static/index.html`）分开，仅使用标准库。

## API

使用 `X-User`、`X-Role` 身份头，角色包括 `planner`、`editor`、`reviewer`、`admin`。

- `POST /api/import`：导入基础数据。
- `POST /api/disruptions`：创建中断事件及第一版草稿。
- `POST /api/disruptions/{id}/versions`：从指定父版本复制出新草稿。
- `POST /api/versions/{id}/changes`：向草稿添加停运、跳站、绕行或无障碍变化。
- `POST /api/versions/{id}/guarantees`：向草稿登记换乘保障（换乘站、接驳线路、步行分钟、最少留乘分钟）。
- `POST /api/versions/{id}/submit|approve|reject|publish`：完成复核发布流程。
- `GET /api/versions/{id}`：版本详情附带保障判断；未发布版本实时重算，已发布版本返回快照中的当时结论。
- `GET /api/route?from=1&to=5&version_id=1&at_minute=1430&accessible=true`：查询路径、耗时和到达时间；带 `version_id` 且路径经过换乘站时返回 `transfer_guarantees` 判断。
- `GET /api/trips/{id}`：查看跨日班次各站时间。
- `GET /api/import-errors`：查看被隔离的错误批次。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖基线/改道路径、版本复制与发布隔离、审批冲突、无障碍路径、跨日时刻、坏数据整批隔离，以及换乘保障的登记校验、待调整与差额标记、快照冻结和末班到达推算。
