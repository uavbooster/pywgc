# 架构说明

本文解释 `pywgc` 内部为什么长成这样。所有设计都可以追溯到同一句话：
**静默失败比崩溃更难查。**

## 1. WGC 的原始形状

`windows-capture` 暴露的 API 是推送式的：

```
WindowsCapture(window_hwnd=...)
  ├── .event(on_frame_arrived)      # 按 handler.__name__ 派发！
  ├── .event(on_closed)
  └── .start_free_threaded()  ->  CaptureControl
```

`start_free_threaded()` 之后，一个 **native C 线程** 开始按屏幕刷新率回调
`on_frame_arrived(frame, control)`，`frame.frame_buffer` 是一块 BGRA 的零拷贝
mapped view。回调返回后这块内存可能被回收 —— **不能把它存起来**。

而业务代码想要的是 `frame = grab()`。所以这一层的全部工作就是：
把推送流的**最新一帧**，以最低成本、且**诚实地**交给拉取方。

## 2. 线程模型

```
native 采集线程                 你的线程
┌──────────────────┐          ┌──────────────────┐
│ on_frame_arrived │          │ grab()           │
│  ├ 裁剪 ROI      │          │  ├ 读 _front     │
│  ├ BGRA→BGR      │  帧槽    │  ├ 帧龄检查      │
│  └ 写 buf[idx]   │ ──────>  │  ├ copy          │
│    (锁内只换指针)│  4 格    │  └ 返回          │
└──────────────────┘          └──────────────────┘
```

关键点：**转换在锁外**。

回调写第 `i` 格时，主线程读的是第 `j` 格（`i ≠ j` 由 4 格环形保证）。
所以 `cvtColor`（约 1 ms）与 `grab()` 的 `copy`（约 0.3 ms）是真正并行的，
锁里只做"切换索引 + 记录时间戳 + 计数"这几条赋值。

反例（曾经写过）：把 `cvtColor` 也放进锁里，结果是回调与 `grab()` 在同一把锁
上串行，单帧成本从 0.5 ms 涨到 1.5 ms 以上，且抖动明显。

为什么是 4 格而不是 2 格：60 fps 下 4 格约 66 ms 余量，即使主线程被 GC
或调度打断也不会让回调覆写正在读的那一格。2 格在卡顿时就会。

## 3. `grab()` 的判定顺序

顺序不能改，每一步都在拦一类具体故障：

```
1. 有帧？ ──否──> 会话已关闭？ ──是──> on_stale=raise ? 抛 : 返回旧帧
   │                       └─否─> 超过 timeout ? ──是──> 尝试自愈 → 抛 / 返回旧帧
   └─是
2. 帧龄 > stale_ms？
   ├─是 → _maybe_heal()
   │      ├─ 自愈成功 → 重取（continue）
   │      └─ 自愈失败 → on_stale=raise ? 抛 StaleFrameError : 返回旧帧
   └─否
3. copy 这一格
4. 全黑？ → blank_limit 内：照常返回；超过：抛 BlankFrameError 或回退旧帧
5. 缓存为 _last_img，返回
```

## 4. 帧龄看门狗：为什么必须有

这是本库存在的**最主要理由**。

WGC 会话可以在完全不报错的情况下死掉：native 线程还在、`CaptureControl`
还有效、`on_closed` 不触发，但回调不再有新帧。朴素的实现会让 `grab()`
永远返回同一张缓冲里的旧图 —— 调用方看到的是"一切正常"。

`_stale_ms`（默认 3 s）就是这条判据：3 秒零新帧 ≈ 必然僵死（真实画面不会
静止这么久）。超过就进自愈。

代价是"静止画面"会被误判。这是一个**有意接受的权衡**：误判的后果是重建一次
会话（丢 1–3 帧）；漏判的后果是下游拿着几分钟前的画面当实时数据。

## 5. 自愈的三个分支

`_maybe_heal()` 的顺序**不能调整**：

| 顺序 | 判据 | 动作 | 为什么必须在前 |
| --- | --- | --- | --- |
| 1 | `IsIconic(hwnd)` | 直接放弃 | 最小化后没有可见表面，重建一百次也没帧 |
| 2 | `IsWindow(hwnd)` 为假 | 重新定位窗口 → 换 hwnd 重建 | 目标进程重启后旧 hwnd 永久失效，用旧句柄重建只会立刻又死 |
| 3 | 其余 | 用原 hwnd 重建；累计 `max_heals` 次失败则降级 GDI | 普通的 DWM 会话失效 |

第 3 分支的 `_heals` 在**收到真新帧**时清零（比较 `frame.timespan`，
不是比较时间）。所以偶发的一次重建不会把计数永久推高。

## 6. `draw_border` 的延迟失败

