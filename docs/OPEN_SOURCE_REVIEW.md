# 开源项目调研与架构落地建议

日期：2026-06-13

这份文档用来约束 NovelReaderV2 的下一步设计：我们不再凭直觉继续堆规则，而是参考已有项目的成熟做法，把“小说理解、人物关系、对白归因、语气规划、TTS 合成”拆成清晰的模块。

## 结论先行

当前方向是对的：不要让代码用正则和启发式规则去理解小说。代码应该负责可追溯、可复跑、可校验；小说理解交给 LLM，但必须给 LLM 提供经过检索的、证据化的人物上下文。

知识图谱是必要的，但它不应该是一个简单的 `characters.json` 摘要文件。它应该是“人物卡 + 别名 + 关系边 + 事件证据 + 引语证据”的组合，并且在 Planner 生成朗读计划时按需检索，而不是整本书一次性塞进 prompt。

LangGraph 暂时不应该作为第一优先级。它适合做长流程编排、断点恢复、人工介入和状态持久化，但它不是知识图谱本身。我们应该先把图谱数据结构和检索接口做稳，再考虑是否用 LangGraph 管理流程。

## 参考项目

### BookNLP

链接：[booknlp/booknlp](https://github.com/booknlp/booknlp)

它最值得参考的不是代码可直接复用，而是“书籍级 NLP 流程”的拆法。BookNLP 面向英文长文档，包含实体识别、人物名聚类、共指消解、引语说话人识别、事件标注等能力。它还会输出实体、引语、整本书人物信息和带标注 HTML。

对 NovelReaderV2 的启发：

- 人物识别不能只靠当前 chunk，必须做书籍级别的别名合并。
- 对白归因应该单独建模，不能混在普通文本切句里。
- 每个说话人判断都应该能回溯到“引语文本、归因词、附近动作、候选人物”。
- 人物卡不只写性格，还应该积累行动、被作用对象、修饰词、称呼和关系。

不能照搬的部分：

- BookNLP 主要面向英文文学文本，中文网文的引号、称谓、省略主语、动作描写方式差异很大。
- 它的模型和数据集不直接解决中文 speaker attribution。

### 中文文学 NER / RE 数据集

链接：[Chinese-Literature-NER-RE-Dataset](https://github.com/lancopku/Chinese-Literature-NER-RE-Dataset)

这个项目提供中文文学文本的命名实体识别和关系抽取数据集，重点是中文文学场景下的实体与关系标注。

对 NovelReaderV2 的启发：

- 中文文学人物关系可以被结构化标注，不是只能靠一段自然语言总结。
- 图谱字段应该区分实体、别名、关系类型、证据位置，而不是只保存一句“他们关系复杂”。
- 后续如果要做微调或评测，可以参考这类数据集的标注方式。

不能照搬的部分：

- 它是数据集，不是完整有声书生成系统。
- 它不能直接解决“当前这句对白是谁说的”和“应该用什么语气读”。

### Microsoft GraphRAG

链接：[microsoft/graphrag](https://github.com/microsoft/graphrag)

GraphRAG 的核心思想是用 LLM 从非结构化文本中抽取结构化数据，再用知识图谱增强后续推理。它也明确提醒：索引成本可能很高，并且需要做 prompt tuning。

对 NovelReaderV2 的启发：

- 先索引，再生成。不要让 Planner 每次从零理解整章。
- 图谱里的每个结论都应带 evidence，后续 Planner 只使用能追溯的上下文。
- 图谱更新应该是独立步骤，可以重跑、缓存、调试。
- 不要一开始就全书重索引，应该先按章节增量构建，再逐步合并到全书图谱。

不能照搬的部分：

- GraphRAG 是通用私有数据问答方案，不是小说有声书流程。
- 它的社区摘要、全局问答等概念对我们未必划算，可能会增加本地 Ollama 的调用成本。

### LightRAG

链接：[HKUDS/LightRAG](https://github.com/HKUDS/LightRAG)

LightRAG 的重点是同时管理知识图谱和向量表示，用双层检索解决普通 chunk RAG 容易割裂上下文的问题，并支持增量更新。

对 NovelReaderV2 的启发：

- 我们需要“双检索”：既检索人物/关系图谱，也检索原文证据片段。
- Planner 不应该看到完整图谱，而应该看到与当前 target chunk 相关的人物卡、关系边、最近事件和引语证据。
- 图谱更新应该支持增量合并，否则长篇小说会越来越难跑。

不能照搬的部分：

- 直接引入 LightRAG 会带来新的存储、embedding、服务配置复杂度。
- 当前阶段应该先实现轻量本地图谱接口，等接口稳定后再决定是否替换为 LightRAG。

### LangGraph

链接：[LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)

LangGraph 是长流程、状态化 agent 的编排框架，重点能力是持久化、人工介入、流式状态、调试和恢复。

对 NovelReaderV2 的启发：

- 如果后续流程变成“图谱抽取 -> 人物合并 -> 人工确认 -> Planner 重跑 -> TTS 批处理 -> 错误续跑”，LangGraph 会有价值。
- 它可以管理 workflow，但不应该承担知识图谱建模。

当前决策：

- 暂不引入 LangGraph。
- 先把模块接口稳定下来：`ingest`、`graph_index`、`context_retrieval`、`planner`、`review`、`tts`、`audio`。
- 当人工复核和断点恢复成为主要痛点时，再引入 LangGraph。

## 对 NovelReaderV2 的架构调整

### 新主线

```text
ingest
  -> graph_index
  -> context_retrieval
  -> planner
  -> review
  -> tts
  -> audio
```

### 模块职责

`ingest`：

- 只负责读取、清洗、章节切分。
- 不判断人物，不切对白。

`graph_index`：

- 从章节或整本小说中抽取人物、别名、关系、事件、引语线索。
- 输出证据化图谱。
- 允许增量更新。

`context_retrieval`：

- 根据当前 target chunk 找出相关人物、关系、最近事件、原文证据。
- 控制 prompt 体积。
- 不生成最终朗读计划。

`planner`：

- 只面对当前 target chunk 和检索上下文。
- 由 LLM 自行决定切分、speaker、emotion、style_prompt、pause_after_ms。
- 代码只校验输出是否来自 target chunk，不能替 LLM 猜 speaker。

`review`：

- 暴露风险，不自动“修文学判断”。
- 检查 speaker 低置信度、文本缺失、证据不足、style 空泛、引号碎片等问题。

`tts`：

- 只消费 plan。
- 不参与小说理解。

`audio`：

- 只负责拼接、停顿、导出。

## 图谱数据结构建议

不要只保存一个扁平人物卡。建议拆成五类对象。

### Character

```json
{
  "id": "char_pei_yuhan",
  "canonical_name": "裴语涵",
  "aliases": ["语涵", "裴仙子"],
  "gender": "女",
  "role": "剑宗人物",
  "personality": ["克制", "骄傲"],
  "speech_style": "话少，压抑情绪时语气变硬",
  "voice_style": "清冷少女音，尾音收紧",
  "confidence": 0.82,
  "evidence_ids": ["ev_001", "ev_019"]
}
```

### Relation

```json
{
  "source": "char_pei_yuhan",
  "target": "char_ji_xiu",
  "type": "冲突/压制",
  "attitude": "厌恶、屈辱、强忍",
  "confidence": 0.76,
  "evidence_ids": ["ev_041"]
}
```

### Event

```json
{
  "id": "event_040",
  "chapter_id": "001",
  "summary": "裴语涵被迫压抑声音，情绪高度紧绷。",
  "participants": ["char_pei_yuhan"],
  "impact": "后续语气应更压抑、断续、带忍耐感",
  "evidence_ids": ["ev_040"]
}
```

### QuoteEvidence

```json
{
  "id": "quote_329",
  "chapter_id": "001",
  "text": "嗯……嗯嗯……唔……",
  "candidate_speakers": ["char_pei_yuhan"],
  "speaker": "char_pei_yuhan",
  "confidence": 0.68,
  "reason": "后文明确写到裴语涵咬牙不让自己出声",
  "evidence_ids": ["ev_328", "ev_332"]
}
```

### Evidence

```json
{
  "id": "ev_332",
  "chapter_id": "001",
  "text": "裴语涵死死地咬着牙齿，不让自己出声。",
  "start_char": 12345,
  "end_char": 12368
}
```

## 知识图谱应该怎么构建

### 不建议

- 不建议只按当前 chunk 生成人物卡。
- 不建议一上来全书塞给 LLM 做一次总结。
- 不建议把图谱当规则引擎，用代码硬匹配谁在说话。

### 建议

第一层：章节级抽取。

- 每章跑一次 `graph_index`。
- 抽取本章新人物、别名、关系、事件、引语候选。
- 每条结论必须带原文 evidence。

第二层：全书级合并。

- 合并别名。
- 合并重复人物。
- 更新长期性格、关系变化、说话习惯。
- 不删除旧证据，只调整置信度。

第三层：Planner 前检索。

- 对当前 target chunk 先做轻量 mention scan。
- 根据提到的人物、前后文动作、上一个 chunk 的 speaker，召回相关人物卡、关系边、最近事件、QuoteEvidence。
- 把召回结果喂给 Planner。

## 如何提高对白归因

重点不是写更多正则，而是让 LLM 看到正确上下文。

Planner prompt 应该包含：

- 当前 target chunk。
- 前后 overlap。
- 当前场景候选人物。
- 候选人物的别名。
- 候选人物之间的关系。
- 最近 3 到 10 个相关事件。
- 与当前引语相邻的动作描写。
- 上一条 plan 的 speaker 和情绪。

Planner 输出必须包含：

```json
{
  "speaker": "裴语涵",
  "confidence": 0.68,
  "reason": "后文动作描写指向裴语涵，且当前场景只有她处于压抑出声状态",
  "needs_review": true
}
```

低置信度不是失败。真正的问题是低置信度没有被标出来。

## 如何提高语气贴合度

语气不应该只是一句模板。每条 plan 至少应包含：

- `emotion`：情绪标签，如压抑、愤怒、惊慌、讥讽、温柔。
- `intensity`：情绪强度，建议 1 到 5。
- `delivery`：语速、音量、停顿、气声、断续、重音。
- `style_prompt`：给 TTS 的自然语言指令。
- `adapted_text`：可选，允许为了朗读自然做轻微表达调整。

示例：

```json
{
  "text": "嗯……嗯嗯……唔……",
  "speaker": "裴语涵",
  "emotion": "压抑、羞耻、强忍",
  "intensity": 4,
  "delivery": {
    "speed": "slow",
    "volume": "low",
    "pause": "broken",
    "breath": "restrained",
    "emphasis": ["嗯", "唔"]
  },
  "style_prompt": "低声、断续、强忍，不要平铺直叙；每个语气词都要带明显压抑和忍耐感，尾音收住。",
  "pause_after_ms": 700
}
```

## 当前项目需要调整的地方

### 立刻调整

- 把现有 `bible` 升级为 `graph_index`，不要只产出简单人物卡。
- 新增 `context_retrieval` 模块，Planner 不直接读取完整 Bible。
- Planner prompt 改成“基于检索上下文规划朗读”，不要让代码先做对白切分。
- Review 增加检查：孤立引号、纯省略号、style 模板化、speaker 证据不足。

### 暂缓调整

- 暂缓引入 LangGraph。
- 暂缓直接接入 LightRAG。
- 暂缓做中文关系抽取微调。
- 暂缓做复杂前端。

### 未来可选

- 当图谱接口稳定后，可以接入 LightRAG 作为图谱和向量检索后端。
- 当人工复核流变复杂后，可以用 LangGraph 管理状态和断点。
- 当有足够标注数据后，可以参考中文文学 NER/RE 数据集训练辅助模型。

## 新的实施顺序

### Step 1：定义图谱 schema

产物：

```text
novelreader_v2/common/graph_schema.py
```

验收：

- Character、Relation、Event、QuoteEvidence、Evidence 都有 Pydantic 模型。
- 所有对象都有 `confidence` 和 `evidence_ids`。

### Step 2：重写 graph_index

产物：

```text
novelreader_v2/graph_index/main.py
```

验收：

- 输入章节文本。
- 输出 `graph/chapter_001.graph.json`。
- 增量合并到 `graph/book_graph.json`。

### Step 3：实现 context_retrieval

产物：

```text
novelreader_v2/context_retrieval/main.py
```

验收：

- 输入 target chunk 和 book graph。
- 输出 `contexts/001/chunk_001.context.json`。
- context 体积可控，不超过配置里的字符上限。

### Step 4：改造 planner

产物：

```text
novelreader_v2/planner/main.py
```

验收：

- Planner 不再读取完整 Character Bible。
- Planner 只读取 target chunk、overlap、retrieved context。
- 输出 plan 时保留 evidence/reason/confidence。

### Step 5：加强 review

产物：

```text
novelreader_v2/review/main.py
```

验收：

- 能发现孤立 `」`、`…」`、纯语气碎片。
- 能发现 style_prompt 模板化。
- 能按 speaker 和 confidence 排列风险项。

## 测试路径

默认测试项目：

```text
H:\NovelReaderV2\projects\demo
```

输入小说：

```text
H:\NovelReaderV2\projects\demo\input\novel.txt
```

旧项目参考输入：

```text
H:\NovelReader\projects\demo\input\novel.txt
```

重点观察样例：

```text
「嗯……嗯嗯……唔……」
裴语涵死死地咬着牙齿，不让自己出声。
```

期望：

- 不能切成三个互相孤立的 plan item 后丢失 speaker。
- 如果切开，三条也必须共享同一个 speaker 判断依据。
- speaker 应该是裴语涵，或者低置信度标记复核。
- style_prompt 必须体现压抑、断续、强忍，而不是“自然中文对白”。

## 最终原则

NovelReaderV2 的目标不是做一个“规则更复杂的文本切割器”，而是做一个可追溯的小说朗读规划系统。

判断质量的瓶颈应该尽量回到模型本身，而不是被代码写死。代码的价值在于：

- 给模型正确上下文。
- 保留证据。
- 控制 prompt 体积。
- 让结果可复查。
- 让失败可定位、可重跑。

