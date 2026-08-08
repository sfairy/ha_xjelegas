"""
新疆电力 大模型验证码解算器

使用火山引擎豆包大模型识别验证码：
1. 滑块验证码：识别缺口位置（LLM 优先，失败可回退像素算法）
2. 点击验证码：识别图标位置（base64 数据，f07/f06 双端点）

与 state_grid 集成的 click_captcha_solver 对齐。
"""

import base64
import io
import json
import logging
import os
import re
from typing import List, Optional, Tuple

from PIL import Image

logger = logging.getLogger(__name__)

# 延迟导入 openai，避免在 HA 启动时报错
_OPENAI_CLIENT = None
_LLM_CONFIG: dict = {}


def configure_llm(api_key: str, base_url: str, model: str) -> None:
    """配置 LLM 参数，在 config_flow 或客户端初始化时调用。"""
    global _LLM_CONFIG, _OPENAI_CLIENT
    _LLM_CONFIG = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }
    _OPENAI_CLIENT = None


def get_llm_client():
    """获取 OpenAI 客户端（懒加载）。"""
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        from openai import OpenAI
        _OPENAI_CLIENT = OpenAI(
            base_url=_LLM_CONFIG.get("base_url", "https://ark.cn-beijing.volces.com/api/v3"),
            api_key=_LLM_CONFIG.get("api_key", ""),
        )
    return _OPENAI_CLIENT


def get_llm_model() -> str:
    """获取当前配置的模型名称。"""
    return _LLM_CONFIG.get("model", "doubao-seed-2-0-pro-260215")


def base64_to_bytes(base64_data: str) -> bytes:
    """将 base64 图片数据转为 bytes。"""
    if base64_data.startswith("data:image"):
        base64_data = base64_data.split(",", 1)[1]
    return base64.b64decode(base64_data)


def base64_to_image(base64_data: str) -> Image.Image:
    """将 base64 图片数据转为 PIL Image。"""
    return Image.open(io.BytesIO(base64_to_bytes(base64_data)))


def image_to_data_uri(img: Image.Image, fmt: str = "PNG") -> str:
    """将 PIL Image 转为 data URI。"""
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/{fmt.lower()};base64,{b64}"


def detect_captcha_type(captcha_data: dict) -> str:
    """
    根据 API 返回的验证码数据检测验证码类型。

    滑块: canvasSrc + blockSrc + blockY
    点选: iconSrc/wordSrc + canvasSrc
    """
    if "iconSrc" in captcha_data or "wordSrc" in captcha_data or "iconSrcs" in captcha_data:
        return "click"
    if "blockSrc" in captcha_data:
        return "slider"
    if "canvasSrc" in captcha_data and "blockSrc" not in captcha_data:
        return "click"
    if captcha_data.get("src") or captcha_data.get("targetType"):
        return "click"
    return "slider"


def solve_click_captcha(
    ref_base64: str,
    main_base64: str,
    main_width: int,
    main_height: int,
) -> List[Tuple[int, int]]:
    """解算点选验证码，返回坐标列表。"""
    try:
        ref_img = base64_to_image(ref_base64)
        icon_uris = _split_strip(ref_img)
        if len(icon_uris) < 3:
            logger.error("参考图标条拆分失败，仅得到 %s 个图标", len(icon_uris))
            return []

        main_img = base64_to_image(main_base64)
        main_uri = image_to_data_uri(main_img)
        coords = _find_all_icons(icon_uris, main_uri, main_width, main_height)
        if len(coords) < 2:
            logger.warning("LLM 仅返回 %s 个坐标点", len(coords))
            return []

        return [
            (max(0, min(x, main_width - 1)), max(0, min(y, main_height - 1)))
            for x, y in coords
        ]
    except Exception as exc:
        logger.error("点选验证码解算失败: %s", exc)
        return []


def solve_slider_captcha_llm(
    canvas_base64: str,
    canvas_width: int = 310,
    canvas_height: int = 200,
) -> int:
    """使用 LLM 解算滑块验证码，返回滑块距离（像素）。"""
    try:
        canvas_img = base64_to_image(canvas_base64)
        bg_w, bg_h = canvas_img.size
        canvas_uri = image_to_data_uri(canvas_img)

        client = get_llm_client()
        response = client.chat.completions.create(
            model=get_llm_model(),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": canvas_uri}},
                    {"type": "text", "text": (
                        f"这是一个滑块拼图验证码的背景图（{bg_w}x{bg_h}像素）。\n"
                        "图中有一个矩形缺口（拼图块被挖掉的位置），缺口边缘有轻微阴影或颜色差异。\n"
                        "请找到这个缺口，返回缺口左侧边缘的X坐标比例（0~1之间）。\n"
                        "输出格式（仅一个数字）：0.XX"
                    )},
                ],
            }],
            max_tokens=50,
        )

        output = response.choices[0].message.content or ""
        nums = re.findall(r'(\d+\.?\d*)', output)
        if not nums:
            logger.warning("无法从 LLM 响应中解析滑块位置")
            return 0
        ratio = float(nums[0])
        if ratio > 1.5:
            ratio = ratio / bg_w
        ratio = max(0.0, min(1.0, ratio))
        distance = int(ratio * canvas_width)
        logger.info("滑块缺口比例: %.3f, 距离: %spx", ratio, distance)
        return distance
    except Exception as exc:
        logger.error("滑块验证码 LLM 解算失败: %s", exc)
        return 0


