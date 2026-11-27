# 故障排查

按"从最常见的开始"排序。每条都给出**判定方法**而不只是结论 ——
这类问题最容易在猜错方向上浪费半天。

## 0. 先看这三个数

```python
import logging
logging.basicConfig(level=logging.INFO)

from pywgc import WindowGrabber, available_backends
print("backends:", available_backends())      # WGC / GDI 是否可导入

g = WindowGrabber(title="你的窗口")
print("backend:", g.backend)                  # 实际生效的是哪个
print("stats:", g.stats)                       # 会话健康度
```

`stats` 里每个字段对应一类故障：

| 字段 | 异常表现 | 含义 |
| --- | --- | --- |
| `backend` | 一直是 `gdi` | WGC 起不来，看 `WARNING` 日志的第一条原因 |
| `frames` | 不增长 | 回调没被触发（会话死了 / 窗口无可见表面） |
| `age_ms` | 持续增长 | 零新帧，正在走向僵死判定 |
| `heals` | 反复增长 | 会话不稳定，正在反复重建 |
| `cb_errors` | > 0 | 回调内转换失败（多为依赖版本变化） |
| `blank_streak` | ≥ `blank_limit` | 全黑（遮挡 / DRM / 内容丢失） |
| `borderless` | `False` | Windows 10，去不掉系统提示边框 |

## 1. 一直用 GDI，但日志里没有原因

升级到最新版。0.1.0 起，`_init_wgc` 的每条 `return False` 都写
`_wgc_error` 并打 WARNING，`backend="wgc"` 时直接抛异常带原因。

如果确实看不到日志：检查你是否覆盖了 `pywgc` 的 logger 级别，
或者库被 import 时 `logging` 还没配置（此时 WARNING 会走
`logging.lastResort`，默认输出到 stderr）。

## 2. `BackendUnavailable: windows_capture 不可用`

```
pip install windows-capture
```

还不行的话，逐条确认：

- Python 版本是否在 3.9–3.13 之间（wheel 只覆盖这些版本）；
- 是否 64 位 Python（该依赖不提供 32 位 wheel）；
- `python -c "import windows_capture; print(windows_capture.__version__)"` 能否成功。

## 3. `BackendUnavailable: ... 一帧都没来`

`start_free_threaded()` 成功，但 `on_closed` 立刻被调用、一帧都没来。
本库会把这条原因原样报出来。常见成因：

- **窗口最小化** —— 没有可见表面。先恢复窗口，或用 `park()` 代替最小化。
- **远程桌面会话** —— WGC 在部分 RDP 配置下拿不到表面。此时只能用 `gdi`。
- **独占全屏 / DRM 内容** —— 两个后端都抓不到（这是平台保护，不是 bug）。

判定方法：窗口可见、非最小化、本地登录，仍然一秒内 `on_closed` → 基本就是
表面保护类问题，改用 `backend="gdi"`。

## 4. `stats["age_ms"]` 一直涨

按 `heals` 分两种情况：

- `heals` 也在涨：会话在反复重建但重建后也拿不到帧 → 回到第 3 节。
- `heals` 不变（自愈被跳过）：最常见是**窗口最小化**，`_maybe_heal` 的第一
  分支直接放弃（最小化重建无意义）。
- 窗口可见、也没最小化，但 `age_ms` 增长：目标程序在不可见/空闲时**暂停渲染**。
  `park()` 之后特别容易遇到。判定：把窗口移回屏幕中央，`age_ms` 是否立刻回落。

## 5. `move_to()` / `park()` 返回 `err=5`（UIPI）

`ERROR_ACCESS_DENIED(5)` 表示目标窗口属于**完整性级别更高**的进程
（典型：目标以管理员运行，你的进程没有）。`SetWindowPos` / `ShowWindow` /
`SetForegroundWindow` 都是发窗口消息，会被 UIPI 拦截，且**静默返回 0**。

两条出路：本进程提权，或让目标程序以普通权限运行。

先探测再动手，避免把权限问题误判成逻辑 bug：

