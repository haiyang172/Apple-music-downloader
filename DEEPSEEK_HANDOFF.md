# 影音下载台 · DeepSeek 开发交接

交接日期为 2026-11-19（悉尼）。在 Windows 上打开 `D:\AMDL` 开发。这个目录是独立 Git 仓库，包含当前源码、图标、打包脚本、离线组件和已发布的 v0.3.38 安装包。旧 C 盘工作区留作备份。应用设置的音乐输出目录仍由用户选择。

## 版本和入口

| 项目 | 交接时的值 |
| --- | --- |
| 已发布供日常使用 | [v0.3.38](https://github.com/haiyang172/Apple-music-downloader/releases/tag/v0.3.38)，本地 `installer/dist/0.3.38/MediaDownloadDesk-Setup.exe` |
| 已发布源码基线 | `0321f6a5a950aabea3930609d2ec70f5531fedb1`， GitHub 分支 `media-download-desk` |
| D 盘工作分支 | `handoff/v0.3.39` |
| 未发布的 v0.3.39 功能快照 | `92684f7d212d63d0f4dbfaec3feb2cbc40f4bb51`，该提交包含下表 9 个文件 |
| 源码入口 | 双击 `Run.cmd` 使用 D 盘随附的 Python 运行 `Studio.py`。当前源码包含尚待修复的标签编辑功能 |
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

## 当前可以复现的问题

1. 在 `D:\AMDL` 运行下面的标签测试。脚本会生成临时音频并使用独立的测试资料目录。2026-11-19 在 D 盘副本中实测，程序能读取五种格式，首次调用 `TagEditStore.apply()` 时失败。
2. 实际错误为 `PermissionError: [WinError 5] Access is denied: 'C:\\'`。调用链是 `apply → _locked → _directory → path.mkdir()`。源码在 D 盘，但测试资料目录位于 Windows 的临时目录，所以错误中出现 C 盘。当前实现遍历完整父路径时也尝试创建已存在的盘符根，只捕获了 `FileExistsError`。
3. 接手时先修复普通目录和盘符根的判定，再运行同一条测试。保留对符号链接、目录联接和异常文件的检查；不需要修改系统权限。此失败发生在首次原文件写入之前，五格式写入、重开历史、实际撤销的后续断言尚未跑完。

```powershell
Set-Location -LiteralPath 'D:\AMDL'
.\handoff\Build-Local.ps1 -CheckOnly
.\runtime\python.exe -I -B .\handoff\verify_tags.py
```

`-CheckOnly` 已在 D 盘通过，验证当前源码、Python、Mutagen、Tk、下载后端以及主要构建输入。`verify_tags.py` 直接执行安装包检查脚本内的标签用例，避免使用另一份过时断言。

## 验收顺序

- **真实格式和音质。** 修复首个错误后，让上述五格式测试全部通过。检查有/无标签、中文、单字段清除。对带封面、歌词和无关标签的样本直接比对这些数据，核对 MP3/WAV 的 ID3 版本和最终音频内容。现有测试覆盖了解码样本和撤销后的完整字节，不足以单独证明所有标签在修改期间均保留。
- **撤销和异常恢复。** 从已完成批次重新打开应用后撤销，原文件须逐字节相同。用测试副本模拟外部修改、磁盘空间不足及中途取消，确保不覆盖后续变更。当前 `list_history()` 不会改写原音频；`apply()` 和 `undo()` 才会触发中断恢复。界面的撤销按钮只接受 `applied`；若只有中断批次，缺少直达恢复入口。验证这个场景，并给用户明确的恢复路径。
- **界面和入口。** 此前使用模拟存储验证过字段勾选、空值清除、后台结果返回、繁忙时关闭、缩放和底部按钮。真正的文件写入与撤销仍须从「本地转码」和「下载队列」入口分别走通。保持平台侧栏和原有下载任务行为。
- **Windows 安装包。** 定向测试通过后用下面命令打候选包并运行完整安装检查。已有测试在临时目录解包，覆盖程序启动、资料库、下载队列、转码等流程。全新 Windows 的标准用户安装、覆盖升级和卸载属于不同验收项，仍待实机或虚拟机验证。

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

## 给 DeepSeek 的第一条任务

> 请从 D:\AMDL 的 handoff/v0.3.39 分支接手影音下载台。先读 DEEPSEEK_HANDOFF.md，再复现 verify_tags.py 的 WinError 5。修复目录检查，验证五种音频格式的标签修改、外部改写保护和重启后撤销，并补上中断批次的恢复验证。请在临时测试文件上操作，报告实际通过的用例和仍待验证的项目。全部通过后再构建 v0.3.39 候选安装包。
