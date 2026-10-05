"""新疆电力 (ele) 子包。

数据取数与解析对齐 state_grid：只从「推送缓存」取数，由 App 通道
(app_api.py + app_supply.py) 每小时灌入；不再走网页加密请求与验证码登录。
"""
