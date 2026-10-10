"""Never expose response bodies or credentials in errors or logs."""

import time

import httpx
from nonebot.log import logger

from .trace import CURRENT, emit


class ServiceError(Exception):
    pass


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    key: str,
    payload: dict,
    timeout: float,
    service: str,
    request_id: str,
) -> dict:
    started = time.monotonic()
    status = 0
    try:
        response = await client.post(
            url, headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=timeout
        )
        status = response.status_code
        if status in (401, 403):
            raise ServiceError(f"{service}鉴权失败，请检查本地配置。")
        if status == 429:
            raise ServiceError(f"{service}服务繁忙或额度不足，请稍后重试。")
        if not 200 <= status < 300:
            raise ServiceError(f"{service}暂不可用，请稍后重试。")
        try:
            data = response.json()
        except ValueError:
            raise ServiceError(f"{service}返回了无效数据。") from None
        if not isinstance(data, dict):
            raise ServiceError(f"{service}返回了无效数据。")
        usage = data.get("usage", {})
        safe_usage = (
            {
                k: v
                for k, v in usage.items()
                if k
                in (
                    "prompt_tokens",
                    "completion_tokens",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
                and type(v) is int
            }
            if isinstance(usage, dict)
            else {}
        )
        logger.info("request={} service={} usage={}", request_id, service, safe_usage)
        scope = CURRENT.get()
        if scope:
            choices = data.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else {}
            emit(
                scope["config"],
                "http_usage",
                request_id=request_id,
                service=service,
                usage=safe_usage,
                finish_reason=choice.get("finish_reason")
                if isinstance(choice, dict) and choices
                else data.get("status"),
            )
        return data
    except httpx.TimeoutException:
        raise ServiceError(f"{service}请求超时，请稍后重试。") from None
    except httpx.RequestError:
        raise ServiceError(f"{service}网络连接失败，请稍后重试。") from None
    finally:
        scope = CURRENT.get()
        if scope:
            emit(
                scope["config"],
                "http_end",
                request_id=request_id,
                service=service,
                status=status,
                elapsed_ms=round((time.monotonic() - started) * 1000, 2),
            )
        logger.info(
            "request={} service={} status={} elapsed={:.2f}",
            request_id,
            service,
            status,
            time.monotonic() - started,
        )
