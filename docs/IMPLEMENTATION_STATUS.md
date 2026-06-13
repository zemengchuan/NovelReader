# 实现状态

日期：2026-06-13

## 已落地

### 1. 公共 chunk 切分

文件：

```text
novelreader_v2/common/chunking.py
```

作用：

- 统一 planner 和 context retrieval 的 chunk 坐标。
- 避免不同模块各切各的，导致 source_start/source_end 对不上。

### 2. 证据化图谱 schema

文件：

```text
novelreader_v2/common/graph_schema.py
```

新增对象：

- `Evidence`
- `CharacterNode`
- `RelationEdge`
- `StoryEvent`
- `QuoteEvidence`
- `ChapterGraph`
- `BookGraph`
- `RetrievedContext`

### 3. graph_index

文件：

```text
novelreader_v2/graph_index/main.py
```

作用：

- 从章节窗口中抽取人物、关系、事件、引语证据。
- 把 LLM 返回的 evidence 文本定位回章节字符坐标。
- 输出章节图谱和全书图谱。

输出：

```text
projects/demo/graph/chapters/001.graph.json
projects/demo/graph/book_graph.json
```

### 4. context_retrieval

文件：

```text
novelreader_v2/context_retrieval/main.py
```

作用：

- 根据当前 chunk 的文本和证据位置，从 book graph 中召回相关人物、关系、事件、引语证据。
- 控制 Planner prompt 体积。

输出：

```text
projects/demo/contexts/001/0001.context.json
projects/demo/contexts/001/manifest.json
```

### 5. planner 改造

文件：

```text
novelreader_v2/planner/main.py
```

变化：

- 不再读取完整 Character Bible。
- 改为读取 `RetrievedContext`。
- 代码先把 `target_text` 确定性切成 `source_spans`。
- LLM 不再复制原文 `text`，只输出 `span_ids`、speaker、emotion、style_prompt 等标注。
- 每个 `span_id` 必须且只能出现一次，缺失、重复、乱序都会触发自动重试。
- `text` 由代码根据 span 坐标从原文拼出，避免 LLM 复制时删减内容。
- 输出增加 `span_ids`、`intensity`、`adapted_text`、`evidence_ids`。
- 保留 speaker 候选归一化、低置信度复核等工程校验。

### 6. review 加强

文件：

```text
novelreader_v2/review/main.py
```

新增检查：

- 孤立引号、省略号、纯标点碎片。
- dialogue 被归为旁白。
- style_prompt 模板化。
- emotion 为空。
- dialogue 缺少 delivery。
- 具名对白缺少 evidence_ids。

### 7. 命令入口更新

文件：

```text
scripts/run_step.py
scripts/run_pipeline.py
```

默认文本流程：

```text
ingest -> graph_index -> context_retrieval -> planner -> review
```

旧 `bible` 步骤暂时保留为兼容入口，但不再是默认主线。

### 8. TTS 合成模块升级

文件：

```text
novelreader_v2/tts/main.py
```

变化：

- 默认后端改为 `qwen3_clone`。
- 支持 `tts.voice_refs` 为不同 speaker 配不同参考音频。
- 支持 `adapted_text` 优先合成。
- 每段输出对应 metadata：

```text
audio_parts/{chapter_id}/meta/{id}.json
```

- Base clone 模式下不把 `style_prompt` 直接塞进文本，避免模型把指令读出来。
- 预留 `qwen3_voice_design` 和 `qwen3_custom_voice` 后端，后续可把 `style_prompt` 作为 instruct 使用。
- 在加载模型前做配置预检，缺参考音频时直接报出缺失 speaker。

## 验证结果

测试命令：

```powershell
python scripts\run_pipeline.py --project projects\demo --config examples\project_config.yaml --chapter 001 --verbose
```

验证配置：

```yaml
graph_index:
  window_chars: 2500
  max_windows: 1

planner:
  target_chars: 700
  overlap_chars: 400
  max_chunks: 1
```

结果：

- `graph_index` 成功完成，未再触发 length 截断。
- 图谱抽出 3 个角色、12 条引语证据、5 个事件、3 条关系。
- `context_retrieval` 成功生成 1 个 chunk context。
- `planner` 成功完成，生成 15 条 plan item。
- `review` 生成 11 个风险提示，主要集中在 `needs_review=true` 和少量 dialogue 缺少 delivery。

## 已修复的问题

### graph_index 输出过长

现象：

```text
done_reason=length
```

处理：

- 收紧 graph_index JSON schema。
- 给 characters/relations/events/quotes 加 `maxItems`。
- 给文本字段加 `maxLength`。
- 测试配置中把图谱窗口缩到 2500 字。

### planner prompt 撞满上下文

现象：

```text
prompt_tokens + eval_tokens 达到 8192
```

处理：

- 压缩 retrieved_context JSON。
- 限制 context_retrieval 默认召回数量。
- Planner 支持单独设置 `num_ctx`。
- 默认 `planner.num_ctx` 设置为 12288。

### planner 漏原文

现象：

```text
相邻 plan item 的 source_end 和 source_start 之间存在非空原文，音频听起来跳内容。
```

处理：

- 主策略改为 span-id planner：代码确定性切出 `source_spans`，LLM 只能输出 `span_ids`。
- 覆盖校验要求每个 span_id 必须且只能出现一次。
- 覆盖失败会自动把缺失/重复问题反馈给 LLM 并重试。
- 重试失败才兜底按 span 自动补回，并标记 `needs_review=true`。

## 仍需继续

### 1. 图谱质量评估

当前只验证了小样例能跑通，还没有系统评估：

- 角色是否漏抽。
- 别名是否错误合并。
- 关系是否过度概括。
- 引语 speaker 证据是否足够。

### 2. Planner 质量评估

需要人工抽样检查：

- speaker 是否合理。
- style_prompt 是否真的有表演价值。
- 语气词是否被合并到正确上下文。
- 低置信度是否被标出。

### 3. TTS 消费 style_prompt

当前 `qwen3_clone` 使用 Qwen3-TTS Base 模型。Base 模型没有单独 `instruct` 参数，所以不会直接消费：

- `emotion`
- `intensity`
- `style_prompt`
- `delivery`

这些字段已经写入每段 metadata。要真正作为指令控制语气，需要切到 `qwen3_voice_design` 或 `qwen3_custom_voice` 后端，或者后续增加“VoiceDesign 生成参考音频，再 Base 克隆”的流程。
