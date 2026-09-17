# 学习通自动化能力平台

> ## ⚠️ 免责声明
>
> 1. 本项目**仅供学习与技术研究**，用于了解自动化架构设计、进程隔离、Adapter 模式等工程实践。
> 2. 本项目与任何第三方平台、机构、组织**没有任何关联**；项目中引用的开源代码版权归其原作者所有，均已按其许可证要求以独立进程方式调用。
> 3. 本项目**不提供任何形式的服务或保证**；使用者因使用、滥用或误用本项目产生的一切后果，由使用者自行承担，**与作者无关**。
> 4. 请遵守所在学校/单位的相关规定以及当地法律法规。**下载或复制本项目即视为已阅读并同意本声明。**

把多个**独立第三方**学习通工具，变成一个可以被**用户 / DeepSeek Agent / MCP Client / 其他 Agent 统一调用**的自动化能力平台。

统一层不重写任何第三方逻辑，也不修改它们的源码——每个第三方项目作为一个独立 Adapter，通过进程 / 网络 / 文件边界接入。某个项目失效时，单独换掉它的 Adapter 即可。

---

## 当前状态

| 阶段 | 状态 | 说明 |
|---|---|---|
| 第一~四步 · 设计 | ✅ 完成 | `docs/01`–`docs/04` |
| **M0 · 离线骨架** | ✅ **完成** | 227 项测试；29 条命令可跑；**不需要账号、不需要网络** |
| **登录态获取** | ✅ **完成** | `cookies_login` 一站式（起独立浏览器 → 登录 → CDP 提取落盘） |
| **M1 · 读侧接入** | ✅ **完成** | 接 `Xuexitong-mcp`（MIT，subprocess + 锁 commit）：课程/章节/进度/作业/通知/考试/课表 **真实数据**；240 项测试全绿 |
| **M2 · 写侧接入** | ✅ **完成** | 接 `Samueli924/chaoxing`（GPL-3.0，仅 subprocess + 锁 commit）：真实跑课/单章/按类型；quiz 默认跳过（M5 接 Agent 答题）；251 项测试全绿 |
| M3 · 真跑 | ⏸ 未开始 | 完整闭环 |
| M4–M6 · 扩展 | ⏸ | 图片题 / 下载 / MCP 联调 / 签到 |

> ⚠️ **除 cookie 组与辅助命令外，其余命令当前都走 Mock 适配器，返回的是内置样本数据。** 判断方法：看输出里的 `adapter=` 字段，`adapter=mock` 即假数据。真实上游要等 M1–M3 接入。

**为什么 M0 先做完全离线的一层**：如果一上来就接真实项目，fallback、超时、风控熔断、暂停断点这些边界情况几乎不可能自然触发，只能靠"读代码觉得对"；而且账号有真实风控风险。M0 用可注入故障的 Mock Adapter 把这些路径全部变成可断言的测试。

---

## 快速开始

> 📖 **想知道"我现在具体该敲什么"，看 [`docs/08-使用手册.md`](docs/08-使用手册.md)** —— 按场景给出可照抄的命令，并标注每条命令当前是真数据还是 Mock。

**零第三方依赖**（stdlib only，不需要 `pip install` 任何东西）。

```bash
# 查看有哪些 Adapter（已注册的 vs 已声明未接入的）
python -m orchestrator.cli adapters

# 能力注册表与覆盖率
python -m orchestrator.cli capabilities

# 列出课程
python -m orchestrator.cli list_courses

# 扫描任务点（重点：按类型/状态汇总、标出含图题目）
python -m orchestrator.cli scan_tasks --course-id 240100001

# 先预览，再执行（写操作必须显式确认）
python -m orchestrator.cli run_chapter --course-id 240100001 --chapter-id ch_02 --dry-run
python -m orchestrator.cli run_chapter --course-id 240100001 --chapter-id ch_02 --confirm

# 只跑视频 / 只跑阅读（类型过滤由统一层完成，上游没有这个入口）
python -m orchestrator.cli run_video_tasks --course-id 240100001 --confirm

# 状态与断点
python -m orchestrator.cli status --all

# 暂停 / 恢复 / 重试 / 终止
python -m orchestrator.cli pause  --request-id <req_id>
python -m orchestrator.cli resume --request-id <req_id> --confirm
python -m orchestrator.cli retry  --request-id <req_id> --confirm
python -m orchestrator.cli stop   --request-id <req_id>

# 跑测试
python tests/run_tests.py
```

