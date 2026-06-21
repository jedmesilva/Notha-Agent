"""
Inferência de fuso horário a partir de número de telefone e/ou cidade cadastrada.

Ordem de prioridade:
  1. Cidade cadastrada no banco (mais precisa — resolve fusos dentro do mesmo país)
  2. DDI + código de área (DDD no Brasil, area code nos EUA etc.)
  3. DDI sozinho (fallback por país)
  4. UTC (último recurso)
"""

# ---------------------------------------------------------------------------
# 1. Cidade → timezone (cidades mais comuns globalmente)
# ---------------------------------------------------------------------------
_CITY_TZ: dict[str, str] = {
    # Brasil
    "são paulo": "America/Sao_Paulo", "sao paulo": "America/Sao_Paulo",
    "rio de janeiro": "America/Sao_Paulo", "rio": "America/Sao_Paulo",
    "belo horizonte": "America/Sao_Paulo", "bh": "America/Sao_Paulo",
    "curitiba": "America/Sao_Paulo", "porto alegre": "America/Sao_Paulo",
    "brasília": "America/Sao_Paulo", "brasilia": "America/Sao_Paulo",
    "salvador": "America/Bahia", "fortaleza": "America/Fortaleza",
    "recife": "America/Recife", "natal": "America/Fortaleza",
    "belém": "America/Belem", "belem": "America/Belem",
    "manaus": "America/Manaus", "porto velho": "America/Porto_Velho",
    "cuiabá": "America/Cuiaba", "cuiaba": "America/Cuiaba",
    "campo grande": "America/Campo_Grande",
    "rio branco": "America/Rio_Branco",
    "boa vista": "America/Boa_Vista",
    "maceió": "America/Maceio", "maceio": "America/Maceio",
    "joão pessoa": "America/Fortaleza", "joao pessoa": "America/Fortaleza",
    "teresina": "America/Fortaleza",
    "são luís": "America/Fortaleza", "sao luis": "America/Fortaleza",
    "macapá": "America/Belem", "macapa": "America/Belem",
    "palmas": "America/Araguaina",
    "vitória": "America/Sao_Paulo", "vitoria": "America/Sao_Paulo",
    "goiânia": "America/Sao_Paulo", "goiania": "America/Sao_Paulo",
    "florianópolis": "America/Sao_Paulo", "florianopolis": "America/Sao_Paulo",
    "campinas": "America/Sao_Paulo",
    # Portugal
    "lisboa": "Europe/Lisbon", "lisbon": "Europe/Lisbon",
    "porto": "Europe/Lisbon",
    "funchal": "Atlantic/Madeira",
    "ponta delgada": "Atlantic/Azores",
    # Angola
    "luanda": "Africa/Luanda",
    # Moçambique
    "maputo": "Africa/Maputo",
    # Cabo Verde
    "praia": "Atlantic/Cape_Verde",
    # USA
    "new york": "America/New_York", "nova york": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "houston": "America/Chicago",
    "phoenix": "America/Phoenix",
    "philadelphia": "America/New_York",
    "san antonio": "America/Chicago",
    "san diego": "America/Los_Angeles",
    "dallas": "America/Chicago",
    "san francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "denver": "America/Denver",
    "miami": "America/New_York",
    "boston": "America/New_York",
    "atlanta": "America/New_York",
    "las vegas": "America/Los_Angeles",
    "honolulu": "Pacific/Honolulu",
    "anchorage": "America/Anchorage",
    # Canada
    "toronto": "America/Toronto",
    "vancouver": "America/Vancouver",
    "montreal": "America/Toronto",
    "calgary": "America/Edmonton",
    # Argentina
    "buenos aires": "America/Argentina/Buenos_Aires",
    "córdoba": "America/Argentina/Cordoba", "cordoba": "America/Argentina/Cordoba",
    # Chile
    "santiago": "America/Santiago",
    # Colômbia
    "bogotá": "America/Bogota", "bogota": "America/Bogota",
    # Peru
    "lima": "America/Lima",
    # México
    "cidade do méxico": "America/Mexico_City", "ciudad de mexico": "America/Mexico_City",
    "mexico city": "America/Mexico_City",
    "monterrey": "America/Monterrey",
    # UK
    "london": "Europe/London", "londres": "Europe/London",
    "manchester": "Europe/London", "birmingham": "Europe/London",
    # Europa
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin", "berlim": "Europe/Berlin",
    "madrid": "Europe/Madrid",
    "barcelona": "Europe/Madrid",
    "roma": "Europe/Rome", "rome": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam",
    "bruxelas": "Europe/Brussels", "brussels": "Europe/Brussels",
    "viena": "Europe/Vienna", "vienna": "Europe/Vienna",
    "estocolmo": "Europe/Stockholm", "stockholm": "Europe/Stockholm",
    "oslo": "Europe/Oslo",
    "copenhague": "Europe/Copenhagen", "copenhagen": "Europe/Copenhagen",
    "helsinki": "Europe/Helsinki",
    "varsóvia": "Europe/Warsaw", "warsaw": "Europe/Warsaw",
    "praga": "Europe/Prague", "prague": "Europe/Prague",
    "budapeste": "Europe/Budapest", "budapest": "Europe/Budapest",
    "bucareste": "Europe/Bucharest", "bucharest": "Europe/Bucharest",
    "atenas": "Europe/Athens", "athens": "Europe/Athens",
    "moscou": "Europe/Moscow", "moscow": "Europe/Moscow",
    "kiev": "Europe/Kiev",
    "istambul": "Europe/Istanbul", "istanbul": "Europe/Istanbul",
    # Oriente Médio
    "dubai": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
    "riad": "Asia/Riyadh", "riyadh": "Asia/Riyadh",
    "tel aviv": "Asia/Jerusalem",
    "jerusalém": "Asia/Jerusalem", "jerusalem": "Asia/Jerusalem",
    "beirute": "Asia/Beirut", "beirut": "Asia/Beirut",
    "doha": "Asia/Qatar",
    "kuwait": "Asia/Kuwait",
    # Ásia
    "mumbai": "Asia/Kolkata", "nova delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata", "kolkata": "Asia/Kolkata",
    "tóquio": "Asia/Tokyo", "tokyo": "Asia/Tokyo",
    "osaka": "Asia/Tokyo",
    "seul": "Asia/Seoul", "seoul": "Asia/Seoul",
    "pequim": "Asia/Shanghai", "beijing": "Asia/Shanghai",
    "xangai": "Asia/Shanghai", "shanghai": "Asia/Shanghai",
    "hong kong": "Asia/Hong_Kong",
    "singapura": "Asia/Singapore", "singapore": "Asia/Singapore",
    "bangkok": "Asia/Bangkok",
    "jakarta": "Asia/Jakarta",
    "kuala lumpur": "Asia/Kuala_Lumpur",
    "manila": "Asia/Manila",
    "taipei": "Asia/Taipei",
    # Oceania
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane",
    "perth": "Australia/Perth",
    "auckland": "Pacific/Auckland",
    # África
    "johannesburgo": "Africa/Johannesburg", "johannesburg": "Africa/Johannesburg",
    "cape town": "Africa/Johannesburg", "cidade do cabo": "Africa/Johannesburg",
    "cairo": "Africa/Cairo",
    "lagos": "Africa/Lagos",
    "nairóbi": "Africa/Nairobi", "nairobi": "Africa/Nairobi",
    "acra": "Africa/Accra", "accra": "Africa/Accra",
    "casablanca": "Africa/Casablanca",
}

