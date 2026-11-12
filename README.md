# AMDL Studio

Windows 桌面下载和转码工具。下载页集中放置链接、音质、保存位置和进度。账户、转码与本地组件各有设置页。

## 使用

1. 运行本地构建的 `AMDL-Studio-Setup.exe`，或双击便携目录里的 `AMDLStudio.exe`。
2. 在「账户」选择「导入 apmyx」，或用「打开 Apple Music 登录」在浏览器登录，再把当前会话的 `media-user-token` 填入账户页。填写账号对应的两字母地区代码。Windows 为当前用户加密保存令牌。
3. 在「下载」选择保存目录，再粘贴 Apple Music 的歌曲、专辑或歌单链接，每行一个。保存目录可以是任意可写的本地或外接磁盘。若填写分类，文件直接放在该分类内。
4. 选择 AAC-LC、ALAC 或 Dolby Atmos 作为下载音质。在「设置」选择保留原音轨，或用 FFmpeg 转换成 FLAC、MP3、AAC、WAV、Opus。MP3 提供 128、192、256 和 320 kbps。

保留原音轨时只重新封装。FLAC 和 WAV 保留解码出的声道和采样精度。MP3、AAC 和 Opus 会把多声道音源混成立体声。有损源转换为 FLAC 后仍保留原有音质。应用根据曲目时长和实际编码检查每个下载文件，并检查输出可完整解码。重复内容会被识别，同名的不同文件会使用后缀保存。

## 组件

安装包带有 Python/Tk、apmyx 下载后端、FFmpeg 及其所需的音频工具。AAC-LC 使用已有的 Apple Music 会话。ALAC 和 Atmos 需要可用的本地音频服务，并受账户和歌曲实际供应影响。

如已注册 `u22-amdl`，应用会按需启动其服务，也可以在「组件」选择本机已有的服务程序。安装向导可选启用 Windows 的 WSL 1 功能。首次安装高音质服务仍需要准备 `u22-amdl` 环境或提供兼容的本地服务。 [Microsoft WSL 文档](https://learn.microsoft.com/en-us/windows/wsl/)

## 本地构建

支持 Windows 10 2004 或更新版本，x64。源码可在装有 apmyx、Python 3.10+、Tk、PyYAML、Mutagen 及所需音频工具的机器运行。

```powershell
python -m pip install -r requirements.txt
python Studio.py
```

在原 AMDL 离线包中也可以用 `Run.cmd` 启动。需要构建安装包时，在 PowerShell 中运行下面的脚本。

```powershell
.\installer\Build.ps1
```

构建脚本会从本机依赖目录整理便携版，再编译安装包。可用 `Get-Help .\installer\Build.ps1 -Detailed` 或查看脚本顶部的参数，显式指定依赖目录。生成的文件放在 `installer\dist`。构建使用第三方组件的本地副本，源码仓库包含应用和构建脚本。第三方组件适用各自的许可。 [第三方组件](THIRD_PARTY.md)

本项目的应用源码使用 [MIT 许可](LICENSE)。
