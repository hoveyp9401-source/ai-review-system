from __future__ import annotations

import asyncio
import json
from datetime import date

from app.config import get_settings
from app.db import engine
from app.llm.client import LLMClient
from scripts.smoke_20260818_unified_daily_rollback import (
    _residue,
    _run_add_case,
)


SOURCE = (
    "主要工作：\n"
    "1. 澳门银河项目管理人员苏国候蓝卡协议（澳门電器公司-缺少部分信息，需待明日才能完成）；\n"
    "2. 澳门新城项目两份项目资料（无欠款证明资料政府回复可能需要27号才能下发，本项目26号投标，可能无法赶上，已向政府说明原因，尽量加急）；\n"
    "3. 澳门新城项目联合体协议（内容已完善，明日发宏富确认）；\n"
    "4. 澳门银河项目地台石材靠幕墙框安装邮件（邮件内容张总已确认，今晚会发送）；\n"
    "5. 澳门银河项目地面不平（地面不平会导致我司消耗很多自流平材料，需一一核对场地移交资料中的地平，把多出来的部分上报顾问公司）；\n"
    "6. 邮件往来……"
)


async def main() -> None:
    client = LLMClient(get_settings())
    try:
        result = await _run_add_case(
            client,
            name="dong_cross_field_overlap",
            report_date=date(2026, 5, 7),
            text=SOURCE,
            required={
                "today_work": (
                    "苏国候蓝卡协议",
                    "澳门新城项目两份项目资料",
                    "澳门新城项目联合体协议",
                    "地台石材靠幕墙框安装邮件",
                    "澳门银河项目地面不平",
                    "邮件往来",
                )
            },
            expected_status="collecting",
        )
    finally:
        await client.close()
    residue = await _residue()
    if any(residue.values()):
        raise AssertionError({"rollback_residue": residue})
    print(
        json.dumps(
            {
                "status": "pass",
                "result": result,
                "rollback_residue": residue,
                "dingtalk_send_calls": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
