"""团队制品存储协议与内存实现。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from matterloop_core import ArtifactRef

from matterloop_agents.collaboration._immutability import freeze_mapping
from matterloop_agents.collaboration.errors import ArtifactNotFoundError


@runtime_checkable
class ArtifactStore(Protocol):
    """在 Agent 之间传递二进制制品的最小存储协议。"""

    async def put(
        self,
        team_run_id: str,
        name: str,
        content: bytes,
        *,
        media_type: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRef:
        """保存制品并返回稳定引用。

        Args:
            team_run_id: 制品所属团队运行标识。
            name: 便于人类识别的制品名称。
            content: 等待保存的完整字节。
            media_type: 可选的 IANA 媒体类型。
            metadata: 可选扩展信息。

        Returns:
            可跨 Agent 传递的核心制品引用。
        """
        ...

    async def read(self, reference: ArtifactRef | str) -> bytes:
        """读取引用对应的完整字节。

        Args:
            reference: 核心制品引用或稳定 URI。

        Returns:
            制品的完整字节。
        """
        ...

    async def delete(self, reference: ArtifactRef | str) -> bool:
        """删除引用对应的制品。

        Args:
            reference: 核心制品引用或稳定 URI。

        Returns:
            本次调用是否实际删除了制品。
        """
        ...


@dataclass(frozen=True, slots=True)
class _StoredArtifact:
    reference: ArtifactRef
    content: bytes


class InMemoryArtifactStore:
    """以 SHA-256 内容摘要生成 ``artifact://`` 引用的内存存储。"""

    def __init__(self) -> None:
        self._artifacts: dict[str, _StoredArtifact] = {}
        self._lock = asyncio.Lock()

    async def put(
        self,
        team_run_id: str,
        name: str,
        content: bytes,
        *,
        media_type: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRef:
        """复制字节、计算摘要并保存制品。

        Args:
            team_run_id: 制品所属团队运行标识。
            name: 便于人类识别的制品名称。
            content: 需要隔离保存的完整字节。
            media_type: 可选的 IANA 媒体类型。
            metadata: 可选的只读扩展信息。

        Returns:
            带 SHA-256、字节长度和 ``artifact://`` URI 的核心制品引用。

        Raises:
            ValueError: 标识、名称或媒体类型为空。
        """
        if not team_run_id.strip():
            raise ValueError("team_run_id must not be empty")
        if not name.strip():
            raise ValueError("name must not be empty")
        if media_type is not None and not media_type.strip():
            raise ValueError("media_type must not be empty when provided")
        copied_content = bytes(content)
        digest = sha256(copied_content).hexdigest()
        uri = f"artifact://{quote(team_run_id, safe='')}/{digest}/{quote(name, safe='')}"
        reference_metadata = {
            **dict(metadata or {}),
            "sha256": digest,
            "size_bytes": len(copied_content),
            "team_run_id": team_run_id,
        }
        reference = ArtifactRef(
            name=name,
            uri=uri,
            media_type=media_type,
            metadata=freeze_mapping(reference_metadata),
        )
        async with self._lock:
            self._artifacts[uri] = _StoredArtifact(reference, copied_content)
        return reference

    async def read(self, reference: ArtifactRef | str) -> bytes:
        """读取制品的隔离字节副本。

        Args:
            reference: ``ArtifactRef`` 或其 ``artifact://`` URI。

        Returns:
            保存的完整字节。

        Raises:
            ArtifactNotFoundError: URI 非法或制品不存在。
        """
        uri = self._uri(reference)
        async with self._lock:
            stored = self._artifacts.get(uri)
            if stored is None:
                raise ArtifactNotFoundError(f"artifact does not exist: {uri}")
            return bytes(stored.content)

    async def delete(self, reference: ArtifactRef | str) -> bool:
        """删除制品并保持重复删除幂等。

        Args:
            reference: ``ArtifactRef`` 或其 ``artifact://`` URI。

        Returns:
            本次调用是否实际删除了制品。

        Raises:
            ArtifactNotFoundError: URI 不是 ``artifact://`` 引用。
        """
        uri = self._uri(reference)
        async with self._lock:
            return self._artifacts.pop(uri, None) is not None

    @staticmethod
    def _uri(reference: ArtifactRef | str) -> str:
        uri = reference.uri if isinstance(reference, ArtifactRef) else reference
        if not uri.startswith("artifact://"):
            raise ArtifactNotFoundError(f"unsupported artifact URI: {uri}")
        return uri


__all__ = ["ArtifactStore", "InMemoryArtifactStore"]
