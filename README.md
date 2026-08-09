# VesselSight 船舶智能监测平台

VesselSight 是一个可独立部署的船舶视频/图像分析平台。它提供船舶与区域检测、船名识别、吃水读数估计、视频跟踪、离线批处理、结果归档，以及用于现场系统对接的实时 WebSocket 接口。

源代码、运行数据和模型权重已分离：克隆本仓库即可构建服务；在执行真实推理前，只需按 [models/README.md](models/README.md) 放入经过授权的 ONNX 权重。

## 功能范围

- 离线图像和视频分析，支持标注结果、JSONL、CSV 和可视化输出。
- 船舶检测、吃水区域/刻度检测、水体分割、船名 OCR 与结构约束吃水估计。
- 视频 TrackTrack 跟踪与单船实例聚合。
- SQLite（本地开发）或 MySQL（Docker 部署）持久化。
- REST API、OpenAPI 文档和 Vue 前端。
- 可选 ZLMediaKit 视频流转发；未配置流媒体时离线分析仍可使用。

## 项目结构

```text
vessel_monitoring_platform/
├── backend/              FastAPI 服务、推理和测试
├── frontend/             Vue 3 + Vite 前端
├── runtime_support/      独立于训练仓库的推理期算法
├── models/               模型部署配置；权重需另行提供
├── assets/               本地数据资产（不随仓库发布）
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

编辑 `.env`，至少替换 `MYSQL_PASSWORD`、`MYSQL_ROOT_PASSWORD` 和 `API_INGEST_TOKEN` 为强随机值。随后按 [models/README.md](models/README.md) 将三个 ONNX 权重置于 `models/`。`.env`、权重、上传文件、数据库和结果均不会被提交到 Git。

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
conda run -n Train pip install -r backend/requirements-gpu.txt

# 或 CPU 环境
# conda run -n Train pip install -r backend/requirements-cpu.txt

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

如果启动时报 `Port 8010 is already in use` 或 `Port 5173 is already in use`，通常表示平台已经运行，而不是模型错误。先执行：

```powershell
.\scripts\status.ps1
```

若 API 和 Web UI 均为 `Listening`，直接打开前端即可；若要重启，执行 `stop-dev.ps1` 后再执行 `start-dev.ps1`。不要同时重复运行启动脚本。

本地开发默认不自动启动 Redis，因此 `status.ps1` 可能显示 `Redis realtime: unavailable`。这只会禁用 Redis 队列/实时结果链路；数据库、模型检查和离线图像/视频分析仍可正常使用。需要 Redis 时，可启动 Docker 服务：

```powershell
docker compose up -d redis
```

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
4. 将模型权重发布到 GitHub Releases、对象存储或受控模型仓库；提供版本号、SHA-256 和下载说明。
5. 在干净的 Docker Desktop 主机上执行“通过 Docker 运行”的 CPU 流程，确认健康检查和前端可用。

## 注意事项

- 不提供权重时，平台可以启动，但真实推理任务会失败并报告模型缺失；不会使用虚构检测结果替代。
- Docker 默认使用 CPU 以保证可移植性；GPU 性能部署必须使用 `docker-compose.gpu.yml`。
- 当前实时入口用于接收外部实例事件和本地视频回放；Redis 队列式独立推理 worker 不包含在本仓库中。
- 对公网部署前必须修改 `.env` 中的密码和令牌，并通过防火墙/反向代理限制 MySQL、Redis、API 和流媒体端口。
