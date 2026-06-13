# NovelReaderV2 架构

## 一、项目目标

NovelReaderV2 的目标是：

```text
中文小说文本
  -> 有角色、有情绪、有停顿的朗读计划
  -> TTS 分段音频
  -> 完整有声书音频
```

项目优先解决四件事：

1. 人物对白尽可能识别正确。
2. 语气尽可能贴合上下文和人物性格。
3. 人物设定在长篇小说中保持一致。
4. 让瓶颈变成 LLM/TTS 模型能力，而不是代码规则误判。

不追求：

- 不用代码规则硬猜文学语义。
- 不维护复杂正则对白归因系统。
- 不把 `segment/dialogue/style` 拆成互相依赖的规则链。

## 二、总流程

正式主流程：

```text
ingest
  -> bible_update
  -> chapter_planner
  -> review
  -> tts
  -> audio
```

对应文件流：

```text
projects/{name}/input/novel.txt
  -> projects/{name}/clean/chapters.jsonl
  -> projects/{name}/bible/character_bible.json
  -> projects/{name}/state/chapter_state.json
  -> projects/{name}/plans/plan.jsonl
  -> projects/{name}/reports/review.md
  -> projects/{name}/audio_parts/*.wav
  -> projects/{name}/output/final.wav
```

## 三、核心思想

### 代码做什么

代码只负责工程确定性：

- 读取和清洗文本。
- 保留章节结构。
- 创建带 overlap 的文本切片。
- 组织 prompt。
- 调用 LLM。
- 校验 JSON/JSONL。
- 检查 text 是否来自原文。
- 检查 speaker 是否可解释。
- 生成 review 报告。
- 缓存 chunk 结果，支持重跑。
- 调用 TTS。
- 拼接音频。

### 代码不做什么

代码不负责文学理解：

- 不用正则猜说话人。
- 不用规则判断“那人”是谁。
- 不用标点规则强行判断语气。
- 不用 if/else 推断人物情绪。
- 不把“知识图谱”当成判定器。

### LLM 做什么

LLM 负责语义任务：

- 判断当前片段有哪些朗读单元。
- 判断旁白、对白、内心独白、语气词、沉默。
- 判断 speaker。
- 结合人物卡判断语气。
- 写出可供 TTS 使用的 style prompt。
- 给出 confidence 和 reason。
- 标出 needs_review。

## 四、Character Bible

Character Bible 是人物长期记忆，也可以理解为角色圣经。

它是必要的，但作用不是替代码做规则判断，而是给 LLM 提供人物上下文。

### 构建粒度

采用两层：

```text
全书级 Character Bible
章节级 Chapter State
```

全书级 Character Bible 保存长期信息：

- 人物名。
- 别名。
- 性别。
- 身份。
- 性格。
- 关系。
- 说话习惯。
- 声音基线。
- 关键证据。

章节级 Chapter State 保存当前章节状态：

- 本章出现人物。
- 当前场景人物。
- 临时未具名角色。
- 本章人物关系变化。
- 上一 chunk 摘要。

### 为什么不是只按整本小说

整本小说一次性构建成本太高，也容易超过上下文。长篇小说还会有人物关系变化。

### 为什么不是只按章节

只按章节会失去长期一致性。人物别名、说话习惯、关系历史很容易断。

### 推荐方式

```text
处理第 1 章：
  读取空 Character Bible
  LLM 解析章节
  写入 character_bible.json
  写入 chapter_state.json

处理第 N 章：
  读取已有 Character Bible
  LLM 解析当前章节
  更新 Character Bible
  写入当前 chapter_state.json
```

## 五、Planner

Planner 是 V2 的核心。

输入：

```text
chapter_text
character_bible
chapter_state
previous_chunk_summary
context_before
target_text
context_after
```

输出：

```text
plans/plan.jsonl
```

Planner 切片策略：

```text
target_text: 当前正式处理文本
context_before: 前文 overlap，只用于理解
context_after: 后文 overlap，只用于理解
```

LLM 只能输出 `target_text` 中的原文内容，不能输出 overlap 内容。

## 六、Plan 数据契约

`plans/plan.jsonl` 每行一个朗读单元。

```json
{
  "id": 1,
  "chapter_id": "001",
  "chunk_id": 1,
  "source_start": 0,
  "source_end": 32,
  "speaker": "裴语涵",
  "text": "「再让我听到你诬蔑家师，我就杀了你。」",
  "kind": "dialogue",
  "emotion": "压怒、威胁",
  "style_prompt": "声音压低，冷静但带杀意，咬字清楚，尾音收紧。",
  "delivery": {
    "pace": "slow",
    "volume": "low",
    "pitch": "cold",
    "breath": "controlled",
    "emphasis": ["诬蔑家师", "杀了你"]
  },
  "pause_after_ms": 700,
  "confidence": 0.91,
  "reason": "前文裴语涵因师尊被辱而拔剑威胁。",
  "needs_review": false
}
```

字段说明：

- `speaker`: 优先来自 Character Bible。无法确定时允许 `未具名男声`、`未具名女声`、`未知角色`。
- `text`: 必须来自原文，不能改写。
- `kind`: `narration` 或 `dialogue`。内心独白、语气词、喊叫都归入 `dialogue`，细节放入 `emotion/style_prompt`。
- `style_prompt`: 给 TTS 的核心语气提示。
- `delivery`: 结构化语气参数。
- `confidence`: LLM 对 speaker/kind/style 判断的置信度。
- `reason`: 必须说明依据，方便人工复核。
- `needs_review`: 低置信度或校验异常时为 true。

## 七、Review

Review 阶段不做文学判断，只做工程校验。

检查项：

- `text` 是否能在原文中找到。
- `speaker` 是否在 Character Bible 或允许的未知角色中。
- `confidence < threshold`。
- `reason` 是否为空。
- `style_prompt` 是否过短或模板化。
- overlap 是否被重复输出。
- 单个 chunk 是否输出过少或过多。

输出：

```text
reports/review.md
```

Review 的目标不是自动修正，而是把风险暴露出来。

## 八、TTS

TTS 只消费 `plans/plan.jsonl`。

TTS 不负责：

- 不判断说话人。
- 不判断语气。
- 不更新人物关系。
- 不修正文案。

TTS 负责：

- 根据 speaker 选择音色。
- 根据 text/style_prompt/delivery 合成音频。
- 输出分段 wav。
- 记录失败和耗时。

## 九、模块边界

```text
common/
  通用 JSON、配置、日志、LLM 调用

ingest/
  文本清洗和章节切分

bible/
  Character Bible 更新

planner/
  LLM-first 朗读计划生成

review/
  工程校验和报告

tts/
  TTS 合成

audio/
  音频拼接
```

## 十、架构底线

后续开发必须遵守：

- 不把文学理解写进正则和 if/else。
- 不让旧的 `segment/dialogue/style` 规则链回到主流程。
- 不绕过 Character Bible 直接让每个 chunk 失忆判断。
- 不允许 LLM 自由编写不在原文中的朗读文本。
- 所有中间产物必须落盘，可人工检查，可单步重跑。

