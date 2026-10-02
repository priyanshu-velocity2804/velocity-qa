import csv
import io
import json
from pathlib import Path
import unittest

from importer import shopify
from importer.links import ProductLink, parse_message
from importer.scrapers import normalise_amazon, normalise_flipkart, weight_in_grams

FIXTURES = Path(__file__).with_name("fixtures")
AMAZON_LINK = ProductLink("amazon", "B073JYC4XM", "https://www.amazon.in/dp/B073JYC4XM", "amazon.in")


def amazon_product():
    return normalise_amazon(json.loads((FIXTURES / "rainforest_B073JYC4XM.json").read_text()), AMAZON_LINK)


class LinkTests(unittest.TestCase):
    def test_slack_formatted_links(self):
        text = ("<@U1> add <https://www.amazon.in/SanDisk/dp/B073JYC4XM/ref=x?th=1|this> and "
                "<https://www.flipkart.com/x/p/itm?pid=ACCHE5DZFMEP4PYG&amp;lid=1> https://google.com")
        links, errors = parse_message(text)
        self.assertEqual([(l.source, l.product_id) for l in links],
                         [("amazon", "B073JYC4XM"), ("flipkart", "ACCHE5DZFMEP4PYG")])
        self.assertEqual(links[0].amazon_domain, "amazon.in")
        self.assertEqual(errors, [])

    def test_other_amazon_domain_and_gp_product(self):
        links, _ = parse_message("https://www.amazon.com/gp/product/B08N5WRWNW?psc=1")
        self.assertEqual((links[0].product_id, links[0].amazon_domain), ("B08N5WRWNW", "amazon.com"))

    def test_duplicates_and_bad_links(self):
        links, errors = parse_message("https://amazon.in/dp/B073JYC4XM https://amazon.in/dp/B073JYC4XM/ "
                                      "https://www.flipkart.com/some-category")
        self.assertEqual(len(links), 1)
        self.assertEqual(len(errors), 1)


class NormaliseTests(unittest.TestCase):
    def test_amazon(self):
        p = amazon_product()
        self.assertEqual((p["price"], p["compare_at_price"], p["currency"]), (2499, 3500, "INR"))
        self.assertEqual(p["brand"], "SanDisk")
        self.assertEqual(p["product_type"], "Micro SD")
        self.assertEqual(len(p["images"]), 4)
        self.assertEqual(p["weight_grams"], 5)

    def test_flipkart_drops_seo_description_and_flattens_specs(self):
        raw = {"title": "Headset�X", "brand": "boAt", "category": "headphone", "price": 1699, "mrp": 5990.0,
               "currency": "INR", "availability": "https://schema.org/InStock", "images": ["https://img/1.jpg"],
               "description": "Buy boAt ... Cash On Delivery!", "highlights": ["Deep Bass"],
               "specifications": {"General": {"Color": ["Black", "Silver"]}, "Dimensions": {"Weight": ["110 g"]}},
               "manufacturer_details": {"Generic Name": "Headphones"}}
        p = normalise_flipkart(raw, ProductLink("flipkart", "ACCHE5DZFMEP4PYG",
                                                "https://www.flipkart.com/x/p/itm?pid=ACCHE5DZFMEP4PYG&lid=9"))
        self.assertEqual(p["title"], "Headset X")
        self.assertEqual(p["description"], "")
        self.assertEqual(p["specifications"], {"Color": "Black, Silver", "Weight": "110 g"})
        self.assertEqual((p["product_type"], p["weight_grams"], p["in_stock"]), ("Headphones", 110, True))
        self.assertEqual(p["source_url"], "https://www.flipkart.com/x/p/itm?pid=ACCHE5DZFMEP4PYG")

    def test_weights(self):
        self.assertEqual(weight_in_grams("1.2 kg"), 1200)
        self.assertEqual(weight_in_grams("500 Grams"), 500)
        self.assertIsNone(weight_in_grams("n/a"))


class ShopifyFormatTests(unittest.TestCase):
    def test_csv_has_one_row_per_image(self):
        rows = list(csv.DictReader(io.StringIO(shopify.to_csv([amazon_product()]))))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["Handle"], "sandisk-128gb-class-10-microsdxc-memory-card")
        self.assertEqual(rows[0]["Variant Price"], "2499.00")
        self.assertEqual(rows[0]["Status"], "draft")
        self.assertEqual((rows[0]["Variant Inventory Tracker"], rows[0]["Variant Inventory Qty"]), ("shopify", "100"))
        self.assertEqual(rows[0]["Variant Inventory Policy"], "deny")
        self.assertEqual(rows[3]["Image Position"], "4")
        self.assertEqual(rows[3]["Title"], "")

    def test_handles_follow_the_product_name(self):
        def handle(title):
            return shopify.handle_for({"title": title, "source_id": "X"})
        self.assertEqual(handle("boAt Airdopes 219, 40H Battery, Free Music"), "boat-airdopes-219")
        self.assertEqual(handle("boAt Airdopes 311 Pro w/ 50 HRS Playback"), "boat-airdopes-311-pro")
        self.assertEqual(handle("boAt Rockerz 110 with 40 HRS Playback"), "boat-rockerz-110")
        self.assertEqual(handle("boAt Rockerz 255 Pro+, 60H Battery"), "boat-rockerz-255-pro-plus")
        self.assertEqual(handle("boAt Rockerz 255 ANC(32dB) w/ 100 HRS"), "boat-rockerz-255-anc")

    def test_inventory_quantity_must_be_positive(self):
        import os
        for bad in ("0", "-5", "lots"):
            os.environ["INVENTORY_QUANTITY"] = bad
            try:
                with self.assertRaises(shopify.ShopifyError):
                    shopify.inventory_quantity()
            finally:
                del os.environ["INVENTORY_QUANTITY"]

    def test_csv_handles_are_unique(self):
        second = dict(amazon_product(), source_id="B000000002")
        rows = list(csv.DictReader(io.StringIO(shopify.to_csv([amazon_product(), second]))))
        handles = [r["Handle"] for r in rows if r["Title"]]
        self.assertNotEqual(handles[0], handles[1])
        self.assertTrue(handles[1].endswith("-b000000002"))

    def test_product_set_input_and_markup(self):
        import os
        os.environ["PRICE_MARKUP_PERCENT"] = "10"
        try:
            data = shopify.to_product_set_input(amazon_product(), "active")
        finally:
            del os.environ["PRICE_MARKUP_PERCENT"]
        variant = data["variants"][0]
        self.assertEqual((variant["price"], variant["compareAtPrice"]), ("2748.90", "3850.00"))
        self.assertEqual(data["status"], "ACTIVE")
        self.assertTrue(variant["inventoryItem"]["tracked"])
        self.assertEqual(len(data["files"]), 4)
        self.assertIn("<table>", data["descriptionHtml"])


if __name__ == "__main__":
    unittest.main()
