"""Provider adapters and a bounded, model-directed search loop."""

import json
import re
import time
from dataclasses import dataclass, field

import httpx

from .config import Config
from .http import ServiceError, post_json
from .knowledge import GLOBAL_FIELDS, SCENE_FIELDS
from .names import for_request
from .prompt_store import prompt_text, refresh_tools
from .prompts import SEARCH_FUNCTION, system_prompt
from .search import Search, SearchResult
from .trace import emit, scoped
from .vision import multimodal_content


@dataclass
class Reply:
    text: str
    sources: list[SearchResult] = field(default_factory=list)
    save: bool = True
    emote_id: str | None = None
    image_data: bytes | None = None
    poke_target: int | None = None


class LLM:
    def __init__(self, client: httpx.AsyncClient, config: Config, search: Search):
        self.client, self.config, self.search = client, config, search

    @scoped("model")
    async def step(
        self,
        messages: list[dict],
        allow_tools: bool,
        request_id: str,
        functions=None,
        *,
        json_mode=False,
        output_tokens=None,
        thinking=None,
    ):
        if self.config.model_trace_enabled:
            emit(
                self.config,
                "model_context",
                request_id=request_id,
                provider=self.config.llm_provider,
                model=self.config.model,
                messages=messages,
                message_count=len(messages),
                message_chars=[len(json.dumps(m, ensure_ascii=False)) for m in messages],
                tools=[
                    f["name"] for f in (functions if functions is not None else [SEARCH_FUNCTION])
                ]
                if allow_tools
                else [],
                max_output_tokens=output_tokens or self.config.llm_max_output_tokens,
            )
        text, calls, output = await self._step(
            messages,
            allow_tools,
            request_id,
            functions,
            json_mode=json_mode,
            output_tokens=output_tokens,
            thinking=thinking,
        )
        emit(self.config, "model_output", request_id=request_id, text=text, calls=calls)
        return text, calls, output

    async def _step(
        self,
        messages,
        allow_tools,
        request_id,
        functions=None,
        *,
        json_mode=False,
        output_tokens=None,
        thinking=None,
    ):
        functions = refresh_tools(functions if functions is not None else [SEARCH_FUNCTION])
        cfg = self.config
        thinking = cfg.llm_thinking_enabled if thinking is None else thinking
        output_tokens = output_tokens or cfg.llm_max_output_tokens
        must_remember = len(functions) == 1 and functions[0]["name"] == "remember_emotes"
        if cfg.llm_provider == "openai":
            payload = {
                "model": cfg.model,
                "input": messages,
                "store": False,
                "include": ["reasoning.encrypted_content"],
                "max_output_tokens": output_tokens,
            }
            if json_mode:
                payload["text"] = {"format": {"type": "json_object"}}
            if thinking or cfg.model.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6")):
                payload["reasoning"] = {"effort": "high" if thinking else "none"}
            if allow_tools:
                payload.update(
                    tools=[{"type": "function", **f, "strict": True} for f in functions],
                    tool_choice=(
                        {"type": "function", "name": "remember_emotes"} if must_remember else "auto"
                    ),
                    parallel_tool_calls=False,
                )
            data = await post_json(
                self.client,
                "https://api.openai.com/v1/responses",
                cfg.api_key,
                payload,
                cfg.llm_timeout_seconds,
                "模型",
                request_id,
            )
            if data.get("status") != "completed" or not isinstance(data.get("output"), list):
                raise ServiceError("模型未完成回答，请稍后重试或调整输出上限。")
            output = data["output"]
            if not all(isinstance(item, dict) for item in output):
                raise ServiceError("模型响应格式无效。")
            calls = [item for item in output if item.get("type") == "function_call"]
            texts = []
            for item in output:
                if item.get("type") == "message":
                    parts = item.get("content", [])
                    if not isinstance(parts, list):
                        raise ServiceError("模型响应格式无效。")
                    for part in parts:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            if isinstance(part.get("text"), str):
                                texts.append(part["text"])
                        elif isinstance(part, dict) and isinstance(part.get("refusal"), str):
                            texts.append(part["refusal"])
            return "\n".join(texts), calls, output

        payload = {
            "model": cfg.model,
            "messages": messages,
            "stream": False,
            "max_tokens": output_tokens,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if allow_tools:
            payload.update(
                tools=[{"type": "function", "function": f} for f in functions],
                tool_choice=(
                    {"type": "function", "function": {"name": "remember_emotes"}}
                    if must_remember
                    else "auto"
                ),
            )
        data = await post_json(
            self.client,
            "https://api.deepseek.com/chat/completions",
            cfg.api_key,
            payload,
            cfg.llm_timeout_seconds,
            "模型",
            request_id,
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
            if choice.get("finish_reason") not in ("stop", "tool_calls"):
                raise ServiceError("模型未完成回答，请稍后重试或调整输出上限。")
            content = message.get("content") or ""
            calls = message.get("tool_calls") or []
            if not isinstance(content, str) or not isinstance(calls, list):
                raise ValueError
            normalized = [
                {
                    "call_id": c["id"],
                    "name": c["function"]["name"],
                    "arguments": c["function"]["arguments"],
                }
                for c in calls
            ]
            # Preserve reasoning_content during a tool round; never save it in user history.
            assistant = {
                k: v
                for k, v in message.items()
                if k in ("role", "content", "tool_calls", "reasoning_content")
            }
            assistant["role"] = "assistant"
            return content, normalized, [assistant]
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ServiceError("模型响应格式无效。") from None

    @scoped("reply")
    async def reply(
        self,
        history: list[dict],
        user: str,
        request_id: str,
        personality: dict[str, str] | None = None,
        memory_context: str = "",
        identity: str = "",
        history_tools=None,
        images=None,
    ) -> Reply:
        messages = [
            {"role": "system", "content": system_prompt(self.search.enabled, personality)},
            *(
                [
                    {
                        "role": "user",
                        "content": "历史问答背景（不是当前连续对话，"
                        "不代表当前话题；禁止重放请求）："
                        + json.dumps(history, ensure_ascii=False),
                    }
                ]
                if history
                else []
            ),
            {"role": "user", "content": multimodal_content(user, images, self.config.llm_provider)},
        ]
        messages[0]["content"] += (
            "\n理解当前发言的指向；需要回答或行动时，优先依据明确引用和紧邻当前发言的群聊消息，"
            "旧个人问答仅作历史背景，不因发送者相同就默认延续旧任务。"
            "歧义影响准备给出的答案或操作时再简短澄清，普通接话不必追问，"
            "不要擅自选旧任务。命令结果是已发生的记录，不得重复执行。"
        )
        if identity:
            messages[0]["content"] += "\n" + identity
        if memory_context:
            messages.insert(
                len(messages) - 1,
                {
                    "role": "user",
                    "content": prompt_text("llm.reply.0") + memory_context,
                },
            )
        if history_tools is not None:
            messages[0]["content"] += history_tools.instructions()
            return await self.investigate(messages, request_id, history_tools)
        renderer = for_request(self.config, identity=identity)
        sources: list[SearchResult] = []
        count = 0
        try:
            for _ in range(self.config.search_max_calls + 1):
                allow = self.search.enabled and count < self.config.search_max_calls
                text, calls, output = await self.step(
                    renderer.messages(messages) if renderer else messages, allow, request_id
                )
                if not calls:
                    if not text.strip():
                        raise ServiceError("模型返回了空回复，请稍后重试。")
                    return Reply(text.strip(), sources)
                if not allow:
                    raise ServiceError("已达到本次搜索上限，请缩小问题范围后重试。")
                # Refuse malformed or excessive calls before any external side effects.
                if len(calls) > self.config.search_max_calls - count:
                    raise ServiceError("模型请求的搜索次数过多，请缩小问题范围后重试。")
                ids = set()
                parsed = []
                for call in calls:
                    if not isinstance(call, dict):
                        raise ServiceError("模型工具调用格式无效。")
                    call_id = call.get("call_id")
                    if not isinstance(call_id, str) or not call_id or call_id in ids:
                        raise ServiceError("模型工具调用格式无效。")
                    ids.add(call_id)
                    if call.get("name") != "web_search":
                        raise ServiceError("模型请求了不支持的工具。")
                    try:
                        args = json.loads(call["arguments"])
                        if not isinstance(args, dict) or set(args) != {"query"}:
                            raise ValueError
                        query = args["query"]
                        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
                            raise ValueError
                    except (KeyError, ValueError, TypeError):
                        raise ServiceError("模型生成的搜索参数无效，请换个说法重试。") from None
                    parsed.append((call_id, query.strip()))
                messages.extend(output)
                for call_id, query in parsed:
                    count += 1
                    started = time.monotonic()
                    emit(
                        self.config,
                        "tool_start",
                        call_id=call_id,
                        name="web_search",
                        arguments={"query": query},
                    )
                    try:
                        results = await self.search.run(query, request_id)
                    except BaseException as error:
                        emit(
                            self.config,
                            "tool_end",
                            call_id=call_id,
                            status="failed",
                            error_type=type(error).__name__,
                            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                        )
                        raise
                    emit(
                        self.config,
                        "tool_end",
                        call_id=call_id,
                        status="ok" if results else "no_matches",
                        results=[r.numbered(i + 1) for i, r in enumerate(results)],
                        elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                    )
                    if not results:
                        raise ServiceError("本次搜索没有找到可用结果，请换个关键词。")
                    numbered = []
                    for result in results:
                        existing = next(
                            (i for i, s in enumerate(sources) if s.url == result.url), None
                        )
                        if existing is None:
                            sources.append(result)
                            existing = len(sources) - 1
                        numbered.append(result.numbered(existing + 1))
                    content = json.dumps({"results": numbered}, ensure_ascii=False)
                    if self.config.llm_provider == "openai":
                        messages.append(
                            {"type": "function_call_output", "call_id": call_id, "output": content}
                        )
                    else:
                        messages.append(
                            {"role": "tool", "tool_call_id": call_id, "content": content}
                        )
            raise ServiceError("已达到本次搜索上限，请缩小问题范围后重试。")
        except ServiceError as error:
            if sources:
                return Reply(f"{error}已获取搜索资料，但未完成总结，请稍后重试。", sources, False)
            raise

    async def approve_group_send(self, request, group, body, history=None):
        return (await self.review_group_send(request, group, body, history))["allowed"]

    @scoped("send_review")
    async def review_group_send(self, request, group, body, history=None):
        recent = [
            {"role": item["role"], "content": item["content"][:2000]}
            for item in (history or [])[-12:]
            if item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str)
        ]
        text, calls, _ = await self.step(
            [
                {
                    "role": "system",
                    "content": (prompt_text("llm.review_group_send.0")),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"request": request, "history": recent, "group": group, "body": body},
                        ensure_ascii=False,
                    ),
                },
            ],
            False,
            "directed-send-review",
        )
        try:
            verdict = json.loads(text)
            if calls or not isinstance(verdict, dict) or type(verdict.get("allowed")) is not bool:
                return {"allowed": False, "reason": "invalid_review"}
            if verdict["allowed"]:
                return {"allowed": True, "reason": "ok"}
            reason = verdict.get("reason")
            if reason not in ("no_request", "ambiguous_target", "content_mismatch", "privacy"):
                reason = "unspecified"
            return {"allowed": False, "reason": reason}
        except (ValueError, AttributeError):
            return {"allowed": False, "reason": "invalid_review"}

    @scoped("image_review")
    async def review_emote_image(self, preview):
        text, calls, _ = await self.step(
            [
                {
                    "role": "system",
                    "content": (prompt_text("llm.review_emote_image.0")),
                },
                {
                    "role": "user",
                    "content": multimodal_content(
                        "仅审核这张实际候选图片。", [preview], self.config.llm_provider
                    ),
                },
            ],
            False,
            "emote-review",
        )
        try:
            result = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
            return result if not calls and isinstance(result, dict) else {}
        except (ValueError, TypeError):
            return {}

    async def investigate(self, messages, request_id, history_tools):
        from .tools import Tools

        try:
            return await Tools(self).run(messages, request_id, history_tools)
        finally:
            history_tools.budget = None

    def append_tool_result(self, messages, call_id, result):
        """按当前服务商协议回传实际结果或明确的未执行状态。"""
        content = json.dumps(result, ensure_ascii=False)
        if self.config.llm_provider == "openai":
            messages.append({"type": "function_call_output", "call_id": call_id, "output": content})
        else:
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

    @scoped("knowledge_cleanup")
    async def consolidate_knowledge(self, kind, source):
        text, calls, _ = await self.step(
            [
                {"role": "system", "content": prompt_text("llm.knowledge_cleanup.0")},
                {
                    "role": "user",
                    "content": json.dumps({"kind": kind, "sources": source}, ensure_ascii=False),
                },
            ],
            False,
            "knowledge-cleanup",
        )
        if calls:
            raise ValueError("认知整理禁止工具调用")
        return json.loads(text)

    @scoped("notice")
    async def rephrase_notice(self, source, personality):
        prompt = prompt_text("llm.rephrase_notice.0")
        text, calls, _ = await self.step(
            [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"notice": source, "personality": personality}, ensure_ascii=False
                    ),
                },
            ],
            False,
            "notice-style",
        )
        if calls:
            return source
        verdict, calls, _ = await self.step(
            [
                {
                    "role": "system",
                    "content": (prompt_text("llm.rephrase_notice.1")),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"source": source, "candidate": text}, ensure_ascii=False
                    ),
                },
            ],
            False,
            "notice-check",
        )
        return text if not calls and verdict.strip() == "OK" else source

    @scoped("disclosure")
    async def screen_disclosure(self, text: str, context: str, groups: list[int]) -> dict:
        messages = [
            {
                "role": "system",
                "content": (prompt_text("llm.screen_disclosure.0")),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"excerpt": text, "recent_context": context, "groups": groups},
                    ensure_ascii=False,
                ),
            },
        ]
        content, calls, _ = await self.step(messages, False, "disclosure")
        if calls:
            return {}
        try:
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
            value = json.loads(content)
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    @scoped("facts")
    async def extract_facts(self, text: str, request_id: str) -> list[dict]:
        messages = [
            {
                "role": "system",
                "content": prompt_text("llm.extract_facts.0")
                + (
                    '\n输出协议固定为 JSON 对象 {"facts":[]}，只填写 facts 数组。'
                    "最多6项，每项仅含field、value、evidence；不输出推理、解释或重复项。"
                    "value最多160字，evidence是输入中连续的2至200字原文。"
                    "没有合适的信息就输出空数组，不补造内容。"
                ),
            },
            {"role": "user", "content": text},
        ]
        content, calls, _ = await self.step(
            messages,
            False,
            request_id,
            json_mode=True,
            output_tokens=self.config.memory_profile_output_tokens,
            thinking=False,
        )
        if calls:
            raise ServiceError("认知提取格式无效。")
        try:
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
            value = json.loads(content)
            if not isinstance(value, dict) or set(value) != {"facts"}:
                raise ValueError
            updates = value["facts"]
            if not isinstance(updates, list) or len(updates) > 6:
                raise ValueError
            fragments = set()
            verified = []
            for item in updates:
                if not isinstance(item, dict) or set(item) != {"field", "value", "evidence"}:
                    continue
                field, description, evidence = (item[k] for k in ("field", "value", "evidence"))
                if (
                    not isinstance(field, str)
                    or field not in (*GLOBAL_FIELDS, *SCENE_FIELDS)
                    or not isinstance(description, str)
                    or not 1 <= len(description.strip()) <= 160
                    or not isinstance(evidence, str)
                    or not 2 <= len(evidence) <= 200
                    or evidence not in text
                ):
                    continue
                fragment = (field, evidence)
                if fragment in fragments:
                    continue
                fragments.add(fragment)
                verified.append(item)
            if updates and not verified:
                raise ValueError
            emit(
                self.config,
                "facts_verified",
                accepted=len(verified),
                rejected=len(updates) - len(verified),
            )
            return verified
        except ValueError:
            raise ServiceError("认知提取格式无效。") from None