加 `--json` 得到机器可读的完整 Envelope；加 `-v` 把结构化日志打到 stderr。

---

## 统一响应信封

**所有**命令返回同一种 Envelope。失败信息在 `error` 和 `fallback_trace` 里，不会被吞掉：

```json
{
  "ok": false,
  "command": "run_chapter",
  "state": "blocked",
  "adapter": "chaoxing-cli",
  "adapter_version": "6ca38cb",
  "data": null,
  "warnings": [],
  "error": {
    "code": "RISK_CONTROL_SUSPECTED",
    "category": "risk_control",
    "message": "检测到风控特征：操作过于频繁",
    "retryable": false
  },
  "fallback_trace": [
    {"adapter": "xuexitong-mcp", "ok": false, "error_code": "ADAPTER_NOT_READY"},
    {"adapter": "chaoxing-cli", "ok": false, "error_code": "RISK_CONTROL_SUSPECTED"}
  ],
  "next_actions": ["账号已熔断 1800s，期间不要重试"]
}
```

`fallback_trace` 是"不隐藏失败"的**机制保证**——它记录试过谁、为什么不行，而不是靠调用方自觉去看日志。

---

## Agent 答题链路（不需要任何 LLM API Key）

需求是"**AI 答题由操控 Agent 来答题**"。实现方式利用了上游的一个事实：`Samueli924/chaoxing` 的 AI 答题走的是 **OpenAI 兼容协议**。

所以统一层在本地起一个 OpenAI 兼容代理，把上游的 `endpoint` 指向它即可——**上游零改动**：

```ini
; chaoxing-cli 的 config.ini
[tiku]
provider = AI
endpoint = http://127.0.0.1:8765/v1
key      = local-agent
model    = agent-in-the-loop
submit   = false
; submit=false：Agent 缺席时只保存不提交，不会阻塞课程
```

```
A1 ──POST /v1/chat/completions──► 本地代理 ──► 待答工单队列
                                                   │
                                    操控 Agent 拉题 ▼ 回填
                                             ┌──────────┐
                                             │ DeepSeek │
                                             │ Agent/人 │
                                             └──────────┘
A1 ◄──200 {"content":"C\nA\nA"}──── 本地代理
```

启动代理并取出配置片段：

```bash
python -m orchestrator.cli shim-serve --port 8765      # 阻塞运行
python -m orchestrator.cli shim_config --port 8765     # 打印上面那段 ini
```

Agent 侧（CLI 或 MCP）：

```bash
python -m orchestrator.cli answer_pending                       # 取出待答工单
python -m orchestrator.cli answer_submit \
    --ticket-id tk_xxx --answers "C
A"                                                            # 回填答案
```

一键跑通整条链路（跨进程、无账号、无网络）：

```bash
python scripts/demo_agent_answering.py
```

---

## 独立浏览器（与日常浏览器、其他 Agent 完全隔离）

平台本体**不需要浏览器**——整条链路走 HTTP + 子进程。浏览器只在两个窄场景才用得上：浏览器脚本类能力（`C38`）和签到的位置/二维码/拍照类型。

当确实需要时，必须使用**项目内的独立 Edge 实例**：

```bash
python -m orchestrator.browser --show      # 查看 profile / 端口 / 本机禁区清单
python -m orchestrator.browser --launch    # 启动独立实例
python -m orchestrator.browser --status    # 探测 CDP 是否在线
```

或直接双击 `启动独立浏览器.cmd`。

| 项 | 值 |
|---|---|
| profile | `<项目根>\.browser\edge-profile`（gitignored） |
| CDP 端口 | 9333（避开最常见的 9222） |
| 监听地址 | 仅 `127.0.0.1` |

