"""测试替身：内存版 Contents 后端 + 固定 curl 样本。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from wxread.state import Conflict


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MemoryContents:
    """满足 ContentsBackend 协议的内存实现，带故障注入。"""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.revs: dict[str, str] = {}
        self.commits: list[tuple[str, str]] = []
        self.conflicts: int = 0          # 接下来 N 次 PUT 直接冲突（不写入）
        self.lose_response: int = 0      # 接下来 N 次 PUT 写入成功但报冲突
        self.deleted: list[str] = []

    # -- 协议

    def get(self, path: str) -> tuple[bytes | None, str | None]:
        if path not in self.files:
            return None, None
        return self.files[path], self.revs[path]

    def put(self, path: str, data: bytes, message: str, sha: str | None = None, create_only: bool = False) -> str:
        if create_only and path in self.files:
            raise Conflict(f"{path} 已存在")
        if sha is not None and self.files.get(path) is not None and self.revs[path] != sha:
            raise Conflict(f"sha 不匹配：{path}")
        if sha is None and path in self.files and not create_only:
            raise Conflict(f"缺少 sha：{path}")
        if self.conflicts > 0:
            self.conflicts -= 1
            raise Conflict("注入的并发冲突")
        self.files[path] = data
        self.revs[path] = digest(data)
        self.commits.append((path, message))
        if self.lose_response > 0:
            self.lose_response -= 1
            raise Conflict("注入的响应丢失（实际已写入）")
        return self.revs[path]

    def delete(self, path: str, message: str, sha: str | None = None) -> None:
        if path not in self.files:
            return None
        self.files.pop(path, None)
        self.revs.pop(path, None)
        self.deleted.append(path)
        self.commits.append((path, message))
        return None

    def list(self, directory: str) -> list[str]:
        prefix = directory.rstrip("/") + "/"
        names: list[str] = []
        for path in self.files:
            if path.startswith(prefix) and "/" not in path[len(prefix) :]:
                names.append(path[len(prefix) :])
        return sorted(names)

    # -- 便捷

    def dump(self) -> dict[str, Any]:
        return {path: json.loads(data.decode("utf-8")) for path, data in self.files.items()}


CURL_SAMPLE = (
    "curl 'https://weread.qq.com/web/book/read' \\\n"
    "  -H 'accept: application/json, text/plain, */*' \\\n"
    "  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0' \\\n"
    "  -H 'cookie: wr_vid=1234567890; wr_skey=abcdefghij; pac_uid=0_e63870bcecc18' \\\n"
    "  --data-raw '{\"appId\":\"wb182564874603h266381671\",\"b\":\"ce032b305a9bc1ce0b0dd2a\","
    "\"c\":\"7f632b502707f6ffaa6bf2e\",\"ci\":27,\"co\":389,\"sm\":\"19聚会《三体》\","
    "\"pr\":74,\"ps\":\"4ee326507a65a465g015fae\",\"pc\":\"aab32e207a65a466g010615\"}'"
)

CURL_SECOND = CURL_SAMPLE.replace("wr_vid=1234567890", "wr_vid=9876543210").replace(
    "wr_skey=abcdefghij", "wr_skey=zyxwvutsrq"
)
