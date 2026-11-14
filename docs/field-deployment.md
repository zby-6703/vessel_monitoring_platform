# 河边电脑部署与日常同步

本流程面向 Windows、Conda `Train`、Python 3.11，同一台河道摄像头。首次准备后，日常用一个更新命令拉取代码、安装锁定依赖、重建前端并重启。现场启动关闭 Python 热重载，前端使用已构建文件。当前验证使用 Python 3.11.15、Node.js 22.14.0。

## 首次部署

1. 安装 Git、Miniconda、Node.js/npm，并克隆代码：

   ```powershell
   git clone https://github.com/zby-6703/vessel_monitoring_platform.git
   cd vessel_monitoring_platform
   ```

2. 单独复制三套模型。当前已准备的迁移包名为 `river-models.zip`，在本机 `data/deployment/`。将它复制到河边电脑后，在项目根目录解压：

   ```powershell
   Expand-Archive -LiteralPath '你保存模型包的位置\river-models.zip' -DestinationPath . -Force
   ```

   权重不在 Git 中，仅拉代码无法获得模型。包内包含 `models/` 下的三套 ONNX、OCR 字典、部署 YAML，以及 `config/model-files.json` 校验清单；不包含账号密码、数据库、历史录像或原机位。更新已有模型时先保留原目录，不要混用来自不同部署包的 YAML/权重。校验清单随代码维护，模型更新时需要取得对应新模型包。

3. 初始化环境与配置，默认 CPU：

   ```powershell
   .\scripts\setup.ps1 -Environment Train -Runtime Cpu
   ```

   如果 Conda 不在 PATH，给所有脚本追加 `-CondaExe '你的Miniconda安装目录\Scripts\conda.exe'`。已有可用 CUDA/ONNX GPU 环境时可选 `-Runtime Gpu`；CPU 与 GPU ONNX Runtime 不要同时安装。现有 `Train` 需使用 Python 3.11。脚本使用锁定的 Python 依赖与 `npm ci`，不会复制另一台电脑的 Python 环境。已有环境缺少 FFmpeg 时执行：

   ```powershell
   conda install -n Train -c conda-forge ffmpeg
   ```

4. 填写这台电脑能访问的摄像头地址和凭据。河边电脑应使用可达的局域网地址；远程电脑的映射地址通常不能直接照搬：

   ```powershell
   conda run -n Train python scripts/configure-camera.py
   ```

   此工具在本机窗口保存账号密码并发现设备 Profile、视频源、服务与取流地址。没有图形界面时，可先编辑私有 `.env` 中设备地址/账号密码，再执行 `conda run -n Train python scripts/configure-camera.py --discover`。本次共享配置已包含同一摄像头的 1×、2×、3×、4×、6×、8× 标定，不需要从原电脑搬运整个 `data/`。不同摄像头必须重新标定。

5. 可在摄像头离线时完成检查，然后启动：

   ```powershell
   conda run -n Train python scripts/check-deployment.py --verify-models --smoke-models
   .\scripts\start-field.ps1 -Environment Train
   ```

   打开 `http://127.0.0.1:5173`。检查“系统状态”的模型与所选码流，设备恢复连接后手动启动摄像头采集。启动应用本身不会自动开启镜头控制。细节采集优先选择设备提供的高分辨率码流；其他电脑可选择辅助流查看，但码流分离不隔离同一物理镜头，自动云台跟随应由一台电脑负责。

## 日常更新

```powershell
.\scripts\update-field.ps1 -Environment Train -Runtime Cpu
```

使用 GPU 环境时保持 `-Runtime Gpu`。脚本先检查受版本控制的源文件是否有本地修改，关闭本机采集、等待录像收尾与机位归位，再停止服务、执行 `git pull --ff-only`、安装锁定依赖、重建前端并以现场模式启动。不自动提交、覆盖或暂存本地代码。

更新完成后刷新浏览器，再手动启动采集，确保页面与后端使用同一版本参数。镜头故障时原机位仍由本机保留，设备恢复后的采集初始化会先尝试归位。

手动拉取也可以：在界面停止采集后依次运行 `stop-dev.ps1`、`git pull --ff-only`、`setup.ps1` 和 `start-field.ps1`。只改代码、共享参数而依赖未变化时，可用 `setup.ps1 -SkipDependencies` 复用现有依赖并重建前端。

## 共享配置与本机配置

| 内容 | 位置 | 同步方式 |
| --- | --- | --- |
| 监测/录制/水平跟随参数、同一镜头的光学标定 | `config/river-monitoring.json` | 随 Git 拉取；启动时内容版本变化才同步 |
| 账号密码、设备地址、端口、取流地址、CPU/GPU 与本机路径 | `.env`、`data/camera_profile.json`、`data/model_settings.json` | 每台电脑单独保存，Git 忽略 |
| 模型文件名、尺寸和 SHA-256 | `config/model-files.json` | 随 Git；检查迁移模型是否匹配 |
| 模型权重及 OCR 字典 | `models/` | 使用独立模型包复制 |
| 数据库、录像、任务、水平起始机位 | `data/` | 本机保留，不从另一台电脑整目录覆盖 |

`.env` 中 `SITE_PROFILE=./config/river-monitoring.json` 启用共享配置。初次启用或共享文件内容变化时，应用在启动阶段同步录制策略，旧参数保存在 `data/results/passages/settings.before-shared-profile.json`，版本标记为同目录 `settings.shared-profile.json`。共享文件不变时，界面保存的本机临时调整会继续保留。设置 `SITE_PROFILE=` 可退出共享策略同步。

共享光学标定补充未在本机 `.env` 明确配置的光学值，本机显式填写的标定优先。摄像头切流仍通过设备发现完成，不把其他电脑保存的带凭据地址直接写入当前电脑。

当前共享基线：实时 OCR/吃水测量关闭、检测跟踪与录像开启、自动区域对焦开启、细节采集优先开启；主体确认 0.4 秒/两次观测、3 秒后允许尝试温和放大；优先 3–8×，取景允许时可退到 2×；放大后至少观察 12 秒，首次重关联最多等待 45 秒，离场需连续 5 秒且至少三次新空观测。每步水平移动最多当前画面宽 15%，原位左右各不超过云台全行程 15%，不主动上下移动。

细节采集顺序为：全景记录并等待合适位置 → 光学放大 → 保持本轮倍率并水平跟船 → 达到水平边界后停转、保持放大 → 目标离开放大画面并确认离场 → 缩回 1×、恢复水平原位、保存并选下一艘。主体已入画、竖直条件允许放大，但船体中心会落出放大视野时，放大前最多做一次必要的水平对准，随后优先放大，不在 1× 下持续追船。故障、手动停止、空间和单轮时长限制仍可结束本轮；视频是否能辨清船名/吃水刻度仍需设备恢复后按实际光照、分辨率和俯仰验收。

## 离线复现验证

本次将代码、配置、构建产物和模型迁移到另一个带空格的目录，用新 `.env`、CPU 模式完成 28 项离线检查，三套 ONNX 加载并执行船舶检测成功，未连接摄像头。模型包只含清单文件；现场启动的 HTTP API 与 WebSocket 转发也已验证。摄像头已断开，因此本轮未声称完成新的真实过船跟随验收。

完整后端测试 428 项通过，前端类型检查/构建通过，PowerShell 脚本语法与现有环境下的初始化流程通过。新建 Conda 环境与在线安装依赖仍需目标电脑能访问对应的软件源；本轮没有改动本机已有 Python 依赖。
