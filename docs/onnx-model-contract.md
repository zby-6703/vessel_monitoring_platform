# 新模型 ONNX 导出与接入要求

核对日期：2026-10-30。适用于当前 `ONNXShipDETRPredictor`、`ONNXDraftFormerPredictor` 和单帧推理链路。

新版模型 B 采用八类 `0,1,2,3,4,5,6,8`，其中原来的 `M` 已合并到 `0` 类。平台已支持这套语义，并兼容旧九类模型；本文的模型 B 模板及导出示例以新版八类为准。

当前部署包为 `models/shipdet_rtdetrv4_s_960x544` 与 `models/v120_char_water`，部署文件及校验值见 [模型说明](../models/README.md)。以下“当前”形状均指这两套权重。

本文区分“当前后端直接兼容的要求”和“可调整的训练/导出选择”。你可以采用新的轻量骨干、检测头和分割头，不必继续使用 ShipDETR/DraftFormer 架构；但导出的 ONNX 必须符合下面的张量接口，或者以后增加专用后端适配器。仅将 `.pt` 转成 `.onnx`，不代表接口已经兼容。

## 1. 两个模型的职责与接口总表

完整分析链路为：

```text
OpenCV BGR 原始帧
  → 模型 A：船舶 + 船名区域 + 吃水区域检测
      → name 区域裁剪 → 现有独立船名 OCR
      → waterlineArea 区域裁剪 → 模型 B：吃水刻度字符检测 + 水体分割
          → 现有结构约束算法 → 吃水深度（米）
```

模型 B 的“字符”是吃水刻度字符，当前不是船名汉字/字母识别。模型 A 只检测船名位置；替换这两个模型后，完整分析仍需要现有第三个船名 OCR 模型及其字典。

| 项目 | 模型 A：三类检测 | 模型 B：字符检测 + 水体分割 |
| --- | --- | --- |
| 运行时适配器 | `ONNXShipDETRPredictor` | `ONNXDraftFormerPredictor` |
| 输入 | 单个 `float32 [1,3,H,W]` 张量 | 单个 `float32 [1,3,H,W]` 张量 |
| 当前权重输入尺寸 | `[1,3,544,960]` | `[1,3,640,320]` |
| 当前颜色/数值 | RGB、0～1，平台外部 `/255` | RGB、0～1，平台外部 `/255` |
| 输出 0 | `pred_logits [1,N,3]` | 新八类：`pred_logits [1,Q,8]`；旧九类：`[1,Q,9]` |
| 输出 1 | `pred_boxes [1,N,4]` | `pred_boxes [1,Q,4]` |
| 输出 2 | 无 | `pred_masks [1,Q,Hm,Wm]` |
| 当前实际输出 | `N=300` | `Q=101`，前 100 个检测、后 1 个分割；`C=8,Hm=640,Wm=320` |
| 分类分数 | 原始 logits；平台做 sigmoid | 原始 logits；平台做 sigmoid |
| 框格式 | 相对填充后输入图的归一化 `cx,cy,w,h` | 同左；相对吃水裁剪图经过 letterbox 后的输入 |

上表形状已在 Train 环境中读取实际 ONNX 图核实，当前模型的本地接入验证记录见 [部署验收记录](model-deployment-2026-10-30.md)。这不等同于重新完成整个标注数据集的精度评测。

## 2. 共同硬性要求

