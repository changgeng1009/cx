# 03 · 统一接口与 Adapter 规范

> 阶段：第三步（设计统一接口和 Adapter 规范）
> 本文是编码阶段的**契约文件**。任何 Adapter 只要满足本文契约，即可被统一层调用，不需要改统一层代码。

---

## 1. 分层架构

```
User / DeepSeek Agent / MCP Client / 其他 Agent
                 │
      ┌──────────┴──────────┐
      │  Unified API / MCP  │   ← 命令层（14 条统一命令）
      └──────────┬──────────┘
                 │
      ┌──────────┴──────────┐
      │     Task Router     │   ← 选路 + fallback + 限流 + 状态机
      └──────────┬──────────┘
                 │
      ┌──────────┴──────────┐
      │ Capability Registry │   ← 能力声明（42 项），Router 的决策依据
      └──────────┬──────────┘
                 │
      ┌──────────┴──────────┐
      │   Adapter Layer     │   ← 每个第三方项目一个 Adapter，进程隔离
      └──────────┬──────────┘
                 │
   ┌─────────────┼─────────────┬──────────────┐
   │             │             │              │
 A1 chaoxing  A2 Advanced   C1 MCP       A7 Downloader
 (subprocess) (subprocess)  (mcp)        (subprocess)
```

**四条不可违反的约束**：

| # | 约束 | 原因 |
|---|---|---|
| R1 | Adapter 只能通过**进程边界 / 网络边界 / 文件边界**与第三方交互 | GPL 隔离（§7） |
| R2 | Adapter 不得修改 `upstreams/` 下任何文件 | 保证可随时 `git pull` 升级 |
| R3 | 统一层不得包含任何第三方业务逻辑的复制 | 你的原则 3 |
| R4 | 单个 Adapter 失效不得影响其他 Adapter | 你的原则 3（可单独替换） |

---

## 2. 统一响应封装（Envelope）

**所有**命令返回同一种信封。这是"不隐藏失败"（你的原则 6）的机制保证——失败信息在 `error` 和 `fallback_trace` 里，不在日志里被吞掉。

```jsonc
{
  "ok": false,                       // 布尔，最终结果
  "command": "run_chapter",          // 调用的统一命令名
  "request_id": "req_20260917_0031_a1f3",
  "account": "acc_01",               // 多账号标识
  "adapter": "chaoxing-cli",         // ★ 实际服务该请求的 Adapter
  "adapter_version": "6ca38cb",      // 第三方仓库 commit sha（可追溯）
  "state": "failed",                 // 任务状态机当前态（§4）
  "started_at": "2026-09-17T00:31:02+08:00",
  "finished_at": "2026-09-17T00:34:18+08:00",
  "duration_ms": 196000,
  "data": null,                      // 成功时的业务数据
  "warnings": [
    "3 个任务点因未开放被跳过（策略=continue）"
  ],
  "error": {                         // 失败时非 null
    "code": "RISK_CONTROL_SUSPECTED",
    "category": "risk_control",
    "message": "响应中出现风控特征，已中止以避免账号风险",
    "retryable": false,
    "adapter_raw": "…………原始 stderr 片段…………"
  },
  "fallback_trace": [                // ★ 不隐藏失败的证据链：试过谁、为什么不行
    { "adapter": "xuexitong-mcp", "capability": "C09", "ok": false,
      "error_code": "ADAPTER_NOT_READY", "elapsed_ms": 812 },
    { "adapter": "chaoxing-cli",  "capability": "C09", "ok": true,
      "elapsed_ms": 195188 }
  ],
  "next_actions": [                  // 需要人工介入时给出可执行指引
    "在浏览器打开该课程，手动完成章节测验后重试"
  ]
}
```

### 2.1 错误分类（`error.category`）

分类决定 Router 的行为，**不允许 Adapter 直接决定"要不要 fallback"**——那是 Router 的职责。

| category | 含义 | Router 行为 | 是否置 `blocked` |
|---|---|---|---|
| `transient` | 网络抖动、超时、5xx | 同 Adapter 退避重试 → 再 fallback | 否 |
| `auth` | 会话失效、密码错误 | 触发重登 → 重试一次 → 转 `needs_manual_action` | 否 |
| `permission` | 无权限、课程未选 | 直接 fallback，不重试 | 否 |
| `not_supported` | 该 Adapter 不具备此能力 | 立即 fallback | 否 |
| `platform_changed` | 平台改版导致解析失败 | 标记 Adapter 降级 → fallback | 否 |
| `risk_control` | 风控/限流/验证码墙 | **立即全局熔断**，不 fallback | **是** |
| `input` | 参数错误 | 不重试，直接返回 | 否 |
| `internal` | 统一层自身 bug | 不重试，记录 | 否 |

