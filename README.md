# 断点续传文件 API（Resumable Upload API）

面向弱网客户端的分片上传/断点续传服务：客户端先创建上传会话，再按**任意顺序**提交带起止偏移的二进制分片；服务端拒绝越界或与已收区间重叠的分片，并通过状态接口返回缺失区间。仅当分片**完整覆盖整个文件**且整文件 **SHA-256 校验通过**时才原子完成并开放下载——任何失败都不会产生可下载成品。

创建会话支持**幂等键**：`POST /sessions` 可携带 `Idempotency-Key` 头，首次响应丢失或并发重试时返回同一会话，不会重复建会话（详见下文「创建上传会话」）。

已经完整上传并校验通过的会话还可以作为**复制来源**：`POST /sessions/{id}/copy-chunk` 由服务端直接把来源成品文件的指定字节区间复制为目标会话的一个分片，弱网客户端无需重新上传重复内容（详见下文「服务端复制分片」）。

已经登记的分片如果事后发现内容损坏（例如最后整文件 SHA-256 校验失败），不必放弃整个会话：`PUT /sessions/{id}/chunks/repair` 可对**一条已登记分片原位替换为等长新内容**，缺失区间与已收字节数保持不变，修复后重新完成整文件校验即可（详见下文「已收分片原位修复」）。

不知道具体是哪一片损坏时，可以先调 **`POST /sessions/{id}/chunks/audit` 分片完整性诊断**：提交最多 32 条 `{start,end,sha256}` 清单，服务端按起点顺序流式核对磁盘分片，逐条返回匹配、摘要不符、文件缺失或长度不符及实际摘要，客户端据此只对有问题的区间发起 repair（详见下文「分片完整性诊断」）。

完成后的文件支持两类下载：

- **会话下载**（原有接口，不变）：`GET /sessions/{id}/download`，无次数限制；
- **受控下载票据**（新增）：可签发放发带**有效期（1～1440 分钟）**与**下载次数上限（1～100 次）**的一次性随机票据，凭票 URL 下载；服务端只保存票据的哈希，可随时撤销。

技术栈：Python 3.12 · FastAPI · SQLite（WAL 模式） · Uvicorn，容器化一键启动。

---

## 一键启动

需要 Docker 与 Docker Compose v2（`docker compose`）：

```bash
docker compose up --build -d
```

服务监听宿主机 **8000** 端口：

- 健康检查：<http://localhost:8000/health>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- 停止并清理：`docker compose down`（数据保留在命名卷 `resumable-data` 中；`-v` 连数据一起删除）

> 单容器单 uvicorn worker 部署。会话级互斥锁是进程内对象，多副本水平扩容需要额外的共享锁/存储，本版本不涉及。

---

## 配置项

全部配置通过环境变量覆盖（见 `docker-compose.yml` 的 `environment` 段）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `/data` | 数据根目录（SQLite 库、分片、成品、临时文件） |
| `DATABASE_URL` | `sqlite:///$DATA_DIR/resumable.db` | SQLite 文件路径（形如 `sqlite:////path/to.db`） |
| `SESSION_TTL_SECONDS` | `86400`（1 天） | 会话存活时长；每次分片活动向后滑动（滑动过期） |
| `CLEANUP_INTERVAL_SECONDS` | `300`（5 分钟） | 后台清理线程的运行间隔 |
| `MAX_CHUNK_BYTES` | `67108864`（64 MiB） | 单个分片最大字节数，`0` 表示不限制 |
| `MAX_UPLOAD_BYTES` | `107374182400`（100 GiB） | 整文件最大字节数，`0` 表示不限制 |
| `MAX_FILENAME_LENGTH` | `255` | 文件名最大长度 |

数据目录布局：

```
$DATA_DIR/
├── resumable.db (+ -wal / -shm)     SQLite 数据库（upload_sessions / chunks / download_tickets / idempotency_keys）
├── files/<session_id>               完成后的成品（原子 rename 落地）
├── parts/<session_id>/<start>-<end> 已接收分片（修复期间另有同名 .repairbak 备份，瞬时存在）
└── tmp/                             正在接收/拼装的临时文件（启动周期清理）
```

