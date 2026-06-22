from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from tools.base import Tool


class DateTimeTool(Tool):
    name = "get_datetime"
    description = (
        "Returns the current date and time. Use when the user asks about "
        "the date, time, day of the week, or any other temporal information."
    )
    parameters = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": (
                    "Timezone in IANA format (e.g. 'America/Sao_Paulo', 'UTC'). "
                    "Default: America/Sao_Paulo."
                ),
            }
        },
        "required": [],
    }

    async def execute(self, timezone: str = "America/Sao_Paulo") -> str:
        try:
            tz = ZoneInfo(timezone)
            now = datetime.now(tz)
            days = ["Monday", "Tuesday", "Wednesday",
                    "Thursday", "Friday", "Saturday", "Sunday"]
            day_name = days[now.weekday()]
            return (
                f"Date: {now.strftime('%Y-%m-%d')}\n"
                f"Time: {now.strftime('%H:%M')} ({timezone})\n"
                f"Day: {day_name}"
            )
        except ZoneInfoNotFoundError:
            from datetime import timezone as tz_utc
            now = datetime.now(tz_utc.utc)
            return f"UTC date/time: {now.strftime('%Y-%m-%d %H:%M')}"