1. 每个模型只提供一个图像输入。后端只向 `session.get_inputs()[0].name` 喂数据，不会提供额外的原图尺寸、缩放量、阈值或第二路输入。
2. 输入是 NCHW、三通道、`float32`；平台实际按单张处理 `B=1`，输出也只取 `[0]`。动态 batch 不会让当前平台自动批处理。
3. 输出必须是独立张量，并严格保持上表的顺序。代码用 `session.run(None, ...)` 按位置解包，不根据 YAML 中的输出名称重排；不能额外输出中间特征或辅助损失。名称建议统一为 `images`、`pred_logits`、`pred_boxes`、`pred_masks`。
4. 基线版本输入和上述输出统一使用 FP32，不能直接提供 FP16/UINT8 输入；当前预处理始终产生 FP32。内部混合精度可后续评估，但边界类型必须兼容。
5. 类别索引从 0 开始，训练标签、输出通道和部署配置必须一致。返回有限数值，不能出现 NaN/Inf；无目标时用低 logits 表示背景。
6. 后端不注册自定义算子库。导出图应使用目标 ONNX Runtime 可执行的算子，不依赖 ATen/Python 回退、训练仓库插件或 TensorRT 自定义插件。
7. 初版建议使用静态输入、`opset_version=17`，与现有两个模型一致。opset 17 是项目基线，不是代码唯一允许的版本；使用其他版本前必须在部署环境实测。ONNX IR 版本也要与运行时兼容，不要手工改 IR/opset 数字来掩盖不兼容。
8. 交付 `model.onnx` 和同目录的 `deploy_config.yaml`。小模型建议权重内嵌为单文件；若使用 external data，所有外部数据文件必须一并交付且相对路径有效。

本机已核实的 Train 环境：Python 3.11.15、PyTorch 2.10.0+cu128、ONNX 1.22.0、onnxruntime-gpu 1.26.0；项目 CPU/GPU 依赖文件均固定 ONNX Runtime 1.26.0。后续导出应记录实际环境，不能假设训练端最新版本等于部署端版本。

## 3. 图像预处理：训练与推理必须一致

两个适配器共用以下流程：

```text
BGR 图像 → 按配置可选 BGR→RGB → 等比缩放并居中填充
  → HWC→CHW → float32 → 按配置可选归一化 → 增加 batch 维
```

- `input.image_size_wh` 的顺序是 `[宽 W, 高 H]`，不是张量中的 `[H,W]`。
- 等比缩放系数 `r=min(W/w,H/h)`；缩放宽高用 `round`，插值为 OpenCV `INTER_LINEAR`。
- 左/上填充分别为 `(W-round(w*r))//2`、`(H-round(h*r))//2`，余数放在右/下。默认填充值是 0；需要 YOLO 常用的 114 时显式配置 `pad_value: 114`，训练也保持一致。
- 不是强行拉伸，也不执行 auto-stride 最小矩形填充；输出总是配置的固定宽高。
- `image_format` 可为 `RGB` 或 `BGR`；相应的均值、标准差也按该通道顺序填写。

**归一化实际读取的键是 `include_norm_in_model`，默认值为 `true`。** 旧配置中的 `normalization_in_model` 没有被这两个适配器读取，只因默认值为 true 才与当前权重匹配。新配置必须使用正确键名。

| 训练模型真正接收的数值 | 部署配置 | 平台行为 |
| --- | --- | --- |
| 模型内部做归一化，图像入口接收 0～255 | `include_norm_in_model: true` | 直接输入 0～255 FP32；忽略外部 mean/std 运算 |
| 模型接收 RGB 0～1 | `image_format: RGB`、`include_norm_in_model: false`、mean 全 0、std 全 255 | 平台执行 `/255` |
| 模型接收 `(RGB/255-mean01)/std01` | `include_norm_in_model: false`、`pixel_mean=255*mean01`、`pixel_std=255*std01` | 平台执行 `(pixel-pixel_mean)/pixel_std` |

不能只写 `value_range: '[0,1]'` 期待自动缩放；`dtype`、`layout`、`value_range`、`resize`、`opset`、输出说明等元数据也不会自动改变执行逻辑。不要重复执行 `/255` 或归一化。

输入尺寸可以调整，只要模型图与配置一致。当前模型 A 使用 960×544，模型 B 使用 320×640 窄高输入；改变尺寸需要重新检查小字符与水线精度。

## 4. 模型 A：三类检测的详细约定

建议直接保留当前类别表：

| 类别 ID | 必须使用的名称 | 含义 |
| --- | --- | --- |
| 0 | `ship` | 船舶整体区域 |
| 1 | `waterlineArea` | 包含吃水刻度及其附近水体/水线的区域 |
| 2 | `name` | 船名文字区域 |

