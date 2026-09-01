import unittest
from unittest.mock import patch
from monitor import ProductParser, fetch_product_page, nutrition_summary, parse_nutrition, product_message


class ParserTest(unittest.TestCase):
    def test_card(self):
        parser = ProductParser()
        parser.feed('''<div class="ProductCard js-product-cart" data-id="65192">
          <a href="/goods/test-65192.html"><img data-src="/upload/test.jpg"></a>
          <div>Молоко 1 л</div><span>79 ₽</span><del>120 ₽</del>
          <button data-max="3"></button></div>''')
        self.assertEqual(parser.cards[0]["id"], "65192")
        self.assertEqual(parser.cards[0]["stock"], "3")
        self.assertEqual(parser.cards[0]["green_price"], "79 ₽")
        self.assertEqual(parser.cards[0]["old_price"], "120 ₽")
        self.assertTrue(parser.cards[0]["href"].startswith("https://vkusvill.ru/"))

    def test_real_card_text(self):
        parser = ProductParser()
        parser.feed('''<div class="ProductCard" data-id="668">
        Овощи//Ягоды 1 3-8 кг 4.6 78 руб /кг 130 руб 78 130
        Дыня Торпеда В корзину кг 0 руб
        <button data-max="9.335"></button></div>''')
        card = parser.cards[0]
        self.assertEqual(card["name"], "Дыня Торпеда")
        self.assertEqual(card["green_price"], "78 руб")
        self.assertEqual(card["old_price"], "130 руб")

    def test_thousands_in_price(self):
        parser = ProductParser()
        parser.feed('''<div class="ProductCard" data-id="88414">4.9 747 руб /шт 1 245 руб
        747 1245 Торт блинный шоколадный, 1 кг В корзину<button data-max="2"></button></div>''')
        self.assertEqual(parser.cards[0]["name"], "Торт блинный шоколадный, 1 кг")
        self.assertEqual(parser.cards[0]["old_price"], "1245 руб")

    def test_multiple_nutrition_variants(self):
        source = '''<div>ООО &quot;АРМЕ ГРУПП&quot;: белки 5 г, жиры 19.5 г, углеводы 29.6 г,
        в том числе сахара (общие) – 12.6 г, соль – 0.2 г; 313.9 ккал<br>
        ООО "СЛАДКОНЦЕПТ": белки 5.4 г, жиры 19.8 г, углеводы 28 г,
        в том числе сахара (общие) – 18.9 г, соль – 0.4 г; 311.8 ккал</div>'''
        variants = parse_nutrition(source)
        self.assertEqual(len(variants), 2)
        summary = nutrition_summary(variants)
        self.assertTrue(summary["approx"])
        self.assertAlmostEqual(summary["kcal"], 312.85)
        self.assertAlmostEqual(summary["protein"], 5.2)

    def test_single_nutrition_is_exact(self):
        variants = parse_nutrition('''<p>АО «ТЕСТ»: белки 10 г, жиры 2,5 г,
        углеводы 3 г, соль – 0,1 г; 73,5 ккал</p>''')
        self.assertEqual(len(variants), 1)
        self.assertFalse(nutrition_summary(variants)["approx"])

    def test_summary_nutrition_fallback(self):
        variants = parse_nutrition("Пищевая ценность на 100 грамм 215.1 Ккал 5.9 Белки, г 18.7 Жиры, г 5.8 Углеводы, г")
        self.assertEqual(variants[0]["kcal"], 215.1)
        self.assertEqual(variants[0]["protein"], 5.9)

    def test_caption_has_savings_and_address(self):
        caption = product_message({"name":"Тест", "old_price":"200 руб", "green_price":"120 руб",
            "stock":"2", "href":"https://vkusvill.ru/goods/test/", "nutrition":{
                "kcal":100, "protein":5, "fat":4, "carbs":3, "approx":False}})
        self.assertIn("80 ₽ (40%)", caption)
        self.assertIn("Лиговский проспект, 232", caption)

    def test_product_page_error_has_context(self):
        with patch("monitor.open_https", side_effect=OSError("forbidden")):
            with self.assertRaisesRegex(RuntimeError, "страницу товара"):
                fetch_product_page("https://vkusvill.ru/goods/test/", "cookie")


if __name__ == "__main__":
    unittest.main()