数据库结构通过 `PRAGMA user_version` 管理版本，**服务启动时自动建库/增量迁移**，老库数据保留：v1 为上传会话与分片表，v2 新增 `download_tickets`（受控下载票据表），v3 新增 `idempotency_keys`（创建会话幂等键表，仅存键的 SHA-256 与请求指纹）。

---

## API 约定

- Base URL：`/api/v1`
- 偏移量为字节偏移，采用**半开区间 `[start, end)`**，即 `end - start = 分片字节数`
- 分片可以按**任意顺序**提交，允许重连后只补传缺失部分
- 错误响应体：`{"detail": {"error": "<错误码描述>", ...附加上下文}}`

### 1. 创建上传会话

```http
POST /api/v1/sessions
Content-Type: application/json
Idempotency-Key: order-2026-09-20-0001   ← 可选

{
  "filename": "movie.mp4",
  "total_size": 104857600,
  "sha256": "9f1b...（64 位小写十六进制，允许 sha256: 前缀）",
  "chunk_size": 4194304
}
```

`chunk_size` 仅为客户端建议值，可为 0。返回 `201` 与完整状态（含 `session_id`、`expires_at`、初始 `missing_ranges`）。

**幂等创建（可选）**：携带 `Idempotency-Key` 头时，相同键 + 相同请求的重试返回**首次创建的会话**（同一 `session_id`，状态码仍为 `201`），不会重复建会话——首次响应丢失后的重试与并发重试都安全。

- 键格式：**1～128 个可见 ASCII 字符**（0x21–0x7E，不含空格），不合法返回 `400`。
- 服务端**只保存键的 SHA-256 与规范化请求指纹**（`filename`/`total_size`/`sha256`/`chunk_size` 规范化后的 SHA-256），明文键不落库。
- 相同键搭配**不同**请求（四个字段任一不同）→ `409`，且不新建会话。
- 键记录与会话在**同一事务**中创建；删除会话（`DELETE /sessions/{id}`）时键记录级联删除，之后同键请求按全新请求处理。
- 不带该头的请求行为不变：每次都创建新会话。

### 2. 提交二进制分片

```http
PUT /api/v1/sessions/{session_id}/chunks?start=0&end=4194304
Content-Type: application/octet-stream
Content-Length: 4194304
X-Chunk-SHA256: 3a7b...（可选，本分片字节的 64 位十六进制 SHA-256，允许 sha256: 前缀）

<原始二进制分片>
```

服务端校验（任一不过即拒绝，不写入任何分片数据）：

- 会话存在且处于 `uploading`（未过期/未放弃/未完成）
- `0 <= start < end <= total_size` —— 越界返回 `400`
- `Content-Length == end - start`，且实际请求体长度一致 —— 不一致返回 `400/413`
- 分片不超过 `MAX_CHUNK_BYTES` —— 超限返回 `413`
- `[start, end)` 与任何已收区间**不得重叠**（相邻允许，如已有 `[0,100)` 再传 `[100,200)`）—— 重叠返回 `409`

**`X-Chunk-SHA256`（可选，逐片校验）**：客户端若提供该头，服务端在流式接收分片的同时增量计算 SHA-256，并在**写入分片目录、登记区间之前**完成校验：

- 头格式错误（不是 64 位十六进制、长度不对、含非法字符等）→ `400 {"error":"invalid X-Chunk-SHA256 header"}`，在读取请求体之前拒绝，**不滑动 TTL、不落盘、不登记**；
- 请求体实际长度与区间不符 → 仍按长度错误返回 `400/413`（长度校验优先于摘要）；
- 长度正确但摘要不匹配 → `422 {"error":"chunk sha256 mismatch","expected":...,"actual":...,"start":...,"end":...}`，临时文件立即删除，**分片不落盘、不登记区间**；客户端用正确内容重传同一区间即可正常继续，最终整文件 SHA-256 校验不受影响。
- 不携带该头的请求**行为完全不变**（仅依赖完成时的整文件 SHA-256 兜底）；同一会话的各个分片可以自由选择是否带头。

