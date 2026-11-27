# pywgc

[![PyPI](https://img.shields.io/pypi/v/pywgc.svg)](https://pypi.org/project/pywgc/)
[![CI](https://github.com/uavbooster/pywgc/actions/workflows/ci.yml/badge.svg)](https://github.com/uavbooster/pywgc/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/pywgc.svg)](https://pypi.org/project/pywgc/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-blue.svg)](#compatibility)

低延迟 Windows 窗口抓屏。`Windows.Graphics.Capture` 优先，GDI `BitBlt` 自动降级，
对外只暴露一个拉取式的 `grab()`。

```python
from pywgc import WindowGrabber

with WindowGrabber(title="记事本") as g:
    frame = g.grab()          # numpy uint8 (h, w, 3), BGR
    print(g.backend)          # "wgc"
    print(frame.shape)        # (684, 1216, 3)
```

---

## 为什么需要这一层

`Windows.Graphics.Capture` 本身是推送模型：起一个 native 会话，画面变化时回调。
这让它又快又稳（单次取帧约 0.5 ms，窗口被遮挡也照常出帧），但直接用在
"每帧拉一次图"的循环里会踩四个坑：

1. **没有拉取接口** —— 回调线程推帧，业务循环只能靠共享状态去凑；
2. **僵死无声** —— 会话断了、窗口换了、native 线程卡住时，回调不再触发，
   而 `grab()` 会永远返回同一张旧图。没有任何异常、没有任何日志，
   下游以为一切正常；
3. **`draw_border=False` 是延迟失败** —— 构造成功，`start_free_threaded()`
   才抛"Toggling the capture border is not supported"。只在构造期
   `try/except` 的话，整个 WGC 会静默死掉，表现成"莫名其妙一直走降级路径"；
4. **窗口最小化 / 进程重启 / 分辨率变化** 都会让会话失效，且失效方式各不相同。

`pywgc` 把这些都封装掉了：多格帧槽 + 帧龄看门狗 + 自愈重连 + 明确降级链，
并且把每一个失败原因都变成**可观察状态**而不是静默。

## 特性

- **快**：WGC 单次 `grab()` 中位 **0.47 ms**，占 60 Hz 预算约 3%（见[性能](#性能)）。
- **遮挡无关**：抓窗口表面而非屏幕像素。窗口被别的窗口盖住、失去前台、
  部分移出屏幕，都照常出帧。
- **不会静默降级**：`backend="wgc"` 强制模式下起不来会抛
  `BackendUnavailable`，消息里带具体原因；`auto` 模式下每次降级都留日志。
- **帧龄看门狗**：`stale_ms` 内没有新帧就重建会话，连续 `max_heals` 次失败才降级。
- **`stats` 观测面**：`frames / age_ms / heals / cb_errors / blank_streak / borderless`
  一次看全会话健康状况。
- **录制安全**：`on_stale="raise"` 让"静默重复帧"变成异常，避免以 60 张/秒
  的速度污染数据集或录像。
- **纯 numpy 出图**：返回 `(h, w, 3)` 的 `uint8` BGR，直接进 OpenCV / PyTorch。
- **无全局副作用**：不抢焦点、不改窗口尺寸、日志走 `logging`（默认 `NullHandler`）。

## 安装

```bash
pip install pywgc                # 核心：WGC 后端
pip install "pywgc[fast]"        # + OpenCV（BGRA→BGR 快约 3 倍）
pip install "pywgc[gdi]"         # + mss（GDI 降级后端）
pip install "pywgc[all]"         # 全部
```

要求 Windows 10 1803 及以上（`Windows.Graphics.Capture` 的起点），Python 3.9+。

> 只装核心依赖时也能用 WGC；`[gdi]` 仅在你需要降级路径或做两后端对照时需要。
> `[fast]` 是可选的性能优化，没装会自动退回 numpy 转换（结果完全一致）。

## 快速开始

### 按标题抓

```python
from pywgc import WindowGrabber

with WindowGrabber(title="记事本") as g:
    while True:
        frame = g.grab()                 # 约 0.5 ms
        cv2.imshow("cap", frame)
        if cv2.waitKey(1) == 27:
            break
```

`title` 是**前缀**匹配，且会用打分挑"主窗口"——一个进程常有多个同名顶层窗口
（主窗口、托盘壳、隐藏的渲染窗口），挑错了会抓到 160×28 在屏幕外的假窗口。
打分规则见 `pywgc.pick_main_window`。

### 按句柄抓（推荐用于长期运行）

标题会随文档名、页面标题变化；句柄不会。拿到一次句柄之后就绑死它：

```python
from pywgc import WindowGrabber, list_windows

cand = [w for w in list_windows() if "Notepad" in w["title"]]
with WindowGrabber(hwnd=cand[0]["hwnd"]) as g:
    ...
```

### 抓局部区域

`region` 是**相对客户区**的 `(x, y, w, h)`，会自动裁到窗口内：

```python
g = WindowGrabber(title="记事本", region=(0, 0, 400, 200))
```

### 后台线程持续抓（GDI 降级时必须）

GDI 单帧约 16 ms，会和你的处理逻辑串行掉帧；WGC 下这层不是瓶颈但依然有用
（隔离调度抖动）：

```python
from pywgc import AsyncGrabber, WindowGrabber

g = WindowGrabber(title="记事本")
ag = AsyncGrabber(g)
try:
    while running:
        frame = ag.latest(timeout=0.5)   # 永远拿到最新一帧，旧帧被丢弃
        process(frame)
finally:
    ag.stop()
    g.close()
```

## 两个后端：为什么 WGC 是默认，GDI 只是兜底

它们的差别不只是速度，而是**语义**：

| 维度 | `wgc`（默认） | `gdi`（降级） |
| --- | --- | --- |
| 数据来源 | DWM 合成后的**窗口表面** | 屏幕**像素** |
| `grab()` 中位耗时 | **0.47 ms** | 16.4 – 17.0 ms |
| 单帧墙钟（含转换） | **5.6 ms** | 17.2 ms |
| CPU / 帧 | **3.1 ms** | 12.4 ms |
| 占 60 Hz 预算（16.7 ms） | **≈ 3%** | ≈ 98–100% |
| 窗口被其它窗口遮挡 | 正常出帧 | 抓到遮挡物；完全遮挡时**全黑** |
| 窗口失去前台 | 正常出帧 | 正常（只要未被遮挡） |
| 窗口部分移出屏幕 | 正常出帧 | 抓不到那部分 |
| 窗口最小化 | 无帧（没有可见表面） | 空图 / 黑图 |
| 系统提示边框 | Windows 10 去不掉，会压到画面边缘 1–2 px | 无 |
| 会话稳定性 | 缩放窗口 / 目标进程重启会断，需重连 | 无状态，不会断 |

**最关键的一行是"遮挡"**：GDI 在窗口被完全遮挡时返回的是
`mean=[0, 0, 0]` 的纯黑图 —— 它不会报错，看起来就像"画面变成了黑色"。
下游若把"全黑"当成有效输入去解码，会得到完全错误但格式合法的结果。
WGC 在同样条件下保持 60.0 fps、`mean=[138.9, 149.6, 158.3]`，画面不变。

所以本库的策略是：**WGC 是唯一的主路径，GDI 只用于降级、取证与受限环境**
（例如远程桌面会话里 WGC 起不来）。这也是 `backend="auto"` 的默认行为。

## 性能

测量条件：Windows 10 22H2，同一窗口，客户区 **1216×684**，
WGC 会话原生 **60 fps**，`cv_threads=1`，取 30 s 稳定态的中位数。

| 指标 | `wgc` | `gdi` |
| --- | --- | --- |
| `grab()` 中位 | 0.47 ms | 16.40 – 16.96 ms |
| `grab()` p99 | < 1.2 ms | 17.4 ms |
| 端到端 fps（含转换） | 58.6 – 59.3 | 58.2（满核占用） |
| 单帧墙钟 | 5.63 ms | 17.18 ms |
| CV 处理 CPU | 3.0 – 3.4 ms / 帧 | 12.4 ms / 帧 |

两点容易踩的度量陷阱：

- **别用"进程 CPU 时间 ÷ 帧数"来比较库的开销**。OpenCV 默认按 CPU 核数开线程池，
  统计时会看到单核的 120% 以上。真正的对比看墙钟（5.63 vs 17.18 ms）。
- **`cv_threads=1` 是有意的**。对 1.6 MB 的 `BGRA→BGR` 这种小操作，
  8 线程实测 3.6 ms CPU，单线程 0.55 ms —— 线程池调度比计算本身还贵。

## 录制 / 数据集采集：请用 `on_stale="raise"`

默认 `on_stale="last"` 会返回上一张有效帧，适合交互式控制循环（宁可慢一帧，
不要打断流程）。但录制场景下这是**危险**的：会话僵死时它会以你采集循环的速度
持续吐同一张图，事后完全分辨不出来。

```python
try:
    frame = g.grab(on_stale="raise")
except (StaleFrameError, BlankFrameError) as e:
    stats = g.stats          # age_ms / heals / cb_errors 都在这里
    recorder.skip(e, stats)  # 记一笔，别写进数据集
```

同时建议把 `g.stats` 随每段录像落盘 —— `heals > 0`、`cb_errors > 0`、
`blank_streak > 0` 都是"这段数据可能不干净"的信号。

## API 参考

### `WindowGrabber`

```python
WindowGrabber(
    title: str | None = None,          # 窗口标题前缀（与 hwnd 二选一）
    hwnd: int | None = None,           # 窗口句柄
    region: tuple | None = None,       # 客户区内的 ROI (x, y, w, h)
    backend: str = "auto",             # "auto" | "wgc" | "gdi"（"mss"/"bitblt"/"desktop" 视为 gdi）
    first_frame_timeout: float = 1.5,  # 等首帧的秒数，超时判定该后端不可用
    cv_threads: int = 1,               # OpenCV 线程数
    stale_ms: float = 3000.0,          # 零新帧多久判定僵死
    max_heals: int = 3,                # 连续自愈上限，超过降级 gdi
    blank_limit: int = 3,              # 连续多少帧全黑才判"没画面"
)
```

| 成员 | 说明 |
| --- | --- |
| `grab(timeout=1.0, on_stale="last")` | 返回 BGR `uint8 (h, w, 3)` |
| `stats` | `dict`：`backend frames heals max_heals cb_errors blank_streak last_ts borderless age_ms` |
| `backend` | `"wgc"` 或 `"gdi"`（当前实际生效的） |
| `hwnd` / `window` / `roi` | 当前绑定的窗口与 ROI |
| `frame_rect` | 窗口**外框**在屏幕上的 `(left, top, w, h)`，用于挪走再挪回 |
| `refresh()` | 重新定位窗口、重算 ROI（尺寸变化会自动重建 WGC 会话） |
| `reopen()` | 若已降级到 gdi，尝试升回 WGC |
| `move_to(x, y)` | 移动窗口，返回 `(ok, err)`，失败带 `GetLastError` |
| `park(x=-4000, y=0)` | 移到屏幕外但不最小化（WGC 仍出帧），返回 `(ok, err)` |
| `close()` | 释放 native 线程与 D3D 设备（支持 `with`） |

### 异常

全部继承 `RuntimeError`，因此旧的 `except RuntimeError` 仍能兜住：

```
CaptureError
├── WindowNotFound       找不到窗口 / ROI 为空
├── BackendUnavailable   强制模式下后端起不来
├── StaleFrameError      取不到新帧（僵死、最小化、会话结束）
└── BlankFrameError      持续全黑（DRM、内容丢失、被遮挡）
```

### 模块级工具

| 函数 | 作用 |
| --- | --- |
| `available_backends()` | `{"wgc": bool, "gdi": bool}`，只做导入探测、无副作用 |
| `list_windows(title_prefix=None, pid=None)` | 枚举顶层窗口，返回客户区坐标 |
| `pick_main_window(windows)` | 从同名窗口里按面积/可见性/最小化打分挑主窗口 |
| `wait_for_window(...)` | 等窗口出现再返回 `WindowGrabber` |
| `window_ops_blocked(hwnd)` | 探测能否操作该窗口，返回 `(blocked, reason)` |
| `enable_dpi_awareness()` | 声明 DPI 感知（导入时已自动调用一次） |

## 日志

库本身不配置任何 handler（符合库的规范），诊断信息走 `logging.getLogger("pywgc")`。
想让降级、自愈、权限问题可见，在你的应用入口配一次即可：

```python
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
```

WARNING 级别会打出：降级原因、`draw_border` 不支持、自愈重建、`SetWindowPos` 失败
（含 `GetLastError` 与 UIPI 提示）。

## 已知限制

这些是**平台本身**的限制，不是实现缺陷，但会影响你的设计：

- **Windows 10 的提示边框去不掉。** `draw_border=False` 在 Win10 上是延迟失败
  （`start_free_threaded()` 才抛 `Toggling the capture border is not supported`），
  库里会自动退回默认边框。代价是画面边缘 1–2 px 可能带系统高亮色。
  `stats["borderless"]` 会告诉你当前是否成功去掉了边框（Win11 通常为 `True`）。
- **最小化窗口不出帧。** WGC 抓的是 DWM 合成结果，最小化后没有可见表面。
  需要"看不见但仍在抓"就用 `park()`：移到屏幕外而不最小化。
- **会停渲染的目标程序抓不到新帧。** `park()` 之后观察 `stats["age_ms"]`，
  若持续增长说明该程序在不可见时暂停渲染。
- **缩放窗口会断会话。** 库会在下次 `refresh()` / `grab()` 时按新尺寸重建，
  重建期间可能丢 1–3 帧。
- **目标进程重启后 hwnd 失效。** 用 `title=` 构造会自动重新定位并重连
  （日志：`窗口已重建（hwnd 0x… -> 0x…），重连 WGC`）。
- **远程桌面 / 某些 DRM 内容起不来。** 表现为 `start` 成功但 `on_closed` 立刻触发、
  一帧都不来。库会明确报这个原因并降级。
- **不同后端像素不完全一致。** 同一帧的像素差约 3%，因此**数据集不要混用后端**。
  `stats["backend"]` 或 `WindowGrabber.backend` 应该随样本一起记录。

## 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 一直是 `backend="gdi"` | WGC 起不来 | 把日志调到 INFO，看第一条 WARNING 的原因 |
| `BackendUnavailable: ... windows_capture 不可用` | 依赖没装 / 加载失败 | `pip install windows-capture`；注意它需要 Windows 10 1803+ |
| `BackendUnavailable: ... 一帧都没来` | 窗口最小化、远程桌面、独占全屏 | 恢复窗口；或改用 `gdi` |
| `stats["age_ms"]` 一直涨 | 会话僵死或目标停止渲染 | 看 `heals`；`heals` 到上限会降级并打 ERROR |
| `stats["cb_errors"]` 增长 | 回调里转换失败（多为依赖升级导致字段变化） | 对比 `windows-capture` 版本 |
| `move_to()` 返回 `err=5` | **UIPI**：目标进程完整性级别更高 | 用管理员权限运行本进程，或让目标程序不提权 |
| 抓到的区域整体偏移 | 进程不是 DPI aware | 已经自动处理；若你自行禁用了，调 `enable_dpi_awareness()` |
| 抓到的画面是黑的 | GDI 被完全遮挡 / DRM 内容 | 换 `wgc`；DRM 内容两个后端都抓不到 |
| `BlankFrameError` 但画面正常 | `blank_limit` 太小，遇到长闪黑 | 调大 `blank_limit` |

关于 UIPI 的一个常见误解：`SendInput` **不受** UIPI 限制（它注入输入队列，
不是给窗口发消息），所以"按键能注入、窗口却挪不动"是完全正常的组合。

## 设计原则

三条，都来自"静默失败比崩溃更难查"：

1. **每条失败路径都必须留下原因。** 强制模式下抛异常并带原因；`auto` 模式下
   每次降级都打 WARNING。绝不出现"莫名其妙就走了降级路径"。
2. **回调里吞异常，但必须计数。** 回调抛异常会打死 native 采集线程，
   所以必须 `except`；但 `cb_errors` 会累加，否则"依赖升级后 100% 丢帧"
   会完全静默地发生。
3. **不把可疑数据当下游的"正常输入"。** 全黑帧不进 `_last_img` 缓存；
   `on_stale="raise"` 让重复帧变成异常。宁可让调用方处理一次错误，
   也不要让它以 60 次/秒的速度安静地收集垃圾。

细节见 [`docs/architecture.md`](docs/architecture.md)。

## 兼容性

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 1803+ / Windows 11 |
| Python | 3.9 – 3.13 |
| 必需依赖 | `numpy>=1.24`, `windows-capture>=1.3.0` |
| 可选 | `opencv-python-headless>=4.8`（`[fast]`）、`mss>=9.0`（`[gdi]`） |
| 进程权限 | 抓屏不需要提权；**操作窗口**（`move_to`/`park`）需要 ≥ 目标进程的完整性级别 |

## 开发

```bash
git clone <repo> && cd pywgc
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"

pytest -q          # 单元测试（离线，不需要真实窗口）
ruff check .       # 风格与静态检查
python -m build    # 产出 dist/*
```

测试用 `WindowGrabber.__new__()` 手工装配对象 + 伪造 native 帧，
因此**不需要显示器、不需要 GPU、不需要开任何窗口**即可运行。
覆盖重点是"静默失败"类路径：帧龄看门狗、自愈、回调异常计数、黑帧策略、
`on_stale` 语义。见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 变更记录

见 [`CHANGELOG.md`](CHANGELOG.md)，遵循 [SemVer](https://semver.org/lang/zh-CN/) 与
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## 许可证

[MIT](LICENSE) © 2026 Tang Cheng
