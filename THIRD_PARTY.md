# 第三方组件

amdl 通过本地进程调用 apmyx 下载引擎、Wrapper Lite 和 FFmpeg。源码仓库包含应用与构建脚本。第三方程序适用各自的许可。

- [apmyx-gui](https://github.com/rwnk-12/apmyx-gui) 提供下载后端。
- [apple-music-downloader](https://github.com/zhaarey/apple-music-downloader) 和 [wrapper](https://github.com/zhaarey/wrapper) 是 AMDL 下载引擎和可选服务的上游。
- [Wrapper Lite v3.0.4](https://github.com/itouakirai/wrapper/releases/tag/v3.0.4) 和其发行版内的 [QEMU](https://www.qemu.org/) 提供本地登录服务。安装包可附带未修改的官方 ZIP。
- [FFmpeg](https://ffmpeg.org/legal.html)、[GPAC](https://github.com/gpac/gpac) 和 [Bento4](https://github.com/axiomatic-systems/Bento4) 提供音频处理、封装和相关命令行工具。
- [Python/Tk](https://www.python.org/psf/license/)、[PyYAML](https://github.com/yaml/pyyaml) 和 [Mutagen](https://github.com/quodlibet/mutagen) 提供运行时、配置解析和音频标签支持。
- [Inno Setup](https://jrsoftware.org/isinfo.php) 用于编译 Windows 安装包。可选的 [LxRunOffline](https://github.com/DDoSolitary/LxRunOffline) 用于启动已有的 WSL 环境。

如要公开分发自己构建的安装包，请核对该包内具体二进制版本的许可、通知和对应源代码要求。应用界面的布局和设置方式参考了 [Mic Studio](https://github.com/haiyang172/Mic-studio)。
