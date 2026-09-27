# 公共交通中断改道发布服务

一个仅使用 Python 标准库实现的线路、站点、班次、施工绕行、无障碍变化和换乘保障发布服务。方案按草稿、复核、批准、发布流转；路径计算会应用停运、跳站、绕行和无障碍限制。

代码分层：资料（`app.py` 的 `Database`，负责建表、取数和事务）、计算（`transfer_guarantee.py` 的纯函数保障判断，以及路由寻路）、页面（`static/index.html`）。

## 运行

```bash
python app.py --init
python app.py --port 8010
```

打开 <http://127.0.0.1:8010>。`--init` 会导入三条示例线路、七个站点、一个 23:50 发车的跨日班次和两班 S4 站 00:20/00:40 的夜班接驳。数据库默认是 `transit_disruption.db`，可用 `--db` 或 `TRANSIT_DB` 修改。

## 业务能力

- 基础数据导入会一次性检查线路、站点经纬度、连续站序、重复站点、站间行驶时间和班次时间。错误批次写入 `import_errors` 后整体拒绝，不留下半批数据。
- 中断事件可以包含 `stop_closure`、`skip_stop`、`detour`、`accessibility_change`，可以设置服务日分钟窗口。
- 路径使用 Dijkstra 算法比较基线与方案版本；跳站时车辆可继续通过，但乘客不能在跳站上下车，经过省略路段的行驶时间会计入下一段。
- 班次时间以服务日零点起算，允许超过 1440 分钟。例如 1430 分发车、21 分钟到达会显示为次日 `00:21`。
- 修改只允许发生在草稿版本；创建新版本会复制父版本变更和换乘保障，已发布快照继续保留。
- 发布在一个 SQLite 事务内写入方案快照和 SHA-256，旧发布版本不会被覆盖。
- 换乘保障：草稿登记换乘站、接驳线路、步行分钟和最少留乘分钟。查路径时按预计到达（含方案改道后的行驶时间）找出能赶上的最晚接驳班次，留乘不足就标成 `待调整` 并写明 `shortfall_minutes` 差几分钟。版本级判断以换乘站末班到达（不含接驳线本身）为预计到达；发布时把每笔保障判断写进快照，基础时刻变化只重算新版本，旧发布版本仍显示当时的结论。

## API

使用 `X-User`、`X-Role` 身份头，角色包括 `planner`、`editor`、`reviewer`、`admin`。

- `POST /api/import`：导入基础数据。
- `POST /api/disruptions`：创建中断事件及第一版草稿。
- `POST /api/disruptions/{id}/versions`：从指定父版本复制出新草稿。
- `POST /api/versions/{id}/changes`：向草稿添加停运、跳站、绕行或无障碍变化。
- `POST /api/versions/{id}/guarantees`：向草稿登记换乘保障（换乘站、接驳线路、步行分钟、最少留乘分钟）。
- `GET /api/versions/{id}/guarantees`：查看版本保障判断；发布版本返回快照结论，未发布版本按当前基础时刻重算。
- `POST /api/versions/{id}/submit|approve|reject|publish`：完成复核发布流程。
- `GET /api/route?from=1&to=5&version_id=1&at_minute=1430&accessible=true`：查询路径、耗时、到达时间和换乘保障判断。
- `GET /api/trips/{id}`：查看跨日班次各站时间。
- `GET /api/import-errors`：查看被隔离的错误批次。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖基线/改道路径、版本复制与发布隔离、审批冲突、无障碍路径、跨日时刻、坏数据整批隔离，以及换乘保障判断、发布快照冻结和基础时刻变化后的新版本重算。
