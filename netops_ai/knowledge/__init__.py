"""固定拼进研判 system prompt 的领域知识，不做检索。只收脱离具体样本也成立的通用经验。"""

from .network_mib_semantics import NETWORK_DOMAIN_KNOWLEDGE

__all__ = ["NETWORK_DOMAIN_KNOWLEDGE"]
