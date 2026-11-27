# 贡献指南

感谢参与。本项目的核心约束只有一条：**不允许出现静默失败**。
任何"失败但看起来正常"的路径都会被当作缺陷处理。

## 开发环境

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

`[dev]` 会装齐 `pytest` / `ruff` / `build` 以及可选运行时依赖。

## 提交前必须通过

```bash
pytest -q
ruff check .
python -m build
```

三条都要绿。`pytest` 是离线的（不需要窗口 / 显示器 / GPU），所以在 CI 和
本地都能跑；如果你的改动需要真实窗口才能验证，请**同时在 PR 描述里说明
手工验证步骤与结果**。

## 代码风格

- 目标 Python 3.9+，但类型标注使用 `from __future__ import annotations`，
  因此可以写 `int | None`。
- 行宽 100；由 `ruff` 强制（见 `pyproject.toml`）。
- 注释用中文，解释**为什么**而不是**做了什么** —— 代码本身已经说明后者。
  涉及平台坑点的地方请写清"踩过的具体现象"，这对后来者最有用。
- 新增对外符号必须同时更新 `__all__` 与 `README.md` 的 API 表。

## 测试约定

- 文件放 `tests/`，命名 `test_*.py`。
- 抓屏逻辑的测试用 `WindowGrabber.__new__()` 手工装配 state +
  伪造 `Frame` 对象（见 `tests/test_capture_offline.py` 的 `_make` / `_FakeFrame`），
  **不要**为了让测试通过而去开真实窗口。
- 回调路径的测试必须同时断言"异常没往上抛"和"计数器涨了" ——
  只测前者会漏掉静默丢帧。
- 修 bug 时先写一个能复现的失败测试，再改代码。

## 提交信息

采用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：

```
feat: 支持按 pid 选择窗口
fix: 窗口缩放后帧槽尺寸未重建导致回调丢帧
docs: 补充 UIPI 的排查说明
test: 覆盖 on_stale=raise 下的黑帧路径
refactor: 抽出 _compute_roi
chore: 升级 windows-capture 下限
```

## PR 检查清单

- [ ] 新增/修改的行为有测试覆盖
- [ ] `pytest -q` / `ruff check .` / `python -m build` 全绿
- [ ] 新增的失败路径有明确原因（异常消息或 WARNING 日志）
- [ ] 对外 API 变化已更新 `README.md`，并在 `CHANGELOG.md` 的 `[Unreleased]` 记录
- [ ] 涉及平台行为变化时，写明测试的 Windows 版本

## 报告缺陷

请附上这些信息，能省掉一半来回：

1. Windows 版本（`winver`）、Python 版本、`pywgc.__version__`；
2. `g.stats` 的完整输出；
3. 打开 INFO 日志后的相关片段（降级 / 自愈 / `SetWindowPos` 失败）；
4. `available_backends()` 的结果；
5. 目标窗口是否**提权运行**（UIPI 是最高频的"莫名失败"来源）。

## 许可证

提交即表示同意以 [MIT](LICENSE) 许可发布你的贡献。