> `risk_control` 是唯一会触发**全局熔断**的类别。理由：风控是针对账号的，换 Adapter 没用，继续调用只会加深风控。

---

## 3. 对外命令规范（14 条）

命令名沿用你的要求，不改名。`[]` 内为可选参数。

### 3.1 读命令（幂等，可安全重试）

| 命令 | 参数 | 返回 `data` 结构 | 能力映射 |
|---|---|---|---|
| `list_courses` | — | `[{course_id, clazz_id, cpi, name, teacher, fid}]` | `C06` |
| `get_course` | `course_id` | `{...课程元数据, chapter_count, task_point_count}` | `C07` |
| `get_progress` | `[course_id]` | `{overall: {done, total, ratio}, courses: [{course_id, name, done, total, ratio}]}` | `C10`,`C11` |
| `scan_tasks` | `course_id` `[chapter_id]` `[types]` | `{task_points: [{id, chapter_id, chapter_name, type, title, status}], summary: {by_type: {...}, by_status: {...}}}` | `C08`,`C09` |
| `get_homework` | `[course_id]` | `{homework: [{course_id, index, title, submitted, progress, due_at, score}], deadlines: [...]}` | `C29` |

`scan_tasks` 的 `types` 取值：`video` \| `audio` \| `document` \| `ppt` \| `reading` \| `live` \| `discussion` \| `quiz`。
`status` 取值：`done` \| `todo` \| `locked`（未开放）\| `unknown`。

> ⚠️ **`scan_tasks` 是一等公民**。因为 `run_video_tasks` / `run_reading_tasks` 的过滤语义必须由统一层承担（第三方项目没有单类型入口），而过滤依赖本命令的输出。若 `scan_tasks` 拿不到任务点类型（见 `02` §4 的 C09 缺口），这两条命令必须**明确降级并告知**，而不是悄悄跑整章。

### 3.2 写命令（非幂等，需 `--confirm`）

| 命令 | 参数 | 说明 | 能力映射 |
|---|---|---|---|
| `run_course` | `course_id` `[--dry-run]` `[--types video,audio]` | 跑完一门课全部未完成任务点 | `C12`–`C21` |
| `run_chapter` | `course_id` `chapter_id` `[--dry-run]` | 只跑指定章节 | `C12`–`C21` |
| `run_video_tasks` | `course_id` `[chapter_id]` | 只跑视频类任务点 | `C12` |
| `run_reading_tasks` | `course_id` `[chapter_id]` | 只跑阅读/文档类任务点 | `C14`,`C15` |

`--dry-run` 必须真的不产生写操作，只输出"将要做什么"。这是对账号安全的最低保障，也是唯一能在不冒封号风险下验证 Router 逻辑的手段。

### 3.3 控制命令

| 命令 | 参数 | 语义 | 能力映射 |
|---|---|---|---|
| `status` | `[--request-id X]` `[--all]` | 查询任务状态；无参数时返回汇总 | `C37` |
| `pause` | `[--request-id X]` | 在**下一个任务点边界**优雅暂停 | `C40`（自建） |
| `resume` | `[--request-id X]` | 从断点继续 | `C40`（自建） |
| `retry` | `[--request-id X]` `[--task-point-id Y]` | 重放失败任务（**不是**重启整个课程） | `C41`（自建） |
| `stop` | `[--request-id X]` | 终止并**保留**断点（区别于 `pause` 的是：不计划恢复） | `C40`（自建） |

### 3.4 辅助命令（非你要求，但 Router 与排障必需）

| 命令 | 说明 |
|---|---|
| `adapters` | 列出已注册 Adapter、其能力声明、健康状态 |
| `probe` | 对所有 Adapter 做轻量探活，输出可用性矩阵 |
| `accounts` | 列出账号及其会话状态 |

### 3.5 v2 新增命令：Agent 答题链路（§11）与签到（§12）

| 命令 | 参数 | 语义 | 能力映射 |
|---|---|---|---|
| `answer_pending` | `[--timeout S]` `[--limit N]` | 取出待答题目工单给操控 Agent（长轮询） | `C43`,`C47` |
| `answer_submit` | `--ticket-id X` `--answers JSON` | 回填 Agent 产出的答案 | `C44`,`C45` |
| `answer_stats` | — | 待答/已答/超时/丢弃计数 | `C47` |
| `sign_in` | `--course-id X` `[--type ...]` | 执行签到 | `C48`,`C49` |
| `sign_status` | `[--course-id X]` | 查询签到状态 | `C48` |
| `sign_watch` | `[--interval S]` | 持续监测新签到并推送 | `C50` |

