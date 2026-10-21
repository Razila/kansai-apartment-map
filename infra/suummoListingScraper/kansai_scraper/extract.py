"""
extract.py -- the ONLY place in the project that knows what a SUUMO listing
detail page looks like.

Why a separate module (instead of code inside the spider)?
  * It is a pure function: HTML text in, a plain dict out. No network, no Scrapy.
    That means we can test it offline against saved pages (see tests/) and find
    out immediately, for free, when SUUMO changes its markup.
  * When SUUMO redesigns a page, this is the single file you edit. This replaces
    the "normalize_listing() choke point" idea from the original plan.

Extraction strategy, in order of preference:
  1. LABEL-BASED: find the <th> whose text is e.g. "所在地" and read the <td> next
     to it. Labels are human-facing words that rarely change, unlike CSS class
     names, which are chosen by SUUMO's developers and change in redesigns.
  2. Class-based, only where there is no label (the money block, agency box).
"""
import re
from decimal import Decimal

from parsel import Selector

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

KANSAI_PREFECTURES = ("大阪府", "京都府", "兵庫県", "滋賀県", "奈良県", "和歌山県")

# prefecture + city (市, or 郡+町/村, or bare 町/村) + optional ward (区).
# The ward is limited to 1-4 non-digit characters so a stray "区" later in the
# street address can't be mistaken for a ward.
_ADDRESS_RE = re.compile(
    r"^(?P<prefecture>" + "|".join(KANSAI_PREFECTURES) + r")"
    r"(?P<city>[^市郡町村]+?市|[^市郡町村]+?郡[^市郡町村]+?[町村]|[^市郡町村]+?[町村])"
    r"(?P<ward>[^区\d]{1,4}区)?"
)

# e.g. "地下鉄谷町線/天神橋筋六丁目駅 歩2分"
_STATION_RE = re.compile(r"(?P<line>\S+?)/(?P<station>\S+?)\s*歩(?P<minutes>\d+)分")


def _clean(text):
    """Collapse all whitespace (including non-breaking spaces) to single spaces."""
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _text(selector):
    """All text inside an element, cleaned. Works for single Selectors."""
    return _clean(selector.xpath("string(.)").get(""))


def parse_yen(text):
    """'9.7万円' -> 97000, '7200円' -> 7200, '-' -> None.
    Decimal avoids float errors like 12.4 * 10000 = 124000.00000000001."""
    t = _clean(text).replace(",", "").replace("円", "")
    m = re.fullmatch(r"([\d.]+)(万)?", t)
    if not m:
        return None
    return int(Decimal(m.group(1)) * (10000 if m.group(2) else 1))


def split_address(address):
    """'大阪府大阪市北区長柄中１' -> ('大阪府', '大阪市', '北区'). Missing parts are None."""
    m = _ADDRESS_RE.match(address or "")
    if not m:
        return None, None, None
    return m.group("prefecture"), m.group("city"), m.group("ward")


def parse_stations(text):
    """Turn the 駅徒歩 cell into a list of {line, station, walk_minutes}."""
    return [
        {"line": m["line"], "station": m["station"], "walk_minutes": int(m["minutes"])}
        for m in _STATION_RE.finditer(_clean(text))
    ]


def classify_stove(equipment):
    """What cooking equipment does the unit come with?
        'IH'        induction cooktop installed (IHクッキングヒーター)
        'stove'     a stove with N burners is listed (2口コンロ, ...)
        'gas_ready' only gas-stove COMPATIBLE (ガスコンロ対応 / 設置可): there is a gas
                    line, but you must bring your own stove
        None        nothing stove-related listed
    'N口コンロ' means the stove top has N burners, i.e. a stove IS installed
    (confirmed against real pages)."""
    if "IHクッキングヒーター" in equipment or "IHコンロ" in equipment:
        return "IH"
    if re.search(r"\d口コンロ|コンロ\d口", equipment):
        return "stove"
    if re.search(r"ガスコンロ(対応|設置可)|コンロ設置可", equipment):
        return "gas_ready"
    if "ガスコンロ" in equipment:
        return "stove"
    return None


def _pick_thumbnail(sel):
    """Prefer the exterior photo (alt text contains 外観); fall back to the first
    photo. SUUMO lazy-loads images, so the real URL is in data-src, not src."""
    imgs = sel.css("#js-view_gallery img.property_view_object-img")
    exterior = [i for i in imgs if "外観" in i.attrib.get("alt", "")]
    chosen = (exterior or list(imgs))[:1]
    if not chosen:
        return None
    return chosen[0].attrib.get("data-src") or chosen[0].attrib.get("src")


# ---------------------------------------------------------------------------
# The main function
# ---------------------------------------------------------------------------

