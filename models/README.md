# 当前模型部署包

2026-10-30 起，平台默认使用以下三个独立 ONNX 模型。前两套由本机 `E:/projects/ShipDraft/deployments/` 的同名交付目录复制并校验，船名 OCR 继续使用现有模型。

```text
models/
├── shipdet_rtdetrv4_s_960x544/
│   ├── deploy_config.yaml
│   ├── delivery_manifest.json
│   └── model.onnx
├── v120_char_water/
│   ├── deploy_config.yaml
│   ├── delivery_manifest.json
│   └── model.onnx
└── svtrv2parseq_RecShipName/
    ├── deploy_config.yaml
    ├── character_dict.txt
    └── model.onnx
```

| 部署包 | 输入 NCHW | 任务与类别 | ONNX 字节数 |
| --- | --- | --- | --- |
| `shipdet_rtdetrv4_s_960x544` | `[1,3,544,960]` | `ship,waterlineArea,name` | 41,934,427 |
| `v120_char_water` | `[1,3,640,320]` | `0,1,2,3,4,5,6,8`，原 M 合并进 0；单通道水体 | 41,879,654 |

两个模型均为 FP32、RGB，平台外部 `/255`、居中零填充。多任务输出为 `[1,101,8]`、`[1,101,4]`、`[1,101,640,320]`，前 100 槽检测、最后 1 槽分割。船舶模型输出 `[1,300,3]` 与 `[1,300,4]`，不执行 NMS。

ONNX SHA-256：

```text
922c3d4e271b23ddc621693a46698db11412c432ab3fa9ccc45e04f425e8c1e9  shipdet_rtdetrv4_s_960x544/model.onnx
fd6d0635733dce1a1c6e76855134ff1d1655d6e63434e5abad02d953bdc803e0  v120_char_water/model.onnx
```

CUDA 会话由平台在加载 CUDA 库前设置 `NVIDIA_TF32_OVERRIDE=0`，并传入 `use_tf32=0`，与交付包验证条件一致。完整分辨率水体输出含未使用的检测 mask 槽，单次 mask 约 78.91 MiB；实际延迟包括输出拷贝，不能仅凭权重大小推算速度。

默认路径已经写入 `backend/app/config.py` 与 `.env.example`。已有安装还需同步自己的 `.env`；界面设置产生的 `data/model_settings.json` 优先于 `.env`。切换模型时配置与权重必须来自同一个部署包，后端及独立 Worker 应重新启动。

Git 仅保存模型说明、部署 YAML 和交付清单。权重、训练 checkpoint、交付 ZIP、运行结果不提交源码仓库。新模型权重尚未发布到历史 [resources-v0.1.0](https://github.com/zby-6703/vessel_monitoring_platform/releases/tag/resources-v0.1.0)，该 release 仅继续提供运行数据和船名 OCR 等资源。其他主机部署时，请单独取得两套新 ONNX 权重并核对以上哈希。

健康页的 `ready` 仅检查文件存在；实际接入验证见 [2026-10-30 验收记录](../docs/model-deployment-2026-10-30.md)。接口与后续导出要求见 [ONNX 接入规范](../docs/onnx-model-contract.md)。