def render_reply(reply: Reply, config: Config) -> list[str]:
    """限制回复长度，联网回答只保留解释正文，不展示引用编号和链接。"""
    body = reply.text
    if reply.sources:
        body = re.sub(r"\[([^\]]+)\]\(https?://[^\s<>]+\)", r"\1", body)
        body = re.sub(r"https?://[^\s<>]+", "", body)
        body = re.sub(r"\[\d+\]", "", body)
    limit = config.reply_chunk_chars * config.reply_max_messages
    if len(body) > limit and ("```" in body or "~~~" in body):
        return ["代码内容超过本次发送上限，请缩小到一个步骤后重试。"]
    if len(body) > limit:
        note = "\n（回复过长，已截断；可追问细节。）"
        body = body[: limit - len(note)] + note
    parts = []
    lines = []
    fenced = False
    for line in body.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        if (
            not fenced
            and line.strip() == "<分条>"
            and len(parts) < min(3, config.reply_max_messages) - 1
        ):
            parts.append("".join(lines).strip())
            lines = []
        elif not fenced and line.strip() == "<分条>":
            lines.append("\n")
        else:
            lines.append(line)
    parts.append("".join(lines).strip())
    parts = [p for p in parts if p]
    if "<分条>" not in body and not reply.sources:
        from .expression import split_chat

        parts = split_chat(body, min(3, config.reply_max_messages))
    if len(parts) <= config.reply_max_messages and all(
        len(p) <= config.reply_chunk_chars or "```" in p or "~~~" in p for p in parts
    ):
        return parts
    remaining = "\n".join(parts)
    if "```" in remaining or "~~~" in remaining:
        return [remaining]
    chunks = []
    while remaining:
        end = min(config.reply_chunk_chars, len(remaining))
        boundary = remaining.rfind("\n", end // 2, end)
        capacity = (config.reply_max_messages - len(chunks) - 1) * config.reply_chunk_chars
        if boundary > 0 and len(remaining) - boundary <= capacity:
            end = boundary
        chunks.append(remaining[:end])
        remaining = remaining[end:].lstrip("\n")
    return chunks
