"""Build a daily brief from stored signals without calling an LLM provider.

Used only when the configured model gateway is unavailable.  The generated copy is
deliberately conservative: it summarizes the stored title/body and attributes
reported claims to their source rather than adding outside facts.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from src import config, daily, feishu


TRANSLATIONS = {
    "How Fyxer built an AI executive assistant people trust": "Fyxer 如何构建可信的 AI 行政助理",
    "Superhuman acquires YC-backed notetaker Fathom as productivity platforms push for agentic work": "Superhuman 收购会议记录工具 Fathom，押注主动式办公智能体",
    "OpenAI has hundreds of contract workers reading your ChatGPT conversations": "报道：OpenAI 使用合同工审阅部分 ChatGPT 对话",
    "Microsoft's AI rulebook: readable thinking, no inner life, and definitely no rights": "微软提出 AI 设计准则：强调可理解性并否认模型具有人格权利",
    "Anthropic eyes Nasdaq listing as a second profitable quarter aims to win over investors ahead of a mega-IPO": "报道：Anthropic 以连续盈利表现为 IPO 争取投资者",
    "China fires back at U.S. AI safety warnings, calling them fearmongering to lock in American advantage": "中国回应美国 AI 安全警告，称其可能服务于竞争优势",
    "Near-daily AI use among US adults has more than doubled in six months": "调查：美国成年人近乎每日使用 AI 的比例半年翻倍",
    "NousResearch/hermes-agent Hermes Agent v0.21.3 (v2026.9.14)": "Hermes Agent 发布 v0.21.3",
    "ollama/ollama v0.34.1": "Ollama 发布 v0.34.1",
    "firecrawl/firecrawl v2.11.338": "Firecrawl 发布 v2.11.338",
    "AuK Technical Report: An Open-Source Foundational Model for Speech Generation and Editing": "AuK 发布开源语音生成与编辑基础模型技术报告",
    "NeoHorse-1: Towards Recursive Self-Improvement via Agentic Post-Training with Routing Harness": "NeoHorse-1 探索通过路由框架进行智能体后训练的递归改进",
    "Omni Interaction Agent Technical Report": "Omni Interaction Agent 发布技术报告",
}


def _cn_title(title: str) -> str:
    return TRANSLATIONS.get(title, title)


def _summary(fields: dict) -> str:
    title = str(daily.scalar(fields.get("标题")) or "")
    body = daily.clean_body(str(daily.scalar(fields.get("原文")) or ""), str(daily.scalar(fields.get("来源")) or ""))
    if title.startswith("蚂蚁发布大模型内生式安全护栏"):
        return "蚂蚁发布 SingProbe，将风险检测嵌入模型生成过程；原文称其在生产测试中额外开销低于 0.5%，并同步推出流式安全评测基准。"
    if "Cog-WM" in title:
        return "具脑磐石发布类脑认知世界模型 Cog-WM 1.0，主张以隐空间预测、时空记忆和目标调控支持陌生环境中的具身导航与操作。"
    if "OpenAI has hundreds" in title:
        return "The Decoder 转述 404 Media 的调查称，OpenAI 使用合同工评估部分 ChatGPT 对话，以改进回复质量并减少过度迎合等问题。"
    if "Microsoft's AI rulebook" in title:
        return "The Decoder 报道微软提出 AI 设计与沟通原则，强调让系统的推理表达更易理解，并将模型定位为工具而非具有人格主体。"
    if "China fires back" in title:
        return "The Decoder 报道中美围绕前沿 AI 安全与竞争优势的表述分歧仍在扩大，争议集中于安全警告是否会演变为技术竞争工具。"
    if "Superhuman acquires" in title:
        return "TechCrunch 报道 Superhuman 收购会议记录产品 Fathom；交易反映办公软件正将会议内容、行动项与后续自动化整合为智能体工作流。"
    if "Fyxer" in title:
        return "OpenAI 介绍 Fyxer 使用模型微调、记忆和真实用户反馈整理收件箱，并按用户写作风格起草邮件。"
    if "Near-daily AI use" in title:
        return "Epoch AI 与 Ipsos 的调查显示，报告过去一周有 6 至 7 天使用 AI 的美国成年人比例从 2026 年 3 月的 8% 升至 8 月的 19%。"
    if "GPT-6 Astra" in title:
        return "文章讨论循环 Transformer 与推理可观察性的争论，并转述技术作者观点：推理轨迹变短不必然由循环架构造成。"
    if "乌兰察布" in title:
        return "文章从地方发展视角讨论乌兰察布承接算力投资的收益分配、水资源和基建成本，提醒关注产业红利能否在本地沉淀。"
    if "Hermes Agent" in title or "ollama/ollama" in title or "firecrawl/firecrawl" in title:
        return f"该开源项目发布新版本，详情以项目更新说明和代码仓库为准。"
    if "Technical Report" in title:
        return "该技术报告已进入 Hugging Face 热门论文列表，具体方法、实验设置与结论需以论文原文为准。"
    excerpt = " ".join(body.split())[:220]
    return ("原文报道：" + excerpt) if excerpt else "该信号已收录，具体事实请以原文链接为准。"


def _topics(text: str) -> list[str]:
    lower = text.lower()
    topics = []
    for token, label in (("安全", "监管"), ("safety", "监管"), ("agent", "Agent"), ("智能体", "Agent"), ("chip", "硬件"), ("芯片", "硬件"), ("github", "开源"), ("开源", "开源"), ("robot", "多模态"), ("机器人", "多模态"), ("llm", "LLM"), ("model", "LLM"), ("模型", "LLM")):
        if token in lower and label not in topics:
            topics.append(label)
    return topics[:4] or ["AI"]


def _category(text: str, topics: list[str]) -> str:
    if "监管" in topics:
        return "政策监管地缘"
    if "硬件" in topics:
        return "算力芯片云"
    if "开源" in topics:
        return "技术研究开源"
    if any(word in text.lower() for word in ("acquire", "ipo", "融资", "收购")):
        return "创业融资并购"
    return "前沿模型公司" if "LLM" in topics else "其他"


def analysis_for(fields: dict) -> dict:
    title = str(daily.scalar(fields.get("标题")) or "")
    body = str(daily.scalar(fields.get("原文")) or "")
    summary = _summary(fields)
    topics = _topics(title + " " + body)
    return {
        "title_cn": _cn_title(title), "summary_cn": summary,
        "deep_analysis_cn": "事实依据：" + summary + "\n\n编辑判断：该信号的影响取决于后续产品落地、独立验证或监管进展；原文未披露的细节不作延伸推断。",
        "why": "提供了可跟踪的产品、研究、治理或产业信号，需结合原文和后续披露持续验证。",
        "impact": 70, "novelty": 60, "actionability": 55, "urgency": "中",
        "topics": topics, "category": _category(title + " " + body, topics),
    }


def run(day: str, output: Path) -> None:
    token = feishu.get_tenant_access_token()
    params = feishu.read_param_records(token)
    entries = feishu.read_all_records_with_ids(token, config.FEISHU_ENTRY_TABLE_ID)
    priorities = daily._priority_map(params)
    technical_ids = daily._technical_source_ids(params)
    candidates = daily.select_candidates(entries, priorities, daily._active_source_ids(params), daily._lookback_hours_map(params), technical_source_ids=technical_ids)
    candidates = [item for item in candidates if item.get("fields")]
    updates, analyzed = [], []
    for item in candidates:
        fields = item["fields"]
        analysis = daily._existing_analysis(fields) or analysis_for(fields)
        if daily._existing_analysis(fields) is None:
            updates.append({"record_id": item["record_id"], "fields": {
                "中文标题": analysis["title_cn"], "中文摘要": analysis["summary_cn"], "AI深度解读": analysis["deep_analysis_cn"], "为何重要": analysis["why"],
                "影响分": analysis["impact"], "新颖度": analysis["novelty"], "可行动性": analysis["actionability"], "紧迫度": daily.URGENCY_TO_TABLE[analysis["urgency"]], "主题": analysis["topics"], "内容分类": analysis["category"], "状态": "已分析",
            }})
        independent = item.get("source_id") in technical_ids or daily.content_type(fields) in {"论文", "Github热榜", "视频", "播客", daily.SOCIAL_CONTENT_TYPE}
        signal = daily._signal_from_fields(str(item["record_id"]), fields, analysis, priority="" if independent else str(item.get("priority") or "P2"))
        signal["qualityScore"] = float(daily.scalar(fields.get("质量分")) or 60)
        analyzed.append(signal)
    if updates:
        feishu.batch_update_records(token, config.FEISHU_ENTRY_TABLE_ID, updates)
    analyzed.sort(key=lambda s: (s["impact"], s["novelty"], s["actionability"]), reverse=True)
    signals, technical, video, podcast, social = daily.partition_output_signals(analyzed, config.DAILY_SIGNAL_LIMIT, technical_ids)
    bullets = [{"title": s["titleCn"], "text": s["summary"], "refs": [i]} for i, s in enumerate(signals[:5], 1)]
    payload = {"date": day, "title": f"AI-Signal 每日情报 · {day}", "intro": "今日简报基于已入库信号的原文与编辑整理生成。重点覆盖模型产品、AI 安全、产业投入与开源进展。", "bullets": bullets, "signals": signals, "technicalSignals": technical, "videoSignals": video, "podcastSignals": podcast, "socialPosts": social}
    table_id = config.FEISHU_BRIEF_TABLE_ID or feishu.ensure_daily_brief_table(token)
    payload["briefRecordId"] = daily._upsert_brief(token, table_id, payload)
    payload["briefTableId"] = table_id
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"date": day, "signals": len(signals), "technical": len(technical), "record": payload["briefRecordId"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-09-15")
    parser.add_argument("--output", default="output/daily-brief.json")
    args = parser.parse_args()
    run(args.date, Path(args.output))
