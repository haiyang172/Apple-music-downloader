# 第三方组件

amdl 通过本地进程调用 apmyx 下载引擎、Wrapper Lite 和 FFmpeg。源码仓库包含应用与构建脚本。第三方程序适用各自的许可。

- [apmyx-gui](https://github.com/rwnk-12/apmyx-gui) 提供下载后端。
- [apple-music-downloader](https://github.com/zhaarey/apple-music-downloader) 和 [wrapper](https://github.com/zhaarey/wrapper) 是 AMDL 下载引擎和可选服务的上游。
- [Wrapper Lite v3.0.4](https://github.com/itouakirai/wrapper/releases/tag/v3.0.4) 和其发行版内的 [QEMU](https://www.qemu.org/) 提供本地登录服务。安装包可附带未修改的官方 ZIP。
- [FFmpeg](https://ffmpeg.org/legal.html)、[GPAC](https://github.com/gpac/gpac) 和 [Bento4](https://github.com/axiomatic-systems/Bento4) 提供音频处理、封装和相关命令行工具。
- [Python/Tk](https://www.python.org/psf/license/)、[PyYAML](https://github.com/yaml/pyyaml) 和 [Mutagen](https://github.com/quodlibet/mutagen) 提供运行时、配置解析和音频标签支持。
- [Microsoft WebView2 SDK 1.0.4314.35](https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.4314.35) 和 [WebView2 Runtime](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution) 提供 Apple Music 官方网页登录窗口。SDK 的许可和通知保存在安装目录的 apple-web-login 文件夹中，完整微软离线运行时安装程序随包提供。
- [qrcodegen 1.8.0](https://pypi.org/project/qrcodegen/1.8.0/) 在本机生成 B 站和网易云音乐的登录二维码，MIT 许可随模块源码提供。
- [yt-dlp 2026.10.26](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.10.26) 提供 YouTube 下载。安装包使用官方 Python ZIP 应用及其内含的 [yt-dlp-ejs](https://github.com/yt-dlp/ejs)，并附带 [QuickJS-NG v0.18.0](https://github.com/quickjs-ng/quickjs/releases/tag/v0.18.0)。相应许可随包提供。 [yt-dlp 许可说明](https://github.com/yt-dlp/yt-dlp#licensing)
- QQ 音乐和网易云音乐的扫码、歌单和歌曲接口参考了 [MusicUnlock v2.3.0](https://github.com/Lin-M11/MusicUnlock/tree/v2.3.0)。应用使用独立的 Python 实现和 Windows 凭据保护。 [MusicUnlock MIT 许可](https://github.com/Lin-M11/MusicUnlock/blob/v2.3.0/LICENSE)
- [Inno Setup](https://jrsoftware.org/isinfo.php) 用于编译 Windows 安装包。可选的 [LxRunOffline](https://github.com/DDoSolitary/LxRunOffline) 用于启动已有的 WSL 环境。

如要公开分发自己构建的安装包，请核对该包内具体二进制版本的许可、通知和对应源代码要求。应用界面的布局和设置方式参考了 [Mic Studio](https://github.com/haiyang172/Mic-studio)。