def extract_listing(html, url=""):
    """Extract one listing from a SUUMO detail page. Raises ValueError if a field
    we cannot live without (id, title, rent, address) is missing, so a broken
    selector fails LOUDLY instead of silently writing empty rows."""
    sel = Selector(text=html)

    # --- identity -----------------------------------------------------------
    # jnc_... identifies the UNIT and is shared by every agency advertising it
    # (the canonical URL has no ?bc=). That makes it the right dedupe key.
    canonical = sel.css("link[rel=canonical]::attr(href)").get("") or url
    m = re.search(r"(jnc_\d+)", canonical) or re.search(r"(jnc_\d+)", url)
    listing_id = m.group(1) if m else None

    # --- the overview tables: label -> value, first occurrence wins ---------
    # The overview is split across TWO tables: property_view_table (address,
    # stations, layout, size, floor) and data_table (build date, parking,
    # conditions, extra fees). We deliberately do NOT read every <table> on the
    # page: the 40 agency boxes below contain look-alike rows (e.g. their own
    # 敷/礼 terms) that would silently overwrite the right values.
    fields = {}
    for tr in sel.css("table.property_view_table tr, table.data_table tr"):
        for th, td in zip(tr.css("th"), tr.css("td")):
            label = re.sub(r"\s", "", _text(th))   # drop ALL whitespace inside labels
            if label and label not in fields:
                fields[label] = _text(td)

    # --- money block at the top of the page (belongs to THIS agency's listing) -
    note = sel.css(".property_view_note")
    rent_yen = parse_yen(_text(note.css(".property_view_note-emphasis")[0])) if note.css(".property_view_note-emphasis") else None
    terms = {}
    for span in note.css(".property_view_note-list span"):
        txt = _text(span)
        if re.search(r"[:：]", txt):
            key, value = re.split(r"[:：]", txt, maxsplit=1)
            terms[key.strip()] = value.strip()

    def required(label):
        # SUUMO shows "-" when a fee does not apply; anything else means it does.
        return terms.get(label, "-") not in ("", "-")

    shikikin_yen = parse_yen(terms.get("敷金"))
    reikin_yen = parse_yen(terms.get("礼金"))

    # --- equipment list: one long comma-separated string of features --------
    equipment = _clean(" ".join(sel.css("#bkdt-option li::text").getall()))

    # --- agency (first box = the agency of the listing we followed) ---------
    agency = sel.css(".itemcassette")[:1]
    agency_name = _clean(agency.css(".itemcassette-header-ttl::text").get("")) if agency else None
    agency_logo = agency.css("img::attr(src)").get() if agency else None

    # --- address, size, dates ------------------------------------------------
    address = fields.get("所在地", "")
    prefecture, city, ward = split_address(address)

    def first_number(text):
        m = re.search(r"\d+(?:\.\d+)?", text or "")
        return m.group(0) if m else None

    size = first_number(fields.get("専有面積"))
    built = re.search(r"(\d{4})年", fields.get("築年月", ""))
    floor = first_number(fields.get("階"))
    total_floors = re.search(r"/\s*(\d+)階建", fields.get("階建", ""))
    stations = parse_stations(fields.get("駅徒歩", ""))
    parking_text = fields.get("駐車場", "-")
    conditions = fields.get("条件", "")

    listing = {
        "listing_id": listing_id,
        "suumo_code": fields.get("SUUMO物件コード"),
        "source_url": url,
        "title_ja": _clean(sel.css("h1").xpath("string(.)").get("")),
        "address_ja": address,
        "prefecture": prefecture,
        "city": city,
        "ward": ward,
        "rent_yen": rent_yen,
        "management_fee_yen": parse_yen(terms.get("管理費・共益費")),
        "requires_shikikin": required("敷金"),
        "requires_reikin": required("礼金"),
        "shikikin_yen": shikikin_yen,
        "reikin_yen": reikin_yen,
        "shikikin_months": round(shikikin_yen / rent_yen, 2) if shikikin_yen and rent_yen else None,
        "reikin_months": round(reikin_yen / rent_yen, 2) if reikin_yen and rent_yen else None,
        # "anything else" up-front: kept as the original Japanese text per item
        "other_initial_costs": {
            k: fields[k] for k in ("仲介手数料", "保証会社", "ほか初期費用", "ほか諸費用") if k in fields
        },
        "room_layout": fields.get("間取り"),
        "size_sqm": float(size) if size else None,
        "built_year": int(built.group(1)) if built else None,
        "floor": int(float(floor)) if floor else None,
        "total_floors": int(total_floors.group(1)) if total_floors else None,
        "stations": stations,
        "nearest_station_walk_min": min((s["walk_minutes"] for s in stations), default=None),
        "parking_available": parking_text not in ("", "-"),
        "parking_text": parking_text,
        # --- feature flags derived from the equipment string ---
        "has_ac": "エアコン" in equipment,
        "stove_type": classify_stove(equipment),    # replaces the old has_oven flag
        "has_washer_hookup": "室内洗濯置" in equipment,
        "secured_entrance": "オートロック" in equipment,
        "bathroom_type": "バス・トイレ別" if "バストイレ別" in equipment else None,
        # ペット相談 = "pets: consult". Allowed subject to the landlord's approval, so
        # the honest name is "negotiable", not "allowed". It is also SUUMO's own
        # search filter. Confirmed on a real page: the term appears in BOTH the
        # equipment list and the 条件 row, so we check both.
        "pets_negotiable": "ペット相談" in equipment or "ペット相談" in conditions,
        # 定期借家 = fixed-term lease (ends on a set date, may not be renewable);
        # 普通借家 = ordinary renewable lease. None if the row is missing.
        "fixed_term_lease": ("定期借家" in fields["契約期間"]) if fields.get("契約期間") else None,
        "equipment_text": equipment,                 # raw, so flags can be recomputed later
        "agency_name_ja": agency_name,
        "agency_logo_url": agency_logo,
        "thumbnail_url": _pick_thumbnail(sel),
    }

    missing = [k for k in ("listing_id", "title_ja", "rent_yen", "address_ja") if not listing[k]]
    if missing:
        raise ValueError(f"extraction failed, missing {missing} for {url or listing_id}")
    return listing
