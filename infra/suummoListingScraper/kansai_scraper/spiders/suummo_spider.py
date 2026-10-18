import json    # read areas.json, our list of what to crawl
import scrapy  # the crawling framework

# All knowledge of SUUMO's detail-page markup lives in extract.py, not here.
from kansai_scraper.extract import extract_listing


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
        for area in self.areas:
            yield scrapy.Request(
                area["search_url"],
                callback=self.parse_listing_page,
                # meta = a small notebook that travels with the request.
                meta={"area_code": area["code"], "page_num": 1},
            )

    def parse_listing_page(self, response):
        # Stage 2: runs once per search-results page.
        area = next(a for a in self.areas if a["code"] == response.meta["area_code"])
        current_page = response.meta.get("page_num", 1)

        # Forward only the keys we need (copying all of response.meta drags
        # along Scrapy internals and triggers MetaCopyDetectionMiddleware).
        clean_meta = {"area_code": response.meta["area_code"]}

        # One link per room row on the page (selector confirmed in DevTools).
        for link in response.css("a.js-cassette_link_href::attr(href)").getall():
            yield response.follow(link, callback=self.parse_listing_detail, meta=clean_meta)

        # Pagination, capped by max_pages in areas.json. This caps SEARCH pages,
        # not detail pages, which is why one capped crawl still makes ~100 requests.
        if current_page < area.get("max_pages", 1):
            # Match on the link TEXT (次へ = "Next") so we never grab 前へ ("Previous").
            next_page = response.css('p.pagination-parts a:contains("次へ")::attr(href)').get()
            if next_page:
                yield response.follow(
                    next_page,
                    callback=self.parse_listing_page,
                    meta={"area_code": response.meta["area_code"], "page_num": current_page + 1},
                )

    def parse_listing_detail(self, response):
        # Stage 3: runs once per listing detail page. The heavy lifting is in
        # extract_listing(); this method only handles success/failure.
        try:
            listing = extract_listing(response.text, response.url)
        except ValueError as err:
            # A page we can't read must be VISIBLE, not silently dropped. The count
            # shows up in the stats dump at the end of every crawl as
            # 'extract/failed'. If that number is high, SUUMO changed its markup.
            self.logger.warning("Skipping listing, extraction failed: %s", err)
            self.crawler.stats.inc_value("extract/failed")
            return

        listing["area_code"] = response.meta["area_code"]
        yield listing  # Scrapy writes it to output.json via the FEEDS setting