**为什么需要这层**：Chromium 的 profile 单例锁是按 `user-data-dir` 分的，不同目录就是完全独立的进程与窗口；而**不带 `--user-data-dir` 启动一定会附着到已有实例上**。所以隔离不是靠"注意"，`orchestrator/browser.py` 会在启动前**拒绝**任何指向系统默认 profile 的路径，并由 `tests/test_browser.py`（22 项）守卫。

同理，本项目不提供"批量结束 msedge.exe"之类的功能——那会杀掉其他 Agent 正在使用的窗口。停止请直接关闭那个窗口。

细节与后续 Adapter 必须遵守的约定见 `docs/06-浏览器隔离约定.md`。

> ⚠️ 出于沙箱限制，Agent 无法让浏览器窗口常驻（沙箱会在命令结束时回收子进程），**请在你自己的终端里启动**。

---

## 登录态与 Cookie

**不要指望"解密浏览器数据库"那条路**——实测本机 Chrome 152 / Edge 153 都启用了
**App-Bound Encryption**，进程外无法解密 cookie（详见 `docs/07`）。本平台不写这段代码。

两条可用的路：

**A. 一站式登录（推荐）**

```bash
python -m orchestrator.cli cookies_login    # 起独立浏览器 → 等你登录 → 自动提取落盘
python -m orchestrator.cli cookies_verify   # 真实请求一次平台，确认会话有效
```

`cookies_login` 会在项目内的独立浏览器里打开学习通，检测到登录动作后自动提取。
检测用的是"cookie 条数相对基线增长"，这是**弱信号**，所以结束时永远会提示去跑
`cookies_verify` —— 那才是真正确认的手段。

CDP 的意义在于：**解密发生在浏览器内部**，我们只是它的调试客户端，全程不碰加密。

**B. 手动粘贴（不需要浏览器）**

```bash
python -m orchestrator.cli cookies_import --header "UID=xxx; _d=yyy; fid=zzz"
```

**其他**

```bash
python -m orchestrator.cli cookies_extract   # 浏览器已登录过，只重新提取一次
python -m orchestrator.cli cookies           # 体检（值已掩码）
python -m orchestrator.cli cookies_clear     # 清除
```

落盘为三份，覆盖上游可能要求的各种格式：

| 文件 | 格式 |
|---|---|
| `accounts/{id}/cookies.json` | 内部规范（唯一事实来源） |
| `accounts/{id}/cookies.txt` | Netscape / curl 格式 |
| `accounts/{id}/cookies.header` | 纯 `Cookie:` 头的值 |

`cookies` 命令输出的是**掩码**，值不会打进终端或日志。详见 `docs/07`。

---

## 架构

```
User / DeepSeek Agent / MCP Client
              │
   统一 API / MCP 服务        14+ 条命令
              │
        Task Router          选路 · fallback · 限流熔断
              │
    Capability Registry      51 项能力声明，manifest 驱动
              │
       Adapter Layer         进程隔离，禁止 import
              │
   ┌──────────┼──────────┬─────────────┐
 chaoxing  advanced  xuexitong    mooc-dl
 (GPL-3.0) (GPL-3.0)   -mcp(MIT)  (无许可)
```

**横切关注点**：账号级限流 · 风控熔断 · JSONL 日志 · 断点状态机

---

## 目录结构

```
docs/                   设计与交付文档
  01-生态分析报告.md
  02-项目能力矩阵.md
  03-统一接口与Adapter规范.md
  04-MVP范围与实施路线.md
  05-M0验收报告.md
  06-浏览器隔离约定.md
  07-凭据获取与Cookie落盘.md
  08-使用手册.md
cx.cmd                  便捷入口（等价于 python -m orchestrator.cli）
启动独立浏览器.cmd       双击启动独立 Edge（父进程是 explorer，窗口能常驻）
orchestrator/           统一层（本仓库唯一自研代码）
  models.py             状态机状态 / 领域实体 / Envelope
  errors.py             8 类错误分类
  capabilities.py       51 项能力定义
  registry.py           能力注册表（只读 manifest）
  router.py             选路 + fallback + 熔断
  state.py              状态机 + 断点
  throttle.py           账号级限流
  risk.py               风控检测
  structured_log.py     JSONL 日志 + 原始日志层
  redact.py             凭据脱敏
  session.py            多账号 / 凭据
  control.py            跨进程暂停/终止通道
  browser.py            独立浏览器实例与隔离守卫
  cdp.py                手写的最小 CDP 客户端（含 WebSocket）
  cookies.py            Cookie 规范存储与多格式导出
  session_verify.py     会话有效性探测（三态判定）
  answer_broker.py      待答工单队列
  openai_shim.py        本地 OpenAI 兼容代理
  services.py           命令层
  cli.py                命令行入口
  mcp_server.py         MCP 出口
  bootstrap.py          装配
  adapters/             Adapter 实现 + manifests/*.json
upstreams/              gitignore｜第三方项目，git clone + 锁 commit，只读
accounts/{id}/          gitignore｜每账号独立工作区
runs/                   gitignore｜*.jsonl 审计日志 + *.raw.log 原始日志
scripts/                演示脚本
tests/                  114 项验收测试
```

