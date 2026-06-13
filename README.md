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

当前已实现最小可用链路：

```text
ingest -> bible -> planner -> review
```

已接入但未完整实测的后续链路：

```text
tts -> audio
```

旧项目 `H:\NovelReader` 只作为原型和可迁移代码来源，不再作为正式架构继续扩写。

## 本地运行

`projects/` 是本地数据目录，不进入 git。先创建本地项目：

```powershell
cd /d H:\NovelReaderV2
mkdir projects\demo\input
copy H:\NovelReader\projects\demo\input\novel.txt projects\demo\input\novel.txt
copy examples\project_config.yaml projects\demo\config.yaml
```

运行文本规划链路：

```powershell
python scripts\run_pipeline.py --project projects\demo --chapter 001 --verbose
```

只重跑某一步：

```powershell
python scripts\run_step.py planner --project projects\demo --chapter 001 --verbose
python scripts\run_step.py review --project projects\demo --chapter 001 --verbose
```

输出：

```text
projects/demo/clean/chapters.jsonl
projects/demo/bible/character_bible.json
projects/demo/state/001.chapter_state.json
projects/demo/plans/001.plan.jsonl
projects/demo/reports/001.review.md
```