curl 示例：

```bash
digest=$(sha256sum chunk-0.bin | awk '{print $1}')
curl -X PUT \
  -H "Content-Type: application/octet-stream" \
  -H "X-Chunk-SHA256: $digest" \
  --data-binary @chunk-0.bin \
  "http://localhost:8000/api/v1/sessions/$SID/chunks?start=0&end=4194304"
```

返回当前累计字节数与剩余 `missing_ranges`。并发提交同一区间时只有一个成功，其余得到 `409`。

### 2.1 服务端复制分片（copy-chunk）

```http
POST /api/v1/sessions/{session_id}/copy-chunk
Content-Type: application/json

{
  "source_session_id": "9f2c...",
  "source_start": 4194304,
  "source_end":   8388608,
  "target_start": 0
}
```

把**已完成来源会话成品**的半开区间 `[source_start, source_end)` 在服务端直接复制成目标会话从 `target_start` 开始的一个分片（目标区间长度 = `source_end - source_start`）。不经过客户端转发，适合“同一批字节要上传到多个新会话 / 重复前缀”的场景。

- 来源会话必须**已完成**（`status=completed`）且**成品文件存在**；来源未完成返回 `409 {"error":"source session is not completed"}`，来源不存在返回 `404`，库里标记完成但成品缺失返回 `409 {"error":"source completed artifact is missing"}`。
- 来源区间越界（`source_end > 来源 total_size`，或 `source_start >= source_end`）返回 `400/422`。
- 目标区间 `[target_start, target_start + 长度)` 越过目标 `total_size` 返回 `400`；单片超过 `MAX_CHUNK_BYTES` 返回 `413`；与目标已收区间重叠返回 `409`（相邻允许）。
- 来源会话与目标会话不得相同，否则 `400`；目标会话已完成时返回 `409`。
- **任一校验失败都不登记分片、不增加 `bytes_received`、不残留临时文件。**

复制成功的响应与普通 `PUT` 分片**完全一致**（`ChunkResponse`：`received` / `bytes_received` / `missing_ranges` / `fully_received` / `expires_at`），登记进同一张 `chunks` 表，因此后续仍按原有规则工作：必须**完整覆盖 + 整文件 SHA-256 校验通过**才能 `complete`，`PUT` 分片与 `copy-chunk` 可以在同一会话中任意混用。

实现上：复制期间同时持有来源、目标两把会话锁（按会话 ID 全局定序加锁，两个方向相反的并发复制不会死锁），先做全部预校验再流式复制到 `tmp/`，随后在单个 `BEGIN IMMEDIATE` 事务内二次校验、移动文件、登记区间与累加字节；事务任一步失败都会回滚并删除临时文件与未登记的落地文件，不留下已登记的损坏分片。复制期间来源无法被并发删除、目标无法被并发放弃。

curl 示例（服务监听 <http://localhost:8000>）：

```bash
curl -X POST http://localhost:8000/api/v1/sessions/$TARGET_SID/copy-chunk \
  -H "Content-Type: application/json" \
  -d '{"source_session_id":"'"$SOURCE_SID"'","source_start":4194304,"source_end":8388608,"target_start":0}'
```

### 2.2 已收分片原位修复（chunks/repair）

`complete` 返回 `422 sha256 mismatch` 时，说明某个已收分片的内容在传输/落盘过程中损坏。与其放弃会话重新上传，客户端可以先想办法定位损坏分片（例如客户端本地按相同偏移切分后比对摘要），再用**原位修复接口**把这一片替换成等长的正确内容：

```http
PUT /api/v1/sessions/{session_id}/chunks/repair?start=4194304&end=8388608
Content-Type: application/octet-stream
Content-Length: 4194304
X-Chunk-SHA256: 7c21...（可选，新内容的 64 位十六进制 SHA-256，允许 sha256: 前缀）

<与原分片等长的新二进制内容>
```

