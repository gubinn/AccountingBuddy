"""Category mapping: keyword -> category ID."""

import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_KEYWORDS = {
    "餐饮": ["餐饮", "餐厅", "饭", "外卖", "美团", "饿了么", "肯德基", "麦当劳", "星巴克", "咖啡", "奶茶", "小吃", "面", "粉", "粥", "饺子", "火锅", "烧烤", "自助", "必胜客", "汉堡", "披萨"],
    "交通": ["地铁", "公交", "滴滴", "打车", "高德", "出租车", "停车", "加油", "火车", "高铁", "飞机", "携程", "飞猪", "共享单车"],
    "购物": ["超市", "淘宝", "京东", "拼多多", "天猫", "唯品会", "购物", "买", "百货"],
    "娱乐": ["电影", "游戏", "腾讯", "爱奇艺", "优酷", "B站", "哔哩哔哩", "网易云", "QQ音乐"],
    "医疗": ["医院", "药店", "诊所", "体检", "药", "挂号"],
    "教育": ["图书", "书", "教育", "课程", "培训", "学费"],
    "通讯": ["话费", "联通", "移动", "电信", "流量"],
    "水电": ["水电", "物业", "燃气", "供暖"],
    "工资": ["工资", "薪资", "薪酬", "收入"],
    "转账": ["转账", "转帐", "汇款"],
}


def _flatten_categories(tree: list[dict]) -> dict[str, int]:
    result: dict[str, int] = {}
    def walk(nodes: list[dict]):
        for node in nodes:
            if node.get("children"):
                walk(node["children"])
            else:
                result[node["name"]] = node["id"]
    walk(tree)
    return result


def build_initial_mapping(api_client: Any, config: dict) -> None:
    existing = config.get("category_mapping", {})
    if existing:
        return
    tree = api_client.get_categories()
    name_to_id = _flatten_categories(tree)
    mapping = {}
    for group_name, keywords in DEFAULT_KEYWORDS.items():
        for kw in keywords:
            for name, cid in name_to_id.items():
                if kw in name or name in kw:
                    mapping[group_name] = cid
                    break
            if group_name in mapping:
                break
    config["category_mapping"] = mapping


class CategoryMapper:
    def __init__(self, mapping: dict[str, int]):
        self.mapping = mapping
        self.keyword_to_id: dict[str, int] = {}
        for category_name, cid in mapping.items():
            if category_name in DEFAULT_KEYWORDS:
                for kw in DEFAULT_KEYWORDS[category_name]:
                    self.keyword_to_id[kw] = cid

    def classify(self, text: str) -> int | None:
        for category_name, cid in self.mapping.items():
            if category_name in text:
                return cid
        for kw, cid in self.keyword_to_id.items():
            if kw in text:
                return cid
        return None
