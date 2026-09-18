"""配置密钥不得以明文外泄（`docs/TODO.md` §24）。

**为什么值一组测试**：密钥字段原先全是裸 `str`，`repr(Settings)` 会原样打印——
而 pytest/CI 的失败回溯**默认打印局部变量**，本仓已出现过
``settings = Settings(deepseek_api_key='...')`` 这种输出形态。本地那次是假 key，
CI 里注入真 key 时同一形态就会把真 key 打进构建日志。

这组用例全部使用**哨兵值**（`SENTINEL_*`），不读取也不断言任何真实凭证。
"""
from __future__ import annotations

from pydantic import SecretStr

from agentflow.config import Settings

#: 每个密钥字段一个哨兵值——只要它出现在任何字符串化输出里，测试就红。
SECRET_FIELDS = {
    "deepseek_api_key": "SENTINEL-deepseek-8f3a",
    "jwt_secret": "SENTINEL-jwt-8f3a",
    "secret_key": "SENTINEL-fernet-8f3a",
    "open_sandbox_api_key": "SENTINEL-sandbox-8f3a",
    "langfuse_secret_key": "SENTINEL-langfuse-8f3a",
    "postgres_dsn": "SENTINEL-dsn-8f3a",
}


def _settings_with_sentinels() -> Settings:
    # `_env_file=None` 绕开仓库 .env；kwargs 优先级高于环境变量，故哨兵一定生效。
    return Settings(_env_file=None, **SECRET_FIELDS)


def test_secret_fields_are_secret_str() -> None:
    """密钥字段必须是 ``SecretStr``——**新增密钥字段时请一并加进 SECRET_FIELDS**。"""
    s = Settings(_env_file=None)
    not_secret = [
        name for name in SECRET_FIELDS
        if not isinstance(getattr(s, name), SecretStr)
    ]
    assert not not_secret, (
        f"这些字段不是 SecretStr，repr 会明文带出：{not_secret}。"
        "值可以是空的（空串同样会泄漏'这里没有密钥'之外的东西——真跑起来就是真 key）"
    )


def test_repr_and_str_never_leak_secret_values() -> None:
    """`repr` / `str` / `model_dump_json` 都不得出现任何密钥明文。"""
    s = _settings_with_sentinels()
    rendered = [repr(s), str(s), s.model_dump_json()]
    for name, sentinel in SECRET_FIELDS.items():
        for text in rendered:
            assert sentinel not in text, (
                f"{name} 的明文出现在了 {text[:80]}… 里 —— "
                "这正是 CI 失败回溯会打印的东西"
            )


def test_error_traceback_does_not_leak() -> None:
    """校验失败时 pydantic 的报错信息同样不得带出明文。

    回溯才是真正的泄漏路径（pytest 打印局部变量），不是只有 `repr` 一条。
    """
    try:
        Settings(_env_file=None, state_store=123)  # 故意触发校验错误
    except Exception as exc:  # noqa: BLE001 —— 只关心错误文本里有没有密钥
        text = str(exc)
        for name, sentinel in SECRET_FIELDS.items():
            assert sentinel not in text, f"校验错误信息泄漏了 {name}"
    else:  # pragma: no cover —— 类型放宽了才会走到
        raise AssertionError("state_store=123 未触发校验错误，本测试已失效")


def test_secret_values_still_readable() -> None:
    """加壳之后明文仍可按需取出——不是把功能改没了。"""
    s = _settings_with_sentinels()
    for name, sentinel in SECRET_FIELDS.items():
        assert getattr(s, name).get_secret_value() == sentinel


def test_secret_truthiness_matches_plain_str() -> None:
    """真值判断必须与裸 str 一致。

    `SecretStr` 是对象，若它恒为真，`if not settings.jwt_secret` 这种
    "是否配置了密钥"的判据会**永远为假**——认证模式、mock 回退全部走错分支。
    """
    empty = Settings(_env_file=None, jwt_secret="", deepseek_api_key="", postgres_dsn="")
    assert not empty.jwt_secret
    assert not empty.deepseek_api_key
    assert not empty.postgres_dsn

    filled = _settings_with_sentinels()
    assert filled.jwt_secret
    assert filled.deepseek_api_key
    assert filled.postgres_dsn


def test_assignment_is_coerced_and_does_not_leak() -> None:
    """赋值也要走校验（`validate_assignment`）。

    测试广泛用 `monkeypatch.setattr(settings, "jwt_secret", "x")` 直接塞裸 str；
    没有 validate_assignment 的话裸 str 会进 `__dict__`，随后
    `.get_secret_value()` 报 AttributeError。顺带锁住"赋值也不会明文外泄"。
    """
    s = Settings(_env_file=None)
    s.jwt_secret = "SENTINEL-assigned-jwt"
    assert isinstance(s.jwt_secret, SecretStr), "赋值未走校验，塞进去的是裸 str"
    assert s.jwt_secret.get_secret_value() == "SENTINEL-assigned-jwt"
    assert "SENTINEL-assigned-jwt" not in repr(s)