# ---------------------------------------------------------------------------
# 2. Brasil: DDD → timezone
# ---------------------------------------------------------------------------
_BRAZIL_DDD_TZ: dict[str, str] = {
    # Sudeste / Sul / Centro-Oeste (UTC-3, sem horário de verão desde 2019)
    **{d: "America/Sao_Paulo" for d in [
        "11", "12", "13", "14", "15", "16", "17", "18", "19",  # SP
        "21", "22", "24",                                        # RJ
        "27", "28",                                              # ES
        "31", "32", "33", "34", "35", "37", "38",               # MG
        "41", "42", "43", "44", "45", "46",                     # PR
        "47", "48", "49",                                        # SC
        "51", "53", "54", "55",                                  # RS
        "61", "62", "64",                                        # DF / GO
        "71", "73", "74", "75", "77",                            # BA
        "79",                                                    # SE
    ]},
    # Nordeste (UTC-3, sem horário de verão)
    **{d: "America/Fortaleza" for d in [
        "81", "87",  # PE
        "82",        # AL
        "83",        # PB
        "84",        # RN
        "85", "88",  # CE
        "86", "89",  # PI
        "98", "99",  # MA
    ]},
    # Norte
    **{d: "America/Belem" for d in ["91", "93", "94", "96"]},   # PA + AP (UTC-3)
    **{d: "America/Manaus" for d in ["92", "97"]},               # AM (UTC-4)
    "69": "America/Porto_Velho",  # RO (UTC-4)
    "68": "America/Rio_Branco",   # AC (UTC-5)
    "95": "America/Boa_Vista",    # RR (UTC-4)
    "63": "America/Araguaina",    # TO (UTC-3)
    # Centro-Oeste específicos
    "65": "America/Cuiaba",       # MT (UTC-4)
    "66": "America/Cuiaba",       # MT interior
    "67": "America/Campo_Grande", # MS (UTC-4)
}

