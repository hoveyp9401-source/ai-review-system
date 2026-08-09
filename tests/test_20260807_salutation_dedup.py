from app.agent2.personal_memory_reply import address_with_preferred_salutation


def test_preferred_salutation_is_not_duplicated_before_a_greeting():
    for content in (
        "庞总好，随时可以开始。",
        "庞总您好，可以再试试。",
        "庞总早上好，今天想查什么？",
    ):
        assert address_with_preferred_salutation(
            content=content,
            salutation="庞总",
            authenticated_display_name="庞浩",
        ) == content


def test_similar_job_title_is_not_mistaken_for_an_opening_vocative():
    content = "庞总监已经确认了方案。"

    assert address_with_preferred_salutation(
        content=content,
        salutation="庞总",
    ).endswith(content)
