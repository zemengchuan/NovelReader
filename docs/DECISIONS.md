# NovelReaderV2 关键决策

## Decision 001：新开项目

决定：

```text
使用 H:\NovelReaderV2 作为正式架构项目。
```

原因：

旧项目 `H:\NovelReader` 已经混合了多套思路：

- 规则路线：`segment -> dialogue -> style`
- 三层上下文路线：`graph -> scene_context -> dialogue`
- LLM-first 路线：`graph -> planner`

继续在旧项目中修改会导致配置、文档、代码互相误导。

## Decision 002：代码不做文学理解

决定：

```text
代码只做工程确定性，LLM 做语义理解。
```

代码负责：

- 切片
- overlap
- 缓存
- 校验
- 复核报告
- 重跑

LLM 负责：

- speaker
- kind
- emotion
- style_prompt
- delivery
- reason

## Decision 003：Character Bible 必要，但不能只是摘要

决定：

```text
保留人物长期记忆，但升级为证据化图谱。
```

原因：

长篇小说需要长期一致性。只靠当前 chunk 容易失忆；只靠一段人物摘要又容易丢失证据。

## Decision 004：图谱采用全书级 + 章节级

决定：

```text
全书级 BookGraph + 章节级 ChapterGraph。
```

全书级记录长期设定、别名、人物关系和跨章节状态。

章节级记录当前章节中的人物、关系、事件、引语证据和 unresolved 问题。

## Decision 005：Planner 直接生成 plan

决定：

```text
Planner 直接生成 plans/*.plan.jsonl。
```

不再默认拆成：

```text
segment -> dialogue -> style
```

原因：

拆得越细，代码越容易接管文学判断，最终又回到规则地狱。

## Decision 006：Review 不自动修正

决定：

```text
Review 只暴露风险，不自动改文学判断。
```

原因：

自动修正很容易再次引入规则误判。Review 应该帮助人工或下一轮 LLM 重跑定位问题。

## Decision 007：TTS 不参与理解

决定：

```text
TTS 只消费 plan，不判断人物和语气。
```

原因：

小说理解和音频生成必须解耦，否则问题难以定位。

## Decision 008：默认主线切到证据化图谱

决定：

```text
默认文本流程改为 ingest -> graph_index -> context_retrieval -> planner -> review。
```

原因：

旧的 `bible -> planner` 只给 Planner 一份人物摘要，容易让模型丢失对白归因证据。新的主线先抽取人物、关系、事件和引语证据，再按 chunk 检索相关上下文，让 Planner 面对的是“当前文本 + 相关证据”，而不是完整图谱或一段抽象总结。

约束：

- `graph_index` 只负责抽取和合并证据化图谱。
- `context_retrieval` 只负责召回相关上下文。
- `planner` 仍由 LLM 判断 speaker、emotion、style_prompt。
- 代码只做校验和风险暴露，不回到规则式文学判断。