---

## 4. 统一任务状态机

状态集合 = 你要求的 6 个 + 2 个必需的补充（说明理由）。

```
                    ┌──────────────────────────────────┐
                    │                                  │
   ┌─────────┐  enqueue  ┌─────────┐   done   ┌───────────┐
   │ pending ├──────────►│ running ├─────────►│ completed │
   └────┬────┘           └──┬───┬──┘          └───────────┘
        │                   │   │
        │            fail   │   │  risk_control / 风控
        │                   ▼   ▼
        │            ┌────────┐ ┌─────────┐
        │            │ failed │ │ blocked │
        │            └───┬────┘ └────┬────┘
        │                │           │ 人工解除后
        │     retry      │           │
        └────────────────┘           │
                                     │
        ┌─────────┐   resume   ┌─────┴─────┐
        │ paused  │◄──────────►│  running  │
        └─────────┘            └─────┬─────┘
                                     │ 需要人工（验证码/人脸/未开放且策略=ask）
                                     ▼
                          ┌──────────────────────┐
                          │ needs_manual_action  │
                          └──────────┬───────────┘
                                     │ 人工完成后 resume
                                     └──► running
```

| 状态 | 含义 | 进入条件 | 退出方式 |
|---|---|---|---|
| `pending` | 已受理未执行 | 命令入队 | `running` |
| `running` | 执行中 | 调度开始 | 其余任一状态 |
| `completed` | 全部目标任务点成功 | 无剩余未完成目标 | 终态 |
| `failed` | 因**可归因于系统**的原因失败 | `transient`/`platform_changed`/`internal` 耗尽重试 | `retry` → `pending` |
| `blocked` | 被**风控/限流**拦截 | `risk_control` 或限流熔断 | 人工冷却后 `resume` |
| `needs_manual_action` | 必须人工介入才能继续 | 验证码、人脸认证、`notopen_action=ask`、会话失效无法自动恢复 | 人工处理后 `resume` |
| `paused` | 用户主动暂停（计划恢复） | `pause` | `resume` |
| `cancelled` | 用户主动终止（不计划恢复） | `stop` | 终态 |

**为什么必须补 `paused` 和 `cancelled`**：你的 6 状态里没有"主动暂停"的位置。若把 `pause` 映射到 `pending`，`status` 查询会误报成"排队中"；映射到 `failed` 则会污染失败统计并触发无意义的 `retry` 逻辑。这两个状态是 `pause`/`stop`/`resume` 三条命令能存在的前提。

**关键设计点**：
- `blocked` ≠ `failed`。二者必须分开，因为 `failed` 会自动重试，而风控下自动重试会**加重风控**。
- `paused` 与断点绑定：暂停时把 `{已完成任务点 ID 集合}` 落盘，`resume` 时据此构造"还剩什么"。
- 状态流转必须**单向可审计**：每次流转写一条 JSONL 日志（§6）。

### 4.1 断点持久化

```jsonc
// accounts/acc_01/state.json
{
  "request_id": "req_20260917_0031_a1f3",
  "state": "paused",
  "course_id": "2401xxxx",
  "target_types": ["video", "reading"],
  "completed_task_points": ["tp_001", "tp_002", "tp_007"],
  "failed_task_points": [
    { "id": "tp_005", "type": "video", "attempt": 3, "last_error_code": "ADAPTER_TIMEOUT" }
  ],
  "skipped_task_points": [
    { "id": "tp_009", "type": "quiz", "reason": "locked" }
  ],
  "cursor": { "chapter_id": "ch_03", "task_point_id": "tp_008" },
  "updated_at": "2026-09-17T00:34:18+08:00"
}
```

---

## 5. Adapter 契约

### 5.1 Adapter Manifest（声明式能力声明）

每个 Adapter 提供一个 `manifest.json`。**Registry 只读这个文件，不读代码**——这样失效替换 Adapter 时，Registry 无需改动。

> **为什么用 JSON 而不是 YAML**（相对初版设计的修正）：统一层要保持**零第三方依赖**（M0 全部用 stdlib，`pytest` 都不装）。YAML 需要 PyYAML，而 JSON 是 stdlib 内置。Manifest 是机器读的声明文件，没有写注释的刚需，JSON 足够。

