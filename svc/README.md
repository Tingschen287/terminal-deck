# 本机项目读取服务

`read_service.py` 用 Python 标准库实现，与 projectctl 同语言、无第三方依赖，也无需改动
安排仓现有 Go 构建。它只监听 Unix stream socket（不创建 TCP 监听器），socket 权限为
`0600`，只把四类只读请求转发给 projectctl 的 `read-project`、`read-node`、
`read-events`、`read-text`。服务不读取凭据、不联网，也没有项目写入操作。

需要 Python 3.11+（当前 projectctl 的语法要求；本机使用 `python3.12`）和 PyYAML。

## 启动

```bash
python3.12 svc/read_service.py serve \
  --socket /tmp/anpai-read-$UID.sock \
  --projectctl /path/to/01.assets/project-control/projectctl.py
```

服务在前台运行。已存在的 socket 路径不会被覆盖；路径最长 96 字节。停止时向该进程发送
SIGTERM，或在前台按 Ctrl-C；服务退出会删除自己创建的 socket：

```bash
kill <pid>
```

## 健康检查

```bash
python3.12 svc/read_service.py health --socket /tmp/anpai-read-$UID.sock
```

## 请求协议

每个连接发送一行 JSON 对象，收到一行 JSON 响应。`project` 是项目根目录或其中路径。

```json
{"op":"health"}
{"op":"project","project":"/path/to/project"}
{"op":"node","project":"/path/to/project","node_id":"n075_read_interface"}
{"op":"events","project":"/path/to/project","limit":50}
{"op":"events","project":"/path/to/project","cursor":"evt_000123_abcd","limit":50}
{"op":"text","project":"/path/to/project","path":"4.eval/control/reviews/.../review.json","max_bytes":1048576}
```

增量事件读取使用上一次响应的 `result.page.next_cursor`。响应保留 projectctl 的
`graph` 与 `event_cursor` 元数据。错误返回 `{"ok":false,"error":"..."}`，不会把
读不到的数据源伪装为空结果。

## 测试

```bash
python3.12 -m unittest discover -s svc -v
```