可以通过配置改变 ID 顺序，但名字必须精确匹配以上字符串（大小写敏感），且与输出通道保持一致。`ship_name_area`、`waterline_area` 是平台结果层名称，不是当前检测器接入时识别的模型类别名。

输出要求：

```text
pred_logits: float32 [1,N,3]  # 不附加背景类，不含 objectness 列
pred_boxes:  float32 [1,N,4]  # cx/W, cy/H, box_width/W, box_height/H
```

- `N` 不必是 300，建议首版固定。三个类别通道全是低 logits 即背景；不得增加 `no_object` 第四类。
- 后端先逐查询取 sigmoid 后的最大类别分数与类别，再以 `score_thresh` 过滤，最后最多保留 `topk` 个框；它不会对分类做 softmax。
- 当前模型 A **没有执行 NMS**。旧 YAML 的 notes 虽提到 NMS，实际实现并未调用；添加 `nms_thresh` 配置也不会生效。新模型若输出大量重复候选，需要在导出包装层产生已去重候选，或后续修改适配器加入 NMS。
- 框必须针对填充后的网络输入坐标系；不能返回原图坐标、像素 `xyxy`、旋转框或未经解码的回归分布。平台会先转 `xyxy`、乘输入宽高，再去填充并除缩放系数，恢复到原帧。
- 无目标的填充槽可用全 `-20` logits、全 0 框。不要把概率 0 直接作为 logit：`sigmoid(0)=0.5` 会产生误检。

平台通过区域中心点是否落入船框来关联区域，完整分析只取该船第一个船名区域及第一个吃水区域。因此训练标签应保证区域有合理上下文、属于相应船舶，尤其吃水区域必须同时覆盖可读刻度与水体交界。完整分析中单独检测到区域而没有 `ship` 框，不会建立船舶结果。

## 5. 模型 B：字符检测与水体分割的详细约定

### 5.1 字符类别与查询排列

新版检测分支严格使用以下八类，原 `M` 目标也输出类别 ID 0：

```text
ID:    0    1    2    3    4    5    6    7
name: '0'  '1'  '2'  '3'  '4'  '5'  '6'  '8'
```

注意 ID 7 对应字符 `'8'`；新模型没有 ID 8，也没有独立的 `M`、字符 `'7'` 或 `'9'` 类。每个框对应单个刻度字符，不是将 `4M`、`42` 或一整行合为一个类别。当前深度模板为 0.2～6.0 米、间隔 0.2 米；其他计量标准需要另行调整物理解析算法。

`0` 类的业务解释已经在平台中实现：

| 检测字符对 | 解析结果 |
| --- | --- |
| 新模型 `40`（右侧 0 可能来自原 M） | 4.0 米 |
| 旧模型 `4M` | 4.0 米 |
| `42` | 4.2 米 |
| `02` | 0.2 米，左侧 0 仍是整数零 |

整米模板的小数位统一为 `0`，旧模型右侧 `M` 按零参与同等匹配评分。别名只应用于小数位，不会把左侧 `M` 当作整数 0，也不会把所有检测到的 `0` 改写成 `M`。API/标注中的新版字符名称保持 `0`，即模型类别本身。

保留旧九类模型时，继续使用其原配置及类别顺序 `0,1,2,3,4,5,6,8,M`、`num_det_classes: 9` 和真实九通道输出。不要只将旧权重配置改成 8，也不需要给新模型补一个虚假的 `M` 输出通道。

定义检测槽数 `Qd`、分割槽数 `Qs>=1`、总槽数 `Q=Qd+Qs`：

```text
pred_logits: float32 [1,Q,8]
pred_boxes:  float32 [1,Q,4]
pred_masks:  float32 [1,Q,Hm,Wm]

前 Qd 槽：
  logits[:Qd,:8]  → 字符分类原始 logits
  boxes[:Qd,:]    → 归一化 cxcywh
  masks[:Qd,:,:]  → 当前平台不使用，但必须保留这些槽

后 Qs 槽：
  logits[Qd:,0:1] → 水体分类原始 logits，water 占第 0 通道
  masks[Qd:,:,:]  → 水体掩码原始 logits
  boxes[Qd:,:]   → 当前平台不使用，可填 0
```

