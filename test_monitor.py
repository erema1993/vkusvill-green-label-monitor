import unittest
from monitor import ProductParser


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


if __name__ == "__main__":
    unittest.main()