---

## 四条不可违反的红线

| # | 红线 | 原因 |
|---|---|---|
| R1 | Adapter 只能通过**进程/网络/文件边界**与第三方交互 | GPL-3.0 传染性隔离 |
| R2 | 不得修改 `upstreams/` 下任何文件 | 保证可随时按 commit 重新拉取 |
| R3 | 统一层不得复制第三方业务逻辑 | 保持自研部分可独立演进 |
| R4 | 单个 Adapter 失效不影响其他 Adapter | 可单独替换 |

R1 由 `tests/test_registry_isolation.py` 做**静态检查**守卫：一旦代码里出现 `import chaoxing` 或 `sys.path.insert`，测试立刻失败。这条不能靠"记得别写"。

### 许可证状态

| 项目 | 许可证 | 允许的接触方式 |
|---|---|---|
| `Samueli924/chaoxing` | GPL-3.0 | 仅 `subprocess` |
| `Advanced-ChaoXingBot` | GPL-3.0 | 仅 `subprocess` |
| `Xuexitong-mcp` | **MIT** | 可 import |
| `yatori-go-core` | **MIT** | 可 import |
| `PyJun/Mooc_Downloader` | 无许可证 | ⚠️ 默认保留所有权利，纳入前须读 LICENSE 原文 |
| `Mortal004/Xuexitong_shuake` | NOASSERTION | ⚠️ 同上 |

---

## 可观测性

每个请求产生三份日志：

| 文件 | 内容 |
|---|---|
| `runs/<req_id>.jsonl` | 结构化审计日志，含 Adapter / 账号 / 课程 / 章节 / 任务类型 / 起止时间 / 成败 / 错误 / fallback |
| `runs/<req_id>.raw.log` | 第三方项目的**原始输出**（脱敏后），解析失败时唯一能定位原因的东西 |
| stdout | 人读摘要 |

凭据脱敏是**强制环节**而不是调用方义务：手机号、密码、cookie、token 在落盘前统一掩码，由 `tests/test_logging.py` 的 V9 用例守卫。

---

## 常见问题

**Q: 为什么 `adapters` 显示真实项目都是"已声明未接入"？**
M0 阶段尚未 clone 任何上游。`manifest.enabled = false` 是有意的——比注册一个"永远失败"的占位实现更诚实。

**Q: 为什么写操作要 `--confirm`？**
避免误触发平台上的真实操作。习惯路径是 `--dry-run` 预览 → `--confirm` 执行。`resume` / `retry` 同样需要确认，因为它们也会产生真实写操作。

**Q: 遇到风控会怎样？**
状态置为 `blocked`，账号进入冷却（默认 30 分钟），且**不会 fallback 到其他 Adapter**——因为风控是针对账号的，换 Adapter 只会加重它。冷却期内 `status` 会显示剩余时间。

**Q: `scan_tasks` 拿不到任务点类型怎么办？**
这是已知的真实缺口（`docs/02` §4）。统一层会明确降级并告知，而不是静默地跑整章。

---

## 合规提示

上游项目均声明"仅用于学习交流"。使用本工具可能违反学习通用户协议，并存在账号被风控的风险。**建议在个人账号上以最小必要频率使用**，风险由使用者自行承担。请勿用于商业用途。