- 仅允许**上传中**（`uploading`）会话调用：会话不存在 → `404`；已过期/已放弃 → `410`；已完成 → `409`（完成后的会话不可改，且分片目录已在完成后清理）。
- `start`/`end`（半开区间 `[start, end)`）必须与某条**已登记分片完全一致**（同一个起止偏移）；只是部分重叠、相邻、或合并区间内跨分片边界都返回 `404 {"error":"no registered chunk matches the range exactly"}`。区间不存在时原文件、分片登记与已收字节数全部保留。
- 新内容长度必须等于 `end - start`：`Content-Length` 不符 → `400/411`；流式接收中实际超出 → `413`；单片超过 `MAX_CHUNK_BYTES` → `413`；`start >= end` → `400`。
- `X-Chunk-SHA256` 头的规则与普通 PUT 分片完全一致：格式错误在读体之前拒绝 → `400`；长度正确但摘要不符 → `422 {"error":"chunk sha256 mismatch",...}`，临时文件删除，**旧分片原封不动**。
- **任何失败（长度/摘要不符、会话失效、区间不存在）都不改变旧分片文件、chunks 登记、`bytes_received` 与 `missing_ranges`，也不滑动 TTL**；客户端修正请求后可直接重试，旧分片始终可用。

成功响应与普通 PUT 分片完全一致（`ChunkResponse`），但语义是**替换而非新增**：

- `received` 仍是被修复的 `{start, end}`；
- `missing_ranges`、`bytes_received`、`fully_received` 与修复前**完全相同**（登记的区间集合没有变化）；
- 成功后滑动活动时间（视为一次分片活动）；随后可再次 `POST /complete` 重新拼装并做整文件 SHA-256 校验。

实现上：首次校验通过后请求体流式写入 `tmp/`（弱网下不整分片进内存，可增量计算摘要）；提交阶段在会话锁内的单个 `BEGIN IMMEDIATE` 事务中复查会话状态与区间登记，随后按 **“旧分片同目录原子改名为备份 → 新分片原子 rename 落位 → 更新活动时间”** 的顺序完成替换，因此并发 `complete` 或 `DELETE`（放弃）只会看到旧分片或新分片，绝不会读到半替换内容。落盘或数据库事务任一步失败都会回滚事务并把旧分片从备份原子恢复，不留临时文件/备份；并发修复同一片串行执行，最后一次成功的内容为准（last-writer-wins）。进程崩溃残留的备份文件由后台清理线程定期回收。

curl 示例（服务监听 <http://localhost:8000>）：

```bash
SID=...                    # 上传中、且 [4194304,8388608) 已登记的会话
digest=$(sha256sum fixed-chunk-1.bin | awk '{print $1}')
curl -X PUT \
  -H "Content-Type: application/octet-stream" \
  -H "X-Chunk-SHA256: $digest" \
  --data-binary @fixed-chunk-1.bin \
  "http://localhost:8000/api/v1/sessions/$SID/chunks/repair?start=4194304&end=8388608"

# 修复成功（缺失区间/字节数不变）后重新完成整文件校验
curl -X POST "http://localhost:8000/api/v1/sessions/$SID/complete"
```

### 2.3 分片完整性诊断（chunks/audit）

`complete` 返回 `422 sha256 mismatch` 后，若客户端无法自行判断哪一片损坏，可用诊断接口把**已登记分片的清单**（每条含区间与期望摘要，最多 **32** 条）提交给服务端，由服务端按起点顺序流式核对磁盘分片，逐条给出存在性、长度与摘要核对结果，客户端再只对有问题的区间调用 `repair`：

```http
POST /api/v1/sessions/{session_id}/chunks/audit
Content-Type: application/json

{
  "chunks": [
    {"start": 0,          "end": 4194304,  "sha256": "3a7b...（64 位十六进制，允许 sha256: 前缀）"},
    {"start": 4194304,    "end": 8388608,  "sha256": "7c21..."},
    {"start": 8388608,    "end": 12582912, "sha256": "91d4..."}
  ]
}
```

