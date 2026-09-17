# 07 · 凭据获取与 Cookie 落盘

> 建立时间：2026-09-17
> 目标：让平台拿到一个可用的学习通登录态，同时**不落盘明文密码**、**不碰日常浏览器**。

---

## 一、先讲一个实测结论：老办法已经不能用了

网上流传的"从浏览器读取 cookie"做法是：读 profile 里的 `Local State`，取出
`os_crypt.encrypted_key`，用 Windows DPAPI 解开得到主密钥，再解密 `Cookies`
这个 SQLite 数据库里的 `encrypted_value`。

**这条路在新版浏览器上已经死了。** 本机实测（2026-09-17）：

| 浏览器 | 版本 | `Local State` 里的 `os_crypt` 字段 |
|---|---|---|
| Chrome | 152.0.7977.83 | `app_bound_encrypted_key`, `audit_enabled`, `encrypted_key` |
| Edge | 153.0.4234.32 | `app_bound_encrypted_key`, `aster_app_bound_encrypted_key`, `audit_enabled`, `encrypted_key` |

两个浏览器都有 `app_bound_encrypted_key`，即启用了 **App-Bound Encryption（ABE）**。
ABE 把解密密钥封给了浏览器自身（受系统服务校验调用方身份保护），
**任何外部进程都拿不到解密能力**——这不是配置问题，也不是权限问题，是设计如此。

顺带一个实测数据：本机 Chrome 两个 profile 共 323 条 cookie、Edge 三个 profile
共 32 条 cookie，**命中 `chaoxing` / `xuexitong` 的 0 条**。也就是说这台机器上
本来也没有可提取的学习通登录态。

**所以本平台不做"解密浏览器数据库"这件事。** 不写这段代码，也不建议任何人写。

---

## 二、正确解法：让浏览器自己交出来

我们有独立浏览器实例（`docs/06`），而且它就是被 `--remote-debugging-port` 启动的。
于是最短路径是：**通过 CDP 向浏览器要 cookie**。

```
我们的进程 ──CDP (WebSocket)──► 独立 Edge ──► 它用自己的密钥解密 ──► 返回明文 cookie
```

关键在于：**解密发生在浏览器内部**。我们只是它的调试客户端，从头到尾不碰加密。
这既绕开了 ABE，也避免了去做任何"破解"性质的事。

CDP 客户端是手写的（stdlib 没有 WebSocket），见 `orchestrator/cdp.py`。
它只支持 `ws://`、文本帧、不做压缩与 TLS——对本地调试足够，且保持零依赖。
帧编解码、事件跳过、64 位长度分支都有测试覆盖（`tests/test_cdp.py`，13 项）。

---

## 三、三条获取路径

| 路径 | 命令 | 需要浏览器 | 需要网络 | 适用场景 |
|---|---|---|---|---|
| **A. 一站式登录** | `cookies_login` | ✅ 自动启动 | 只需本机 | **推荐**。一条命令走完：起浏览器 → 等你登录 → 自动提取 |
| **A′. 只提取** | `cookies_extract` | ✅ 需已在跑 | 只需本机 | 浏览器已登录过，只想重新提取一次 |
| **B. 手动粘贴** | `cookies_import` | ❌ | ❌ | 不想开浏览器；或自动提取失败时的兜底 |

三条路最终都落到同一份 **`cookies.json`**，所以后续 Adapter 不需要关心 cookie 是哪来的。

### 路径 A：一站式登录（`cookies_login`）

```bash
python -m orchestrator.cli cookies_login
```

它做四件事：

1. 启动项目内的独立浏览器（已在跑则复用），打开 `https://i.chaoxing.com`
2. 记录当前学习通 cookie 条数作为**基线**
3. 轮询等待条数相对基线增长到阈值（默认 +3）
4. 完成后自动提取并落盘，同时打印文件名