配置中的 `tasks.detection_queries` 必须等于 `Qd`，新模型设置 `tasks.num_det_classes: 8`、`tasks.num_seg_classes: 1`。**不能只返回 `[1,Qs,Hm,Wm]` 的 mask**。分割槽其余 7 个分类通道可填低 logits；分割槽第 0 通道表示 water，检测槽第 0 通道表示字符 0，二者由查询位置区分。

平台会检查类别 ID 从 0 连续编号、类别名称数量与 `num_det_classes` 一致；执行推理时还会检查 logits 通道数、单张 batch、三个输出的 Q 一致且至少保留一个分割槽。八类配置搭配九通道图、错误输出顺序或缺失分割槽将明确报错，避免静默生成错位类别。请检查真正的 ONNX 张量，不只修改 YAML 的说明字段。

`Qd` 不必是 100、`Qs` 不必是 40；`Hm,Wm` 也不强制是 H/4、W/4。它们表示覆盖整个 letterbox 输入的低分辨率网格。增减查询或降低分割分辨率后需实测速度和水线误差。

### 5.2 后端怎样生成水体

后端执行：

```python
sem_water = sum(
    sigmoid(water_class_logit[q]) * sigmoid(mask_logits[q])
    for q in range(Qs)
)
```

随后将分割图线性插值到网络输入大小，去除 letterbox 填充，再缩放回吃水裁剪图大小。多个查询的贡献直接求和，结果可能大于 1，并非 softmax 类别图。

当前 `get_binary_mask(threshold=0.5)` 的实际规则为：

- 若 `sem_water.max() <= 1`，以 `sem_water > 0.5` 判水体。
- 若 `sem_water.max() > 1`，以 `sem_water > max(0.1, 0.3*sem_water.max())` 判水体。
- 最终二值图 `1=水体、0=非水体`；`seg_class_map: {water: 0}` 中的 0 是输出通道编号，不是二值图像素值。

因此不能把二值 0/1 mask、已经 sigmoid 的概率图、背景/水体二通道 argmax 标签，直接当作 `pred_masks`。平台还会对它做 sigmoid 和查询融合。

字符分支将所有 `Qd×8` 分数摊平后取全局 top-k，同一个查询可能产生多个类别候选；随后按配置执行 NMS，默认 `class_aware`、IoU 0.5、top-k 80。若一个框存在多个高分类分数，需在导出包装层处理或检查后续刻度去重的实际效果。

### 5.3 轻量语义分割头如何接入

无需为了接口而训练 40 个分割查询。若新模型返回字符 logits、已解码的归一化字符框，以及单通道水体 logits，可以在导出前加一个包装层，使用 `Qs=1`：

```python
import torch


class DraftExportAdapter(torch.nn.Module):
    def __init__(self, core):
        super().__init__()
        self.core = core

    def forward(self, images):
        # core 的输入归一化方式必须与部署配置一致。
        # 必须自行实现训练网络到以下三个张量的解码：
        det_logits, det_boxes, water_logits = self.core(images)
        # [B,Qd,8]、[B,Qd,4]、[B,1,Hm,Wm]；水体是原始 logits。
        batch, queries, classes = det_logits.shape
        hm, wm = water_logits.shape[-2:]
        water_score = torch.cat([
            det_logits.new_full((batch, 1, 1), 20.0),
            det_logits.new_full((batch, 1, classes - 1), -20.0),
        ], dim=-1)
        logits = torch.cat([det_logits, water_score], dim=1)
        boxes = torch.cat([
            det_boxes, det_boxes.new_zeros((batch, 1, 4))
        ], dim=1)
        masks = torch.cat([
            water_logits.new_zeros((batch, queries, hm, wm)),
            water_logits,
        ], dim=1)
        return logits, boxes, masks
```