**清单规则（任一不满足，整批拒绝，不返回任何逐条结果）：**

- `chunks` 必须有 **1～32** 条；条数为空或超过 32 返回 `422`；单条 `start >= end`、偏移为负、摘要格式错误也返回 `422`。
- 每条的半开区间 `[start, end)` 都必须与一条**已登记分片完全一致**；存在未登记、仅部分重叠或跨分片边界的区间 → `400 {"error":"audit manifest ranges must exactly match registered chunks","unregistered":[...]}`（允许只核对已登记分片的一个子集，顺序任意）。
- 清单内区间**不可重复**（相同 `{start,end}` 出现两次）→ `400 {"error":"audit manifest contains duplicate ranges","duplicates":[...]}`。

**响应（`200`）：** 按 `start` 升序逐条返回（与提交顺序无关）：

```json
{
  "session_id": "e3a886dc...",
  "results": [
    {"start": 0,       "end": 4194304, "expected_sha256": "3a7b...", "status": "ok",
     "actual_size": 4194304, "actual_sha256": "3a7b..."},
    {"start": 4194304, "end": 8388608, "expected_sha256": "7c21...", "status": "digest_mismatch",
     "actual_size": 4194304, "actual_sha256": "ab12..."},
    {"start": 8388608, "end": 12582912, "expected_sha256": "91d4...", "status": "missing",
     "actual_size": null, "actual_sha256": null}
  ]
}
```

- `status` 取值：`ok`（文件存在、长度与区间一致且摘要匹配）、`digest_mismatch`（长度一致但摘要不符）、`missing`（已登记但磁盘分片缺失）、`length_mismatch`（磁盘文件长度与 `end-start` 不符）。
- 非 `ok` 的结果都带 `actual_size` 与 `actual_sha256`（文件缺失时两者为 `null`），据此即可确定 repair 的目标区间：`digest_mismatch` / `length_mismatch` 用正确内容对同一区间发起 repair；`missing` 也可直接 repair 补齐（登记仍在）。

**会话与只读语义：**

- 仅允许**上传中**会话：会话不存在 → `404`；已过期/已失效 → `410`；已完成 → `409`（完成后分片目录已删除，诊断无意义）。
- 诊断**全程持有会话锁**，与分片提交、repair、complete、放弃互斥：同一次诊断读到的磁盘分片集合必然自洽，不会因与修复/完成/放弃交错而误报。
- 诊断是**纯只读**操作：**不滑动 TTL**（`last_access_at` / `expires_at` 不变）、不改分片登记与 `bytes_received`、不产生临时文件；分片以 1 MiB 分块流式计算摘要，大分片也不会整文件进内存。

curl 示例（服务监听 <http://localhost:8000>，用 `jq` 挑出非 ok 的区间）：

```bash
SID=...                    # 上传中、且清单区间均已登记的会话
curl -sS -X POST "http://localhost:8000/api/v1/sessions/$SID/chunks/audit" \
  -H "Content-Type: application/json" \
  -d '{"chunks":[
        {"start":0,"end":4194304,"sha256":"3a7b..."},
        {"start":4194304,"end":8388608,"sha256":"7c21..."}
      ]}' | jq '.results[] | select(.status != "ok")'

# 对返回的每个非 ok 区间用正确内容发起原位修复，随后重新 complete
curl -X PUT --data-binary @fixed-chunk-1.bin \
  "http://localhost:8000/api/v1/sessions/$SID/chunks/repair?start=4194304&end=8388608"
curl -X POST "http://localhost:8000/api/v1/sessions/$SID/complete"
```

### 3. 查询状态 / 缺失区间

```http
GET /api/v1/sessions/{session_id}
```