def _split_strip(strip_img: Image.Image) -> List[str]:
    """将参考图标条三等分为独立图标的 data URI 列表。"""
    w, h = strip_img.size
    part_w = w // 3
    uris = []
    for i in range(3):
        left = i * part_w
        right = (i + 1) * part_w if i < 2 else w
        icon = strip_img.crop((left, 0, right, h))
        icon = icon.resize((icon.width * 3, icon.height * 3), Image.LANCZOS)
        uris.append(image_to_data_uri(icon))
    return uris


def _find_all_icons(
    icon_uris: List[str],
    main_uri: str,
    main_width: int,
    main_height: int,
) -> List[Tuple[int, int]]:
    """单次 LLM API 调用，找到所有图标位置。"""
    prompt = (
        f"大图（{main_width}×{main_height}像素）是一个图标网格。\n"
        "找到3个参考图标(A, B, C)各自在大图网格中的位置。\n"
        "匹配规则：形状和颜色必须一致，空心/实心、线条粗细是关键区分点，允许旋转。\n\n"
        '输出JSON：{"coords":[[xA,yA],[xB,yB],[xC,yC]]}\n'
        "其中x、y为图标中心的比例坐标（0~1）。"
    )

    content = []
    labels = ["A", "B", "C"]
    for i, uri in enumerate(icon_uris[:3]):
        content.append({"type": "image_url", "image_url": {"url": uri}})
        content.append({"type": "text", "text": f"参考图标{labels[i]}"})
    content.append({"type": "image_url", "image_url": {"url": main_uri}})
    content.append({"type": "text", "text": prompt})

    client = get_llm_client()
    response = client.chat.completions.create(
        model=get_llm_model(),
        messages=[
            {"role": "system", "content": "Output valid JSON only. No markdown, no explanation."},
            {"role": "user", "content": content},
        ],
        max_tokens=4096,
        response_format={"type": "json_object"},
    )
    output = response.choices[0].message.content or ""
    return _parse_click_coordinates(output, main_width, main_height)


def _parse_click_coordinates(
    text: str, main_width: int, main_height: int
) -> List[Tuple[int, int]]:
    """从 LLM 返回文本中提取 JSON 坐标并转为像素。"""
    match = re.search(r'\{.*"coords"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            result = []
            for x, y in data["coords"]:
                x, y = float(x), float(y)
                if max(x, y) <= 1.5:
                    result.append((round(x * main_width), round(y * main_height)))
                else:
                    result.append((round(x), round(y)))
            return result
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    coords = []
    paren_pairs = re.findall(r'\(\s*(\d+\.?\d*)\s*[,，]\s*(\d+\.?\d*)\s*\)', text)
    for x_str, y_str in paren_pairs:
        coords.append((float(x_str), float(y_str)))
    if not coords:
        nums = re.findall(r'(\d+\.?\d+)', text)
        for i in range(0, len(nums) - 1, 2):
            coords.append((float(nums[i]), float(nums[i + 1])))

    result = []
    for x, y in coords[:3]:
        if max(x, y) <= 1.5:
            result.append((round(x * main_width), round(y * main_height)))
        else:
            result.append((round(x), round(y)))
    return result


class CaptchaSolver:
    """基于大模型的验证码解算器（兼容旧接口）。"""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = (api_key or os.getenv('ARK_API_KEY', '')).strip()
        self.model = model or "doubao-seed-2-0-pro-260215"
        self.base_url = base_url or "https://ark.cn-beijing.volces.com/api/v3"
        if self.api_key:
            configure_llm(self.api_key, self.base_url, self.model)

    def solve_slider(self, bg_url: str) -> float:
        """识别滑块缺口比例（0~1），兼容 URL/data URI/base64。"""
        try:
            bg_data = self._download(bg_url)
            if not bg_data:
                return 0.0
            canvas_b64 = base64.b64encode(bg_data).decode()
            with Image.open(io.BytesIO(bg_data)) as img:
                bg_w, _ = img.size
            distance = solve_slider_captcha_llm(canvas_b64, canvas_width=bg_w)
            return distance / bg_w if bg_w > 0 else 0.0
        except Exception as exc:
            logger.error("滑块解算错误: %s", exc)
            return 0.0

    def solve_click(self, ref_url: str, main_url: str,
                    main_width: int, main_height: int) -> List[Tuple[int, int]]:
        """识别点击验证码坐标。"""
        try:
            ref_raw = self._download(ref_url) if ref_url else None
            main_raw = self._download(main_url)
            if not main_raw:
                return []
            main_b64 = base64.b64encode(main_raw).decode()
            ref_b64 = base64.b64encode(ref_raw).decode() if ref_raw else ""
            if ref_b64:
                return solve_click_captcha(ref_b64, main_b64, main_width, main_height)
            return []
        except Exception as exc:
            logger.error("点击验证码解算错误: %s", exc)
            return []

    def _download(self, url: str) -> Optional[bytes]:
        """下载图片，支持 http、data URI 和纯 base64。"""
        try:
            if url.startswith("data:"):
                _, encoded = url.split(",", 1)
                return base64.b64decode(encoded)
            if url.startswith("http"):
                import requests
                resp = requests.get(url, timeout=15)
                if resp.status_code == 200:
                    return resp.content
                return None
            return base64.b64decode(url)
        except Exception as exc:
            logger.error("下载错误: %s", exc)
            return None