这里固定高水体分类分数只是格式桥接，代表使用整个语义分割头的预测；不是用常量代替水体预测。`sigmoid(20)` 接近 1，因此水体主要由 `water_logits` 决定。若模型使用背景/水体二分类 logits，可先取 `water_logit-background_logit` 得到单通道 logit，再包装。实际训练代码尚未提供，此包装层需与新网络的输出对接，并按第 8 节验收。

## 6. YOLO 或其他轻量架构的适配边界

原生 YOLO 的输出布局取决于具体版本与导出方式，不能通过修改 YAML 的 `model_type` 自动兼容。模型注册器始终实例化上面的两个固定适配器，`algorithm` 或模型文件名不会选择新解码器。

导出包装层至少要完成：

1. 解码回归/anchor/DFL 等架构专用表示；将 `[B,C,N]` 等布局转成约定的 `[B,N,C]`。
2. 将框转换为填充后输入图的归一化 `cxcywh`。
3. 若头部输出概率 `p`，先 `clamp(p,eps,1-eps)` 再计算 `log(p/(1-p))`。有 objectness 时先按原网络规则计算最终类别概率，再转 logit；不能丢掉 objectness 或重复乘入。
4. 模型 A 必须解决重复框问题；模型 B 按第 5 节排列检测和分割槽，实例分割的 prototype+coefficients 不能直接当作水体语义输出。
5. 若使用图内 NMS，验证 ONNX 算子、输出数量/顺序与空检测的行为；不能直接输出 `[x1,y1,x2,y2,score,class]` 交给现有解析器。

另一条路线是保留新模型原生 ONNX 输出，并修改 `onnx_backends.py` / `model_registry.py` 添加适配器。该路线属于后续平台开发；本文件描述的直接替换路线无需修改业务链路。

## 7. 部署配置模板与导出参数

可复制以下模板到各自模型目录并命名为 `deploy_config.yaml`：

- [模型 A 配置模板](model-templates/ship_detector.deploy.yaml)
- [模型 B 配置模板](model-templates/draft_multitask.deploy.yaml)

**模板采用当前模型的 RGB、平台外部 `/255`。旧模型若有模型内部归一化，仍须使用其对应配置。** 模型 B 模板使用当前 320×640 输入；实际部署包的配置应与权重一起使用。

`Qd` 只在模型 B 配置中参与检测/分割切分；A 的 `N` 无需另填配置。类别名同时出现在多个字段时要保持一致；B 优先读取 `det_class_map`，A 优先读取 `class_names` 再回退 `class_id_to_name`。八类模板中 `det_class_map`、`class_names`、`tasks.num_det_classes` 已同步；无需添加新的 M→0 配置开关。

导出调用示意如下。`export_model` 必须是已加载训练权重、已经完成上述接口包装的 `torch.nn.Module`；`.pt` 可能是 state_dict、训练 checkpoint 或框架封装对象，没有适用于所有训练架构的通用加载命令。

```python
from pathlib import Path
import torch


def export_platform_onnx(export_model, destination, *, width, height, multitask):
    export_model = export_model.cpu().float().eval()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sample = torch.zeros((1, 3, height, width), dtype=torch.float32)
    output_names = ["pred_logits", "pred_boxes"]
    if multitask:
        output_names.append("pred_masks")
    with torch.no_grad():
        torch.onnx.export(
            export_model, (sample,), str(destination),
            input_names=["images"], output_names=output_names,
            opset_version=17,
            dynamo=False,  # 明确使用 TorchScript 导出路径建立 opset 17 基线
            export_params=True,
            keep_initializers_as_inputs=False,
            external_data=False,
        )
```

示意：A 使用 `width=960,height=544,multitask=False`；B 使用 `width=320,height=640,multitask=True`。必须先依据训练架构加载权重、完成包装，然后再调用函数；随机零输入仅用于导出与形状跟踪，不代表验收样本。