```bash
# 参数
--timeout 300     # 等待登录秒数（默认 300）
--poll 3          # 轮询间隔秒（默认 3）
--min-delta 3     # cookie 增长阈值（默认 3）
--no-open         # 不启动浏览器（窗口已在运行）
--url ...         # 换个登录地址
```

**为什么用"增长量"而不是某个 cookie 名**：cookie 名是平台内部实现，没有权威文档。硬编码 `_uid` 这类名字，平台一改版就静默失效。增长量是行为层面的信号，更稳。

**但必须说清楚这是一条弱信号**：它只能说明"浏览器侧发生过登录动作"。所以 `cookies_login` 结束时**永远会提示去跑 `cookies_verify`**，而不是宣称"登录成功"。

### 路径 A′：只提取（`cookies_extract`）

```bash
python -m orchestrator.browser --launch --url https://i.chaoxing.com   # 1. 起独立浏览器
#                                                                    # 2. 在那个窗口里登录
python -m orchestrator.cli cookies_extract                          # 3. 提取
```

提取会**先过隔离守卫**：profile 路径指向系统默认 profile 就直接拒绝，不提取。

### 路径 B：手动粘贴（`cookies_import`）

从浏览器 DevTools 的 Network 面板任意一个请求里复制 `Cookie` 请求头的值：

```bash
python -m orchestrator.cli cookies_import --header "UID=xxx; _d=yyy; fid=zzz"
python -m orchestrator.cli cookies_import --file cookies.txt        # Netscape 格式
python -m orchestrator.cli cookies_import --file cookies.json       # 本平台格式
```

**手动粘贴时域名的处理方式**（一个刻意的取舍）：`Cookie:` 请求头里**没有域名信息**。
如果按"域名不属于学习通就丢弃"来过滤，这条路会被直接废掉。所以：

- 缺域名的 cookie **补上 `.chaoxing.com`**（可用 `--domain` 覆盖）
- `filter_domains` 对**域名为空**的 cookie 一律保留
- 只有域名**已知**时（CDP / Netscape / JSON 导入）才真正按域名过滤

### 路径 A 的终点：`cookies_verify`

```bash
python -m orchestrator.cli cookies_verify
```

它用已落盘的 cookie **真实请求一次** `https://i.chaoxing.com/base`（来源：上游
`Xuexitong-mcp` 的实测协议备忘）。这是唯一能真正回答"这个登录态还能用吗"的手段。

**判定是三态**，因为 cookie 里哪些字段代表登录态没有任何权威文档：

| verdict | 含义 | 后续 |
|---|---|---|
| `valid` | 拿到登录后页面并命中标记 | 可以进 M1 |
| `invalid` | 被重定向到 `passport2.chaoxing.com` | 明确未登录/已失效，重新 `cookies_login` |
| `inconclusive` | 请求成功但识别不出标记 | **不猜**。这不是"登录失败"，只是无法自动确认 |
| `unreachable` | 网络不通/超时 | 查网络或代理 |

两个刻意的实现选择：

1. **不跟随重定向**。跳转到登录页本身就是"未登录"最强的信号，跟过去只会白白多拉一个登录页。
2. **不记录响应体**。页面里可能有姓名、课程信息，只记大小与命中的标记。

宁可报 `inconclusive` 也不报假的 `valid`——一个假的"登录成功"会让后续所有排障都建立在错误前提上。


---

## 四、落盘格式：一份内部格式，三种导出

上游各项目对 cookie 的期望不一样，所以内部统一存 `CookieJar`，再按需导出——
**而不是让每个 Adapter 各存一份**。将来发现某个上游要什么格式，加一个 exporter 即可。

| 文件 | 格式 | 给谁用 |
|---|---|---|
| `accounts/{id}/cookies.json` | 内部规范（唯一事实来源） | 平台自己读；也是文件名 `cookies.json` 的由来 |
| `accounts/{id}/cookies.txt` | Netscape / curl 格式 | 认 `cookies.txt` 的上游工具。HttpOnly 用 `#HttpOnly_` 前缀（curl / wget / yt-dlp 的约定） |
| `accounts/{id}/cookies.header` | 纯 `Cookie:` 头的值 | 最可能被上游"cookie 登录"入口直接吃掉的形式 |