```json
{
  "session_id": "...",
  "filename": "movie.mp4",
  "total_size": 104857600,
  "status": "uploading",
  "bytes_received": 8388608,
  "fully_received": false,
  "complete": false,
  "missing_ranges": [{"start": 4194304, "end": 8388608}, {"start": 12582912, "end": 104857600}],
  "expires_at": "2026-09-21T12:00:00Z",
  "last_access_at": "2026-09-20T12:00:00Z",
  "completed_at": null,
  "download_url": null
}
```

客户端断连恢复时：先查状态，按 `missing_ranges` 补传即可。

### 4. 原子完成

```http
POST /api/v1/sessions/{session_id}/complete
```

- 区间未全覆盖 → `409`，响应里带当前 `missing_ranges`，不产生成品
- 已覆盖但 SHA-256 不匹配 → `422`（返回 `expected`/`actual`），会话保持 `uploading`，客户端可修正后重试或放弃；不产生成品
- 全部通过 → 拼装临时文件 → 校验 → 与状态翻转在同一事务窗口内**原子发布**，状态变为 `completed`，开放下载。完成接口幂等。

### 5. 下载成品（会话下载，不计数）

```http
GET /api/v1/sessions/{session_id}/download
```

仅 `completed` 会话返回 `200`（支持 `Range` 断点下载续传）；其他状态一律 `404`，失败流程中不存在任何可下载产物。该接口保持不变、不校验票据、不限制次数。

### 6. 签发受控下载票据

```http
POST /api/v1/sessions/{session_id}/tickets
Content-Type: application/json

{
  "expires_in_minutes": 60,
  "max_downloads": 5
}
```

- `expires_in_minutes`：有效期分钟数，范围 **1～1440**；`max_downloads`：下载次数上限，范围 **1～100**。越界返回 `422`。
- 仅 `completed` 会话可签发，否则 `409`。
- 服务端生成约 256 bit 熵的随机票据，**数据库只保存其 SHA-256**，明文仅在 `201` 响应中返回这一次，服务端无法再次展示，也无法从库中反推：

```json
{
  "ticket_id": "777aaac7...",
  "session_id": "e3a886dc...",
  "created_at": "2026-09-20T16:00:51Z",
  "expires_at": "2026-09-20T17:00:51Z",
  "max_downloads": 5,
  "remaining_downloads": 5,
  "revoked": false,
  "download_url": "/api/v1/tickets/777aaac7.../download",
  "ticket": "777aaac7....Uw21NON6si8F-Ri4B2HKWFXMCcDkY4wY3u_eyaR8rmQ"
}
```

`ticket` 即下载凭证（`<ticket_id>.<随机串>`，URL-safe），把它拼到下载路径即可分发；请像对待密码一样保管它，票据 URL 可能出现在代理/访问日志中。

### 7. 凭票下载（原子计数，支持 Range）

```http
GET /api/v1/tickets/{ticket}/download
GET /api/v1/tickets/{ticket}/download
Range: bytes=100-199
```

服务端在**单个 SQLite `BEGIN IMMEDIATE` 事务**内完成全部校验与扣减：

1. 票据存在（id 与随机串的哈希同时匹配），否则 `404`；
2. 未撤销，否则 `403`；
3. 未过期（当前时间 ≤ `expires_at`），否则 `410`；
4. `remaining_downloads > 0` 且会话成品仍存在，否则 `429` / `404`。

校验通过后用条件 `UPDATE ... WHERE revoked=0 AND expires_at>? AND remaining_downloads>0` 原子扣减一次再提交。并发请求在事务内串行，后到者看到的剩余次数为 0，**不可能突破上限**；任何校验失败都在扣减前回滚，**绝不计数**。

- 完整下载返回 `200`，`Range` 请求返回 `206`——**Range 同样按一次计数**（下载器的断点续传会消耗票据次数）。
- 次数用尽后返回 `429 {"detail":{"error":"ticket download limit reached"}}`。
- 票据独立计数，互不影响；删除所属会话会级联删除其票据。

### 8. 撤销票据

```http
POST /api/v1/tickets/{ticket}/revoke
```

