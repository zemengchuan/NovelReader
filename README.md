# NovelReaderV2

NovelReaderV2 是一个中文小说有声书生成项目。

项目目标不是写一堆规则去“猜”小说，而是把小说稳定转换成可供 TTS 使用的朗读计划，并尽量保留人物、关系、情绪、语气和上下文连续性。

## 当前主线

```text
ingest
  -> graph_index
  -> context_retrieval
  -> planner
  -> review
  -> tts
  -> audio
```

核心原则：

- 代码负责切片、缓存、证据定位、校验、重跑和文件交接。
- LLM 负责人物理解、对白归因、情绪判断和朗读计划。
- 知识图谱负责保存人物、别名、关系、事件、引语证据。
- Planner 只读取当前 chunk 和检索出的相关上下文，不直接读取整本图谱。
- Review 只暴露风险，不自动修正文学判断。
- TTS 只消费 plan，不参与小说理解。

## 重要文档

```text
docs/ARCHITECTURE.md
docs/IMPLEMENTATION_PLAN.md
docs/DECISIONS.md
docs/OPEN_SOURCE_REVIEW.md
```

新增架构参考先看：

```text
docs/OPEN_SOURCE_REVIEW.md
```

## 本地项目目录

`projects/` 是本地数据目录，不进入 git。

示例结构：

```text
projects/demo/input/novel.txt
projects/demo/config.yaml
```

可以从示例配置复制：

```powershell
cd /d H:\NovelReaderV2
mkdir projects\demo\input
copy examples\project_config.yaml projects\demo\config.yaml
```

## 快速验证

建议先用示例配置只跑 1 个图谱窗口和 1 个 planner chunk：

```powershell
python scripts\run_pipeline.py --project projects\demo --config examples\project_config.yaml --chapter 001 --verbose
```

如果输出质量可以，再把项目自己的 `projects\demo\config.yaml` 里这些限制改成 0：

```yaml
graph_index:
  max_windows: 0

planner:
  max_chunks: 0
```

然后跑整章：

```powershell
python scripts\run_pipeline.py --project projects\demo --chapter 001 --verbose
```

## 单步重跑

```powershell
python scripts\run_step.py ingest --project projects\demo --chapter 001 --verbose
python scripts\run_step.py graph_index --project projects\demo --chapter 001 --verbose
python scripts\run_step.py context_retrieval --project projects\demo --chapter 001 --verbose
python scripts\run_step.py planner --project projects\demo --chapter 001 --verbose
python scripts\run_step.py review --project projects\demo --chapter 001 --verbose
```

TTS 和音频拼接：

```powershell
python scripts\run_step.py tts --project projects\demo --chapter 001 --verbose
python scripts\run_step.py audio --project projects\demo --chapter 001 --verbose
```

运行 TTS 前，至少要在 `projects\demo\config.yaml` 配一个默认参考音频：

```yaml
tts:
  backend: qwen3_clone
  default_ref_audio: H:\cosyvoice\音频\东雪莲\Azuma_3.wav
  default_ref_text: ""
  auto_transcribe_ref: true
  max_items: 3
```

`default_ref_text` 为空时，会尝试用 Whisper 自动识别参考音频逐字稿。如果你没有安装 Whisper，或者识别不准，可以手动填参考音频对应文本。

如果要给不同角色配不同音色：

```yaml
tts:
  voice_refs:
    旁白:
      ref_audio: H:\path\to\narrator.wav
      ref_text: ""
    叶临渊:
      ref_audio: H:\path\to\male.wav
      ref_text: ""
    裴语涵:
      ref_audio: H:\path\to\female.wav
      ref_text: ""
```

当前 `qwen3_clone` 使用 Qwen3-TTS Base 模型做参考音频克隆。Base 模式没有独立的 `instruct` 参数，所以不会把 `style_prompt` 硬塞进文本里朗读；系统会优先使用 `adapted_text`，并把 emotion/style/delivery 写入每段的 metadata，后续切到 VoiceDesign/CustomVoice 后再作为 instruct 使用。

## 主要输出

```text
projects/demo/clean/chapters.jsonl
projects/demo/graph/book_graph.json
projects/demo/graph/chapters/001.graph.json
projects/demo/contexts/001/0001.context.json
projects/demo/plans/001.plan.jsonl
projects/demo/reports/001.review.md
projects/demo/audio_parts/001/*.wav
projects/demo/output/001.wav
```

## 当前验证状态

已用 `projects/demo/input/novel.txt` 的第 1 章做过小样例验证：

- `graph_index` 抽出人物、关系、事件和引语证据。
- `context_retrieval` 能为 chunk 生成候选人物和证据上下文。
- `planner` 能基于检索上下文输出朗读计划。
- `review` 能标记低置信度、缺少 delivery、需要人工复核等风险。