# ---------------------------------------------------------------------------
# 3. USA: area code (3 dígitos) → timezone (principais)
# ---------------------------------------------------------------------------
_USA_AREA_TZ: dict[str, str] = {
    # Eastern
    **{d: "America/New_York" for d in [
        "201", "202", "203", "205", "207", "212", "215", "216", "217", "219",
        "229", "231", "234", "239", "240", "248", "251", "252", "253", "267",
        "269", "270", "272", "276", "278", "283", "301", "302", "303", "304",
        "305", "313", "315", "317", "321", "330", "331", "332", "336", "339",
        "347", "351", "352", "380", "385", "386", "401", "404", "407", "410",
        "412", "413", "414", "419", "423", "424", "425", "440", "441", "443",
        "445", "448", "458", "463", "464", "470", "475", "478", "484", "502",
        "503", "508", "509", "513", "515", "516", "518", "540", "551", "561",
        "567", "571", "574", "580", "585", "586", "601", "602", "603", "606",
        "607", "610", "614", "615", "616", "617", "618", "619", "620", "623",
        "626", "630", "631", "636", "646", "647", "650", "651", "657", "659",
        "660", "661", "662", "664", "667", "669", "671", "678", "681", "689",
        "701", "703", "704", "706", "707", "708", "712", "713", "714", "716",
        "717", "718", "719", "720", "724", "726", "727", "730", "731", "732",
        "734", "737", "740", "743", "747", "752", "754", "757", "758", "760",
        "762", "763", "764", "765", "769", "770", "772", "773", "774", "775",
        "779", "781", "786", "787", "801", "802", "803", "804", "808", "810",
        "812", "813", "814", "815", "816", "817", "818", "828", "830", "831",
        "832", "835", "838", "839", "840", "843", "845", "847", "848", "850",
        "854", "856", "857", "859", "860", "862", "863", "864", "865", "870",
        "872", "878", "901", "904", "906", "907", "908", "910", "912", "913",
        "914", "915", "916", "917", "918", "919", "920", "925", "928", "929",
        "930", "931", "934", "936", "937", "938", "940", "941", "947", "949",
        "951", "952", "954", "956", "959", "970", "971", "972", "973", "975",
        "978", "979", "980", "984", "985", "989",
    ]},
}

