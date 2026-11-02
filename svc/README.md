# 本机项目读取服务

`read_service.py` 用 Python 标准库实现，与 projectctl 同语言、无第三方依赖，也无需改动
安排仓现有 Go 构建。它只监听 Unix stream socket（不创建 TCP 监听器），socket 权限为
`0600`，只把四类只读请求转发给 projectctl 的 `read-project`、`read-node`、
`read-events`、`read-file`；另可只读地提供 owner 收件箱的结构化视图（见文末）。服务不读取凭据、不联网，
也没有任何写入操作。

**socket 上不传任何原文。** 文件正文与人写文本（事件原因、处置要求、评审 blocker、合同
目标与验收标准等）都不经本服务：文件只有路径、大小、sha256、mtime 与账本引用；人写文本只有
`{length, sha256, source, field}` 摘要；其余字段是格式受控的标识或枚举，映射按固定键白名单
输出、未知键丢弃。原文由 GUI 用响应里的 `project_root` 拼上 `source` 在本机自己打开。细节见
projectctl README「不写盘的 JSON 读取」。

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
{"op":"file","project":"/path/to/project","path":"4.eval/control/reviews/.../review.json"}
```

增量事件读取使用上一次响应的 `result.page.next_cursor`。响应保留 projectctl 的
`graph` 与 `event_cursor` 元数据。读不到的数据源不会伪装为空结果。

错误响应固定为 `{"ok":false,"error":"<错误码>","message":"<固定说明>"}`，不含任何请求内容、
projectctl 输出或异常文本。服务的 stderr 同样只写固定错误码（每行 `read service error: <错误码>`），
任何运行模式下都不收集、不转写 projectctl 的 stderr（直接丢弃），不写异常文本或请求内容，
没有调试开关。服务自身的错误码：`request_invalid`、
`request_timeout`、`capacity_reached`、`unsupported_operation`、`argument_invalid`、
`projectctl_unavailable`、`projectctl_timeout`、`projectctl_invalid_response`、
`projectctl_failed`、`internal_error`；projectctl 的读取错误码（`node_not_found`、
`path_outside_project`、`project_root_unsafe`、`history_invalid` 等）原样转发错误码，未知的一律 `projectctl_failed`。

每个连接的请求读取超时为 5 秒，最多同时处理 16 个连接，监听队列容量为 64。
容量占满时新连接收到 `capacity_reached` 错误；慢连接超时后
释放处理名额，不会无限占用线程。容量拒绝在 accept 时立即回一行错误再关闭连接，
客户端应先读响应；连上后先写请求可能遇到对端已关闭（`BrokenPipeError`）。

## owner 收件箱：只读视图与答复写回（n077）

收件箱 `00.projects/owner-inbox.md` 的条目格式见 n077 交付物 `inbox-format.md`。两条通道分开：

| 通道 | 进程 / socket | 谁能用 | 做什么 |
|---|---|---|---|
| 只读 | `read_service.py serve --inbox <文件>`，同一个读取 socket，`{"op":"inbox"}` | 本用户任何进程 | 返回待你定、已定、信任档位表的结构化结果 |
| 写回 | `inbox_write_service.py serve`，**另一个进程、另一个 socket** | 只有登记的 owner 界面程序 | 在一条待你定条目末尾追加答复 |

只读视图同样不传原文：标题、背景、依据、选项、推荐、默认、主管名、答复文字都只给
`{field, chars, bytes, sha256, byte_start, byte_end}`，GUI 用 `result.file.path` 在本机打开文件、核对
`result.file.sha256` 后按字节切片。传出的只有日期、枚举（档位 L0–L3、答复 yes/no/change、动作 none/graph/staff/text）、
选项字母、Graph 标识、行号与偏移。解析不了的条目 `status: unparsed`，带原因码与字节范围，不丢、不猜。
条目的 `warnings` 是格式警告码：`title_bare_node_id` 表示标题里有裸节点 id（应写 `[项目简称·节点简称 编号]`），
只提示，不影响解析与写回。
文件路径只来自启动参数，请求里给的路径一律忽略。

```bash
python3.12 svc/read_service.py serve --socket /tmp/anpai-read-$UID.sock \
  --projectctl /path/to/01.assets/project-control/projectctl.py \
  --inbox /path/to/00.projects/owner-inbox.md

python3.12 svc/inbox_write_service.py serve --socket /tmp/anpai-inbox-write-$UID/w.sock \
  --inbox /path/to/00.projects/owner-inbox.md \
  --state-dir ~/.local/state/anpai \
  --owner-client /Applications/Anpai.app/Contents/MacOS/Anpai
```

写回请求（一行 JSON）：

```json
{"op":"answer","request_id":"<8–64 位字母数字-_>","expected_sha256":"<读取时的 file.sha256>",
 "entry":{"line_start":30,"sha256":"<条目 sha256>"},"answer":{"kind":"yes|no|change","text":"<change 时一行文字>"}}
```

- 只在条目末尾追加：旧格式在行尾加 ` 你答：行`，新格式在条目后插入一行 `  - 你答：行`；别的字节不动，
  也不挪条目、不打勾（照办与挪到「已定」仍由主管按 playbook 做）。
- 文件哈希不等于 `expected_sha256` 就拒绝（`inbox_changed`，附 `current_sha256`），GUI 重读后用新的
  `request_id` 再答。写的过程：临时文件 → fsync → 再核对 → 原子交换（macOS `renamex_np RENAME_SWAP`）→
  核对换下来的确是原文件，否则换回并拒绝。不支持原子交换就拒绝（`swap_unavailable`），不退化成覆盖。
- `--state-dir` 下 `inbox-writeback.jsonl` 是只追加的写回记录（0600）：时间、request_id、条目行号与哈希、答复、
  前后哈希、阶段（prepared / applied / rejected / recovered / uncertain）。同一 `request_id` 重发返回原结果，不重写。
- 身份：对端可执行文件必须是 `--owner-client` 登记的程序（启动时记下 inode、大小、mtime，之后被换就拒绝），
  进程链上不能有已知 Agent CLI；macOS 以外一律拒绝。细节见 `owner_identity.py` 文件头。
- 错误响应只有固定错误码与固定说明，stderr 只写 `inbox write service error: <错误码>`。

## 测试

```bash
python3.12 -m unittest discover -s svc -v
```

身份端到端用例需要 macOS 与 `cc`（现编 `testdata/owner_client_fixture.c` 当 owner 界面程序），否则跳过。
