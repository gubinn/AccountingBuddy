import unittest

from category_mapper import (CategoryMapper, MerchantRuleSet,
                             build_initial_mapping, build_merchant_map,
                             flatten_category_tree, normalize_merchant)


class FakeAPI:
    def get_categories(self):
        return [
            {"id": 1, "parentId": 0, "name": "餐饮", "type": 1, "children": [
                {"id": 11, "parentId": 1, "name": "外卖", "type": 1, "children": []},
                {"id": 12, "parentId": 1, "name": "饮品", "type": 1, "children": []},
                {"id": 13, "parentId": 1, "name": "食材", "type": 1, "children": []},
                {"id": 14, "parentId": 1, "name": "堂食", "type": 1, "children": []},
            ]},
            {"id": 2, "parentId": 0, "name": "购物", "type": 1, "children": [
                {"id": 21, "parentId": 2, "name": "日用品", "type": 1, "children": []},
                {"id": 22, "parentId": 2, "name": "服饰", "type": 1, "children": []},
            ]},
            {"id": 3, "parentId": 0, "name": "住房", "type": 1, "children": [
                {"id": 31, "parentId": 3, "name": "水电燃气", "type": 1, "children": []},
                {"id": 32, "parentId": 3, "name": "房租/房贷", "type": 1, "children": []},
            ]},
            {"id": 4, "parentId": 0, "name": "工资", "type": 2, "children": [
                {"id": 41, "parentId": 4, "name": "基本工资", "type": 2, "children": []},
            ]},
            {"id": 5, "parentId": 0, "name": "其他支出", "type": 1, "children": []},
            {"id": 6, "parentId": 0, "name": "其他收入", "type": 2, "children": []},
            {"id": 7, "parentId": 0, "name": "交通", "type": 1, "children": [
                {"id": 71, "parentId": 7, "name": "公交地铁", "type": 1, "children": []},
                {"id": 72, "parentId": 7, "name": "打车", "type": 1, "children": []},
            ]},
            {"id": 8, "parentId": 0, "name": "通讯", "type": 1, "children": []},
            {"id": 9, "parentId": 0, "name": "订阅服务", "type": 1, "children": []},
            {"id": 10, "parentId": 0, "name": "人情往来", "type": 1, "children": []},
            {"id": 15, "parentId": 0, "name": "医疗", "type": 1, "children": [
                {"id": 151, "parentId": 15, "name": "药品", "type": 1, "children": []},
                {"id": 152, "parentId": 15, "name": "门诊", "type": 1, "children": []},
            ]},
            {"id": 16, "parentId": 0, "name": "教育", "type": 1, "children": [
                {"id": 161, "parentId": 16, "name": "课程", "type": 1, "children": []},
            ]},
            {"id": 17, "parentId": 0, "name": "娱乐", "type": 1, "children": [
                {"id": 171, "parentId": 17, "name": "旅行", "type": 1, "children": []},
            ]},
            {"id": 18, "parentId": 0, "name": "金融", "type": 1, "children": [
                {"id": 181, "parentId": 18, "name": "利息", "type": 1, "children": []},
            ]},
            {"id": 19, "parentId": 0, "name": "投资", "type": 2, "children": [
                {"id": 191, "parentId": 19, "name": "利息", "type": 2, "children": []},
            ]},
            {"id": 40, "parentId": 0, "name": "固定支出", "type": 1, "children": [
                {"id": 401, "parentId": 40, "name": "房租", "type": 1, "children": []},
                {"id": 402, "parentId": 40, "name": "房贷", "type": 1, "children": []},
                {"id": 403, "parentId": 40, "name": "保险", "type": 1, "children": []},
            ]},
            {"id": 42, "parentId": 0, "name": "专项支出", "type": 1, "children": [
                {"id": 421, "parentId": 42, "name": "旅行", "type": 1, "children": []},
                {"id": 422, "parentId": 42, "name": "学费", "type": 1, "children": []},
                {"id": 423, "parentId": 42, "name": "大额购物", "type": 1, "children": []},
            ]},
        ]


class FakeLLM:
    def classify(self, text, note, tx_type):
        return 5 if "未知商户" in text else None


class MappingTests(unittest.TestCase):
    def test_flatten_tree_includes_children(self):
        flat = flatten_category_tree(FakeAPI().get_categories())
        self.assertEqual(flat[31]["name"], "水电燃气")
        self.assertEqual(flat[31]["parent_id"], 3)
        self.assertEqual(flat[31]["path"], "住房/水电燃气")
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