```jsonc
{
  "id": "chaoxing-cli",
  "name": "Samueli924/chaoxing CLI Adapter",
  "kind": "subprocess",            // subprocess | mcp | http
  "enabled": true,
  "upstream": {
    "repo": "https://github.com/Samueli924/chaoxing",
    "path": "upstreams/chaoxing",
    "pinned_commit": "6ca38cb",    // 必须锁 commit，避免上游改版静默破坏
    "license": "GPL-3.0",
    "isolation": "process"         // GPL 项目强制 process，禁止 in-process
  },
  "runtime": {
    "language": "python",
    "version": ">=3.13",
    "install": "pip install -r requirements.txt",
    "entry": "python main.py"
  },
  "priority": 10,                  // 越小越优先；同能力多实现时按此排序
  "capabilities": {
    "C01": { "level": "full", "params": ["phone", "password"] },
    "C02": { "level": "partial", "note": "支持 cookie 文件登录" },
    "C09": { "level": "partial", "note": "仅能从日志推断任务点，无结构化出口" },
    "C12": { "level": "full", "params": ["course_id", "speed"] },
    "C13": { "level": "full" },
    "C14": { "level": "full" },
    "C15": { "level": "partial" },
    "C16": { "level": "full", "module": "api/live.py" },
    "C18": { "level": "full", "params": ["notopen_action"] },
    "C19": { "level": "full", "params": ["notopen_action"], "enum": ["retry", "ask", "continue"] },
    "C20": { "level": "full", "params": ["speed"], "max": 2 },
    "C22": { "level": "full", "params": ["provider", "tokens", "submit", "cover_rate"] },
    "C24": { "level": "none" },
    "C25": { "level": "full", "note": "内置 1.5MB 字体映射表" },
    "C26": { "level": "full" },
    "C34": { "level": "full" },
    "C39": { "level": "full", "module": "api/notification.py" },
    "C44": { "level": "partial", "note": "通过 C46 代理注入答案，不改其源码" },
    "C46": { "level": "full", "note": "消费本地 OpenAI 兼容代理" }
  },
  "health": {
    "probe": {
      "capability": "C06",
      "method": "invoke",
      "args": { "dry_run": true },
      "timeout_ms": 30000
    },
    "degrade_on": ["platform_changed"]
  },
  "limits": {
    "max_concurrency": 1,          // 单账号并发，防止风控
    "min_interval_ms": 1500        // 两次调用最小间隔
  }
}
```

**`level` 取值语义**（决定 Router 是否选它）：

| level | 含义 | Router 行为 |
|---|---|---|
| `full` | 完整实现 | 优先选 |
| `partial` | 部分/受限实现 | 次选，且必须把限制写进 `warnings` |
| `none` | 明确不支持 | 跳过 |

`C24`（图片题）在 A1 上显式写 `"level": "none"` —— **显式声明不支持比省略更有价值**，因为它让 Router 能明确地把含图题目路由到 A2，而不是在 A1 上失败后才 fallback。

### 5.2 Adapter 运行期接口

```python
# orchestrator/adapters/base.py
from typing import Protocol, Any
from dataclasses import dataclass

@dataclass
class ProbeResult:
    healthy: bool
    latency_ms: int
    detail: str = ""

@dataclass
class AdapterResult:
    ok: bool
    data: Any | None
    error: "AdapterError | None"
    raw_output: str = ""          # 原始 stdout/stderr，供排障，不丢

@dataclass
class AdapterError:
    code: str                     # 机器可读，如 ADAPTER_TIMEOUT
    category: str                 # transient|auth|permission|not_supported|
                                 # platform_changed|risk_control|input|internal
    message: str
    retryable: bool = False

class Adapter(Protocol):
    manifest: "Manifest"

    def setup(self, account: "AccountContext") -> None:
        """幂等准备：生成配置文件、准备 workdir。不得写入 upstreams/。"""

    def probe(self) -> ProbeResult:
        """轻量探活。Router 在选路前可能调用。"""

    def supports(self, capability_id: str) -> bool:
        """是否支持某能力。默认读 manifest，Adapter 可动态覆盖。"""

    def invoke(self, capability_id: str, params: dict,
               ctx: "TaskContext") -> AdapterResult:
        """执行。必须：
           1. 把 params 翻译成该项目的原生调用方式
           2. 把原生输出翻译成 AdapterResult
           3. 把异常映射到 category（不得自行决定 fallback）
           4. 在 ctx 上报告进度回调（用于状态机与日志）
        """

    def cancel(self, ctx: "TaskContext") -> None:
        """协作式取消：置标志 → 等在跑的任务点结束 → 终止子进程。"""

    def teardown(self) -> None:
        """清理临时资源。"""
```