```python
from pywgc import window_ops_blocked
blocked, reason = window_ops_blocked(hwnd)
print(blocked, reason)     # True / "ERROR_ACCESS_DENIED(5): ..."
```

**注意**：`SendInput` 不受 UIPI 限制（注入输入队列，不是窗口消息），
所以"能往窗口注入按键、却挪不动窗口"是正常组合，不要往代码 bug 上查。

## 6. `cb_errors` 在增长

回调里 `except` 吞掉的异常。基本上只有三种来源：

1. `frame.frame_buffer` 的形状与 ROI 不匹配（窗口正在缩放，会话还没重建）；
2. 依赖升级后 `Frame` 的字段名变了（`frame_buffer` / `timespan`）；
3. `dst` 帧槽尺寸与 ROI 不一致（内部 bug，请提 issue 附 `stats`）。

持续增长意味着**每帧都在丢**，此时 `frames` 不涨而 `cb_errors` 涨。
先对齐 `windows-capture` 的版本。

## 7. 画面整体偏移

DPI。本库在 import 时已调用 `enable_dpi_awareness()`（per-monitor v2）。
如果你在 import 之前就让进程变成了 DPI-unaware（例如某个启动器显式设了
`SetProcessDPIAware()` 之外的兼容性标志），坐标会按逻辑像素给出，而 GDI
按物理像素抓 —— 结果就是整体缩放/偏移。

判定：把显示缩放临时改成 100%，偏移消失即为 DPI 问题。

## 8. 画面边缘有一条异色细边

Windows 10 的捕获提示边框。`draw_border=False` 在 Win10 上是延迟失败
（`start_free_threaded()` 才抛，构造阶段不报错），库会自动退回默认边框。

`stats["borderless"]` 为 `False` 即表示当前带边框。规避方式：把 ROI 向内缩
2–3 px，或升级到 Windows 11（通常可去掉）。

## 9. 抓到的画面是黑的

先看哪个后端：

- `gdi`：几乎必然是**被完全遮挡**。判定：把窗口切到前台，`mean` 是否恢复。
- `wgc`：DRM 保护的表面，或窗口内容已丢失。`blank_streak` 会持续增长。
- 两个后端都黑：内容本身是黑的，不是抓屏问题。

## 10. `BlankFrameError` 但画面肉眼正常

`blank_limit` 太小，而目标确实会长时间闪黑（切场景、全屏加载）。
调大 `blank_limit`（例如 10），或改用 `on_stale="last"`。

注意不要为了"消灭异常"把 `blank_limit` 调到很大：超过阈值的全黑帧会回退到
旧帧，掩盖真正的内容丢失。

## 11. 缩放窗口后丢帧

预期行为。帧槽尺寸固定，`refresh()` 检测到 ROI 尺寸变化会重建会话，
重建期间丢 1–3 帧。若你的循环不调 `refresh()`，就只会在下一次
`grab()` 的自愈路径里才发现（表现为短暂的 `age_ms` 增长）。

## 12. 目标进程重启后一直 `WindowNotFound`

用 `hwnd=` 构造时这是**预期**的：句柄已失效，库无法凭空猜出新句柄。

改用 `title=`（库会自动重新定位并重连，日志
`窗口已重建（hwnd 0x… -> 0x…），重连 WGC`），或在你的循环里：

```python
from pywgc import wait_for_window
g = wait_for_window(title="你的窗口", timeout=30)   # 等进程回来
```

## 13. 数据集/录像里出现长时间静止画面

录制场景必须用 `on_stale="raise"`。默认 `on_stale="last"` 会在会话僵死时
以采集循环的速度持续吐同一张图，事后无法分辨。

```python
frame = g.grab(on_stale="raise")     # 抛错 → 记一笔并跳过，别写进数据集
```

同时把 `g.stats` 随每段样本落盘：`heals > 0` / `cb_errors > 0` /
`blank_streak > 0` 都是"这段数据可疑"的信号。

## 14. 两个后端的结果对不上

像素差约 3%，属正常（GDI 抓屏幕像素、WGC 抓合成表面，缩放与色彩管理路径不同）。
**不要混用**：训练/推理用同一个后端，并把 `backend` 记进样本元数据。
