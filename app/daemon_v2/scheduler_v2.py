import time
from collections import defaultdict


class SchedulerV2:
    """
    Provider/IP round-robin with dual cooldowns:
    - provider cooldown (global)
    - provider+ip cooldown (strict)
    Optional domain+ip cooldown support is passed via callback.
    """

    def __init__(self, providers_order: list[str]):
        self.providers_order = providers_order[:]
        self.ip_provider_cursor = defaultdict(int)

    def pick_for_ip(
        self,
        ip_id: str,
        pending_by_provider: dict[str, list[dict]],
        is_provider_ready,
        is_provider_ip_ready,
        is_domain_ip_ready,
    ):
        non_empty = [p for p in self.providers_order if pending_by_provider.get(p)]
        if not non_empty:
            return None, None

        start = self.ip_provider_cursor[ip_id] % len(self.providers_order)
        ordered = self.providers_order[start:] + self.providers_order[:start]

        for p in ordered:
            if not pending_by_provider.get(p):
                continue
            if not is_provider_ready(p):
                continue
            if not is_provider_ip_ready(p, ip_id):
                continue

            q = pending_by_provider[p]
            # Domain/person grouping is already ordered in queue; enforce domain-ip gate.
            for i, item in enumerate(q):
                domain = item.get("domain", "")
                if not is_domain_ip_ready(domain, ip_id):
                    continue
                chosen = q.pop(i)
                self.ip_provider_cursor[ip_id] = (self.providers_order.index(p) + 1) % len(self.providers_order)
                return p, chosen

        return None, None

    @staticmethod
    def backoff_seconds(attempts: int, base: int = 5, cap: int = 240) -> int:
        return min(cap, base * (2 ** max(0, attempts - 1)))

    @staticmethod
    def now() -> int:
        return int(time.time())