三个文件的 `cookies.json` 是规范源，另外两个是导出物——**改数据只改 json，然后重新导出**。

> ⚠️ 到底上游读哪种格式，**要在 M2 接入 `Samueli924/chaoxing` 时按它的实际实现确认**，
> 不靠猜。三种都生成，就是为了让那次确认只需要"选一个"，而不是"改一套"。

---

## 五、体检：`cookies` 命令回答"为什么登录不上"

```
$ python -m orchestrator.cli cookies
[OK  ] cookies  state=completed  9ms
  共 4 条｜学习通相关 4 条｜会话型 4｜已过期 0
  来源：devtools-manual  采集于 2026-09-17T13:58:07+08:00
  - .chaoxing.com                    4 条
  json : ...\accounts\acc_01\cookies.json
  txt  : ...\accounts\acc_01\cookies.txt
  hdr  : ...\accounts\acc_01\cookies.header
```

它至少能回答四个常见问题：

1. **一条都没有** → 还没在独立窗口里登录学习通
2. **有 cookie 但 `学习通相关 0`** → 登录的是别的站点，或者提取时用错了 profile
3. **`已过期` 大于 0** → 登录态已经不完整，重新登录一次
4. **`会话型 0`** → 说明全是持久 cookie，反而可能是没登录成功

---

## 六、凭据纪律

Cookie 就是凭据，所以约束和密码一样严格：

| 约束 | 实现 |
|---|---|
| 不进版本库 | `accounts/` 已在 `.gitignore` |
| 不写日志 | `_public_params()` 按**键名**剔除；`header` / `cookies` / `session_id` 都在敏感词表里 |
| 不进 Envelope | 返回值只含路径与计数，不含值；`cookies` 命令输出的是**掩码** |
| 权限收紧 | 落盘后 `chmod 0600`（Windows 上为 best-effort） |
| 可一键清除 | `cookies_clear` |

有一条容易漏的：**`redact()` 的值为 `UID=xxx` 这类自定义名字时匹配不上**，
所以 cookie 字符串必须在**键名层面**拦掉，而不能指望值层面的正则。
`tests/test_cookies.py::CookieLeakTests` 就是守这个的。

---

## 七、当前状态与还没做的事

| 项 | 状态 | 说明 |
|---|---|---|
| 登录引导（起浏览器 + 等待 + 检测） | ✅ | `cookies_login`，用基线增长量检测，弱信号 |
| 会话有效性验证 | ✅ | `cookies_verify`，真实请求一次，三态判定 |
| 多账号 cookie | ✅ | 每账号独立 `accounts/{id}/cookies.*`，由 `--account` 选择 |
| 标记库准确性 | ⚠️ | `session_verify.LOGGED_IN_MARKERS` 目前是保守的结构性标记（`dataurl` / `mooc2-ans` 等）。**首次真实验证时可能会返回 `inconclusive`**，那不是失败，而是提示需要按实际页面补标记 |
| 会话失效自动重登（`C03`） | ⏸ | 目前是靠 `cookies_verify` 发现失效后手动重登。全自动重登要等 M1/M3，且它属于 `C03`（统一层自建能力） |
| 自动刷新 cookie | ⏸ | 手动 `cookies_extract` 即可。学习通的会话有效期通常够用，不值得为它做后台轮询（轮询本身也有风控风险） |
| 密码登录路径 | ⏸ | 上游 A1 支持 `-u/-p`（走 AES 加密）。**本平台不落盘明文密码**，只在调用时从 `credentials.json` 读入内存 |
| cookie 是否真能被上游接受 | ⏸ | 取决于上游读哪种格式，M2 接入时确认 |
