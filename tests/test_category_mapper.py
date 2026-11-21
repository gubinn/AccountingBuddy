import unittest

from category_mapper import (CategoryMapper, MerchantRuleSet,
                             build_initial_mapping, build_merchant_map,
                             flatten_category_tree, normalize_merchant)


class FakeAPI:
    def get_categories(self):
        return [
            {"id": 1, "parentId": 0, "name": "餐饮", "type": 1, "children": [
                {"id": 11, "parentId": 1, "name": "外卖", "type": 1, "children": []},
            ]},
            {"id": 2, "parentId": 0, "name": "购物", "type": 1, "children": []},
            {"id": 3, "parentId": 0, "name": "住房", "type": 1, "children": [
                {"id": 31, "parentId": 3, "name": "水电燃气", "type": 1, "children": []},
            ]},
            {"id": 4, "parentId": 0, "name": "工资", "type": 2, "children": []},
            {"id": 5, "parentId": 0, "name": "其他支出", "type": 1, "children": []},
            {"id": 6, "parentId": 0, "name": "其他收入", "type": 2, "children": []},
            {"id": 7, "parentId": 0, "name": "交通", "type": 1, "children": []},
            {"id": 8, "parentId": 0, "name": "通讯", "type": 1, "children": []},
            {"id": 9, "parentId": 0, "name": "订阅服务", "type": 1, "children": []},
            {"id": 10, "parentId": 0, "name": "人情往来", "type": 1, "children": []},
        ]


class FakeLLM:
    def classify(self, text, note, tx_type):
        return 5 if "未知商户" in text else None


class MappingTests(unittest.TestCase):
    def test_flatten_tree_includes_children(self):
        flat = flatten_category_tree(FakeAPI().get_categories())
        self.assertEqual(flat[31]["name"], "水电燃气")
        self.assertEqual(flat[31]["parent_id"], 3)
        self.assertEqual(flat[4]["type"], 2)

    def test_build_merges_missing_groups(self):
        config = {"category_mapping": {"餐饮": 1}}
        changed, types, categories = build_initial_mapping(FakeAPI(), config)
        self.assertTrue(changed)
        self.assertEqual(config["category_mapping"]["购物"], 2)
        self.assertEqual(config["category_mapping"]["住房"], 3)
        self.assertEqual(config["category_mapping"]["通讯"], 8)
        self.assertEqual(config["category_mapping"]["订阅服务"], 9)
        self.assertEqual(config["category_mapping"]["人情往来"], 10)
        self.assertEqual(types[1], 1)
        self.assertEqual(types[4], 2)
        self.assertEqual(categories[8]["name"], "通讯")

    def test_classifier_respects_type(self):
        mapper = CategoryMapper({"工资": 4, "餐饮": 1}, {4: 2, 1: 1})
        # An expense mentioning 工资 must not land in an income category
        self.assertNotEqual(mapper.classify("工资卡消费", tx_type=1), 4)
        self.assertEqual(mapper.classify("工资代发", tx_type=2), 4)
        self.assertEqual(mapper.classify("美团外卖", tx_type=1), 1)

    def test_generic_keywords_removed(self):
        mapper = CategoryMapper({"餐饮": 1}, {1: 1})
        self.assertIsNone(mapper.classify("面包店", tx_type=1))


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.rules = MerchantRuleSet()

    def test_normalization(self):
        self.assertEqual(normalize_merchant("支付宝-北京三快在线科技有限公司"),
                         "北京三快在线科技")
        self.assertEqual(normalize_merchant("财付通-微信支付-微信红包快捷支付"),
                         "微信红包")
        self.assertEqual(normalize_merchant("Apple Pay, 线上支付消费，Octopus Cards Limited"),
                         "OctopusCardsLimited")

    def test_legal_entity_rules(self):
        cases = {
            "支付宝-北京三快在线科技有限公司": "餐饮",
            "支付宝-上海拉扎斯信息科技有限公司": "餐饮",
            "支付宝-中国铁路网络有限公司": "交通",
            "支付宝-贝壳租房": "住房",
            "支付宝-上海盒马网络科技有限公司": "购物",
            "支付宝-阿里云计算有限公司": "订阅服务",
            "支付宝-中广电移动网络有限公司": "通讯",
            "财付通-微信支付-微信红包快捷支付": "人情往来",
            "支付宝-上海海王星辰药房": "医疗",
            "支付宝-北京大学": "教育",
        }
        for merchant, expected in cases.items():
            self.assertEqual(self.rules.match(merchant), expected, merchant)

    def test_unknown_merchant_returns_none(self):
        self.assertIsNone(self.rules.match("支付宝-某某不明商户"))


class LearnedMappingTests(unittest.TestCase):
    def test_build_merchant_map_from_budgetpilot(self):
        merchants = [
            {"name": "美团商户", "alias": "美团,外卖", "categoryId": 1},
            {"name": "交通商户", "alias": None, "categoryId": 7},
            {"name": "未知商户", "alias": None, "categoryId": 5},  # 其他支出 -> ignore
        ]
        mapping = build_merchant_map(merchants, {1: 1, 7: 1, 5: 1}, excluded={5})
        self.assertEqual(mapping["美团商户"], 1)
        self.assertEqual(mapping["外卖"], 1)
        self.assertEqual(mapping["交通商户"], 7)
        self.assertNotIn("未知商户", mapping)

    def test_learned_mapping_wins_over_rules(self):
        mapper = CategoryMapper(
            {"餐饮": 1, "购物": 2}, {1: 1, 2: 1},
            merchant_map={"支付宝-盒马": 1},
        )
        # User explicitly taught that this merchant is 餐饮
        self.assertEqual(mapper.classify("支付宝-盒马", tx_type=1), 1)

    def test_llm_fallback_is_last(self):
        mapper = CategoryMapper(
            {"餐饮": 1, "其他支出": 5}, {1: 1, 5: 1},
            llm=FakeLLM(),
        )
        self.assertEqual(mapper.classify("未知商户", tx_type=1), 5)
        self.assertIsNone(mapper.classify("未知商户", tx_type=1, allow_llm=False))


if __name__ == "__main__":
    unittest.main()