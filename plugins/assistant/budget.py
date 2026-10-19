"""每次唤醒共享的时间、工具和模型 Token 预算。"""

import time


class Budget:
    def __init__(self, config):
        self.deadline = time.monotonic() + config.request_timeout_seconds
        self.max_calls, self.max_tokens = config.agent_max_calls, config.agent_max_tokens
        self.calls = self.tokens = 0
        self.cost = 0.0
        self.max_cost = config.agent_max_cost
        self.input_price, self.output_price = config.agent_input_price, config.agent_output_price

    def usage(self, usage):
        if not isinstance(usage, dict):
            return
        total = usage.get("totalTokens", usage.get("total_tokens"))
        if total is None:
            total = sum(
                usage.get(k, 0) or 0
                for k in ("inputTokens", "outputTokens", "prompt_tokens", "completion_tokens")
            )
        if isinstance(total, (int, float)) and total >= 0:
            self.tokens += int(total)
        inputs = sum(
            usage.get(k, 0) or 0 for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens")
        )
        outputs = usage.get("outputTokens", 0) or 0
        self.cost += (inputs * self.input_price + outputs * self.output_price) / 1000000

    def remaining(self):
        return {
            "cost": max(0, round(self.max_cost - self.cost, 6)) if self.max_cost else None,
            "cost_exhausted": bool(self.max_cost and self.cost >= self.max_cost),
            "calls": max(0, self.max_calls - self.calls),
            "tokens": max(0, self.max_tokens - self.tokens),
            "seconds": max(0, round(self.deadline - time.monotonic(), 1)),
            "hint": "共享资源预算；自行分配查询和执行，耗尽后收尾。Token 是用量，不是货币账单。",
        }

    def take(self):
        left = self.remaining()
        if left["cost_exhausted"] or not all(left[k] > 0 for k in ("calls", "tokens", "seconds")):
            return False
        self.calls += 1
        return True
