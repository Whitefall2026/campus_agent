# Windows 打包

当前安装包版本：`1.0.1`。

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
- `dist\installer\RUC-Agent-Setup-1.0.1.exe`：Windows x64 安装包。
- `dist\installer\SHA256SUMS-1.0.1.txt`：主程序与安装包校验值。

安装版个人数据保存在 `%LOCALAPPDATA%\RUC Agent\data`。升级和卸载程序不会
主动删除该目录。

当前构建未配置 Authenticode 代码签名。首次运行可能触发 Windows SmartScreen；
应先用 `SHA256SUMS-1.0.1.txt` 核对安装包完整性。正式对外发布时建议在构建流程
后增加可信代码证书签名与签名验证步骤。
