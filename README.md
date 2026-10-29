# VesselSight 船舶智能监测平台

VesselSight 是一个可独立部署的船舶视频/图像分析平台。它提供船舶与区域检测、船名识别、吃水读数估计、视频跟踪、离线批处理、结果归档，以及用于现场系统对接的实时 WebSocket 接口。

源代码、运行数据和模型权重已分离：克隆本仓库即可构建服务；运行数据和模型权重已发布到 GitHub Releases：[resources-v0.1.0](https://github.com/zby-6703/vessel_monitoring_platform/releases/tag/resources-v0.1.0)。在执行真实推理前，只需按 [models/README.md](models/README.md) 放入对应的 ONNX 权重。

训练新模型或从 `.pt` 导出部署模型前，请参阅 [新模型 ONNX 导出与接入要求](docs/onnx-model-contract.md)，其中包含三类检测、字符检测与水体分割的张量接口、预处理、配置模板和验收步骤。

## 功能范围

- 离线图像和视频分析，支持标注结果、JSONL、CSV 和可视化输出。
- 船舶检测、吃水区域/刻度检测、水体分割、船名 OCR 与结构约束吃水估计。
- 视频 TrackTrack 跟踪与单船实例聚合。
- SQLite（本地开发）或 MySQL（Docker 部署）持久化。
- REST API、OpenAPI 文档和 Vue 前端。
- 可选 ZLMediaKit 视频流转发；未配置流媒体时离线分析仍可使用。
- 实时摄像头服务器采集、按过船事件保存全景及聚焦视频、历史录像检索与回看。

## 项目结构

```text
vessel_monitoring_platform/
├── backend/              FastAPI 服务、推理和测试
├── frontend/             Vue 3 + Vite 前端
├── runtime_support/      独立于训练仓库的推理期算法
├── models/               模型部署配置；权重可从 Release 获取
├── assets/               本地数据资产（不随仓库发布；Release 中提供数据包）
├── data/                 本地运行数据、日志、上传和结果（不提交）
├── deploy/               Dockerfile、Nginx 和 ZLMediaKit 配置
├── docker-compose.yml    CPU 默认部署
└── docker-compose.gpu.yml NVIDIA GPU 覆盖配置
```

## 前置条件

### Docker 部署（推荐给使用者）

只需要安装并启动 Docker Desktop。Windows 上推荐启用 WSL 2 后端；Docker Desktop 的 Settings 中应显示 Docker Engine 正在运行。

首次使用请在 PowerShell 中确认：

```powershell
cd vessel_monitoring_platform
.\scripts\docker-doctor.ps1

# 或 Docker Desktop 已在 PATH 时：
docker version
docker compose version
```

`docker-doctor.ps1` 会自动定位常见的 Docker Desktop 安装位置；因此即使安装程序尚未将 `docker.exe` 加入 PATH，也能完成检查。若要直接使用 `docker` 命令，仍建议将 Docker Desktop 的 `resources\bin` 目录加入系统 PATH 后重开 PowerShell。

若要使用 NVIDIA GPU 容器，还需要：Windows NVIDIA 显卡驱动支持 WSL、Docker Desktop 启用 WSL integration，并且以下命令能成功显示显卡信息：

```powershell
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

没有 GPU 或上述命令失败时，直接使用本项目默认的 CPU Compose 即可；不需要额外安装 Python、Node.js、MySQL 或 Redis。

### 本地开发

- Python 3.11（建议 Conda 环境）。
- Node.js 22 或更高版本。
- ONNX Runtime：CPU 使用 `requirements-cpu.txt`；CUDA 使用 `requirements-gpu.txt`。

## 通过 Docker 运行

### 1. 准备配置和模型

```powershell
cd vessel_monitoring_platform
Copy-Item .env.example .env
```

编辑 `.env`，至少替换 `MYSQL_PASSWORD`、`MYSQL_ROOT_PASSWORD` 和 `API_INGEST_TOKEN` 为强随机值。随后从 [resources-v0.1.0](https://github.com/zby-6703/vessel_monitoring_platform/releases/tag/resources-v0.1.0) 下载运行数据和模型资源包，并按 release 中的目录结构解压到仓库根目录；再按 [models/README.md](models/README.md) 将三个 ONNX 权重置于 `models/`。`.env`、权重、上传文件、数据库和结果均不会被提交到 Git。

### 2. CPU 部署

```powershell
docker compose config -q
docker compose up -d --build
docker compose ps
```

首次构建会下载基础镜像和 Python/前端依赖，耗时取决于网络。服务就绪后访问：

- 前端：http://127.0.0.1:8088
- API 文档：http://127.0.0.1:8010/api/docs
- 健康检查：http://127.0.0.1:8010/api/health

健康检查中的 `dependencies.database` 和 `dependencies.redis` 应为 `ready`。如果模型已配置，三个 `models[*].status` 也应为 `ready`。

### 3. NVIDIA GPU 部署（可选）

先完成上方的 GPU 自检，再执行：

```powershell
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

该覆盖配置会使用 CUDA 基础镜像、`onnxruntime-gpu` 并请求一张 NVIDIA GPU。不要在 CPU 主机上使用它。

### 4. 可选：启动流媒体转发

离线分析不需要 ZLMediaKit。仅在需要 RTSP/RTMP 等流转发时启动：

```powershell
docker compose --profile stream up -d zlmediakit
```

将真实播放地址配置到 `.env` 的 `LIVE_STREAM_URL`，然后重建 API：

```powershell
docker compose up -d --force-recreate api
```

### 日常维护

```powershell
# 查看服务状态和日志
docker compose ps
docker compose logs -f api web

# 停止服务，但保留 MySQL/Redis 数据卷
docker compose down

# 停止并删除数据库与缓存数据（不可恢复）
docker compose down -v
```

运行数据位于项目目录的 `data/`；MySQL 与 Redis 使用 Docker volume。生产环境请定期备份两者，并使用反向代理/TLS 保护对外端口。

## 本地开发运行

```powershell
cd vessel_monitoring_platform
Copy-Item .env.example .env

# GPU 环境（本机 Train 环境已有 torch 时通常选择此项）
conda run -n Train python -m pip install -r backend/requirements-gpu.txt

# 或 CPU 环境
# conda run -n Train python -m pip install -r backend/requirements-cpu.txt

cd frontend
npm ci
cd ..
```

启动脚本会同时启动 FastAPI（8010）和 Vite 前端（5173）。不需要先执行 `conda activate Train`；脚本会用指定的 Conda 环境启动后端。默认会从 `CONDA_EXE` 或系统 `PATH` 查找 `conda.exe`：

```powershell
.\scripts\start-dev.ps1 -Environment Train
```

若 Conda 未加入 PATH，显式传入路径即可：

```powershell
.\scripts\start-dev.ps1 -CondaExe "D:\app\miniconda3\Scripts\conda.exe" -Environment Train
```

开发地址为前端 http://127.0.0.1:5173、API 文档 http://127.0.0.1:8010/api/docs。状态检查、停止和重新启动：

```powershell
.\scripts\status.ps1
.\scripts\stop-dev.ps1

# 需要重新启动时：先停止，再启动
.\scripts\start-dev.ps1 -CondaExe "D:\app\miniconda3\Scripts\conda.exe" -Environment Train
```

停止脚本不需要激活 Conda 环境。即使服务是手动启动的，或 `data/logs/dev-processes.json` 丢失，也会检查 8010、5173 端口并停止已确认的平台进程；无法识别的进程会保留并提示。重复执行停止脚本会提示服务已停止。

如果启动时报 `Port 8010 is already in use` 或 `Port 5173 is already in use`，通常表示平台已经运行，而不是模型错误。先执行：

```powershell
.\scripts\status.ps1
```

若 API 和 Web UI 均为 `Listening`，直接打开前端即可；若要重启，执行 `stop-dev.ps1` 后再执行 `start-dev.ps1`。不要同时重复运行启动脚本。

本地开发默认不自动启动 Redis，因此 `status.ps1` 可能显示 `Redis realtime: unavailable`。这只会禁用 Redis 队列/实时结果链路；数据库、模型检查和离线图像/视频分析仍可正常使用。需要 Redis 时，可启动 Docker 服务：

```powershell
docker compose up -d redis
```

## 过船自动录像与聚焦

### 光学变焦循环：每轮保存一个视频

实时监测默认提供“光学变焦循环”模式：先将真实摄像头镜头恢复到已标定的 1×；观察并选择可录制的目标后，先打开录像并写入触发帧，再按目标船体高度自适应选择已标定光学倍率，或使用固定倍率；本轮锁定目标，让目标船头、船身、船尾自然通过当前取景，必要时受控调整倍率；确认该目标离场后，镜头缩回 1×；确认镜头到位并等待全景稳定后结束本轮，只保存一个完整的 H.264 MP4。下一轮只使用缩回后新采集、新推理的全景画面，重新选择移动目标，避免背景停泊船反复抢占。

该模式控制的是摄像头光学镜头，不会用数字裁剪、连续运动脉冲时长或 `GotoHomePosition` 冒充精确 3×/1×。录制范围是摄像头当时实际输出的画面，包含放大、经过、缩回的过程，不额外生成一份相同的数字聚焦视频，也无法从同一台已放大的镜头同时获得独立的全景视频。视频重新编码且不含音频。单轮达到安全最长时长、停止采集、关闭录制、检测器故障时会结束当前轮并尝试缩回 1×；未确认缩回的片段标记为中断，显示错误并暂停自动循环。

界面可设置精确目标倍率、触发确认次数、船尾消失确认时间（默认 1 秒）、镜头到位超时（默认 15 秒）、缩回后的全景稳定时间（默认 0.5 秒）。放大后必须先在该视野中实际检测到船体，才开始接受船尾离场判断，防止船头尚未进入窄视野时误判离场。船尾离场确认仅使用当前放大视野的新检测结果；没有新推理、镜头移动中的画面及缩回前的旧检测均不会作为离场或下一轮触发证据。光学循环运行时手动变焦和对焦控制暂时互斥，“停止运动”会暂停循环。摄像头取景方向保持固定，不自动旋转云台跟随整艘船，以保留船舶从船头到船尾依次通过的过程。

需要支持 ONVIF `AbsoluteMove` 与绝对变焦位置反馈的摄像头，并完成真实光学倍率标定。标定必须包括 1× 和希望使用的每一个倍率；不在表内的倍率不会插值估算。请关闭摄像头自身的数字/混合变焦，使表中位置确实对应光学镜头位置。配置示例中的位置仅示意，不能直接作为其他摄像头的标定值：

```dotenv
CAMERA_ONVIF_ZOOM_CALIBRATION={"1":0.0,"3":0.4}
# 多个兼容 PTZ 配置时必须明确指定；只有一个兼容配置时自动获取。
CAMERA_ONVIF_PTZ_CONFIGURATION_TOKEN=PTZConfig_1
# 多个绝对变焦空间时才需指定，值须来自设备 GetConfigurationOptions。
CAMERA_ONVIF_ZOOM_SPACE_URI=
CAMERA_ONVIF_ZOOM_TIMEOUT=15
CAMERA_ONVIF_ZOOM_TOLERANCE=0.005
```

标定表的键是实际倍率，值是设备所声明的绝对镜头位置；归一化镜头位置 0.4 并不表示 4× 或 40% 的光学倍率。平台从设备的 PTZ 配置选项读取可用绝对变焦坐标空间及范围，拒绝数字专用变焦空间；每次移动后轮询镜头位置和运动状态，到位后才进入下一状态。`CAMERA_ONVIF_ZOOM_TOLERANCE` 是设备位置范围的误差比例，默认 0.5%。设备不支持、倍率未标定、位置反馈缺失或移动超时会明确显示错误；没有实际摄像头时可检验状态机与模拟 ONVIF 请求，不能验证镜头实物的画质和倍率。

### 自适应倍率、近船优先与停泊排除

光学循环默认开启自适应倍率。以稳定观测到的船体检测框高度除以参考画面高度，得到 `height_ratio`；目标高度占比默认 55%，计算建议倍率为 `0.55 / height_ratio`。竖直高度较少受船头刚入画、船身长度尚不完整的影响，但仍会受到船型、遮挡、镜头俯仰和检测框抖动影响。系统按实际已标定档位、倍率上下限以及竖直视野边界选择可用值，不插值生成未标定倍率。默认放大范围为 1.1–8×；船体已经足够大，或继续放大会裁掉目标竖直范围时，可保留 1× 录制。界面区分计算建议值与实际选择档位，不承诺一定达到目标高度。每轮锁定同一艘船，但倍率可随船体靠近或远离受控更新。默认调整冷却为 3 秒、滞回幅度 15%，用于避免框抖动导致镜头不断来回变焦；每次仍只移动到已标定的安全档位。关闭自适应后仍可使用原固定倍率设置。

多船场景先排除不具备录制资格的目标，再按船框相对高度优先选择表观较近的移动船舶；近似时参考画面位置，并用切换余量抑制候选来回跳变。这是单目画面的近远启发式，不能代替经标定的真实距离测量：高船体的大船可能比低船体的小船实际更远。每轮锁定一艘目标，不因为背景中还有船而一直延长当前视频；变焦后的目标重识别不明确时继续短时观察，默认 15 秒仍无法关联原目标就缩回 1× 重新选船，记录 target_not_reacquired 原因，避免把另一艘船冒认成原目标或让背景船长期占用本轮。

停泊排除默认开启。启动时先观察船舶自身运动，尚无运动证据的目标显示“观测中”，不会立即抢占镜头。默认在有效观测支持下持续静止 15 秒后标记为静止目标；运动确认时长默认 1 秒，位移阈值为参考 1× 画面尺度的 1.5%。静止判断使用位移历史与背景运动补偿，排除摄像头缓慢转动、轻微抖动造成的共同位移；没有检测、镜头移动或不同视野之间的时间间隔不直接累计成停泊证据。补偿不可靠时暂缓判断，目标恢复移动后可以重新成为候选。该标记是视觉策略状态，不是船舶实际停泊业务状态，现场可根据波浪、船速、遮挡和镜头稳定程度调整阈值。

界面会显示目标 ID、1× 船高占比、运动/观测/静止状态及选择原因；历史录像保留目标与倍率决策信息。选择列表中的框属于参考视野，界面不会把这些框直接叠到已经变焦的画面上。当前镜头朝向保持固定，目标是否能完整通过放大视野仍取决于安装角度和航道位置。

### 使用真实片段进行离线策略模拟

以下命令使用项目真实船体检测模型，对提供的片段采样检测、估计背景运动并回放选船与倍率决策。所有 Python 命令使用 `Train`：

```powershell
& 'D:\app\miniconda3\envs\Train\python.exe' backend/scripts/simulate_adaptive_monitor.py --input assets/20251211082541.mp4 --output data/adaptive-scene --sample-fps 4

# 调整策略后复用真实检测缓存，避免重复模型推理。
& 'D:\app\miniconda3\envs\Train\python.exe' backend/scripts/simulate_adaptive_monitor.py --input assets/20251211082541.mp4 --output data/adaptive-scene --sample-fps 4 --reuse-detections
```

可传 `--settings path.json` 指定策略，`--calibrated-factors 1,2,3,4,6,8` 指定模拟可用档位，或使用 `--detect-only` 仅建立检测缓存。输出目录包含 `contactsheet.jpg`、`detections.jsonl`、`detection-source.json`、`decisions.jsonl`、`strategy-replay.mp4` 和 `report.json`。策略视频在原画面上标注目标、静止状态与建议倍率；它不驱动摄像头，不模拟真实光学画质提升。

该片段约 26 秒、2560×1440，标称帧率 100 fps，但实际平均帧率约 24.515 fps 且存在可变帧间隔；本地录制及模拟以解码 PTS 为时钟，不用帧号除以标称帧率推算。素材含 Z:3 倍率水印和缓慢镜头运动，水印不能代替本机镜头标定。离线决策以输入画面为参考视野，能够验证检测、选船和背景运动补偿的行为，但短片不足以证明所有目标长期停泊，也不能替代现场光学变焦与真实距离验证。

进一步使用同一真实检测缓存运行完整的录制状态机，采用隔离输出目录和模拟镜头，不操作实物摄像头：

```powershell
& 'D:\app\miniconda3\envs\Train\python.exe' backend/scripts/simulate_optical_cycles.py --input assets/20251211082541.mp4 --detections data/adaptive-scene/detections.jsonl --output data/adaptive-cycle-run --sample-fps 4
```

输出包括 `simulation.mp4`、`summary.json`、`timeline.json` 和 `passages/` 中的单轮录像。模拟放大画面是数字裁剪，检测框从真实原画面检测几何投影而来，没有重新对放大图推理。示例验收中约 2.75 秒锁定前景红船并选择相对输入画面的 2×，14.68 秒因船高增长降低至 1×；原片结束时船尾仍未离场，保存的 23.75 秒片段正确标记为中断，不能计为一次自然完成的过境。默认 15 秒静止阈值在该短片中未积累足够有效证据，5/8 秒诊断中的静止标记也只是候选，不是实际停泊真值。

### 数字取景事件录制（兼容模式）

实时监测页可选择开启“过船录制”。检测结果连续满足确认条件后开始保存视频；预录缓冲补上船舶刚进入视野的画面，短时漏检由离场延时衔接，确认离场后自动结束。本地视频回放也接入同一流程，可在接入现场摄像头前验证设置。摄像头采集和本地回放互斥，切换前须停止当前来源并等待会话结束。

每次过船保存两份无检测框的视频：完整画面的“原视频”和用于查看船体、船名及吃水区域的“聚焦视频”。这里的“原视频”是源画面逐帧重新编码的 H.264 MP4，不是摄像头码流的逐字节备份；不保留音频。全景保留源分辨率（奇数边长会取相邻偶数），本地视频保持原始帧率和时长，跳帧推理不会使录像加速。聚焦视频按画面比例裁剪后缩放，最大输出 1280×720。多船同时出现时，自动聚焦包含所有已检测船舶及其相关区域；长时间持续有船的事件会按单段时长分段。

自动聚焦依据船体、船名、吃水区域和刻度的联合范围，并保留边缘余量；范围过大时自动减少放大倍率，优先保证船体完整。手动模式可自定义中心和放大倍率。数字放大只能放大现有像素，无法恢复远距离源画面中本来没有的文字或刻度细节；现场清晰度仍由光学镜头、传感器分辨率、光线和安装距离决定。光学变焦通过可选的 ONVIF 接口单独控制，不执行未经标定的自动云台转向。

录像设置保存在 `data/results/passages/settings.json`，片段和索引位于 `data/results/passages/`；刷新页面或重启服务后可继续检索历史视频。默认关闭录制；光学模式默认按船高自适应并排除静止目标，关闭自适应后固定倍率默认 3×。移动候选确认后默认检测 1 次触发，置信度阈值 0.45、单轮安全时长 300 秒、存储配额 10 GB、保留 30 天；已有设置保留原确认次数。兼容数字模式默认预录 3 秒、离场延时 5 秒，数字倍率限制为 1–8×；本地回放始终采用数字流程，不驱动实物摄像头。界面可调整这些值以及数字模式的聚焦方式、倍率、中心和边缘余量。到期或超过配额的历史片段自动清理；磁盘、编码和来源错误会显示在录像状态中。正在录制或转码的片段完成后才能正常回看，开启录制需要服务端 PATH 中有带 `libx264` 的 `ffmpeg`；Docker 镜像已安装。数字模式预录缓冲同时受 64 MB 内存上限限制，高分辨率下实际预录时间可能短于设置值。

摄像头由后端持续采集，浏览器关闭不影响已经启动的采集和录像；API 服务需要保持运行。服务重启后需要重新启动摄像头采集。本功能采用单进程内的采集与录像管理器，部署时使用一个 API worker；横向扩展需将各摄像头分配到独立采集服务并持久化共享索引。

### 摄像头接入

首次接入可使用本机配置工具，使用项目的 `Train` 解释器运行：

```powershell
& 'D:\app\miniconda3\envs\Train\python.exe' .\scripts\configure-camera.py --credentials-only
& 'D:\app\miniconda3\envs\Train\python.exe' .\scripts\configure-camera.py --discover
```

第一个命令打开本机窗口填写设备 IP、HTTP/RTSP 端口和账号密码；第二个命令读取设备实际声明的 ONVIF 服务地址、媒体 Profile、视频源和 PTZ 配置标识，并取得主码流 URI。密码只保存到 Git 忽略的本机 `.env`；发现报告、首次配置前的备份保存在 `data/camera-setup/`，终端输出不包含密码或带凭据的取流地址。默认入口为设备的 `/onvif/device_service`，其他入口可在 `.env` 的 `CAMERA_ONVIF_DEVICE_URL` 中明确指定。设备返回不同主机的服务或流地址时拒绝发送凭据，不跟随重定向。

自动选择优先采用支持 PTZ 的最高分辨率配置；多个配置分辨率相同时会列出候选 Token，需要使用 `--profile-token` 明确选择从设备读取的主码流 Token。之后重复发现可沿用已保存的选择。该工具只查询设备能力，不移动镜头、不修改摄像头编码参数，也不把设备位置猜算为光学倍率。`CAMERA_ONVIF_ZOOM_CALIBRATION` 仍须通过实际镜头倍率与位置反馈标定。

远程链路可使用 EasyTier：两台电脑加入同一私有组网，仅在现场电脑发布摄像头地址的 `/32` 子网代理。先在运行后端的电脑验证 HTTP 和 RTSP 可达，再运行上述工具。RTSP 使用 `OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp`；设备返回的编码帧率会写入 `LIVE_CAPTURE_FPS_OVERRIDE`，避免部分相机 RTSP 声明 100 fps、实际编码 25 fps 时生成不必要的重复帧。值为 `0` 时沿用源帧率，`LIVE_CAPTURE_FPS` 仍只作无效帧率的后备值。

完成配置后重启 API，在实时监测页启动摄像头采集并检查镜头能力。开启自动光学循环前需确认真实倍率标定已完成。服务重启会结束当前采集会话；启动后重新点击“启动摄像头采集”。也可以手动填写 `.env`：

```dotenv
LIVE_CAMERA_ID=camera-01
LIVE_CAMERA_NAME=现场监测点 01
LIVE_CAPTURE_URL=rtsp://username:password@192.168.1.100:554/stream1
LIVE_CAPTURE_FRAME_STEP=3
LIVE_CAPTURE_FPS=25
```

`LIVE_CAPTURE_URL` 是仅由服务器读取的 RTSP 或可被 FFmpeg 解码的直接 HTTP 视频流，不会通过状态接口返回给浏览器。`LIVE_STREAM_URL` 仍是可选的浏览器预览地址，可使用 ZLMediaKit 转出的 HTTP-FLV。仅设置预览地址不会启动服务端检测或自动录像。`LIVE_CAPTURE_FRAME_STEP` 控制送入模型的采样间隔；采集线程始终将每帧交给录像器，模型繁忙时推理队列只保留最新一帧。`LIVE_CAPTURE_FPS` 仅在来源未报告有效帧率时作为后备值。网络中断会结束本次事件并显示错误，检查网络后可重新启动；模型不可用时直接报告失败，不产生模拟检测。

兼容数字模式开启录制时，推理采样间隔（跳帧数÷源帧率）必须小于离场延时的一半；现场推理结果的实际间隔也必须短于离场延时。服务会检查初始采样及后续结果，间隔过长时明确停止并提示减少跳帧、提高离场延时或优化模型速度。光学循环使用独立的船尾消失确认时间；模型处理间隔不会自动当作“船已消失”，但采样过稀会延后判断，因此现场应保持足够的推理频率。船舶检测器故障会结束会话并显示错误；仅船名 OCR 或吃水估计失败时继续保留可用的船舶检测及录像。

可选的 ONVIF 光学变焦及镜头对焦需要从摄像头配置或厂商 ONVIF 工具取得真实服务地址和 Token：

```dotenv
CAMERA_ONVIF_PTZ_URL=http://192.168.1.100/onvif/ptz_service
CAMERA_ONVIF_PROFILE_TOKEN=MainProfile
CAMERA_ONVIF_IMAGING_URL=http://192.168.1.100/onvif/imaging_service
CAMERA_ONVIF_SOURCE_TOKEN=VideoSource_1
CAMERA_ONVIF_USERNAME=username
CAMERA_ONVIF_PASSWORD=password
```

PTZ 服务与媒体 Profile Token 用于光学放大、缩小、停止和返回设备已设原位；Imaging 服务与 VideoSource Token 用于近焦、远焦和自动对焦。设备需实际支持对应 [ONVIF PTZ](https://www.onvif.org/ver20/ptz/wsdl/) 与 [Imaging](https://www.onvif.org/specs/2206/ONVIF-Imaging-Service-Spec-v2206.pdf) 操作。接口使用 HTTP Digest 和 WS-Security UsernameToken 摘要认证，摄像头时间需与服务器同步。不支持的设备、Token 错误和身份验证失败会明确返回错误；未配置设备时不会假装已经执行物理变焦。

### 相关 API

- `GET /api/realtime/camera`、`POST /api/realtime/camera/start`、`POST /api/realtime/camera/stop`：服务器采集状态、启动和停止；启动可传 `{"frame_step": 3}`。
- `GET /api/camera/ptz`、`POST /api/camera/ptz`：设备能力检查和手动镜头命令，命令体为 `{"action": "zoom_in"}`，也支持 `zoom_out`、`focus_near`、`focus_far`、`autofocus`、`stop`、`home`。
- `GET/PUT /api/recording/settings`、`GET /api/recording/status`：录制配置与运行状态。
- `GET /api/recordings`：按关键词、来源、时间和页码查询；`GET /api/recordings/{id}/original` 或 `/focused` 用于播放，加 `?download=true` 下载；`DELETE /api/recordings/{id}` 删除完成的片段。
- `/ws/realtime`：摄像头发布 `camera_frame` / `camera_status`，本地回放发布 `local_playback_frame` / `local_playback_status`；帧数据均包含 `session_id`、`camera_id`、`frame_id`、JPEG `image` 和结构化 `frame`。

## 测试与构建

```powershell
# 后端
conda run -n Train pytest -q backend/tests

# 前端生产构建
cd frontend
npm run build
```

## 发布到 GitHub 的清单

1. 将 `vessel_monitoring_platform/` 复制到新的空目录并在该目录执行 `git init`。
2. 仅提交源码、部署配置、模型配置和文档；确认 `.env`、`data/`、模型权重和业务素材没有被加入暂存区。
3. 为项目选择并添加适用的 `LICENSE`，并确认模型、数据集和示例媒体的再分发授权。
4. 将模型权重和运行数据发布到 GitHub Releases、对象存储或受控模型仓库；提供版本号、SHA-256 和下载说明。
5. 在干净的 Docker Desktop 主机上执行“通过 Docker 运行”的 CPU 流程，确认健康检查和前端可用。

## 注意事项

- 不提供权重时，平台可以启动，但真实推理任务会失败并报告模型缺失；不会使用虚构检测结果替代。
- Docker 默认使用 CPU 以保证可移植性；GPU 性能部署必须使用 `docker-compose.gpu.yml`。
- 当前实时入口支持服务器摄像头采集、本地视频回放和外部实例事件；Redis 队列式独立推理 worker 不包含在本仓库中。
- 对公网部署前必须修改 `.env` 中的密码和令牌，并通过防火墙/反向代理限制 MySQL、Redis、API 和流媒体端口。