### 5.3 三类 Adapter 的翻译层职责

| kind | `invoke` 做什么 | 典型实现 |
|---|---|---|
| `subprocess` | 生成 `config.ini` → 拼 CLI 参数 → `subprocess.Popen` → **流式解析 stdout** 提取进度 → 解析退出码 | A1 / A2 / A7 |
| `mcp` | MCP stdio 客户端 → `tools/call` → 结果映射 | C1 |
| `http` | `requests` 调 endpoint → 结果映射 | D1、B3 的 `/api/search` |

**stdout 流式解析是 A1 类 Adapter 的难点**：`main.py` 会持续打印中文进度（如"正在学习: xxx 章节"）。解析器需要：
- 用正则匹配任务点开始/结束标志，**边跑边报告进度**（否则 `status` 命令只能看到"运行中"）
- 正则必须**容错**：上游改文案时降级为"能报进度但不报细节"，而不是崩掉
- **绝不用日志解析来做控制流判断**（控制流应由退出码 + 状态文件决定）

---

## 6. 统一日志规范

**全部**操作写 JSONL（一行一事件），落到 `runs/{request_id}.jsonl`。字段覆盖你要求的全部维度：

```jsonc
{
  "ts": "2026-09-17T00:31:02.113+08:00",
  "level": "INFO",
  "event": "task_point.completed",     // 点分命名：{对象}.{动作}

  "request_id": "req_20260917_0031_a1f3",
  "task_id": "tp_001",
  "parent_request_id": null,

  "adapter": "chaoxing-cli",           // ★ 用了哪个 Adapter
  "adapter_version": "6ca38cb",
  "account": "acc_01",

  "course":  { "id": "2401xxxx", "name": "数字电路" },      // ★ 课程
  "chapter": { "id": "ch_03", "name": "第三章 组合逻辑", "index": 3 }, // ★ 章节
  "task_type": "video",                // ★ 任务类型
  "task_point_id": "tp_001",

  "state_from": "running",             // ★ 状态流转
  "state_to": "completed",
  "attempt": 1,                        // ★ 第几次尝试

  "started_at": "2026-09-17T00:31:02.113+08:00",  // ★ 开始时间
  "finished_at": "2026-09-17T00:34:18.001+08:00", // ★ 结束时间
  "duration_ms": 195888,

  "ok": true,                          // ★ 成功/失败
  "error": null,                       // ★ 错误信息
  "extra": { "speed": 2.0, "reported_progress": "100%" }
}
```

### 6.1 事件字典（固定集合，便于统计）

| event | 触发时机 |
|---|---|
| `request.received` / `request.finished` | 命令入口/出口 |
| `adapter.selected` | Router 选定 Adapter |
| `adapter.fallback` | 切换到备选（含 `reason`） |
| `adapter.probe` | 探活结果 |
| `state.transition` | 任何状态流转 |
| `course.started` / `course.finished` | 课程级 |
| `chapter.started` / `chapter.finished` | 章节级 |
| `task_point.started` / `task_point.completed` / `task_point.skipped` / `task_point.failed` | 任务点级 |
| `risk.control_detected` | 风控特征命中 |
| `pause.requested` / `pause.effective` / `resume.effective` | 控制流 |
| `manual_action.required` | 转 `needs_manual_action` |

### 6.2 日志分层

- **审计层**（`runs/*.jsonl`）：结构化，机器读，长期保留
- **人读层**（stdout）：彩色摘要，只显示关键事件
- **原始层**（`runs/{request_id}.raw.log`）：第三方项目的原始 stdout/stderr **原样保留**

保留原始层是排障的关键——当 Adapter 解析失败时，唯一能定位原因的就是第三方项目的原始输出。

---

## 7. 许可证隔离策略（重要）

| 上游许可证 | 项目 | 允许的接触方式 | 禁止 |
|---|---|---|---|
| **GPL-3.0** | A1, A2, A5, B1 | `subprocess` / `git clone` 到 `upstreams/` 后当作**独立程序**运行 | ❌ `import` 其任何模块<br>❌ 复制其代码片段进统一层<br>❌ 静态链接 |
| **MIT** | A3, C1, D1 | ✅ 可 `import`、可复制、可修改 | ⚠️ 需保留版权声明 |
| **无许可证 / NOASSERTION** | A4, B2, B3 | ⚠️ **默认"保留所有权利"**。纳入前必须读 LICENSE 原文确认 | ❌ 在未确认前纳入 |

