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

## 双击启动与关闭

完成上述首次部署后，双击项目根目录的 `platform.bat`（可为它创建桌面快捷方式）。同一个文件提供菜单，输入选项后按回车执行，操作结束后返回菜单：

- 按 `1` 开启：以现场模式启动后端和已构建前端，成功后自动用默认浏览器打开 `http://127.0.0.1:5173`，同时显示网址，便于手动访问。摄像头采集仍在页面手动开启。
- 按 `2` 关闭：先停止本机采集，等待录像收尾和机位恢复，再关闭前后端。若收尾失败，保留服务运行并显示错误；已停止时可重复执行。
- 按 `0` 退出窗口：平台继续运行。关闭浏览器同样不会停止后端采集，需要关闭平台时重新运行此文件并按 `2`。

默认环境为 `Train`。脚本会从 `CONDA_EXE`、PATH、Windows Python 注册表和常见用户/公共安装目录查找 Conda，无需先打开 Anaconda Prompt。自定义位置无法自动查找到时，可在命令提示符中明确传参：

```bat
platform.bat -CondaExe "D:\your Miniconda\Scripts\conda.exe" -Environment Train
```

也可在桌面快捷方式的“目标”中给 `.bat` 追加这些参数，开启和关闭均使用同一组环境设置。脚本根据自身位置查找项目，可从其他工作目录运行；不要单独复制 `.bat` 离开项目。首次运行不会自动安装依赖或下载模型，缺少环境时先完成 `setup.ps1`。窗口保留运行结果，按回车返回菜单；调试时可设置 `VESSELSIGHT_NO_PAUSE=1` 跳过暂停，不需打开浏览器可另设置 `VESSELSIGHT_NO_BROWSER=1`。操作失败会显示具体错误，再返回菜单。

启动入口 `platform.bat` 使用纯 ASCII，中文菜单由带 UTF-8 BOM 的 `scripts/platform-menu.ps1` 显示，兼容 Windows PowerShell 5.1 和中文 CMD 的初始代码页。保留这两个文件的编码与 CRLF 换行；Git 已固定它们的换行方式。更新脚本后关闭旧菜单窗口，再重新双击 `platform.bat`。

## 日常更新

```powershell
.\scripts\update-field.ps1 -Environment Train -Runtime Cpu
```

使用 GPU 环境时保持 `-Runtime Gpu`。脚本先检查受版本控制的源文件是否有本地修改，关闭本机采集、等待录像收尾与机位归位，再停止服务、执行 `git pull --ff-only`、安装锁定依赖、重建前端并以现场模式启动。不自动提交、覆盖或暂存本地代码。

更新完成后刷新浏览器，再手动启动采集，确保页面与后端使用同一版本参数。镜头故障时原机位仍由本机保留，设备恢复后的采集初始化会先尝试归位。

手动拉取也可以：先运行 `platform.bat` 并按 `2` 关闭、按 `0` 退出窗口，再依次执行 `git pull --ff-only`、`setup.ps1`；最后运行 `platform.bat` 并按 `1` 开启。只改代码、共享参数而依赖未变化时，可用 `setup.ps1 -SkipDependencies` 复用现有依赖并重建前端。

## 共享配置与本机配置

| 内容 | 位置 | 同步方式 |
| --- | --- | --- |
| 新电脑初始参数、手动重置时的默认参数、同一镜头光学标定 | `config/river-monitoring.json` | 随 Git 拉取；有效本机监测配置始终优先 |
| 本机保存并应用的监测/录制/跟随参数 | `data/results/passages/settings.json` | 每次启动读取；重启和更新代码都不自动覆盖 |
| 账号密码、设备地址、端口、取流地址、CPU/GPU 与本机路径 | `.env`、`data/camera_profile.json`、`data/model_settings.json` | 每台电脑单独保存，Git 忽略 |
| 模型文件名、尺寸和 SHA-256 | `config/model-files.json` | 随 Git；检查迁移模型是否匹配 |
| 模型权重及 OCR 字典 | `models/` | 使用独立模型包复制 |
| 数据库、录像、任务、水平起始机位 | `data/` | 本机保留，不从另一台电脑整目录覆盖 |