PyTorch 2.10 默认推荐 `dynamo=True`；上面显式选用另一导出路径是为项目 opset 17 基线提供明确示例，不代表新架构都能用这一导出器。可使用 dynamo 导出，但必须检查最终图的真实 opset、输入输出、IR 与运行结果，不能只看调用参数或“导出成功”。参数语义参见 [PyTorch 2.10 ONNX 官方文档](https://docs.pytorch.org/docs/2.10/onnx.html)。

本文的导出函数、语义分割包装层和八类配置模板已使用合成小模型完成 `torch → ONNX checker → ONNX Runtime → 当前平台适配器` 验证，检查了 FP32 数值对齐、RGB `/255`、类别、框坐标及水体掩码。八类 `0` 与旧九类 `M` 的整米解析、跨整米网格匹配和最终吃水计算另有回归测试。这个检查验证接口设计与示例能衔接，不替代新版实际权重的验收。

所有 Python、导出、验证和依赖安装均使用 Conda `Train`，例如：

```powershell
conda run -n Train python export_your_model.py
conda run -n Train python -c "import onnx; onnx.checker.check_model('models/your_model/model.onnx')"
# 若缺少导出依赖，再按训练框架要求在 Train 中安装；不需要为本文修改现有环境。
# conda run -n Train python -m pip install <所需导出依赖>
```

## 8. 替换前的验收要求

1. **结构检查**：ONNX checker 通过；用 ONNX Runtime 读取真实输入输出的名称、顺序、类型、形状；使用真实图片执行后确认所有值有限。新版模型 B 的 `pred_logits` 必须为 `[1,Q,8]`，三个输出的 Q 必须相等，且 Q 大于 `detection_queries`。
2. **数值对齐**：同一张原图使用平台的 `predictor.preprocess(image)` 产生完全相同输入，分别送入 PyTorch 包装模型与 ONNX，再比较原始 logits、框和 mask。FP32 可先以 `atol=1e-4,rtol=1e-3` 为诊断起点，容差需要按模型确定；同时比较解码结果，不能只看均值误差。简化、量化或精度转换后重新对齐。
3. **真实适配器检查**：实例化当前平台的两个适配器，执行 A 的 `predictor(frame)`、B 的 `predictor.predict(draft_crop)`；验证所有类别、框、二值水体与吃水读数，不能只运行裸 `InferenceSession`。
4. **坐标回映**：至少覆盖横图、竖图、非目标宽高比图、边缘目标和小字符；检查框与水线是否因去填充、缩放发生偏移。B 的输出先回到局部裁剪图，平台再叠加区域偏移到整帧，模型不要自行再加原帧偏移。
5. **边界场景**：无船、无字符、无水体、同帧多船、重复候选、反光/倒影、模糊遮挡、不同倍率，以及阈值附近预测。八类模型重点验证原 M 输出为 0 后的 `40→4.0`、`42→4.2`、`02→0.2` 和 `4.2→4.0→3.8` 的跨整米排列。单个 4 不等同于完整 40；未检测到可靠刻度时允许吃水估计失败，不能伪造读数。
6. **业务阈值**：A 适配器默认 0.1、top-k 100；B 默认 0.1、top-k 80。单帧服务还会用 0.3 过滤船/区域/字符，吃水估计器也设置 `min_confidence=0.3`；只调低 YAML 阈值不会取消下游过滤。新模型置信度需要校准。
7. **速度与精度**：在同一设备、同一分辨率和样本集上预热后记录推理延迟 p50/p95、端到端延迟、显存、文件大小；同时记录区域检测精度、小字符召回、水线像素误差和最终吃水误差。模型 B 按每个吃水 ROI 执行，完整链路还包含 OCR、恢复坐标与深度估计；参数少不保证整个平台更快。
8. **部署设备**：至少执行 CPU 验证；实际使用 CUDA 时再执行 CUDA 验证，确认会话激活 `CUDAExecutionProvider`。当前两个部署包要求禁用 TF32；平台在加载 CUDA 库前设置 `NVIDIA_TF32_OVERRIDE=0`，并显式传入 CUDA provider 的 `use_tf32=0`。启动独立外部推理程序时也应在初始化 CUDA 前设置环境变量。当前平台请求 CUDA 却未成功加载该 provider 时会报错；会话保留 CPU provider，部分算子仍可能在 CPU 执行，因此速度结论以实际测量为准。CUDA/cuDNN 包匹配参见 [ONNX Runtime CUDA 官方文档](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)，图版本兼容参见 [官方兼容说明](https://onnxruntime.ai/docs/reference/compatibility.html)。

真实适配器的最小调用示例，保存为脚本后从项目根目录使用 Train 执行：

```python
import sys
from pathlib import Path
import cv2

sys.path.insert(0, str(Path("backend").resolve()))
from app.inference.onnx_backends import (
    ONNXShipDETRPredictor, ONNXDraftFormerPredictor,
)

frame = cv2.imread("your_full_frame.jpg")
crop = cv2.imread("your_draft_crop.jpg")
assert frame is not None and crop is not None
ship = ONNXShipDETRPredictor("models/your_ship/model.onnx", device="cpu")
draft = ONNXDraftFormerPredictor("models/your_draft/model.onnx", device="cpu")
print(ship(frame)["boxes"].numpy())
prediction = draft.predict(crop)
print([(box.class_name, box.score, box.xyxy) for box in prediction.detection.boxes])
mask = prediction.segmentation.get_binary_mask(threshold=0.5)
cv2.imwrite("your_water_mask.png", mask * 255)
```

## 9. 在平台中替换与交付

建议每个模型单独建立新目录保留旧版回退，例如：

```text
models/light_ship_v1/model.onnx
models/light_ship_v1/deploy_config.yaml
models/light_draft_v1/model.onnx
models/light_draft_v1/deploy_config.yaml
```

在“系统设置 → 配置权重”修改前两组配置与权重路径；完整分析继续保留第三组船名识别路径。也可在项目根目录 `.env` 设置：

```dotenv
SHIP_DETECTOR_CONFIG=./models/light_ship_v1/deploy_config.yaml
SHIP_DETECTOR_WEIGHTS=./models/light_ship_v1/model.onnx
DRAFTFORMER_CONFIG=./models/light_draft_v1/deploy_config.yaml
DRAFTFORMER_WEIGHTS=./models/light_draft_v1/model.onnx
```

系统设置保存到 `data/model_settings.json`，其非空值优先于 `.env`。已有界面覆盖时，只修改 `.env` 不会生效；应修改界面路径，或通过“恢复默认路径”清除覆盖。适配器优先读取权重同目录的 `deploy_config.yaml`，不要让界面选择的配置与同目录配置内容冲突；注册器仍要求设置中指定的配置路径实际存在。

更换前先结束排队/运行任务、停止实时采集或回放；保存会释放离线模型，在后续任务加载新模型。独立运行的实时 Worker 需要重启。直接覆盖同路径权重或修改 `.env` 后应重启相关后端进程，避免内存中的旧会话继续运行。

**`/api/health` 中模型 `ready` 目前只代表配置与权重文件存在，不代表成功加载 ONNX、执行推理或接口匹配。** 必须完成上面的真实适配器检查，并通过平台离线完整分析、视频/实时链路复验后再认定接入成功。

每个交付包记录：模型版本、训练架构与代码提交、类别表、输入尺寸/颜色/归一化/填充值、Qd/Qs、输出格式、opset/IR、导出依赖版本、ONNX SHA-256、测试设备与精度/速度报告。训练 `.pt` 和部署 `.onnx` 分别保存便于溯源；权重按项目约定放 Releases 或模型存储，源码仓库只提交配置与轻量文档。

## 10. 对应源码

- [ONNX 预处理与输出解析](../backend/app/inference/onnx_backends.py)
- [模型固定注册逻辑](../backend/app/inference/model_registry.py)
- [类别映射、区域裁剪与完整推理](../backend/app/inference/frame_inference.py)
- [模型路径与配置校验](../backend/app/config.py)
- [健康检查](../backend/app/api.py)
- [吃水刻度语义与水线算法](../runtime_support/algorithms/structure_constrained_vessel_draft_depth_estimation.py)