**落地做法**：
1. `upstreams/` 下全部用 `git clone` 引入，**锁 commit**（`git checkout <sha>`），并在 `upstreams/LOCK.json` 记录 `{repo, sha, license, cloned_at}`。
2. 统一层的 Python 代码**永不** `sys.path` 加入 `upstreams/*`。
3. 配置注入走"生成配置文件到 `accounts/{id}/` → 用 `-c` 参数指向它"，**不往 `upstreams/` 写任何文件**。
4. 目录同时加了 `.gitignore`，避免误提交上游代码进本仓库。

---

## 8. 凭据与会话管理（多账号）

```
accounts/
├─ acc_01/
│  ├─ meta.json          # {label, phone_masked, added_at, enabled}
│  ├─ credentials.json   # ★ gitignore，仅本机，权限 0600
│  ├─ cookies.txt        # 会话（如 Adapter 支持）
│  ├─ config.ini         # 为 A1/A2 生成的配置
│  └─ state.json         # 断点
```

原则：
- **凭据绝不进日志、绝不进 error 消息、绝不出现在 `data` 返回里**。日志写 phone 时只写掩码 `138****8888`。
- **每个账号独立 workdir + 独立子进程**：这样账号 A 的风控状态、cookie、断点天然隔离。
- 会话生命周期由统一层管：调用前 `probe` 判定有效性（缓存 5 分钟，避免每次都探测），失效则触发重登一次，仍失败 → `needs_manual_action`（`auth` 类）。

---

## 9. 限流与风控（横切关注点）

不放到任何 Adapter 里，因为它是账号级的：

```python
# 伪代码
class AccountThrottle:
    min_interval_ms = 1500      # 同一账号两次上游调用最小间隔
    max_concurrency = 1         # 同一账号同时最多 1 个上游进程
    backoff = [5, 15, 45, 120]  # 秒，指数退避
    cooldown_after_block = 1800 # 命中风控后冷却 30 分钟

class RiskDetector:
    """在 Adapter 返回的 raw_output / 状态码里找风控特征。"""
    SIGNATURES = [
        "操作过于频繁", "请稍后再试", "访问受限", "403", "429",
        "验证码", "安全验证", "异常操作",
    ]
    def detect(self, raw: str, status: int) -> bool: ...
```

识别到风控后：
1. 置状态 `blocked`
2. 全局熔断该账号（`cooldown_after_block`）
3. **不 fallback**（换 Adapter 是同一个账号，只会加重风控）
4. 返回明确错误，`next_actions` 提示人工冷却

---

## 10. 目录结构

```
学习通刷课脚本/
├─ docs/                          # 本文档集
│  ├─ 01-生态分析报告.md
│  ├─ 02-项目能力矩阵.md
│  ├─ 03-统一接口与Adapter规范.md
│  ├─ 04-MVP范围与实施路线.md
│  ├─ README.md                   # 第七步产出
│  └─ TROUBLESHOOTING.md          # 第七步产出
├─ orchestrator/                  # ★ 统一层（本仓库唯一自研代码）
│  ├─ cli.py                      # 统一命令入口（29 条）
│  ├─ mcp_server.py               # 暴露为 MCP
│  ├─ registry.py                 # Capability Registry
│  ├─ router.py                   # Task Router + fallback
│  ├─ state.py                    # 状态机 + 断点
│  ├─ control.py                  # 跨进程暂停/终止通道
│  ├─ throttle.py                 # §9 限流
│  ├─ risk.py                     # §9 风控检测
│  ├─ structured_log.py           # §6 日志
│  ├─ redact.py                   # 凭据脱敏（强制环节）
│  ├─ session.py                  # §8 凭据/多账号
│  ├─ answer_broker.py            # §11 待答工单队列（Agent 答题）
│  ├─ openai_shim.py             # §11 本地 OpenAI 兼容代理
│  ├─ fixtures.py                 # 演示/测试样例数据
│  ├─ services.py                 # 命令层
│  ├─ bootstrap.py                # 装配
│  ├─ models.py                   # Envelope / 状态 / 错误
│  ├─ errors.py
│  └─ adapters/
│     ├─ base.py
│     ├─ mock.py                  # 故障注入用
│     ├─ chaoxing_cli.py
│     ├─ advanced_bot_cli.py
│     ├─ xuexitong_mcp.py
│     ├─ mooc_downloader.py
│     └─ manifests/*.json
├─ upstreams/                     # gitignore，git clone，锁 commit，只读
├─ accounts/                      # gitignore，多账号工作区
├─ runs/                          # gitignore，日志
│  ├─ <req_id>.jsonl              # 审计层
│  ├─ <req_id>.raw.log            # 原始层（脱敏后）
│  ├─ control/                    # 跨进程控制信号
│  └─ answer/                     # 待答工单：tickets.jsonl / pending/ / answers/
├─ scripts/
│  └─ demo_agent_answering.py     # Agent 答题链路跨进程演示
├─ tests/                         # 114 项（见 docs/05 验收矩阵）
│  ├─ run_tests.py                # 零依赖入口
│  ├─ test_router.py              # V4/V5/V6 fallback 与熔断
│  ├─ test_state_and_control.py   # V3/V7 状态机与断点
│  ├─ test_commands.py            # V1/V2 命令契约
│  ├─ test_logging.py             # V8/V9 日志与脱敏
│  ├─ test_registry_isolation.py  # V10 GPL 隔离静态检查
│  └─ test_agent_pipeline.py      # C36/C43–C47 答题链路与 MCP
└─ pyproject.toml
```

