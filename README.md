# VesselSight 船舶智能监测平台

VesselSight 是一个可独立部署的船舶视频/图像分析平台。它提供船舶与区域检测、船名识别、吃水读数估计、视频跟踪、离线批处理、结果归档，以及用于现场系统对接的实时 WebSocket 接口。

源代码、运行数据和模型权重已分离：克隆本仓库即可构建服务；运行数据和模型权重已发布到 GitHub Releases：[resources-v0.1.0](https://github.com/zby-6703/vessel_monitoring_platform/releases/tag/resources-v0.1.0)。在执行真实推理前，只需按 [models/README.md](models/README.md) 放入对应的 ONNX 权重。

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

实时监测页可选择开启“过船录制”。检测结果连续满足确认条件后开始保存视频；预录缓冲补上船舶刚进入视野的画面，短时漏检由离场延时衔接，确认离场后自动结束。本地视频回放也接入同一流程，可在接入现场摄像头前验证设置。摄像头采集和本地回放互斥，切换前须停止当前来源并等待会话结束。

每次过船保存两份无检测框的视频：完整画面的“原视频”和用于查看船体、船名及吃水区域的“聚焦视频”。这里的“原视频”是源画面逐帧重新编码的 H.264 MP4，不是摄像头码流的逐字节备份；不保留音频。全景保留源分辨率（奇数边长会取相邻偶数），本地视频保持原始帧率和时长，跳帧推理不会使录像加速。聚焦视频按画面比例裁剪后缩放，最大输出 1280×720。多船同时出现时，自动聚焦包含所有已检测船舶及其相关区域；长时间持续有船的事件会按单段时长分段。

自动聚焦依据船体、船名、吃水区域和刻度的联合范围，并保留边缘余量；范围过大时自动减少放大倍率，优先保证船体完整。手动模式可自定义中心和放大倍率。数字放大只能放大现有像素，无法恢复远距离源画面中本来没有的文字或刻度细节；现场清晰度仍由光学镜头、传感器分辨率、光线和安装距离决定。光学变焦通过可选的 ONVIF 接口单独控制，不执行未经标定的自动云台转向。

录像设置保存在 `data/results/passages/settings.json`，片段和索引位于 `data/results/passages/`；刷新页面或重启服务后可继续检索历史视频。默认关闭录制，默认预录 3 秒、离场延时 5 秒、连续确认 2 次、置信度阈值 0.45、单段 300 秒、存储配额 10 GB、保留 30 天。界面可调整这些值以及聚焦方式、倍率、中心和边缘余量。到期或超过配额的历史片段自动清理；磁盘、编码和来源错误会显示在录像状态中。正在录制或转码的片段完成后才能正常回看，开启录制需要服务端 PATH 中有带 `libx264` 的 `ffmpeg`；Docker 镜像已安装。预录缓冲同时受 64 MB 内存上限限制，高分辨率下实际预录时间可能短于设置值。

摄像头由后端持续采集，浏览器关闭不影响已经启动的采集和录像；API 服务需要保持运行。服务重启后需要重新启动摄像头采集。本功能采用单进程内的采集与录像管理器，部署时使用一个 API worker；横向扩展需将各摄像头分配到独立采集服务并持久化共享索引。

### 摄像头接入

在 `.env` 配置后重启 API，然后在实时监测页启动摄像头采集：

```dotenv
LIVE_CAMERA_ID=camera-01
LIVE_CAMERA_NAME=现场监测点 01
LIVE_CAPTURE_URL=rtsp://username:password@192.168.1.100:554/stream1
LIVE_CAPTURE_FRAME_STEP=3
LIVE_CAPTURE_FPS=25
```

`LIVE_CAPTURE_URL` 是仅由服务器读取的 RTSP 或可被 FFmpeg 解码的直接 HTTP 视频流，不会通过状态接口返回给浏览器。`LIVE_STREAM_URL` 仍是可选的浏览器预览地址，可使用 ZLMediaKit 转出的 HTTP-FLV。仅设置预览地址不会启动服务端检测或自动录像。`LIVE_CAPTURE_FRAME_STEP` 控制送入模型的采样间隔；采集线程始终将每帧交给录像器，模型繁忙时推理队列只保留最新一帧。`LIVE_CAPTURE_FPS` 仅在来源未报告有效帧率时作为后备值。网络中断会结束本次事件并显示错误，检查网络后可重新启动；模型不可用时直接报告失败，不产生模拟检测。

开启录制时，推理采样间隔（跳帧数÷源帧率）必须小于离场延时的一半；现场推理结果的实际间隔也必须短于离场延时。服务会检查初始采样及后续结果，间隔过长时明确停止并提示减少跳帧、提高离场延时或优化模型速度，避免一直检测却无法触发确认录像。船舶检测器故障会结束会话并显示错误；仅船名 OCR 或吃水估计失败时继续保留可用的船舶检测及录像。

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
