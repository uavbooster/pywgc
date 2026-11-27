# 发布流程

本文件记录 `pywgc` 的发布步骤。**第 1 节的顺序不能颠倒**，否则会在 PyPI 上留下
无法修正的版本。

## 0. 先分清两个"地址"

| | 何时存在 | 谁来填 |
| --- | --- | --- |
| PyPI 包页面 `https://pypi.org/project/pywgc/` | 上传成功后自动生成 | 不用填 |
| 源码仓库 `https://github.com/uavbooster/pywgc` | 需自行创建 | **要**，填进 `pyproject.toml`（已填） |

`[project.urls]` 指的是**源码仓库**，不是 PyPI 页面。

## 1. 顺序：先建仓库，再填 urls，最后上传

**PyPI 对已发布版本的元数据是只读的。** 版本一旦上传，`project.urls` 就永久定死；
想补链接只能 yank 该版本并发布新版本，而旧版本会永久保留
（`pip install pywgc==0.1.0` 永远能装到那个没有源码链接的版本）。

因此：

```
建远程仓库  →  填 [project.urls]  →  build  →  twine check  →  上传
```

仓库通常选 GitHub 或 Gitee，建**空仓库**（不要勾选 README / .gitignore / license，
否则首次 push 会冲突）。

### 1.1 在 GitHub 建空仓库

1. 登录 github.com，右上角 `+` → **New repository**
2. **Repository name** 填 `pywgc` —— 要和 `pyproject.toml` 里的 `name` 一致
3. 选 **Public**（要发到 PyPI，源码必然公开）
4. **三个初始化选项全部不勾**：Add a README file / Add .gitignore / Choose a license
5. 点 **Create repository**，页面就会给出 `https://github.com/uavbooster/pywgc`

> 为什么三项都不能勾：本地已经有 `README.md`、`.gitignore`、`LICENSE`。
> 勾了会在远端先产生一个提交，本地首次 push 直接被拒
> （`! [rejected] main -> main (fetch first)`），只能用
> `--allow-unrelated-histories` 强合，平白多一个无意义的合并提交。

### 1.2 首次推送

```powershell
winget install --id Git.Git -e     # 若尚未安装 Git（会弹 UAC）
cd c:\sf2ai\packages\pywgc

git init
git config user.name  "Tang Cheng"
git config user.email "362015863+uavbooster@users.noreply.github.com"
git add .
git commit -m "Initial commit: pywgc 0.1.0"
git branch -M main
git remote add origin https://github.com/uavbooster/pywgc.git
git push -u origin main
```

推送前先确认 ignore 生效，输出里**不该**出现 `dist/`、`.pytest-libs/`、
`.pytest_cache/`：

```powershell
git status --short
```

认证：GitHub 自 2021-08 起不接受账号密码。Git for Windows 自带 Credential
Manager，`git push` 时弹一次浏览器授权即可，之后自动记住。若改用 Token，
建 Personal Access Token（classic），勾 `repo` 权限，把它当密码填。

注意：`packages/pywgc` 如果将来要和整个 `c:\sf2ai` 一起纳入版本控制，这个
独立的 `.git` 会变成嵌套仓库。本包的定位是独立发布，通常只发布本目录即可。

拿到地址后只需改一处：`pyproject.toml` 的 `[project.urls]`（目前整段被注释，
取消注释并填值即可）。README 顶部的四个徽章都指向 `pypi.org` 与 `LICENSE`，
不含占位符，发布后自动生效，不需要改。

## 2. 首次上传前

```powershell
pip install build twine
python -m build
python -m twine check dist/*
```

包名可用性查 JSON API 而不是网页（网页可能被 Cloudflare 拦成验证页，
看着像 404 但其实是 403）：

```powershell
# 404 = 名字可用；200 = 已被占用
curl.exe -s -o NUL -w "%{http_code}`n" https://pypi.org/pypi/pywgc/json
curl.exe -s -o NUL -w "%{http_code}`n" https://test.pypi.org/pypi/pywgc/json
```

> 2026-11 实测：两个索引均返回 404，`pywgc` 名字未被占用。PyPI 名字先到先得，
> 建议尽早占位——但**占位就意味着元数据定死**，所以仍要先完成第 1 节。

`twine check` 必须两个包都 `PASSED` —— 它会渲染 README 并校验元数据，
长描述里的坏链接、非法分类器都会在这一步暴露。

## 3. 先发 TestPyPI

正式上传前务必先在 TestPyPI 走一遍，避免把错误版本推上正式索引：

```powershell
python -m twine upload --repository testpypi dist/*

# 用一个干净的 venv 验证装得上（PowerShell 语法，注意 PS 5.1 不支持 &&）
$v = Join-Path $env:TEMP 'pywgc-t'
python -m venv $v
& "$v\Scripts\pip.exe" install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ pywgc
& "$v\Scripts\python.exe" -c "import pywgc; print(pywgc.__version__)"
Remove-Item -Recurse -Force $v
```

注意 `--extra-index-url` 不能省：TestPyPI 上没有 `numpy`、`windows-capture`
这些真实依赖，缺了它会在装依赖时失败（而包本身其实没问题）。同理，
从 TestPyPI 装的包**不要**留在正式环境里。

TestPyPI 的包名与正式索引**彼此独立**，可以在那边先占位试跑。

## 4. 正式上传

```bash
python -m twine upload dist/*
```

认证用 API token（不要用密码）：用户名填 `__token__`，密码填 `pypi-...`。
建议写进 `$env:USERPROFILE\.pypirc`：

```ini
[pypi]
username = __token__
password = pypi-xxxxxxxxxxxxxxxxxxxx
```

## 5. 上传之后

- **不能删除，只能 yank。** yank 后该版本不再被新解析选中，但已锁定的依赖仍能下载。
  所以宁可在 TestPyPI 多试，也不要靠上传纠错。
- **不能覆盖同版本号。** 任何代码或元数据变更都必须递增版本号。
- 验证线上结果：

```powershell
pip index versions pywgc
pip download pywgc --no-deps -d "$env:TEMP\dl"   # 确认 wheel 与 sdist 都在
```

## 6. 版本号规则（SemVer）

| 变更类型 | 版本 |
| --- | --- |
| 修 bug、补文档、调整日志文案 | PATCH `0.1.1` |
| 新增 API / 新参数（向后兼容） | MINOR `0.2.0` |
| 破坏性变更（改签名、改返回结构） | `1.0.0`（0.x 阶段可用 MINOR） |

同步三处：`pyproject.toml` 的 `version`、`src/pywgc/__init__.py` 的
`__version__`、`CHANGELOG.md`。三者不一致会导致
`importlib.metadata.version()` 与 `pywgc.__version__` 打架。

## 7. 发布检查清单

- [ ] 远程仓库已建，`[project.urls]` 已填真实地址（唯一需要手工替换的占位符）
- [ ] 版本号在三处一致
- [ ] `CHANGELOG.md` 的 `[Unreleased]` 已改为本次版本号 + 日期
- [ ] `pytest -q` 全绿
- [ ] `ruff check .` 无告警
- [ ] `python -m build` 产出 wheel + sdist
- [ ] `python -m twine check dist/*` 两个都 PASSED
- [ ] TestPyPI 安装验证通过
- [ ] `dist/` 里没有混入上一个版本的残留文件
- [ ] git tag 与版本号一致（如 `v0.1.0`）