想拿到不含系统提示边框的画面，需要 `draw_border=False`。但它在 Windows 10 上
**构造成功、启动失败**：

```python
cap = WindowsCapture(window_hwnd=..., draw_border=False)   # 成功
cap.event(on_frame_arrived)                                # 成功
cap.start_free_threaded()
# -> RuntimeError: Toggling the capture border is not supported
```

如果只在构造期 `try/except`，异常会在这里逃出去把整个 WGC 初始化打断，
表现为"莫名其妙一直用 GDI"。本库的做法：

1. 先试 `draw_border=False`；
2. `start_free_threaded()` 抛错 → 退回不带该参数的默认边框；
3. 结论写进**进程级** `_BORDER_OK`，后续实例直接跳过白试。

第 3 步很重要：每次白试的代价是一次失败的 D3D 会话初始化。

## 7. 黑帧策略

两个后端都可能返回全黑：

- GDI：窗口被完全遮挡（抓到的是遮挡物）、最小化后的空图；
- WGC：DRM 保护的表面、窗口内容丢失。

"全黑"对下游是最危险的输入 —— 格式合法、内容全零，视觉模型会给出
一个**确定的错误答案**而不是报错。

处理方式：

- `blank_limit` 帧之内：照常交付（场景切换确实会闪黑 1–2 帧），
  但**不写入 `_last_img`**；
- 超过 `blank_limit`：`on_stale="raise"` 抛 `BlankFrameError`，
  否则回退 `_last_img`；连 `_last_img` 都没有就抛错。

"闪黑帧不缓存"这条容易被漏掉。如果把它缓存了，`_last_img` 就变成黑图，
后面所有回退都会返回黑图 —— 故障从"一次闪黑"升级为"永久黑屏且无人报警"。

## 8. 降级链

```
backend="auto"                                backend="wgc"
─────────────────                             ─────────────────
try WGC                                       try WGC
  ├─ 成功 → backend="wgc"                       ├─ 成功 → backend="wgc"
  └─ 失败 → gdi                                 └─ 失败 → BackendUnavailable(原因)
              │
              └─ 运行期连续 max_heals 次自愈失败 → 降级 gdi
                                                     │
                                                     └─ reopen() 可升回 wgc
```

`reopen()` 解决一个很常见的启动时序问题：进程启动那一刻窗口是**最小化**的，
WGC 等不到首帧而降级 GDI。之后窗口被恢复，如果还赖在 GDI 上，就白白失去了
"被遮挡也能抓"的能力，而且 CPU 高一个数量级。

## 9. 两个后端的语义差异（不只是速度）

| | `wgc` | `gdi` |
| --- | --- | --- |
| 数据来源 | DWM 合成后的窗口表面 | 屏幕像素的拷贝 |
| 被遮挡 | 无影响 | 抓到遮挡物；完全遮挡 → 全黑 |
| 失去前台 | 无影响 | 无影响（只要没被遮挡） |
| 移出屏幕 | 无影响 | 抓不到 |
| 最小化 | 无帧 | 空图 / 黑图 |
| 会话状态 | 有状态，缩放/重启会断 | 无状态 |

"移出屏幕仍能抓"这个特性撑起了 `park()`：把窗口挪到 `(-4000, 0)` 而不最小化，
人看不见但 WGC 照常出帧。它比"最小化"更适合长时间采集，因为最小化后
WGC 一帧都不出。

## 10. 三条设计原则的代码落点

| 原则 | 落点 |
| --- | --- |
| 每条失败路径留下原因 | `_init_wgc` 里每个 `return False` 之前都写 `_wgc_error` + WARNING；`backend="wgc"` 时把原因塞进 `BackendUnavailable` |
| 吞异常但必须计数 | `on_frame_arrived` 的 `except` 里累加 `_cb_errors` |
| 不把可疑数据当正常输入 | `_blank_fallback` 不缓存黑帧；`on_stale="raise"` 抛 `StaleFrameError` |

对应的三个"曾经的静默缺陷"（都是实测踩出来的）：

1. `draw_border=False` 的失败被构造期 `try/except` 漏掉 → 整库静默走 GDI；
2. `on_closed` 立刻触发、一帧不来时没有任何日志 → 排查时只看到"用了降级后端"；
3. 会话僵死时 `grab()` 无限返回旧图 → 录制端写出几分钟的重复帧。

## 11. 性能数据的观测方式

仓库里所有数字都是**墙钟**测量的，不是 `time.process_time()`。原因见
README 的"度量陷阱"：OpenCV 默认按核数开线程池，把 `process_time` 除以帧数
会得到"单核 120%"这类无意义的数。

想自己复现，固定三件事：同一窗口、同一 ROI、`cv_threads=1`。
`grab()` 的耗时应当只在**已有帧**的状态下测（即先跑起来再计时），
否则会把首帧等待时间算进去。