class ChildCategoryTests(unittest.TestCase):
    def setUp(self):
        self.config = {"category_mapping": {}}
        changed, self.types, self.categories = build_initial_mapping(FakeAPI(), self.config)
        self.mapping = self.config["category_mapping"]

    def mapper(self, merchant_map=None):
        return CategoryMapper(self.mapping, self.types,
                              merchant_map=merchant_map,
                              categories=self.categories)

    def test_rules_resolve_child_paths(self):
        m = self.mapper()
        self.assertEqual(m.classify("支付宝-北京三快在线科技有限公司", 1), 11)  # 餐饮/外卖
        self.assertEqual(m.classify("支付宝-星巴克", 1), 12)                  # 餐饮/饮品
        self.assertEqual(m.classify("支付宝-盒马", 1), 13)                    # 餐饮/食材
        self.assertEqual(m.classify("支付宝-中国铁路网络有限公司", 1), 421)  # 专项支出/旅行
        self.assertEqual(m.classify("支付宝-贝壳租房", 1), 401)              # 固定支出/房租
        self.assertEqual(m.classify("支付宝-北京大学", 1), 161)               # 教育/课程
        self.assertEqual(m.classify("支付宝-北京大学口腔医院", 1), 152)      # 医疗/门诊
        self.assertEqual(m.classify("支付宝-携程", 1), 421)                  # 专项支出/旅行
        self.assertEqual(m.classify("入账工资", 2), 41)                       # 工资/基本工资

    def test_learned_child_wins_over_rules(self):
        # 用户把盒马改成“购物/日用品”，应尊重用户选择
        m = self.mapper(merchant_map={"支付宝-盒马": 21})
        self.assertEqual(m.classify("支付宝-盒马", 1), 21)

    def test_rule_child_beats_learned_top(self):
        # 旧商户表里的“餐饮”一级映射不应阻止细分到“餐饮/外卖”
        m = self.mapper(merchant_map={"支付宝-北京三快在线科技有限公司": 1})
        self.assertEqual(m.classify("支付宝-北京三快在线科技有限公司", 1), 11)

    def test_ctrip_is_special_travel(self):
        # 旅行改为专项预算，携程/飞猪不再按普通交通处理
        m = self.mapper()
        self.assertEqual(m.classify("支付宝-携程旅行网", 1), 421)


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
            "支付宝-北京三快在线科技有限公司": "餐饮/外卖",
            "支付宝-上海拉扎斯信息科技有限公司": "餐饮/外卖",
            "支付宝-中国铁路网络有限公司": "专项支出/旅行",
            "支付宝-贝壳租房": "固定支出/房租",
            "中美联泰大都会人寿保险有限": "固定支出/保险",
            "支付宝-北京大学软件与微电子学院": "专项支出/学费",
            "京东支付-周生生京东自营旗舰店": "专项支出/大额购物",
            "支付宝-北京首都国际机场股份有限公司": "专项支出/旅行",
            "支付宝-上海风采航运旅游有限公司": "专项支出/旅行",
            "支付宝-上海盒马网络科技有限公司": "餐饮/食材",
            "支付宝-阿里云计算有限公司": "订阅服务/AI订阅",
            "支付宝-中广电移动网络有限公司": "通讯",
            "财付通-微信支付-微信红包快捷支付": "人情往来",
            "支付宝-上海海王星辰药房": "医疗/药品",
            "支付宝-北京大学": "教育/课程",
            "支付宝-北京大学口腔医院": "医疗/门诊",
            "京东支付-爱步（ECCO）运动京东自营": "购物/服饰",
        }
        for merchant, expected in cases.items():
            self.assertEqual(self.rules.match(merchant), expected, merchant)

    def test_unknown_merchant_returns_none(self):
        self.assertIsNone(self.rules.match("支付宝-某某不明商户"))


class LearnedMappingTests(unittest.TestCase):
    def test_build_merchant_map_from_budgetpilot(self):
        merchants = [
            {"name": "美团商户", "alias": "美团,外卖", "aliases": ["支付宝-北京三快在线科技有限公司"],
             "categoryId": 1},
            {"name": "交通商户", "alias": None, "categoryId": 7},
            {"name": "未知商户", "alias": None, "categoryId": 5},  # 其他支出 -> ignore
        ]
        mapping = build_merchant_map(merchants, {1: 1, 7: 1, 5: 1}, excluded={5})
        self.assertEqual(mapping["美团商户"], 1)
        self.assertEqual(mapping["外卖"], 1)
        self.assertEqual(mapping["支付宝-北京三快在线科技有限公司"], 1)
        self.assertEqual(mapping["交通商户"], 7)
        self.assertNotIn("未知商户", mapping)

    def test_llm_fallback_is_last(self):
        mapper = CategoryMapper(
            {"餐饮": 1, "其他支出": 5}, {1: 1, 5: 1},
            llm=FakeLLM(),
        )
        self.assertEqual(mapper.classify("未知商户", tx_type=1), 5)
        self.assertIsNone(mapper.classify("未知商户", tx_type=1, allow_llm=False))


if __name__ == "__main__":
    unittest.main()