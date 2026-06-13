# NovelReaderV2 Agent Instructions

本项目的核心方向是 LLM-first 中文小说有声书生成。

必须遵守：

- 主流程是 `ingest -> bible -> planner -> review -> tts -> audio`。
- 代码只做工程确定性：切片、overlap、缓存、校验、重跑、文件交接。
- LLM 做文学理解：speaker、kind、emotion、style_prompt、delivery、reason。
- Character Bible 是人物上下文，不是代码规则表。
- Review 只暴露风险，不自动做文学判断。
- TTS 不参与小说理解。
- 不要把旧项目的 `segment/dialogue/style` 规则链迁移为主线。
- 不要继续添加正则或 if/else 去猜复杂对白归属。

Windows + 中文路径注意事项：

- 文件统一使用 UTF-8。
- 写 JSON 时使用 `ensure_ascii=False`。
- 修改中文文档时避免 PowerShell here-string 和命令行大段中文。
- 修改后检查中文没有连续问号占位。