幂等：已撤销再次调用仍返回 `200` 与票据元数据（`revoked: true`，剩余次数原样保留）。票据不存在/伪造返回 `404`。撤销后任何凭票下载立即得到 `403`，且不扣减。

### 9. 放弃会话

```http
DELETE /api/v1/sessions/{session_id}
```

幂等，返回 `204`：删除会话记录、已收分片与成品。

### 10. 存储汇总（运维只读）

```http
GET /api/v1/storage/summary
```

在**单个 SQLite 只读快照**中按会话状态汇总，供运维容量观测。响应固定包含 `uploading`、`completed`、`aborted`、`expired` 四行（即使计数为零也保留），外加由四行求和得到的 `total` 行：

```json
{
  "statuses": [
    {"status": "uploading", "session_count": 3, "declared_bytes": 210, "stored_bytes": 130},
    {"status": "completed", "session_count": 1, "declared_bytes": 10,  "stored_bytes": 10},
    {"status": "aborted",   "session_count": 0, "declared_bytes": 0,   "stored_bytes": 0},
    {"status": "expired",   "session_count": 0, "declared_bytes": 0,   "stored_bytes": 0}
  ],
  "total": {"status": "total", "session_count": 4, "declared_bytes": 220, "stored_bytes": 140}
}
```

字段口径：

- `declared_bytes`：该状态所有会话 `total_size`（客户端声明大小）之和。
- `stored_bytes`：实际占用的逻辑字节数——`uploading` 取 `bytes_received` 之和，`completed` 取 `total_size` 之和，`aborted` / `expired` 恒为 `0`。

该接口是**纯只读**的：以 `mode=ro` + `PRAGMA query_only` 的独立连接执行单条聚合 SQL，**不扫描文件系统、不更新 `last_access_at`、不滑动会话 TTL，也不触发访问时懒过期**（已过 `expires_at` 但尚未被后台清理的会话仍按 `uploading` 统计）。

### 状态码与生命周期

| 状态 | 含义 |
| --- | --- |
| `uploading` | 收片中；超过 `expires_at` 未活动则过期 |
| `completed` | 校验通过、成品可下载 |
| `expired` | 已被懒过期或后台清理标记，再提交分片得到 `410`，分片已清除 |
| `aborted` | 已放弃（删除接口直接硬删除，正常不可见） |

---

## 过期与清理

- **滑动过期**：创建会话按 `SESSION_TTL_SECONDS` 生成 `expires_at`；每次成功的分片活动自动顺延。
- **后台清理线程**：每 `CLEANUP_INTERVAL_SECONDS` 执行一轮，把过期会话标记为 `expired`、删除其分片目录，并清理已完成会话的分片、孤儿目录和崩溃残留的临时文件。
- **访问时懒过期**：即使后台线程尚未轮到，对已过期会话的任何操作都会立即得到 `410` 并触发清理。
- 已完成的成品**不**随会话过期删除。
- 下载票据的过期与次数在每次凭票下载时于事务内即时校验，过期/撤销的票据不依赖后台清理；删除会话时票据随外键 `ON DELETE CASCADE` 一并删除。

## 弱网客户端建议

- 分片大小取 256 KiB～4 MiB；上传失败时按指数退避重试同一分片（重复提交重叠分片会得到 `409`，可直接忽略并查状态）。
- 恢复后先调 `GET /sessions/{id}`，只补 `missing_ranges`。
- 可选为每个分片附带 `X-Chunk-SHA256` 头：弱网/代理导致的分片片内损坏在提交时即可被 `422` 发现并就地重传，不必等到最后整文件校验失败后放弃整个会话。
- 全部补齐后调 `complete`；若得到 `422 sha256 mismatch`，说明某个已收分片内容损坏：可先调 `POST /chunks/audit` 提交最多 32 条 `{start,end,sha256}` 清单，服务端逐条返回 `ok` / `digest_mismatch` / `missing` / `length_mismatch` 及实际摘要，再只对有问题的区间用 `PUT /chunks/repair` 原位替换为等长正确内容（缺失区间与已收字节数不变），重新 `complete`，无需重新创建会话。

