"""学习子包内部共享的文本归一化工具。"""

from __future__ import annotations

import re

_TOKEN_PATTERN = re.compile(r"[a-z\u4e00-\u9fff][a-z0-9\u4e00-\u9fff]*")
_DIGIT_PATTERN = re.compile(r"\d+")


def tokenize(text: str) -> frozenset[str]:
    """提取小写关键词集合，用于相似度与签名计算。

    Args:
        text: 任意自然语言文本。

    Returns:
        去重后的小写词项集合。
    """
    return frozenset(_TOKEN_PATTERN.findall(text.lower()))


def normalized_signature(text: str) -> str:
    """把文本归一化为稳定签名：小写、去数字后的有序关键词集合。

    Args:
        text: 失败原因等自由文本。

    Returns:
        以空格连接的有序关键词签名；无有效词项时返回空字符串。
    """
    stripped = _DIGIT_PATTERN.sub(" ", text.lower())
    return " ".join(sorted(tokenize(stripped)))


def overlap_score(left: frozenset[str], right: frozenset[str]) -> float:
    """计算两个词项集合的 Jaccard 重叠度。

    Args:
        left: 第一个词项集合。
        right: 第二个词项集合。

    Returns:
        0 到 1 之间的重叠度；任一集合为空时返回 0。
    """
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


__all__ = ["normalized_signature", "overlap_score", "tokenize"]