`.env` 中 `SITE_PROFILE=./config/river-monitoring.json` 提供现场默认策略与同一镜头光学标定。没有本机策略文件时，以现场默认策略初始化；已有有效 `data/results/passages/settings.json` 时，始终使用本机最新保存的配置，即使共享文件更新或旧的共享版本标记缺失，也不会重新套用默认参数。首次初始化的版本信息保存在同目录 `settings.shared-profile.json`；本机策略损坏时才允许从默认策略恢复。设置 `SITE_PROFILE=` 使用通用默认策略，本机保存配置仍优先。

实时监测中的“循环录制”和“识别与筛选”分别保存参数。“恢复默认配置”回填当前页签的现场默认值，点击“保存并应用”后生效。“恢复上次保存”可撤销当前页签的待保存修改。循环录制开关即时生效并单独保存，恢复默认参数时保持当前开关状态。云台、光学变焦和手动对焦在同级“手动镜头”页签中操作。

共享光学标定补充未在本机 `.env` 明确配置的光学值，本机显式填写的标定优先。摄像头切流仍通过设备发现完成，不把其他电脑保存的带凭据地址直接写入当前电脑。

在设备云平台调整拍摄角度前，先在本平台停止采集并等待录像收尾和水平归位，再调整机位、检查原倍率画面与放大后的水线位置，最后重新启动采集。干净停止后，新采集会采用当前角度作为新的水平基准；只有原机位尚未恢复时，平台才会先尝试恢复旧机位。完整说明见[云平台调整摄像头角度](camera-view-adjustment.md)。

当前共享基线：实时 OCR/吃水测量关闭、检测跟踪与录像开启、自动区域对焦开启、[分界线多轮放大](line-patrol.md)开启，竖直位置/完整船体视野对倍率的限制关闭。原倍率与放大空间分别使用 1/3、2/3 两条线；优先 3–8×，选用已标定档位。放大后至少观察 12 秒，首次重关联最多等待 45 秒，离场需连续 5 秒且至少三次新空观测。每步水平移动最多当前画面宽 15%，原位最大左旋和最大右旋分别为云台全行程 15%，可独立调整，不主动上下移动。

左→右时：原倍率记录到船头越左线并经连续新观测确认 → 放大并等待船只进入 → 船尾越过放大画面第一条线（左线）后右旋跟随 → 达最大右旋范围停转 → 船尾离开放大画面后缩回并归位 → 原倍率船尾尚未越右线则重复，否则保持原倍率直到船尾离开整幅画面再保存整段。右→左对称，船尾越过放大画面右线后开始左旋，跟随仅发生在放大空间。故障、手动停止、空间和单轮时长限制仍可结束本轮；视频是否能辨清船名/吃水刻度仍需实际光照、分辨率和俯仰验收。

## 离线复现验证

部署整理阶段已将代码、配置、构建产物和模型迁移到另一个带空格的目录，用新 `.env`、CPU 模式完成 28 项离线检查，三套 ONNX 加载并执行船舶检测成功，未连接摄像头。模型包只含清单文件；现场启动的 HTTP API 与 WebSocket 转发也已验证。分界线流程使用模拟镜头和真实 MP4 编解码验证，仍需自然过船和吃水刻度清晰度验收。

完整后端测试 480 项通过，前端类型检查/构建通过，覆盖越线抖动清零、匹配预览先于镜头决策推送、双向第一条放大分界线触发、手动已标定调倍及控制互斥；PowerShell 脚本语法与现有环境下的初始化流程通过。实际服务重启后，本机监测参数保持不变，默认配置预览不会改写保存文件；手动调倍按钮不改写自动监测参数。新建 Conda 环境与在线安装依赖仍需目标电脑能访问对应的软件源；本轮没有改动本机已有 Python 依赖。