---

## 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest httpx
DATA_DIR=./data uvicorn app.main:app --reload --port 8000

# 测试（自动使用临时数据目录）
python3 -m pytest -q
```

测试覆盖：乱序完整上传与下载、越界/重叠/长度不符拒绝、缺失区间计算、并发重叠竞态（仅一个成功）、SHA-256 不匹配、空文件、放弃、过期清理与未知会话；受控票据覆盖参数边界、只存哈希、完整/Range 下载计数、耗尽 `429`、过期 `410`、撤销 `403`、伪造票据 `404`、30 并发不突破上限、票据相互独立、删除会话级联失效；幂等键覆盖同键重放返回同一会话、同键不同请求 `409`、键格式边界（1/128 字符、空、空格、非 ASCII）、只存哈希、10 并发同键重试只建一个会话、8 并发同键不同请求恰一个成功、删除会话级联删除键记录，以及 v1/v2 老库自动迁移到最新结构。`X-Chunk-SHA256` 覆盖匹配摘要接受（小写/大写/`sha256:` 前缀）、摘要不符 `422`（不落盘、不登记、可重传后正常完成整文件校验）、头格式错误 `400`（读体前拒绝）、长度错误优先于摘要错误、无头行为不变，以及带头/不带头分片混合上传。`copy-chunk` 覆盖区间复制后与 PUT 分片混传并通过整文件 SHA-256、整片复制、复制到不同目标偏移、来源与目标不同大小、来源可继续下载；拒绝未完成/不存在/成品缺失的来源、来源区间越界、目标越界、来源目标相同、与已收区间重叠（相邻允许）、单片超限（413）、目标已完成、目标未知；失败不登记不增字节不留临时文件；数据库登记失败（注入故障）回滚并清理落地文件后可重试、复制期间成品被删返回 409；反向并发复制不死锁、同区间并发只有一个成功、并发删除来源/放弃目标两种结局均不产生损坏登记。`chunks/repair` 覆盖损坏分片修复后重新通过整文件校验（带/不带摘要头、修复后再修复回原内容）、成功响应的缺失区间与字节数不变、区间必须与已登记分片完全一致（未登记/部分重叠/跨边界 404、非法区间 400）、Content-Length 不符 400、流式超长 413、单片超限 413、摘要不符 422（旧分片/登记/字节数/缺失区间保留、可重试后完成）、头格式错误 400、任何失败不滑动 TTL、未知会话 404、已完成 409（成品不变）、过期 410、越界 400、登记仍在但磁盘分片丢失时修复补齐；注入故障使新分片落位后的事务 UPDATE 失败时旧分片原子恢复且可重试成功；complete 在修复流式接收期间先完成（用旧内容发布、修复 409）、abort 在修复流式接收期间生效（修复 404/410、目录与临时文件清空）、双修复并发均 200 且最终内容恰为其一，以及后台清理回收崩溃残留的 `.repairbak` 备份。`chunks/audit` 覆盖乱序清单按起点升序返回且全部 `ok`、清单为已登记分片子集、摘要不符（返回实际摘要后据此 repair 再诊断即 `ok`）、磁盘文件缺失（`missing` 且可 repair 补齐）、文件长度不符（`length_mismatch` 带实际长度与实际摘要）、摘要字段大写与 `sha256:` 前缀规整；非法清单整批拒绝——未登记/部分重叠/跨边界区间 `400`（登记与字节数不变）、重复区间 `400`、空清单/超过 32 条/`start>=end`/负偏移/摘要格式错误/缺字段 `422`，且 32 条恰好通过形态校验；诊断不滑动 TTL（`expires_at`/`last_access_at` 不变）、不改登记与已收字节、不残留临时文件；未知会话 `404`、已完成 `409`、过期 `410`、放弃后 `404`；并发场景下诊断全程持会话锁——repair 与 complete 在诊断流式核对期间必须排队，诊断结果不会出现新旧分片混杂的半替换状态。
