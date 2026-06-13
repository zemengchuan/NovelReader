# NovelReaderV2

NovelReaderV2 是一个中文小说有声书生成项目。

项目目标不是做复杂规则 NLP，而是把中文小说稳定转换成可供 TTS 使用的朗读计划，并尽可能保留人物、语气、情绪和上下文连续性。

## 核心路线

```text
ingest
  -> bible_update
  -> chapter_planner
  -> review
  -> tts
  -> audio
```

核心原则：

- 代码负责切片、缓存、校验、重跑、文件交接。
- LLM 负责人物理解、对白归因、语气判断、朗读计划。
- Character Bible 作为人物长期记忆，不作为硬编码规则引擎。
- Review 阶段只做工程校验，不做文学判断。

## 重要文档

```text
docs/ARCHITECTURE.md
docs/IMPLEMENTATION_PLAN.md
docs/DECISIONS.md
```

先读 `docs/ARCHITECTURE.md`，再看 `docs/IMPLEMENTATION_PLAN.md`。

## 当前状态

这是 V2 干净项目骨架。旧项目 `H:\NovelReader` 只作为原型和可迁移代码来源，不再作为正式架构继续扩写。

