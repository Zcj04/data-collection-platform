# -*- coding: utf-8 -*-
"""错误与日志中的凭据查询参数脱敏，不改变实际 HTTP 请求。"""

import re


_CREDENTIAL_PARAMETER = re.compile(
    r"(\b(?:user_?name|password|passwd|pwd|access_?token|refresh_?token|"
    r"token|secret_?key|api_?key|mtgsig|cookie|authorization)\s*=\s*)"
    r"[^&\s\"'<>)]*",
    re.IGNORECASE,
)


def redact_sensitive_text(value: str) -> str:
    return _CREDENTIAL_PARAMETER.sub(r"\1[REDACTED]", str(value))
