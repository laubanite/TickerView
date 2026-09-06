# 第三方组件与许可 / Third-Party Notices

TickerView 依赖以下开源组件。许可证以各上游仓库的 LICENSE 为准，发布前请逐一核对。

## 运行时依赖(Python)
| 组件 | 用途 | 许可证(以官方为准) |
|---|---|---|
| [akshare](https://github.com/akfamily/akshare) | 行情/数据抓取 | MIT |
| [pandas](https://github.com/pandas-dev/pandas) | 数据处理 | BSD-3-Clause |
| [PyYAML](https://github.com/yaml/pyyaml) | 配置读写 | MIT |
| [requests](https://github.com/psf/requests) | HTTP | Apache-2.0 |
| [Flask](https://github.com/pallets/flask) | 本地 Web 服务 | BSD-3-Clause |
| [pywebview](https://github.com/r0x0r/pywebview) | 桌面悬浮窗宿主 | BSD-3-Clause |
| [pythonnet](https://github.com/pythonnet/pythonnet) | pywebview 的 .NET 互操作 | MIT |
| [pystray](https://github.com/moses-palmer/pystray) | 系统托盘 | LGPL-3.0(以官方为准) |
| [Pillow](https://github.com/python-pillow/Pillow) | 图标处理 | HPND/MIT-CMU |

## 前端资源(随包分发,见 `alphaprism/web/static/vendor/`)
| 组件 | 许可证 |
|---|---|
| [Vue 3](https://github.com/vuejs/core) | MIT |
| [Apache ECharts](https://github.com/apache/echarts) | Apache-2.0 |

## 打包/分发工具(不随包分发,仅用于构建)
- [PyInstaller](https://github.com/pyinstaller/pyinstaller):GPL-2.0-or-later **带 Bootloader 例外**——用它打包出的应用不受 GPL 传染，可正常以 MIT 发布。
- [Inno Setup](https://jrsoftware.org/isinfo.php):免费用于安装器制作。
- Microsoft Edge WebView2 Runtime:微软专有，随 Windows 提供或经官方 bootstrapper 分发（遵循微软再分发条款）。

## 应用图标(已处理)
原从 **阿里巴巴矢量图标库(iconfont.cn)** 下载的图标(三角形形态 / 倒三角形)因**不担保商用再分发授权**,已于二期收尾**从仓库删除**,不再随包分发。
当前 `tray_icon.png` / `tray_icon.ico` 由 `logo3.png`(**AI 生成、无第三方版权**)提取、套白底圆角砖生成(脚本 `scripts/gen_logo_final.py`),随本项目以 **MIT** 许可发布。
> 备注:`assets/` 下 `logo.jpg` / `logo1.svg` / `logo2.jpg` 为早期参考、当前未被代码引用;公开上传前请自行确认来源,不确定就一并删除。

## 数据免责
行情数据经 akshare 抓取自公开接口，仅供**个人学习研究**使用，不构成任何投资建议；请遵守各数据源的服务条款。**请勿把抓取到的行情数据提交进本仓库**。
