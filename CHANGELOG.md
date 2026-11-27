# 变更记录

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.0] — 2026-11-27

首个公开发布。从零设计的拉取式 WGC 封装，附带 GDI 降级后端。

### Added

- `WindowGrabber`：单窗口抓屏，`grab()` 返回 BGR `uint8 (h, w, 3)`。
- 双后端：`wgc`（`Windows.Graphics.Capture`）与 `gdi`（`mss` / `BitBlt`），
  `backend="auto"` 先试 WGC 再降级。
- 多格帧槽（4 格）：native 回调写、`grab()` 读，转换在锁外完成，
  单次取帧约 0.47 ms。
- 帧龄看门狗：`stale_ms` 内无新帧判定僵死，`max_heals` 次自愈失败后降级 GDI。
- 自愈分支覆盖 hwnd 失效（目标进程重启）后的重新定位与重连。
- `stats` 观测面：`frames / age_ms / heals / max_heals / cb_errors /
  blank_streak / last_ts / borderless`。
- `on_stale` 语义：`"last"` 返回上一张有效帧，`"raise"` 抛
  `StaleFrameError`（录制场景必需）。
- 黑帧策略：连续 `blank_limit` 帧全黑才判"没有画面"；全黑帧永不写入
  有效帧缓存。
- `draw_border=False` 的延迟失败降级链：构造 → `start_free_threaded()`
  才失败 → 自动退回默认边框；结果记忆到进程级，避免重复付出 D3D 会话代价。
- `backend="wgc"` 强制模式下所有失败路径都抛 `BackendUnavailable` 并带原因。
- `AsyncGrabber`：后台线程持续抓屏，只保留最新帧。
- 窗口工具：`list_windows` / `pick_main_window` / `wait_for_window` /
  `move_window`（带回 `GetLastError`）/ `window_ops_blocked`（零副作用探测）/
  `frame_rect` / `client_rect`。
- `WindowGrabber.park()`：移到屏幕外但不最小化，WGC 仍持续出帧。
- 导入即声明 per-monitor DPI 感知。
- `py.typed`，公开 API 带完整类型标注。
- 38 项离线单元测试：不需要真实窗口 / 显示器 / GPU。

### Known limitations

- Windows 10 上无法去掉系统提示边框（平台限制，`stats["borderless"]` 可查）。
- 窗口最小化时 WGC 不出帧（没有可见表面），需用 `park()` 代替最小化。