# ---------------------------------------------------------------------------
# 4. DDI → timezone representativo por país
# ---------------------------------------------------------------------------
_DDI_TZ: dict[str, str] = {
    "55": "America/Sao_Paulo",
    "1":  "America/New_York",
    "44": "Europe/London",
    "33": "Europe/Paris",
    "49": "Europe/Berlin",
    "34": "Europe/Madrid",
    "39": "Europe/Rome",
    "351": "Europe/Lisbon",
    "31": "Europe/Amsterdam",
    "32": "Europe/Brussels",
    "41": "Europe/Zurich",
    "43": "Europe/Vienna",
    "46": "Europe/Stockholm",
    "47": "Europe/Oslo",
    "45": "Europe/Copenhagen",
    "358": "Europe/Helsinki",
    "48": "Europe/Warsaw",
    "420": "Europe/Prague",
    "36": "Europe/Budapest",
    "40": "Europe/Bucharest",
    "30": "Europe/Athens",
    "7":  "Europe/Moscow",
    "380": "Europe/Kiev",
    "90": "Europe/Istanbul",
    "81": "Asia/Tokyo",
    "82": "Asia/Seoul",
    "86": "Asia/Shanghai",
    "91": "Asia/Kolkata",
    "92": "Asia/Karachi",
    "971": "Asia/Dubai",
    "966": "Asia/Riyadh",
    "972": "Asia/Jerusalem",
    "961": "Asia/Beirut",
    "974": "Asia/Qatar",
    "965": "Asia/Kuwait",
    "62": "Asia/Jakarta",
    "63": "Asia/Manila",
    "64": "Pacific/Auckland",
    "61": "Australia/Sydney",
    "65": "Asia/Singapore",
    "66": "Asia/Bangkok",
    "60": "Asia/Kuala_Lumpur",
    "886": "Asia/Taipei",
    "852": "Asia/Hong_Kong",
    "27": "Africa/Johannesburg",
    "234": "Africa/Lagos",
    "20": "Africa/Cairo",
    "254": "Africa/Nairobi",
    "233": "Africa/Accra",
    "212": "Africa/Casablanca",
    "244": "Africa/Luanda",
    "258": "Africa/Maputo",
    "238": "Atlantic/Cape_Verde",
    "52": "America/Mexico_City",
    "54": "America/Argentina/Buenos_Aires",
    "56": "America/Santiago",
    "57": "America/Bogota",
    "51": "America/Lima",
    "58": "America/Caracas",
    "593": "America/Guayaquil",
    "598": "America/Montevideo",
    "595": "America/Asuncion",
    "591": "America/La_Paz",
    "502": "America/Guatemala",
    "503": "America/El_Salvador",
    "504": "America/Tegucigalpa",
    "505": "America/Managua",
    "506": "America/Costa_Rica",
    "507": "America/Panama",
    "53": "America/Havana",
    "1809": "America/Santo_Domingo",
    "1787": "America/Puerto_Rico",
}


def infer_timezone(phone: str, cidade: str | None = None) -> str:
    """
    Infere o timezone IANA mais provável para um usuário.

    Ordem de prioridade:
      1. Cidade cadastrada no banco
      2. DDI + código de área (DDD ou area code)
      3. DDI sozinho
      4. UTC
    """
    # 1. Cidade cadastrada — mais precisa
    if cidade:
        tz = _CITY_TZ.get(cidade.lower().strip())
        if tz:
            return tz

    if not phone:
        return "UTC"

    digits = "".join(c for c in phone if c.isdigit())

    # 2. DDI de 4 dígitos (ex: 1809, 1787)
    ddi4 = digits[:4]
    if ddi4 in _DDI_TZ:
        return _DDI_TZ[ddi4]

    # 3. DDI de 3 dígitos (ex: 351, 420, 244)
    ddi3 = digits[:3]
    if ddi3 in _DDI_TZ:
        return _DDI_TZ[ddi3]

    # 4. DDI de 2 dígitos (ex: 55, 44, 49)
    ddi2 = digits[:2]

    # Brasil: refinar pelo DDD (2 dígitos após o 55)
    if ddi2 == "55" and len(digits) >= 4:
        ddd = digits[2:4]
        tz = _BRAZIL_DDD_TZ.get(ddd)
        if tz:
            return tz
        return "America/Sao_Paulo"  # fallback Brasil

    # EUA/Canadá: refinar pelo area code (3 dígitos após o 1)
    if digits[0] == "1" and len(digits) >= 4:
        area = digits[1:4]
        tz = _USA_AREA_TZ.get(area)
        if tz:
            return tz
        return "America/New_York"  # fallback EUA

    if ddi2 in _DDI_TZ:
        return _DDI_TZ[ddi2]

    # 5. DDI de 1 dígito (ex: 7 = Rússia)
    ddi1 = digits[:1]
    if ddi1 in _DDI_TZ:
        return _DDI_TZ[ddi1]

    return "UTC"
