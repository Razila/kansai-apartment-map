import json    # read areas.json, our list of what to crawl
import re      # regular expressions: used to pull the stable ID out of a URL
import scrapy  # the crawling framework


class SuummoSpider(scrapy.Spider):
    # How you run it: `scrapy crawl suummo`. Must be unique within the project.
    name = "suummo"

    def __init__(self, *args, **kwargs):
        # Runs once when the spider is created. super().__init__ keeps Scrapy's
        # own setup working; we add our config loading on top.
        super().__init__(*args, **kwargs)

        # The "what to crawl" list lives in a JSON file, not in this code, so
        # you can add or remove wards without touching Python. The relative path
        # means this only works when you run `scrapy crawl` from the folder that
        # contains areas.json (the project root, next to scrapy.cfg).
        with open("areas.json", encoding="utf-8") as f:
            self.areas = json.load(f)

    async def start(self):
        # Scrapy 2.13+ calls this to get the first requests. (The older
        # start_requests() was removed in 2.16, and a spider that only defines
        # it silently crawls nothing.)
        # `async def` + `yield` makes this an "async generator": it hands
        # requests to Scrapy one at a time instead of building a list.
        for area in self.areas:
            yield scrapy.Request(
                area["search_url"],
                # callback = "when this response comes back, run this method"
                callback=self.parse_listing_page,
                # meta = a small notebook that travels with the request, so the
                # next stage knows which area it's in and which page number.
                meta={"area_code": area["code"], "page_num": 1},
            )

    def parse_listing_page(self, response):
        # Stage 2: runs once per search-results page.

        # Look up this area's config (needed for its max_pages cap).
        # next(... for ... if ...) means "first match in the list".
        area = next(a for a in self.areas if a["code"] == response.meta["area_code"])
        current_page = response.meta.get("page_num", 1)

        # Forward only the keys we actually need. Copying all of response.meta
        # drags along Scrapy's internal bookkeeping (e.g. download_latency),
        # which triggered the MetaCopyDetectionMiddleware warning you saw.
        clean_meta = {"area_code": response.meta["area_code"]}

        # Find every "詳細を見る" link on the page (the selector you confirmed in
        # DevTools: the stable js- prefixed class). ::attr(href) extracts the URL
        # text itself; getall() returns all matches as a list.
        for link in response.css("a.js-cassette_link_href::attr(href)").getall():
            # response.follow handles relative URLs like "/chintai/jnc_.../" by
            # joining them to the current page's address. Each yield queues one
            # request, whose response goes to parse_listing_detail.
            yield response.follow(link, callback=self.parse_listing_detail, meta=clean_meta)

        # Pagination: only continue if we haven't reached the cap in areas.json.
        # This is why max_pages limits search pages but not detail pages: it only
        # gates the "next page" request, not the per-listing requests above.
        if current_page < area.get("max_pages", 1):  # default 1 = fail safe
            # Find the link whose text is 次へ ("Next"). Matching on text avoids
            # accidentally grabbing the 前へ ("Previous") link in the same
            # container. Note: straight quotes only; curly quotes break this.
            next_page = response.css('p.pagination-parts a:contains("次へ")::attr(href)').get()
            if next_page:
                yield response.follow(
                    next_page,
                    callback=self.parse_listing_page,  # same method again: a loop
                    # page_num + 1 is how the loop knows when to stop.
                    meta={"area_code": response.meta["area_code"], "page_num": current_page + 1},
                )

    def parse_listing_detail(self, response):
        # Stage 3: runs once per listing page. Still a placeholder: it records
        # identifiers only. Title, rent, address, 敷金/礼金 come next.

        # Extract just "jnc_000109954940" from ".../chintai/jnc_000109954940/?bc=...".
        # The ?bc=... part is a tracking parameter that can change between
        # crawls, so the full URL would make one listing look like many.
        listing_id_match = re.search(r"/chintai/(jnc_\d+)/", response.url)

        # `yield` a dict = "here is one finished record". Scrapy writes it to
        # output.json via the FEEDS setting.
        yield {
            "listing_id": listing_id_match.group(1) if listing_id_match else None,
            "source_url": response.url,
            "area_code": response.meta["area_code"],
        }