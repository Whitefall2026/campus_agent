# 桌面安装包构建

当前安装包版本：`2.1`。

## 构建

```powershell
python -m pip install -r packaging\requirements-build.txt
winget install --id JRSoftware.InnoSetup -e
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

构建会依次执行完整测试、语法检查、PyInstaller 冻结程序、冻结版冒烟测试、
Inno Setup 安装包编译和 SHA-256 校验文件生成。测试数据位于临时隔离目录，
不会读取或修改开发者的 `data/`。

## 产物

- `dist\RUCAgent\RUCAgent.exe`：目录版主程序。
- `dist\installer\RUC-Agent-Setup-2.1.exe`：Windows x64 安装包。
- `dist\installer\SHA256SUMS-2.1.txt`：主程序与安装包校验值。

安装版个人数据保存在 `%LOCALAPPDATA%\RUC Agent\data`。升级和卸载程序不会
主动删除该目录。

安装器默认勾选“随 Windows 启动”。该启动项使用 `--no-browser` 参数，登录后
应用只在系统托盘中后台运行；重新运行安装包并取消勾选可关闭自启动，卸载时
启动项也会自动删除。

当前构建未配置 Authenticode 代码签名。首次运行可能触发 Windows SmartScreen；
应先用 `SHA256SUMS-2.1.txt` 核对安装包完整性。正式对外发布时建议在构建流程
后增加可信代码证书签名与签名验证步骤。

## macOS

在原生 Mac 上安装独立构建依赖，然后运行脚本；不会安装 Windows 微信依赖：

```bash
python3 -m pip install -r packaging/requirements-macos.txt
bash packaging/build_macos.sh
```

要求 Python 3.12+、Node.js，Python 与主机架构一致。脚本依次执行隔离全量测试、JS 检查、PyInstaller `.app` 构建、架构校验、冻结程序 HTTP/静态资源冒烟以及 DMG 和 SHA-256 文件生成。

产物为 `dist/macos/RUC-Agent-2.1-macos-arm64.dmg` 或 `RUC-Agent-2.1-macos-x86_64.dmg`，附对应 `SHA256SUMS-2.1-macos-<架构>.txt`。DMG 内包含应用及 Applications 快捷方式，拖入应用程序即可安装。macOS 菜单栏可打开浏览器、数据目录或退出；重复启动复用已有实例。个人数据使用 `~/Library/Application Support/RUC Agent/data`，不包含在 DMG 中。

`.github/workflows/release-macos.yml` 在 v2.1 标签推送时分别使用 Apple Silicon 与 Intel runner 构建，测试通过后上传到该版本已有 Release；也可通过 workflow_dispatch 重跑。发布协调者应先创建 Release 草稿，再推送标签。仅使用 GitHub 临时任务令牌，不需要个人 API Key。

目前未配置 Apple Developer 签名/公证；发布产物不能宣称已获 Gatekeeper 信任。macOS 微信读取不支持，核心功能不受影响。
