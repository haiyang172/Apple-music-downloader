<img src="assets/AMDLStudio.png" alt="amdl" width="72" height="72">

# amdl

Windows 桌面下载和转码工具。界面、Wrapper Lite 登录服务、AMDL 下载引擎和 FFmpeg 由同一个程序管理。

## 使用

1. 运行本地构建的 `amdl-setup.exe`，或双击便携目录里的 `amdl.exe`。
2. 在「账户」输入 Apple 账号和密码。Apple 要求双重认证时输入收到的验证码。登录成功后，Wrapper Lite 会在当前 Windows 用户目录中保存会话。amdl 默认在下次打开时恢复该会话。需要换账号时可先退出登录。
3. 在「下载」选择保存目录，再粘贴 Apple Music 的歌曲、专辑或歌单链接，每行一个。保存目录可以是任意可写的本地或外接磁盘。若填写分类，文件直接放在该分类内。
4. 选择 AAC-LC、ALAC 或 Dolby Atmos 作为下载音质。在「设置」选择保留原音轨，或用 FFmpeg 转换成 FLAC、MP3、AAC、WAV、Opus。MP3 提供 128、192、256 和 320 kbps。

保留原音轨时只重新封装。FLAC 和 WAV 保留解码出的声道和采样精度。MP3、AAC 和 Opus 会把多声道音源混成立体声。有损源转换为 FLAC 后仍保留原有音质。应用根据曲目时长和实际编码检查每个下载文件，并检查输出可完整解码。重复内容会被识别，同名的不同文件会使用后缀保存。

## 组件

安装包带有 Python/Tk、apmyx 下载后端、FFmpeg 及其所需的音频工具。登录服务使用 [Wrapper Lite](https://github.com/itouakirai/wrapper)，由 amdl 按需启动。服务只监听本机回环接口。首次启动会校验并解压随安装包提供的官方组件，源码运行时也可从固定的官方发行版获取。音质档位受账户和歌曲实际供应影响。

旧版 apmyx 会话仍可在「账户 → 兼容会话设置」导入。兼容模式的 ALAC 和 Atmos 可以使用已有的 `u22-amdl` 或在「组件」指定兼容服务。安装向导中的 WSL 1 选项用于这一模式。 [Microsoft WSL 文档](https://learn.microsoft.com/en-us/windows/wsl/)

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

构建脚本会从本机依赖目录整理便携版，再编译安装包。使用 `-WrapperLiteArchive` 指定 [v3.0.4 Windows x64 ZIP](https://github.com/itouakirai/wrapper/releases/tag/v3.0.4) 可把已校验的登录组件放入安装包。生成的文件放在 `installer\dist`。第三方组件适用各自的许可。 [第三方组件](THIRD_PARTY.md)

本项目的应用源码使用 [MIT 许可](LICENSE)。
