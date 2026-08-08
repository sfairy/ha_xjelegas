"""
公共工具函数模块。

提供跨模块共用的数据提取、日期标准化、数值提取等函数，
消除 debug_info.py / gas/sensor.py / ele/sensor.py 中的重复代码。
"""

from typing import Any, Dict, List, Optional


def safe_extract_rows(source_data: Any, keys: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    从任意嵌套结构的 API 响应中安全提取字典列表。

    兼容的嵌套路径（按优先级）：
    - 直接 list
    - dict["rows"]
    - dict["data"]["rows"] / dict["data"]["result"]
    - dict["obj"]["rows"] / dict["obj"]["result"]
    - dict["data"] (当 data 是 list)
    - dict["obj"] (当 obj 是 list)

    Args:
        source_data: API 返回的原始数据，可能是 list / dict / None
        keys: 自定义优先尝试的键名列表，默认 ["rows", "data", "obj"]

    Returns:
        List[Dict[str, Any]]: 提取到的字典列表，失败或类型不匹配返回空列表
    """
    if not source_data:
        return []
    if keys is None:
        keys = ["rows", "data", "obj"]

    if isinstance(source_data, list):
        return [item for item in source_data if isinstance(item, dict)]

    if not isinstance(source_data, dict):
        return []

    for key in keys:
        value = source_data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for nested_key in ("rows", "result", "data"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]

    return []


def extract_balance(source_data: Any) -> float:
    """从余额接口响应中提取余额字段。兼容 obj 包裹结构与多种字段命名。"""
    if not source_data:
        return 0.0
    target_data = source_data
    if isinstance(source_data, dict) and isinstance(source_data.get("obj"), dict):
        target_data = source_data["obj"]
    if isinstance(target_data, dict):
        for key in ("balance", "money", "surplus", "canUse", "accBalance", "actualBalance"):
            val = target_data.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
    return 0.0


def extract_arrearage(source_data: Any) -> float:
    """从余额接口响应中提取欠费字段。兼容 obj 包裹结构与多种字段命名。"""
    if not source_data:
        return 0.0
    target_data = source_data
    if isinstance(source_data, dict) and isinstance(source_data.get("obj"), dict):
        target_data = source_data["obj"]
    if isinstance(target_data, dict):
        for key in ("arrearage", "amt", "oweFee", "debt", "oweAmt"):
            val = target_data.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
    return 0.0


def balance_state_from_arrearage(source_data: Any) -> float:
    """计算账户余额状态值：欠费>0 返回负数，否则返回余额。与 gas/sensor.py 逻辑一致。"""
    arrearage = extract_arrearage(source_data)
    if arrearage > 0:
        return -arrearage
    return extract_balance(source_data)


def normalize_day(value: Any) -> str:
    """将日期统一为 YYYY-MM-DD 格式。支持 20240307、2024-03-07、2024/3/7 等。"""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(int(value))
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if " " in value:
        value = value.split(" ", 1)[0]
    if "T" in value:
        value = value.split("T", 1)[0]
    value = value.replace("/", "-").replace(".", "-")
    digits = value.replace("-", "")
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    parts = [p for p in value.split("-") if p]
    if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
        year = parts[0].zfill(4)
        month = parts[1].zfill(2)
        day = parts[2].zfill(2)
        if len(year) == 4:
            return f"{year}-{month}-{day}"
    return value


def normalize_month(value: Any) -> str:
    """将月份统一为 YYYY-MM 格式。支持 202403、2024-03 等。"""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(int(value))
    if not isinstance(value, str):
        return ""
    value = value.strip().replace("/", "-").replace(".", "-")
    digits = value.replace("-", "")
    if len(digits) == 6 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}"
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}"
    return value


def extract_number(row: Dict[str, Any], keys: List[str]) -> float:
    """按候选字段依次取值并转换为浮点数。任一键解析成功即返回，全部失败返回 0.0。"""
    for key in keys:
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (ValueError, TypeError):
                continue
    return 0.0
