# 新疆电力燃气 (xjelegas) Home Assistant 集成

将新疆电力与新疆燃气账号数据接入 [Home Assistant](https://www.home-assistant.io/)，提供用量、余额、账单等传感器，并附带 Lovelace 卡片。

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/sfairy/ha_xjelegas.svg)](https://github.com/sfairy/ha_xjelegas/releases)
[![Validate](https://github.com/sfairy/ha_xjelegas/actions/workflows/validate.yml/badge.svg)](https://github.com/sfairy/ha_xjelegas/actions/workflows/validate.yml)

## 功能

- 支持公用事业类型：电力（`ele`）、燃气（`gas`）
- UI 配置流程（Config Flow）与选项更新
- 阶梯计价 / 固定单价计费配置
- Lovelace 自定义卡片（`xjelegas-card.js`）
- 电力登录支持 LLM 验证码识别与备用邮箱限流回退

## 安装

### HACS（推荐）

1. 打开 HACS → Integrations → 右上角菜单 → Custom repositories
2. Repository 填写：`https://github.com/sfairy/ha_xjelegas`
3. Category 选择：`Integration`
4. 添加后搜索 **新疆电力燃气** 并安装
5. 重启 Home Assistant

也可使用 My Home Assistant 快捷链接：

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=sfairy&repository=ha_xjelegas&category=integration)

### 手动安装

1. 将本仓库中的 `custom_components/xjelegas` 目录复制到 Home Assistant 配置目录下的 `custom_components/`
2. 重启 Home Assistant

## 配置

1. 进入 **设置 → 设备与服务 → 添加集成**
2. 搜索 **新疆电力燃气**
3. 选择类型（电力 / 燃气）并填写账号信息
4. 电力账号如需自动识别验证码，请配置 LLM API Key（及可选 Base URL / Model）
5. 可在集成选项中配置计费模式、刷新间隔与调试开关

## 要求

- Home Assistant ≥ 2024.1.0
- 有效的新疆电力或新疆燃气账号
- 电力验证码识别依赖 `openai` 兼容接口（见 `manifest.json` requirements）

## 仓库结构

```text
ha_xjelegas/
├── custom_components/xjelegas/
│   ├── brand/
│   ├── ele/
│   ├── gas/
│   ├── translations/
│   ├── www/
│   ├── manifest.json
│   └── ...
├── hacs.json
└── README.md
```

## 问题反馈

请在 [Issues](https://github.com/sfairy/ha_xjelegas/issues) 中提交问题或建议。

## License

MIT
