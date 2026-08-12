from __future__ import annotations

import unicodedata


def strip_server_rendered_salutations(
    *,
    content: str,
    salutations: tuple[str, ...],
) -> str:
    sanitized = content
    ordered = sorted(
        set(salutations),
        key=lambda value: (-len(value), value),
    )
    for salutation in ordered:
        candidate = _strip_leading_address(sanitized, salutation)
        if candidate != sanitized:
            sanitized = candidate
            break
    for salutation in ordered:
        onboarding = _onboarding_notice(salutation)
        if sanitized.endswith(onboarding):
            sanitized = sanitized[: -len(onboarding)].rstrip()
            break
    return sanitized


def compose_preferred_salutation_onboarding(
    *,
    content: str,
    salutation: str,
    authenticated_display_name: str | None = None,
) -> str:
    addressed_content = address_with_preferred_salutation(
        content=content,
        salutation=salutation,
        authenticated_display_name=authenticated_display_name,
    )
    return (
        f"{addressed_content}{_onboarding_notice(salutation)}"
    )


def address_with_preferred_salutation(
    *,
    content: str,
    salutation: str,
    authenticated_display_name: str | None = None,
) -> str:
    normalized = content
    if authenticated_display_name:
        normalized = _remove_opening_vocative(
            normalized,
            authenticated_display_name,
        )
    stripped = normalized.lstrip()
    if stripped.startswith(salutation):
        remainder = stripped[len(salutation) :]
        if (
            not remainder
            or unicodedata.category(remainder[0]).startswith("P")
            or remainder.startswith(
                ("好", "您好", "你好", "早上好", "上午好", "下午好", "晚上好")
            )
        ):
            return normalized
    normalized = _remove_opening_vocative(
        normalized,
        salutation,
    )
    return f"{salutation}，{normalized}"


def _remove_opening_vocative(
    content: str,
    label: str,
) -> str:
    stripped = content.lstrip()
    position = stripped.find(label, 0, 32)
    if position < 0:
        return content
    after_position = position + len(label)
    before = stripped[position - 1] if position else ""
    after = (
        stripped[after_position]
        if after_position < len(stripped)
        else ""
    )
    if position == 0:
        if after and not _is_address_boundary(after):
            return content
        remainder = stripped[after_position:]
        if remainder and _is_address_boundary(remainder[0]):
            remainder = remainder[1:]
        return remainder.lstrip()
    if (
        before not in {",", "，", "、", ";", "；", " "}
        or (after and not _is_address_boundary(after))
    ):
        return content
    return (
        f"{stripped[: position - 1]}"
        f"{stripped[after_position:]}"
    )


def _is_address_boundary(character: str) -> bool:
    return (
        character.isspace()
        or unicodedata.category(character).startswith("P")
    )


def _strip_leading_address(content: str, salutation: str) -> str:
    stripped = content.lstrip()
    if not stripped.startswith(salutation):
        return content
    remainder = stripped[len(salutation) :]
    if not remainder:
        return ""
    if not unicodedata.category(remainder[0]).startswith("P"):
        return content
    return remainder[1:].lstrip()


def _onboarding_notice(salutation: str) -> str:
    return (
        "\n\n"
        f"我已经把“{salutation}”作为你的称呼保存在个人记忆里。"
        "这个称呼合适吗？如果想改，直接告诉我以后怎么称呼你就可以。"
    )
