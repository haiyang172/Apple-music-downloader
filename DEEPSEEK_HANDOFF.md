# 影音下载台 · DeepSeek 开发交接

交接日期为 2026-11-19（悉尼）。在 Windows 上打开 `D:\AMDL` 开发。这个目录是独立 Git 仓库，包含当前源码、图标、打包脚本、离线组件和已发布的 v0.3.38 安装包。旧 C 盘工作区留作备份。应用设置的音乐输出目录仍由用户选择。

## 版本和入口

| 项目 | 交接时的值 |
| --- | --- |
| 已发布供日常使用 | [v0.3.38](https://github.com/haiyang172/Media-Download-Desk/releases/tag/v0.3.38)，本地 `installer/dist/0.3.38/MediaDownloadDesk-Setup.exe` |
| 已发布源码基线 | `0321f6a5a950aabea3930609d2ec70f5531fedb1`， GitHub 分支 `media-download-desk`（2026-11-19 已推进到本轮的 v0.3.39 修复提交） |
| D 盘工作分支 | `handoff/v0.3.39` |
| 未发布的 v0.3.39 功能快照 | `92684f7d212d63d0f4dbfaec3feb2cbc40f4bb51`，该提交包含下表 9 个文件 |
| 2026-11-19 修复轮 | `156ada6`（标签目录与 Vorbis 注释缺陷、中断恢复与扩展测试）、`68f37bf`（PowerShell 5.1 构建兼容、仓库改名），候选包已构建并通过完整安装检查 |
| 源码入口 | 双击 `Run.cmd` 使用 D 盘随附的 Python 运行 `Studio.py`。标签编辑与中断恢复已可用 |
| 产品范围 | [产品策划案](docs/影音下载台-产品策划案.md)、[应用使用说明](README.md) |

GitHub 的旧 `main` 与当前产品源码有较大差异。查看这一轮的完整功能改动可运行 `git diff 0321f6a 92684f7`。D 盘分支和交接提交保存在本地。

## v0.3.39 逐文件更新

| 文件 | 改动和复查入口 |
| --- | --- |
| [tag_edit.py](tag_edit.py) · 新增 | `TagEditStore` 负责读取标签、校验文件指纹、暂存、批量写入、保存原件及撤销。支持 MP3、M4A、FLAC、WAV、Opus。单批 1–200 个文件。首要问题在 `_directory()`。重点复查 `_install()`、`_restore()`、`_history()`、`apply()`、`undo()` 的失败和中断路径。 |
| [tag_edit_ui.py](tag_edit_ui.py) · 新增 | `TagEditor` 提供「修改标签」「修改历史」两页。读取文件后默认不勾选字段。勾选的空值会清除字段。后台读写通过队列把结果交给界面。窗口在写入过程中收到关闭请求时，要等取消或还原操作结束。 |
| [Studio.py](Studio.py) | 新增标签编辑窗口及下载队列右键入口。`edit_saved_tags()` 收集所选任务的音频文件，`edit_audio_tags()` 打开窗口。数据放在当前 Windows 用户的 `%LOCALAPPDATA%\amdl\data\tag-edits`。主窗口关闭时等待活动写入结束。 |
| [local_convert_ui.py](local_convert_ui.py) | 「本地转码」右上角新增「编辑标签」。`edit_tags()` 将选中的支持格式交给同一个编辑器。无文件时也可从编辑器添加文件或浏览历史。 |
| [installer/Build.ps1](installer/Build.ps1) | 安装包源码清单新增两个标签模块。 |
| [installer/Test-Installer.ps1](installer/Test-Installer.ps1) | 末尾增加真实五格式的读写、解码样本、撤销后原文件字节，以及从本地页面打开编辑器、修改勾选字段、重新打开历史后撤销的测试。搜索 `音频标签测试` 可定位。 |
| [app-version.txt](app-version.txt) | 源码版本从 0.3.38 改为 0.3.39。 |
| [installer/Setup.iss](installer/Setup.iss) | 安装器的默认版本号同步为 0.3.39。 |
| [README.md](README.md) | 「本地转码」中加入标签编辑使用说明。描述的是开发版本的目标行为，端到端验收状态见下文。 |

## 当前可以复现的问题 → 本轮已修复（2026-11-19）

1. ~~首次调用 `TagEditStore.apply()` 失败~~ 已修复。`_directory()` 改为先判断路径是否已存在，只对不存在的路径调用 `mkdir()`，盘符根（如 `C:\`）不再触发 `PermissionError: [WinError 5]`。每一级仍执行 `_plain_directory()`，对符号链接、目录联接和异常文件类型的检查全部保留。
2. ~~`VCommentDict` 报 `TypeError: pop expected at most 1 argument, got 2`~~ 已修复。FLAC/Opus 的 Vorbis 注释继承自 `list`，`list.pop` 覆盖了 `DictMixin.pop`；`_edit()` 的非 MP3/MP4 分支改用 `key in tags` + `del tags[key]`。该错误在 WinError 5 修复后才暴露。
3. `handoff/Build-Local.ps1` 的 `-CheckOnly` 一行式在 Windows PowerShell 5.1 下内嵌双引号会被剥掉（`SyntaxError`）。已改写为无引号形式（`root.joinpath(sys.argv[2])`），5.1 和 7 均可运行。
4. 同类问题在 `installer/Build.ps1` 的 qrcodegen 解包与 Wrapper Lite 版本钉读取中也存在（`"qrcodegen.py"`、`("VERSION", ...)` 被剥掉），已改为通过 `sys.argv` 传递。`installer/Test-Installer.ps1` 是 UTF-8 无 BOM，5.1 会把第 77 行的中文 `'影音下载台'` 读成乱码导致解析失败，已加 UTF-8 BOM。以上改动在 PowerShell 5.1 和 7 下均可运行。

## 新增功能与扩展验证

- [tag_edit.py](tag_edit.py) 新增 `TagEditStore.recover()`：在锁内执行 `_history(recover=True)`，完成中断批次的回滚或继续撤销。
- [tag_edit_ui.py](tag_edit_ui.py) 历史页新增中断状态「写入中断」「撤销中断」「准备中断」，选中这类批次时出现「恢复中断批次」按钮，恢复后刷新历史并还原原文件。
- [installer/Test-Installer.ps1](installer/Test-Installer.ps1) 标签段扩展了以下用例（`verify_tags.py` 直接执行，已全部通过）：
  - 无标签文件、单字段清除、中文写入；
  - 五格式带封面、歌词和无关标签的样本：修改后封面/歌词/无关标签与解码音频逐项比对不变，MP3 核对 ID3 v2.3 与 v2.4 版本、WAV 核对 v2.4；
  - 外部改写保护：预览后改动文件时 apply 拒绝；已应用批次撤销前被改动时 undo 拒绝并标记 conflict，均不覆盖后续更改；
  - 中途取消：准备阶段取消不动原文件（cancelled），写入阶段取消回滚全部文件（rolled_back）；
  - 中断批次：模拟写入中断（applying）、撤销中断（undoing）与恢复期间外部改写三种日志状态，`recover()` 分别还原、完成撤销或标记 conflict；
  - 界面恢复入口：历史页选中「写入中断」批次，点「恢复中断批次」后原文件逐字节还原；
  - 下载队列入口：右键路径打开同一编辑器，应用后撤销逐字节还原。
- [README.md](README.md) 在「本地转码」一节补充中断批次的恢复说明。

复现命令不变：

```powershell
Set-Location -LiteralPath 'D:\AMDL'
.\handoff\Build-Local.ps1 -CheckOnly
.\runtime\python.exe -I -B .\handoff\verify_tags.py
```

## 验收顺序

- **真实格式和音质。** 修复首个错误后，让上述五格式测试全部通过。检查有/无标签、中文、单字段清除。对带封面、歌词和无关标签的样本直接比对这些数据，核对 MP3/WAV 的 ID3 版本和最终音频内容。现有测试覆盖了解码样本和撤销后的完整字节，不足以单独证明所有标签在修改期间均保留。
- **撤销和异常恢复。** 已通过。重启后撤销原文件逐字节相同；外部修改时 apply/undo 均停止并标记 conflict 且不覆盖；准备与写入阶段取消分别保留原文件并回滚；模拟的 applying/undoing 中断批次由 `recover()` 和界面「恢复中断批次」按钮还原，恢复期间的外部更改受到保护。
- **界面和入口。** 已通过。「本地转码」和「下载队列」两个入口均已用真实文件走通应用→历史→撤销，文件名不变、平台侧栏和下载任务不受影响。字段勾选、空值清除、缩放和底部按钮此前已用模拟存储验证，本轮未改动这些路径。
- **Windows 安装包。** 已于 2026-11-19 构建候选包并通过完整安装检查：`.\handoff\Build-Local.ps1` 全量构建成功，`.\installer\Test-Installer.ps1 -Setup '.\installer\dist\0.3.39\MediaDownloadDesk-Setup.exe'` 全部段落通过（含新增标签用例）。候选包 SHA-256 为 `6b0b2ae5b2720967d93fca22d1f665df8d2aca65539fd4e8a84f63173e42853a`，约 500 MB，仍只在本地 `installer/dist/0.3.39/`。全新 Windows 的标准用户安装、覆盖升级和卸载属于不同验收项，仍待实机或虚拟机验证。

```powershell
Set-Location -LiteralPath 'D:\AMDL'
.\handoff\Build-Local.ps1
.\installer\Test-Installer.ps1 -Setup '.\installer\dist\0.3.39\MediaDownloadDesk-Setup.exe'
```

[Build-Local.ps1](handoff/Build-Local.ps1) 将现有构建脚本的 Python、第三方库、后端、媒体工具、Wrapper Lite 和 Inno Setup 路径指向本项目。Windows 自带的 .NET 编译器仍从系统目录读取。交接时没有构建或发布 v0.3.39 安装包。

## v0.3.38 基线及未完成范围

| 模块 | 已有功能与验证边界 |
| --- | --- |
| Apple Music | 官方登录、缓存资料库、滚动列表、排序和逐曲队列。用户已确认登录后自动显示个人资料库。 |
| B 站 | 识别歌名与歌手、按单层目录分类、分 P、合集、收藏夹、断点续传。v0.3.38 新增单条下载预览，可调整音轨、保存格式和文件名。平台不提供源文件大小时显示未知。 |
| QQ 音乐、网易云音乐 | 扫码、私有歌单缓存、搜索、歌曲入队、来源音质及保存格式选择。用户尚未完成真实账号的个人歌单和完整歌曲下载测试。 |
| YouTube、抖音 | YouTube 支持公开搜索和播放列表按需加载、完整入队。抖音目前通过官方浏览器保存后导入。 |
| 后续工作 | 产品策划案中的直播监控录制、歌单追更、曲库去重审阅等仍待开发。用户已暂停当前开发目标，本次保存的是供下一位开发者使用的交接点。 |

v0.3.38 安装包 SHA-256 为 `af4468ea5198022e99bfc92825802ef64760886f18589284104057623982bb70`。交接时对照原工作区逐文件校验了 88 个源码和资源文件、3,292 个运行和构建文件（含该安装包）。校验清单位于本机 `.git/info/d-drive-handoff-sha256.json`。

## 2026-11-20 开发轮（v0.3.40 进行中）

- **网易云修复。** 2026 年网易云下线了匿名取流（`outer/url` 全量 404、匿名 weapi 无地址），现在未登录下载直接提示「需要登录」。登录请求带官方 `realIP` 参数（国内地址）绕过海外节点风控；播放地址 v1 路由改为现行网页端的 AES-ECB eapi 信封（CNG ECB 已用 FIPS-197 验证），旧 bitrate 路由改为 weapi POST。登录链路 begin/poll 已实测可用，eapi 取流需用户扫码后实测。
- **Qt 迁移。** `StudioQt.py` + `qtui/` 包：左侧导航、浅深主题（持久化到 `dark_mode`）。`qtui/core.py` 复用共享的队列/引擎/登录服务与 `run_tasks` 管线，下载队列页可操作（开始/取消/移除/刷新、实时状态），本地转码、标签编辑（`qtui/tag_editor.py` 复用 `TagEditStore`）、YouTube（`qtui/youtube.py`）、QQ/网易云公开搜索、链接解析与个人歌单（`qtui/music.py`）、B 站分 P、合集与个人收藏夹（`qtui/bilibili.py`）、Apple Music 公开搜索（`qtui/apple.py`）、扫码登录账户页（`qtui/account.py`）、Apple 账户表单（`qtui/apple_account.py`）、Apple 资料库连接与个人歌曲（`qtui/library.py`）、下载预览（`qtui/preview.py`）均已迁移并可入队。**目标清单中的页面已全部迁移完成。** `RunQt.cmd` 启动，安装包已集成 PySide6-Essentials 6.12.0（约 210 MB，THIRD_PARTY.md 已记录许可），安装检查用 `QT_QPA_PLATFORM=offscreen` 验证骨架、本地转码、标签编辑、五平台入队、扫码登录、Apple 登录与资料库、预览、个人歌单与收藏夹。Tkinter 仍是默认界面（`RunQt.cmd` 为可选新界面）。
- **Tkinter 增强。** 窗口大小/位置/最大化状态在关闭时保存、启动时恢复；一批任务结束且窗口不在前台时任务栏闪烁加提示音（`notify_done` 设置）；**深色模式**：设置页勾选「深色模式」后下次启动切换深色调色板（与 Qt 壳共享 `dark_mode` 键）。
- **歌单追更。** [playlist_watch.py](playlist_watch.py) 按批次保存已见曲目日志；QQ/网易云歌单页新增「追更新歌」按钮只入队新增曲目，普通入队自动更新日志。
- **曲库去重审阅。** [dedup_review.py](dedup_review.py) + [dedup_review_ui.py](dedup_review_ui.py)：本地转码页头「去重审阅」扫描目录，按内容完全一致分组，默认保留每组一个、其余标记删除，删除前复核指纹、被改动的文件跳过。
- **待办。** 抖音应用内直下（需 a_bogus 签名或 Edge CDP 浏览器方案，下轮继续）；Qt 逐页迁移（Apple/B站/QQ/网易云/YouTube、账户、预览、本地转码、标签编辑）；网易云 eapi 取流与登录需真实账号验证；安装包仍是 0.3.39 版本号，功能收敛后升 0.3.40 并发布。

## 给 DeepSeek 的第一条任务（已接手的交接点）

> 请从 D:\AMDL 的 handoff/v0.3.39 分支接手影音下载台。先读 DEEPSEEK_HANDOFF.md，再复现 verify_tags.py 的 WinError 5。修复目录检查，验证五种音频格式的标签修改、外部改写保护和重启后撤销，并补上中断批次的恢复验证。请在临时测试文件上操作，报告实际通过的用例和仍待验证的项目。全部通过后再构建 v0.3.39 候选安装包。

以上内容已于 2026-11-19 完成：两个存储层缺陷修复、`verify_tags.py` 全绿（含新增的中断恢复与双入口用例）、UI 恢复入口上线、v0.3.39 候选安装包构建成功且 `Test-Installer.ps1` 完整安装检查通过。剩余项：全新 Windows 的标准用户安装、覆盖升级、卸载的实机或虚拟机验收。
