"""Advanced Skill 层 — 为宿主 Agent 准备摘要输入包，并渲染其结论。

架构（重要）：
    Host Agent → Skill instructions → CLI → service → parser → db

**本层不调用任何 LLM，不绑定任何 LLM SDK，不需要额外 API Key。**
推理由宿主 Agent（Claude Code 等）完成。

分工：
  prepare.py  确定性数据准备 —— 取数、噪声过滤、id 标注、统计
              （产出「摘要输入包」，不含任何总结）
  render.py   把 Agent 产出的结论 JSON 渲染成 text / markdown / html
  schema.py   输入输出契约

两个能力：
  - summarize_chat  ：指定会话的话题聚类摘要
  - daily_digest    ：全部会话的日报
"""
from . import prepare, render

__all__ = ["prepare", "render"]
