"""统一错误码。

Skill / CLI / Agent 都依赖这套稳定的错误契约，不要随意增删或改语义。
新增错误码时同步更新 docs/INTERFACE_DESIGN.md。
"""
from __future__ import annotations

# ---- 错误码（稳定枚举）----
CHAT_NOT_FOUND = "CHAT_NOT_FOUND"
AMBIGUOUS_NAME = "AMBIGUOUS_NAME"
NO_MESSAGE_DB = "NO_MESSAGE_DB"
KEYS_MISSING = "KEYS_MISSING"
DB_DECRYPT_FAILED = "DB_DECRYPT_FAILED"
LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
INVALID_PARAM = "INVALID_PARAM"
LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"

# HTTP 风格语义的进程退出码
EXIT_OK = 0
EXIT_NOT_FOUND = 1        # 对象不存在
EXIT_INVALID_PARAM = 2    # 参数错误
EXIT_DATA_UNAVAILABLE = 3  # 密钥/解密等数据层问题
EXIT_INTERNAL = 4         # 未预期错误

_EXIT_BY_CODE = {
    CHAT_NOT_FOUND: EXIT_NOT_FOUND,
    AMBIGUOUS_NAME: EXIT_NOT_FOUND,
    NO_MESSAGE_DB: EXIT_NOT_FOUND,
    INVALID_PARAM: EXIT_INVALID_PARAM,
    KEYS_MISSING: EXIT_DATA_UNAVAILABLE,
    DB_DECRYPT_FAILED: EXIT_DATA_UNAVAILABLE,
    INTERNAL_ERROR: EXIT_INTERNAL,
}


class ServiceError(Exception):
    """service 层统一异常。携带稳定错误码与候选列表。"""

    def __init__(self, code: str, message: str, candidates: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = candidates or []

    @property
    def exit_code(self) -> int:
        return _EXIT_BY_CODE.get(self.code, EXIT_INTERNAL)

    def to_dict(self) -> dict:
        err: dict = {"code": self.code, "message": self.message}
        if self.candidates:
            err["candidates"] = self.candidates
        return {"error": err}


def not_found(what: str, name: str) -> ServiceError:
    return ServiceError(CHAT_NOT_FOUND, f"找不到{what}: {name}")


def ambiguous(what: str, name: str, candidates: list) -> ServiceError:
    return ServiceError(AMBIGUOUS_NAME,
                        f"{what} “{name}” 匹配到 {len(candidates)} 个，请用 username 精确指定",
                        candidates)


def invalid_param(message: str) -> ServiceError:
    return ServiceError(INVALID_PARAM, message)