**技术选型说明**：
- **Python 3.13**：与 A1/A2 对齐（它们要求 3.13+），避免多套运行时。
- **CLI 用 `argparse` 而非 click**：零额外依赖，且这层的价值在编排不在 CLI 体验。
- **MCP 用官方 `mcp` SDK**：与 C1 对齐。
- **不用 asyncio 编排上游进程**：上游是阻塞的同步 Python，用 `subprocess` + 线程池更简单可靠。异步只用在 MCP 服务层。
- **状态持久化用 JSON 文件而非 SQLite**：状态量极小，JSON 便于人工查看和手工修复（排障价值 > 性能）。
- **零第三方依赖**：M0 全部用 stdlib（含 HTTP 代理用 `http.server`），测试用 `unittest`。不装 `pytest`、不装 PyYAML。

---

## 11. Agent 答题链路（v2 核心设计）

### 11.1 需求变更与设计取舍

你要求"**AI 答题由操控 Agent 来答题**"。这条要求看起来是个收缩（不自己接 LLM），实际上**简化了整个架构**，因为上游 A1 的 AI 答题恰好走 **OpenAI 兼容协议**。

对比两条路线：

| | 路线 A：配上游 endpoint（初版设计） | 路线 B：本地 OpenAI 代理（本次采用） |
|---|---|---|
| 谁答题 | 第三方 LLM API | **操控本平台的 Agent / 人** |
| 需要 API Key | 需要（你提供） | **不需要** |
| 上游改动 | 无 | 无 |
| 题目可控 | 否（题目在黑盒里） | **是**（可审计、可人工复核、可缓存） |
| 与 Agent 的关系 | 无关 | **成为平台的一等能力** |

路线 B 的做法：统一层在本地起一个 HTTP 服务，实现 `POST /v1/chat/completions`。把 A1 的 `config.ini` 指向它：

```ini
[tiku]
provider = AI
endpoint = http://127.0.0.1:8765/v1
key      = local-agent
model    = agent-in-the-loop
```

于是 A1 每次要答题时，就会 POST 题目过来；统一层把题目转成"待答工单"交给操控 Agent；Agent 回填答案后，统一层把答案包装成 OpenAI 响应体返回。**上游零改动**。

```
A1 (python main.py)
      │  POST /v1/chat/completions  {"messages":[{"role":"user","content":"题目..."}]}
      ▼
 openai_shim.py  ──►  answer_broker.py  (待答工单入队)
                              │
                              │ Agent 拉取（CLI / MCP）
                              ▼
                      操控 Agent / 人
                              │  answer_submit
                              ▼
 openai_shim.py  ◄── 答案格式化（A#B#C / 正确 / 填空|分隔）
      │  200 {"choices":[{"message":{"content":"A"}}]}
      ▼
A1 继续走它原有的提交逻辑
```

### 11.2 为什么这个方案是对的

