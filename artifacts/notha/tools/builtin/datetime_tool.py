from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from tools.base import Tool


class DateTimeTool(Tool):
    name = "obter_data_hora"
    description = (
        "Retorna a data e hora atual. Use quando o usuário perguntar sobre "
        "data, hora, dia da semana ou qualquer informação temporal."
    )
    parameters = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": (
                    "Fuso horário no formato IANA (ex: 'America/Sao_Paulo', 'UTC'). "
                    "Padrão: America/Sao_Paulo."
                ),
            }
        },
        "required": [],
    }

    async def execute(self, timezone: str = "America/Sao_Paulo") -> str:
        try:
            tz = ZoneInfo(timezone)
            now = datetime.now(tz)
            dias = ["segunda-feira", "terça-feira", "quarta-feira",
                    "quinta-feira", "sexta-feira", "sábado", "domingo"]
            dia_semana = dias[now.weekday()]
            return (
                f"Data: {now.strftime('%d/%m/%Y')}\n"
                f"Hora: {now.strftime('%H:%M')} ({timezone})\n"
                f"Dia: {dia_semana}"
            )
        except ZoneInfoNotFoundError:
            from datetime import timezone as tz_utc
            now = datetime.now(tz_utc.utc)
            return f"Data/hora UTC: {now.strftime('%d/%m/%Y %H:%M')}"
