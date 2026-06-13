# NovelReaderV2 实施计划

本文档用于固定开发路线，避免后续继续偏移。

## 一、当前目录

```text
H:\NovelReaderV2
```

旧项目：

```text
H:\NovelReader
```

旧项目只作为原型和代码来源，不再继续承载正式架构。

## 二、阶段路线

### Phase 0：项目骨架

目标：

- 创建 V2 项目目录。
- 固定架构文档。
- 固定实施计划。
- 固定关键决策。

产物：

```text
README.md
docs/ARCHITECTURE.md
docs/IMPLEMENTATION_PLAN.md
docs/DECISIONS.md
configs/default.yaml
pyproject.toml
```

验收：

- 文档能清楚说明主流程。
- 默认流程只有一条主线：`ingest -> bible_update -> planner -> review -> tts -> audio`。
- 旧规则路线不进入主流程。

### Phase 1：基础工程层

目标：

- 实现配置读取。
- 实现 JSON/JSONL 读写。
- 实现 Ollama 调用。
- 实现日志。
- 实现项目路径解析。

可迁移旧项目代码：

```text
H:\NovelReader\novelreader\common\config.py
H:\NovelReader\novelreader\common\jsonio.py
H:\NovelReader\novelreader\common\ollama.py
```

V2 目标路径：

```text
H:\NovelReaderV2\novelreader_v2\common
```

验收：

- 能读取 `configs/default.yaml`。
- 能合并 `projects/demo/config.yaml`。
- 能写 UTF-8 JSON/JSONL。
- 中文不出现连续问号占位。

### Phase 2：ingest

目标：

- 读取小说文本。
- 清洗换行、BOM、空格。
- 保留章节结构。
- 输出章节 JSONL。

输入：

```text
projects/demo/input/novel.txt
```

输出：

```text
projects/demo/clean/chapters.jsonl
```

章节格式：

```json
{
  "chapter_id": "001",
  "title": "第一章 那年有个少女",
  "text": "章节正文..."
}
```

验收：

- 中文章节标题保留。
- 正文不乱码。
- 每章文本可追溯回原文。

### Phase 3：Character Bible

目标：

- 读取章节文本。
- 读取已有 Character Bible。
- 调用 LLM 更新人物卡。
- 输出全书级人物设定。

输入：

```text
clean/chapters.jsonl
bible/character_bible.json
```

输出：

```text
bible/character_bible.json
state/{chapter_id}.chapter_state.json
```

Character Bible 字段：

```json
{
  "characters": {
    "裴语涵": {
      "name": "裴语涵",
      "aliases": ["裴仙子", "女剑仙"],
      "gender": "女",
      "identity": "剑宗人物，叶临渊旧徒",
      "personality": ["清冷", "骄傲", "克制"],
      "speech_style": "言辞简短，冷硬，压怒时更平静",
      "voice_style": "清冷少女音，语速偏慢，尾音收紧",
      "relations": {
        "叶临渊": "师徒/旧日牵绊"
      },
      "evidence": ["原文证据"]
    }
  }
}
```

验收：

- 人物不拼音化。
- 别名能归并到人物。
- 关系和性格有证据。
- 不把旁白、声音、房间等当人物。

### Phase 4：Planner

目标：

- 以章节为单位切 chunk。
- 每个 chunk 带前后 overlap。
- 把 Character Bible 和 Chapter State 喂给 LLM。
- LLM 直接生成朗读计划。

输入：

```text
clean/chapters.jsonl
bible/character_bible.json
state/{chapter_id}.chapter_state.json
```

输出：

```text
plans/{chapter_id}.plan.jsonl
```

Planner prompt 必须包含：

- 候选 speaker。
- 相关人物卡。
- 当前章节状态。
- 上一个 chunk 摘要。
- context_before。
- target_text。
- context_after。

硬约束：

- 只能输出 target_text 中的原文。
- 不能输出 overlap 文本。
- speaker 必须来自候选或允许的未知角色。
- 每条必须有 confidence 和 reason。

验收重点样例：

```text
「嗯……嗯嗯……唔……」
```

期望：

- speaker 能结合前后文判到具体人物，或低置信度标记复核。
- style_prompt 不能是空泛模板。
- emotion 必须体现断续、压抑、强忍等上下文。

### Phase 5：Review

目标：

- 不修文学判断，只暴露风险。
- 生成人工可读报告。

输出：

```text
reports/{chapter_id}.review.md
```

检查：

- 原文匹配失败。
- speaker 不在候选中。
- confidence 低。
- needs_review 为 true。
- reason 为空。
- style_prompt 过短。
- chunk 输出异常。

验收：

- 能列出所有高风险 plan 行。
- 报告中包含 id、speaker、text、reason。

### Phase 6：TTS

目标：

- 复用旧项目 Qwen3-TTS 接入。
- 消费 plan。
- 生成分段音频。

可迁移旧项目代码：

```text
H:\NovelReader\novelreader\tts\main.py
```

V2 输出：

```text
audio_parts/{chapter_id}/{id}.wav
```

验收：

- 能根据 speaker 选择默认音色或角色音色。
- 能跳过已存在音频。
- 失败不影响已完成分段。

### Phase 7：Audio

目标：

- 拼接分段音频。
- 根据 pause_after_ms 插入停顿。
- 输出完整章节音频。

输出：

```text
output/{chapter_id}.wav
```

验收：

- 音频顺序正确。
- 停顿生效。
- 采样率一致。

## 三、默认运行命令

未来目标命令：

```powershell
cd /d H:\NovelReaderV2
python scripts\run_pipeline.py --project projects\demo --chapter 001 --verbose
```

单步命令：

```powershell
python scripts\run_step.py ingest --project projects\demo
python scripts\run_step.py bible --project projects\demo --chapter 001
python scripts\run_step.py planner --project projects\demo --chapter 001
python scripts\run_step.py review --project projects\demo --chapter 001
python scripts\run_step.py tts --project projects\demo --chapter 001
python scripts\run_step.py audio --project projects\demo --chapter 001
```

## 四、测试文件路径

Demo 输入：

```text
H:\NovelReaderV2\projects\demo\input\novel.txt
```

如果需要复用旧测试文本：

```text
H:\NovelReader\projects\demo\input\novel.txt
```

参考音频可复用旧路径：

```text
H:\cosyvoice\音频
```

## 五、验收标准

第一章测试必须至少检查：

- 叶临渊自言自语能归到叶临渊。
- 裴语涵对白能归到裴语涵。
- 季修对白能归到季修或低置信度复核，不应错归到裴语涵。
- 语气词片段能结合前后文判断 speaker。
- `style_prompt` 不出现大量空泛模板。
- `reason` 能说明依据。
- 低置信度条目进入 review 报告。

## 六、不要做的事

- 不要把旧项目的 `segment/dialogue/style` 规则链迁移成主线。
- 不要继续添加正则去猜复杂对白。
- 不要让 TTS 阶段参与小说理解。
- 不要把 Character Bible 当固定规则表。
- 不要在没有 review 的情况下直接大批量合成音频。