1. **完全满足"不复制第三方逻辑"**：`openai_shim.py` 是纯本地协议适配，与学习通无关，不含任何上游代码。
2. **它把"AI 答题"变成了平台能力而非配置**：题目经过平台，因此可以审计、可以人工复核、可以缓存复用（同一课重复题目不必再问 Agent）。
3. **离线可测**：整个链路是本地 HTTP + 本地队列，**不需要账号、不需要网络**。这是 v2 里唯一能在 M0 阶段就完整交付的新功能。
4. **可降级**：Agent 长时间不响应时，代理返回"无法作答"，A1 会按它自己的 `submit=false` 策略只保存不提交——不会阻塞整个课程。

### 11.3 工单数据模型

```jsonc
// runs/answer_tickets.jsonl（追加写，进程重启可恢复）
{
  "ticket_id": "tk_000001",
  "created_at": "2026-09-17T01:02:03.114+08:00",
  "state": "pending",              // pending | answered | timeout | dropped
  "request_id": "req_...",         // 关联的刷课请求
  "course_id": "2401xxxx",
  "chapter_id": "ch_03",
  "raw_prompt": "…………上游发来的完整 prompt…………",
  "questions": [                   // 尽力结构化；解析失败则留空并保留 raw_prompt
    { "index": 1, "type": "single_choice", "stem": "...",
      "options": [{"key": "A", "text": "..."}], "image_ref": null }
  ],
  "answers": null,                 // Agent 回填：["A", "B#C", "正确"]
  "answered_at": null,
  "answered_by": null              // "agent" | "human"
}
```

**关键设计**：`raw_prompt` 是**必须保留的原文**。结构化解析（`questions`）是尽力而为——上游 prompt 格式会变，解析失败时仍要把原文给 Agent，让 Agent 自己理解。**这比"解析失败就报错"健壮得多。**

### 11.4 超时与不阻塞保证

- Agent 拉取的默认超时 `answer_timeout_s`（默认 120s）。超时后工单置 `timeout`，代理立刻返回"无法作答"。
- **绝不让上游进程无限等待**：上游是同步阻塞的 Python，卡住会导致整门课停摆。所以代理侧必须有硬超时。
- 超时工单保留在队列里，`answer_submit` 仍可迟到回填（用于事后审计和缓存）。

---

## 12. 签到设计要点（v2 新增）

### 12.1 与刷课的本质差异

| | 刷课 | 签到 |
|---|---|---|
| 触发方式 | 主动发起，可排队 | **被动降临，有窗口期**（通常几分钟） |
| 时效要求 | 低（可以慢慢跑） | **高**（错过就没了） |
| 依赖 | HTTP 协议 | **位置 / 摄像头 / 实时轮询** |
| 失败代价 | 任务点没刷完 | 直接缺勤 |

因此签到**不能复用刷课的调度模型**。它需要：
1. **独立的监测循环**（`sign_watch`），短间隔轮询
2. **实时性优先**：签到请求要**插队**，不能被刷课任务挡住
3. **通知链路**：监测到新签到立刻推送（复用 `C39`）
4. **状态隔离**：签到失败不触发刷课的熔断，反之亦然

### 12.2 候选实现与接入方式

| 项目 | 许可 | 形态 | 接入 |
|---|---|---|---|
| `aquamarine5/ChaoxingSignFaker` | AGPL-3.0 | Android (Kotlin) | ❌ 无法在服务端调用 |
| `eric-gitta-moore/chaoxing-sign-app` | GPL-3.0 | TS Web/App，多账号批量 | `subprocess` + 其 HTTP 接口（需实测） |
| `EnderWolf006/XBT` | GPL-3.0 | TS + Go 后端 | `subprocess` |

**结论**：签到暂无符合"进程级可调用"的优质候选。**建议的顺序是**：
1. 先用 `C1` 的协议备忘**自建最小签到 Adapter**（普通签到通常只是几个 HTTP 请求，路最短）
2. 位置/二维码/拍照签到再评估 `chaoxing-sign-app`
3. `ChaoxingSignFaker`（AGPL）作为最后手段，且**必须严格进程隔离**（AGPL 传染性最强）

> ⚠️ 签到功能**必须等你登录后**才能做任何实测——包括确认当前活跃签到长什么样、请求体是什么形状。M0 阶段只落地接口与状态模型。

### 12.3 签到状态机复用

签到任务复用 §4 的 8 状态机，但语义微调：

- `needs_manual_action` 在签到场景高频出现（需要位置、需要拍照、需要人脸）
- `blocked` 的触发条件更敏感（签到请求频率高，更容易触发风控）
- 新增语义：`completed` 表示"在窗口期内成功签到"，**超时未签到应归为 `failed`**（窗口已过，重试无意义），而不是 `blocked`
