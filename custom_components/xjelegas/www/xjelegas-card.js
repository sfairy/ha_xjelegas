/**
 * xjelegas-card.js - 电力燃气信息卡片
 *
 * Home Assistant 自定义 Lovelace 卡片，用于展示电力/燃气使用量、缴费信息、
 * 阶梯计费、图表统计等。支持多用户、主题切换、设备运行统计等功能。
 *
 * 主要组件：
 * - EleGasCard: 卡片主体，负责数据展示和交互
 * - EleGasCardEditor: 可视化配置编辑器
 *
 * 模块分区：
 * - SECTION 1: 常量与配置
 * - SECTION 2: 工具函数（含 debounce/throttle）
 * - SECTION 3: HTML 模板
 * - SECTION 4: EleGasCard 主卡片类
 * - SECTION 5: EleGasCardEditor 编辑器类
 */

// ==================== SECTION 1: 常量与配置 ====================

// 图表用量颜色（电力/燃气用量柱状图、饼图主色）
const USAGE_CHART_COLOR = "#FF9800";

// 总计饼图年度配色（每年一色，形成闭环）
const YEAR_TOTAL_CHART_COLORS = [
  "#FF9800",
  "#FF5722",
  "#4CAF50",
  "#2196F3",
  "#9C27B0",
  "#E91E63",
  "#00BCD4",
  "#795548",
  "#607D8B",
  "#FFEB3B",
];

// 视图切换相关常量
const LOADING_HTML =
  '<div style="display: flex; align-items: center; justify-content: center; height: 100%; color: var(--text-color);">加载历史数据中...</div>';
const DATA_CHECK_INTERVAL_MS = 100;
const DATA_LOAD_TIMEOUT_MS = 10000;
const CHART_RESIZE_DELAY_MS = 10;
const THEME_CHECK_INTERVAL_MS = 60 * 1000; // 主题时间切换检查间隔 1 分钟
const BALANCE_UPDATE_INTERVAL_MS = 30 * 1000; // 余额刷新间隔 30 秒

/** 图表 Y 轴数值格式化：大于 100 时显示为 x.xk */
const axisValueFormatter = (value) =>
  value > 100 ? (value / 1000).toFixed(1) + "k" : value;

/** 计算百分比并格式化为一位小数 */
const formatPercentage = (val, total) =>
  total ? ((val / total) * 100).toFixed(1) : "0";

/** 数字补零为两位字符串 */
const pad2 = (n) => String(n).padStart(2, "0");

/**
 * 将时间字符串格式化为 YYYY-MM-DD
 * @param {string} timeStr - 原始时间字符串或日期对象
 * @returns {string} 格式化后的日期字符串，空输入返回空字符串
 */
function formatDateYYYYMMDD(timeStr) {
  if (!timeStr) return "";
  const str = String(timeStr).trim();
  const match = str.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (match) return `${match[1]}-${pad2(match[2])}-${pad2(match[3])}`;
  try {
    const d = new Date(str);
    if (!isNaN(d.getTime())) {
      return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
    }
  } catch (_) {}
  return str;
}

// 相对日期映射（今天、昨天、前天、x天前）
const RELATIVE_DATE_MAP = [
  { diff: 0, text: "今天", class: "relative-date-today" },
  { diff: 1, text: "昨天", class: "relative-date-yesterday" },
  { diff: 2, text: "前天", class: "relative-date-day-before-yesterday" },
];

// 缴费来源与 CSS 类映射
const SOURCE_CLASS_MAP = [
  { keywords: ["支付宝", "alipay", "Alipay"], class: "alipay" },
  { keywords: ["微信", "WeChat", "wechat"], class: "wechat" },
  { keywords: ["网上国网交费", "国网", "sgcc"], class: "sgcc-online" },
  { keywords: ["电e宝", "e宝"], class: "eebao" },
  { keywords: ["社会网点", "网点"], class: "outlet" },
  { keywords: ["银行卡", "银行"], class: "bank" },
  { keywords: ["其他"], class: "other" },
];

// 缴费来源类名 -> 饼图颜色
const SOURCE_COLOR_MAP = {
  alipay: "#1677FF",
  wechat: "#07C160",
  "sgcc-online": "#14806E",
  eebao: "#E91E63",
  outlet: "#FA8C16",
  bank: "#9C27B0",
  other: "#8C8C8C",
  default: "#13C2C2",
};
/** 缴费来源未匹配时的饼图备用颜色 */
const SOURCE_FALLBACK_PALETTE = [
  "#00BCD4",
  "#FF5722",
  "#673AB7",
  "#795548",
  "#607D8B",
  "#4CAF50",
  "#F44336",
  "#FFEB3B",
];

// ==================== SECTION 2: 工具函数（含 debounce/throttle） ====================

/** 防抖：延迟执行，重复调用时重置计时。返回函数带 cancel/flush 方法 */
function debounce(fn, ms) {
  let timer = null;
  let lastArgs = null,
    lastThis = null;
  const debounced = function (...args) {
    lastArgs = args;
    lastThis = this;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn.apply(lastThis, lastArgs);
    }, ms);
  };
  debounced.cancel = () => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  };
  debounced.flush = () => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
      fn.apply(lastThis, lastArgs);
    }
  };
  return debounced;
}

/** 节流：在间隔内最多执行一次 */
function throttle(fn, ms) {
  let last = 0,
    timer = null;
  return function (...args) {
    const now = Date.now();
    const remaining = ms - (now - last);
    if (remaining <= 0) {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      last = now;
      fn.apply(this, args);
    } else if (!timer) {
      timer = setTimeout(() => {
        last = Date.now();
        timer = null;
        fn.apply(this, args);
      }, remaining);
    }
  };
}

// ==================== SECTION 3: HTML 模板 ====================
// 卡片 DOM 结构：header(用户信息/多用户切换) -> balance-section(余额/单价/预计用天)
// -> tier-indicator(阶梯) -> data-container(本月/上月/年统计) -> 视图切换区(日历/年/月/日)
// -> 日详情模态框、缴费模态框、设备事件 tooltip

const template = document.createElement("template");
template.innerHTML = `
  <style>
    /* 卡片主题色变量，便于统一调整 */
    .elegas-card {
      --balance-color: #ff5722;
      --price-color: #2196f3;
      --tier-1-color: rgb(85, 197, 147);
      --tier-2-color: rgb(248, 195, 55);
      --tier-3-color: rgb(247, 147, 53);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      background: var(--card-bg);
      border-radius: 16px;
      padding: 4px 8px;
      color: var(--card-name-color, #333);
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
      max-width: 500px;
      margin: 0 auto;
      transition: background 0.3s ease;
      position: relative;
      overflow: hidden;
    }

    .header {
      display: flex;
      flex-direction: column; /* 纵向布局，让元素上下排列 */
      justify-content: flex-start;
      align-items: stretch; /* 让子元素横向伸展 */
      margin: 0;
      padding: 4px 0;
      gap: 3px; /* 元素之间的间距 */
    }

    .user-info {
      font-size: 15px;
      font-weight: 600;
      color: var(--card-name-color, #333);
      text-align: center;
      width: 100%;
    }

    .user-info.hidden {
      display: none;
    }

    .multi-user-info {
      display: flex;
      position: relative;
      align-items: center;
      gap: 0;
      margin: 0 -8px; /* 抵消父容器的左右内边距，让丝带横向贯穿 */
      padding: 0;
      background: transparent; /* 去掉背景色 */
      border-radius: 0;
      width: calc(100% + 16px); /* 增加宽度来抵消左右内边距 */
      overflow-x: auto; /* 允许横向滚动 */
      justify-content: space-around;
      border-bottom: 1px solid var(--button-primary-transparent); /* 底部增加一条分隔线 */
      padding-bottom: 5px; /* 为分隔线增加一些间距 */
    }

    .multi-user-info.hidden {
      display: none; /* 隐藏时 */
    }

    /* 向上发光的横条指示器 */
    .slider-indicator {
      position: absolute;
      height: 3px; /* 横条高度 */
      bottom: 0; /* 位于底部 */
      left: 0;
      width: 80px; /* 默认宽度，会被 JS 覆盖 */
      background: linear-gradient(to top, var(--button-color-active), transparent);
      border-radius: 3px 3px 0 0;
      transition: left 0.3s cubic-bezier(0.4, 0, 0.2, 1),
                  width 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      z-index: 1;
      pointer-events: none;
      box-shadow: 0 -4px 12px var(--button-color-active); /* 向上发光效果 */
    }

    .user-block {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      background: transparent;
      padding: 0;
      min-width: 50px;
      cursor: pointer;
      transition: all 0.3s ease;
      z-index: 2;
      opacity: 0.5; /* 默认低亮度 */
      user-select: none;
      flex-shrink: 0;
      border-radius: 8px;
    }

    .user-block.active {
      opacity: 1; /* 高亮显示 */
    }

    .user-block.active .user-block-balance {
      color: var(--balance-value-color, var(--button-color-active)); /* 焦点卡片：与主卡片余额同色 */
      text-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
      font-weight: 800;
    }

    .user-block.active .user-block-name {
      color: var(--button-color-active);
      font-weight: 600;
    }

    .user-block:not(.active) .user-block-balance {
      color: var(--text-color);
      opacity: 0.65; /* 非焦点卡片：弱化显示 */
    }

    .user-block:hover {
      opacity: 0.8; /* hover 时稍微提高亮度 */
    }

    .user-block-balance {
      font-size: 14px;
      font-weight: 700;
      color: var(--card-value-color);
      margin-bottom: -6px;
      text-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
      transition: all 0.3s ease;
    }

    .user-block-name {
      font-size: 10px;
      color: var(--text-color);
      opacity: 0.85;
      font-weight: 500;
      transition: all 0.3s ease;
    }

    .user-block:not(.active) .user-block-name {
      opacity: 0.7; /* 非焦点卡片名称弱化 */
    }

    .balance-section {
      text-align: left;
      padding: 3px 0 0;
      background: var(--card-button-bg);
      border-radius: 12px;
      backdrop-filter: blur(5px);
    }

    .balance-label-container {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: -6px;
    }

    .balance-icon {
      width: 16px;
      height: 16px;
      opacity: 0.8;
      color: var(--svg-icon-color);
    }

    .balance-label {
      font-size: 14px;
      opacity: 0.8;
      margin-bottom: 0;
      order: 0;
      font-weight: 600;
    }

    .price-label-container {
      display: flex;
      align-items: center;
      gap: 4px;
      margin-right: 2px;
    }

    .price-icon {
      width: 14px;
      height: 14px;
      opacity: 0.8;
      color: var(--svg-icon-color);
    }

    .balance-price-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      grid-template-rows: 1fr 1fr;
      align-items: flex-start;
      margin-bottom: -6px;
      order: 2;
      gap: 5px;
    }
    
    .balance-item {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 0;
      min-width: 0;
      grid-row: span 2;
    }
    
    .balance-amount {
      left: 20px;
      font-size: 28px;
      font-weight: 700;
      color: var(--balance-color);
      display: flex;
      align-items: baseline;
      text-shadow: 0 2px 4px rgba(255, 87, 34, 0.2);
      letter-spacing: -0.5px;
      position: relative;
      overflow: hidden;
    }

    /* 45度角白色光带扫过动画 */
    .balance-amount::before {
      content: '';
      position: absolute;
      top: -50%;
      left: -50%;
      width: 200%;
      height: 200%;
      background: linear-gradient(
        45deg,
        transparent 30%,
        rgba(255, 255, 255, 0.4) 50%,
        transparent 70%
      );
      transform: rotate(45deg);
      animation: lightSweep 2s infinite;
    }

    @keyframes lightSweep {
      0% {
        transform: rotate(45deg) translateX(-100%);
      }
      100% {
        transform: rotate(45deg) translateX(100%);
      }
    }

    .balance-unit {
      font-size: 14px;
      opacity: 0.8;
      margin-left: 4px;
      color: var(--button-color);
    }

    .elegas-price-display,
    .remaining-days-display {
      display: flex;
      align-items: center;
      font-size: 16px;
      font-weight: 600;
      color: var(--price-color);
      background: var(--card-button-bg);
      padding: 3px 6px;
      border-radius: 8px;
      flex: 1;
      justify-content: flex-start;
      min-width: 0;
    }

    .price-label {
      font-size: 12px;
      opacity: 0.7;
      margin-right: 0;
      font-weight: normal;
    }

    .price-value {
      color: var(--price-color);
      font-weight: 700;
      text-shadow: 0 1px 2px rgba(33, 150, 243, 0.2);
    }

    .price-unit {
      font-size: 11px;
      opacity: 0.8;
      margin-left: 2px;
      color: var(--button-color);
      margin-top: 2px;
    }

    .date-info {
      font-size: 9px;
      opacity: 0.7;
      margin-top: -11px;
      margin-left: 20px;
      touch-action: manipulation;
      -webkit-touch-callout: none;
      -webkit-user-select: none;
      user-select: none;
    }

    .data-date-info {
      display: inline-block;
      font-size: 9px;
      opacity: 0.7;
      margin-top: 0;
      margin-left: 20px;
      touch-action: manipulation;
      -webkit-touch-callout: none;
      -webkit-user-select: none;
      user-select: none;
    }

    .relative-date-info {
      display: inline-block;
      font-size: 8px;
      padding: 1px 4px;
      border-radius: 3px;
      margin-left: 4px;
      font-weight: 600;
      color: white;
      vertical-align: middle;
    }

    .relative-date-today {
      background: #4CAF50;
    }

    .relative-date-yesterday {
      background: #FF9800;
    }

    .relative-date-day-before-yesterday {
      background: #2196F3;
    }

    .relative-date-other {
      background: #F44336;
    }

    /* 在现有的.tier-indicator样式中添加 */
    .tier-indicator {
      position: relative;
      padding: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      margin: 5px 0 0 0;
      justify-content: space-between;
    }

    /* 添加一个内层容器，用于限制阶梯图的实际宽度 */
    .tier-indicator-container {
      position: relative;
      width: 100%;
      max-width: 420px; 
      margin: 0 auto;
    }
    .tier-label {
      font-size: 14px;
      margin-bottom: 15px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      width: 100%;
    }

    .tier-label-left {
      display: flex;
      align-items: center;
      gap: 6px;
      font-weight: 600;
      max-width: 200px;
    }

    .tier-label-right {
      font-size: 10px;
      opacity: 0.8;
      text-align: right;
      margin-left: auto;
      flex-shrink: 0;
      width: auto;
      min-width: 120px;
    }

    .tiers-container {
      display: flex;
      align-items: flex-end;
      margin-bottom: 10px;
      position: relative;
      justify-content: center;
      gap: 2px;
      z-index: 2;
    }

    .tier {
      position: relative;
      flex: 1;
      min-height: 40px;
      display: flex;
      flex-direction: column;
      justify-content: flex-end;
      max-width: 140px;
      top: 13px;
      transition: top 0.3s ease;
    }

    .tier-block {
      position: relative;
      height: 15px;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 0 8px;
      border-radius: 4px;
      font-size: 10px;
      font-weight: 600;
      color: var(--text-color, #333);
      text-align: center;
      line-height: 1.2;
      z-index: 1;
      box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
      transition: all 0.3s ease;
      overflow: hidden;
      white-space: nowrap; /* 防止文字换行 */
    }

    /* 第一阶梯样式 - 右侧尖箭头，左侧直角 */
    .tier-1 .tier-block {
      background-color: var(--tier-1-color);
      /* 路径：左上 -> 右上折点 -> 右尖端 -> 右下折点 -> 左下 */
      clip-path: polygon(0% 0%, calc(100% - 12px) 0%, 100% 50%, calc(100% - 12px) 100%, 0% 100%);
      margin-right: -10px;
    }

    /* 第二阶梯样式 - 右侧尖箭头，左侧内凹箭头 */
    .tier-2 .tier-block {
      background-color: var(--tier-2-color);
      /* 路径：左上 -> 右上折点 -> 右尖端 -> 右下折点 -> 左下 -> 左侧内凹点 */
      clip-path: polygon(0% 0%, calc(100% - 12px) 0%, 100% 50%, calc(100% - 12px) 100%, 0% 100%, 12px 50%);
      margin: 0 -10px;
    }

    /* 第三阶梯样式 - 右侧直角，左侧内凹箭头 */
    .tier-3 .tier-block {
      background-color: var(--tier-3-color);
      /* 路径：左上 -> 右上 -> 右下 -> 左下 -> 左侧内凹点 */
      clip-path: polygon(0% 0%, 100% 0%, 100% 100%, 0% 100%, 12px 50%);
      margin-left: -10px;
    }

    /* 红色竖线指示器 - 放在tiers-container中，与current-indicator相同的定位基准 */
    .red-line-indicator {
      position: absolute;
      top: 13px;
      left: 0;
      width: 3px;
      height: 15px;
      background-color: #ff0000;
      z-index: 2;
      box-shadow: 0 0 3px rgba(255, 0, 0, 0.7);
      transform: translateX(-50%);
    }

    /* 水波纹涟漪动画效果 */
    .red-line-indicator::before,
    .red-line-indicator::after {
      content: '';
      position: absolute;
      top: 50%;
      left: 50%;
      width: 3px;
      height: 15px;
      background-color: #ff0000;
      border-radius: 2px;
      transform: translate(-50%, -50%);
      animation: ripple 2s infinite;
    }

    .red-line-indicator::after {
      animation-delay: 1s;
    }

    @keyframes ripple {
      0% {
        width: 3px;
        height: 15px;
        opacity: 1;
        box-shadow: 0 0 0 0 rgba(255, 0, 0, 0.7);
      }
      50% {
        opacity: 0.7;
      }
      100% {
        width: 25px;
        height: 35px;
        opacity: 0;
        box-shadow: 0 0 0 8px rgba(255, 0, 0, 0);
      }
    }

    .current-indicator-clone {
      position: absolute;
      top: -18px;
      transform: translateX(-50%);
      z-index: 2;
      pointer-events: none;
      visibility: visible !important;
    }

    .current-indicator-clone:after {
      content: '';
      position: absolute;
      bottom: -4px;
      left: 50%;
      transform: translateX(-50%);
      width: 0;
      height: 0;
      border-left: 4px solid transparent;
      border-right: 4px solid transparent;
      border-top: 4px solid;
    }

    .current-indicator-clone.tier-1:after {
      border-top-color: var(--tier-1-color);
    }

    .current-indicator-clone.tier-2:after {
      border-top-color: var(--tier-2-color);
    }

    .current-indicator-clone.tier-3:after {
      border-top-color: var(--tier-3-color);
    }

    .tier.current .tier-block {
      box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2);
      filter: brightness(1.05);
    }

    .tier-content {
      margin-top: 2px;
      text-align: center;
      font-size: 11px;
      color: var(--button-color);
      line-height: 1.3;
      padding: 6px 4px;
      border-radius: 4px;
      position: relative;
      overflow: hidden;
      z-index: 0;
      transition: all 0.3s ease;
    }

    .tier-content.hidden {
      display: none;
    }

    .tier-title {
      font-weight: 600;
      margin-bottom: 2px;
      color: var(--text-color, #444);
    }

    .tier-range {
      margin-bottom: 2px;
    }

    .tier-price {
      font-weight: 500;
    }

    .tier-1 .tier-price {
      color: var(--tier-1-color);
    }

    .tier-2 .tier-price {
      color: var(--tier-2-color);
    }

    .tier-3 .tier-price {
      color: var(--tier-3-color);
    }

    .current-indicator {
      position: absolute;
      top: -18px;
      left: 50%;
      transform: translateX(-50%);
      background-color: var(--balance-color);
      color: white;
      padding: 3px 5px;
      border-radius: 10px;
      font-size: 11px;
      font-weight: 600;
      white-space: nowrap;
      box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
      z-index: 2;
      display: flex;
      align-items: center;
      gap: 4px;
      pointer-events: none; /* 防止遮挡点击事件 */
      --arrow-offset: 0px;
      min-width: 0;
      /* 确保指示器不会超出容器边界 */
      max-width: calc(100% - 16px); /* 左右各保留8px边距 */
      overflow: visible;
    }

    /* 根据当前阶梯设置背景色 */
    .current-indicator.tier-1 {
      background-color: var(--tier-1-color);
    }

    .current-indicator.tier-2 {
      background-color: var(--tier-2-color);
    }

    .current-indicator.tier-3 {
      background-color: var(--tier-3-color);
    }

    .indicator-arrow {
      position: absolute;
      top: 7px;
      width: 0;
      height: 0;
      border-left: 4px solid transparent;
      border-right: 4px solid transparent;
      border-top: 4px solid;
      z-index: 2;
      transform: translateX(-50%);
    }

    .tier-1 .indicator-arrow {
      border-top-color: var(--tier-1-color);
    }

    .tier-2 .indicator-arrow {
      border-top-color: var(--tier-2-color);
    }

    .tier-3 .indicator-arrow {
      border-top-color: var(--tier-3-color);
    }

    .data-container {
      display: flex;
      flex-direction: row;
      gap: 6px; /* 进一步减少间距 */
      margin-bottom: 10px;
      margin-top: 20px;
      transition: margin-top 0.3s ease;
    }

    /* 当tier-content隐藏时，data-container向上移动 */
    .data-container.compact {
      margin-top: 0;
    }

    /* 统一的统计区域样式 */
    .current-month-stats,
    .month-stats,
    .year-stats {
      flex: 1;
      background: var(--card-button-bg);
      border-radius: 12px;
      padding: 3px;
      backdrop-filter: blur(5px);
    }

    .current-month-stats {
      min-width: 100px;
    }

    .month-stats,
    .year-stats {
      min-width: 100px;
    }

    /* 统一的标签样式 */
    .month-label,
    .year-label {
      font-size: 12px;
      opacity: 0.8;
      margin-bottom: 2px;
      display: flex;
      align-items: center;
      gap: 3px;
    }

    /* 统一的网格样式 */
    .month-grid,
    .year-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0;
      margin-bottom: 4px;
    }

    /* 统一的统计项样式 */
    .month-stat,
    .year-stat {
      text-align: center;
      display: flex;
      flex-direction: column;
      align-items: center;
    }

    /* 统一的数值样式 */
    .month-stat-value,
    .year-stat-value {
      font-size: 12px;
      font-weight: 600;
      color: var(--usage-color);
      display: flex;
      align-items: baseline;
    }

    /* 统一的单位样式 */
    .month-stat-unit,
    .year-stat-unit {
      font-size: 10px;
      opacity: 0.8;
      margin-left: 2px;
    }

    .year-stat-value.yellow {
      color: var(--money-color);
    }

    .year-stat-value.green {
      color: var(--usage-color);
    }

    .icon {
      width: 16px;
      height: 16px;
      opacity: 0.7;
      color: var(--svg-icon-color);
    }

    .tier-icon {
      width: 14px;
      height: 14px;
      color: var(--svg-icon-color);
    }

    /* 背景图标基础样式（用 mask 实现，图标颜色随 background-color 变化，兼容半透明主题） */
    .elegas-card::before {
      content: '';
      position: absolute;
      top: 150px;
      left: 39%;
      transform: translate(-50%, -50%);
      width: 150px;
      height: 150px;
      opacity: 0.08;
      background-color: var(--text-color, #333);
      background-repeat: no-repeat;
      background-position: center;
      background-size: contain;
      pointer-events: none;
      z-index: 1;
    }

    /* 当multiclass只有一个时，背景图标的top值改为110px */
    .elegas-card.single-class::before {
      top: 110px;
    }

    /* 电力背景图标 - 高压电塔（mask 方式，颜色由 background-color 控制） */
    .elegas-card.bg-ele::before {
      -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='black' d='M8.28,5.45L6.5,4.55L7.76,2H16.23L17.5,4.55L15.72,5.44L15,4H9L8.28,5.45M18.62,8H14.09L13.3,5H10.7L9.91,8H5.38L4.1,10.55L5.89,11.44L6.62,10H17.38L18.1,11.45L19.89,10.56L18.62,8M17.77,22H15.7L15.46,21.1L12,15.9L8.53,21.1L8.3,22H6.23L9.12,11H11.19L10.83,12.35L12,14.1L13.16,12.35L12.81,11H14.88L17.77,22M11.4,15L10.5,13.65L9.32,18.13L11.4,15M14.68,18.12L13.5,13.64L12.6,15L14.68,18.12Z'/%3E%3C/svg%3E");
      mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='black' d='M8.28,5.45L6.5,4.55L7.76,2H16.23L17.5,4.55L15.72,5.44L15,4H9L8.28,5.45M18.62,8H14.09L13.3,5H10.7L9.91,8H5.38L4.1,10.55L5.89,11.44L6.62,10H17.38L18.1,11.45L19.89,10.56L18.62,8M17.77,22H15.7L15.46,21.1L12,15.9L8.53,21.1L8.3,22H6.23L9.12,11H11.19L10.83,12.35L12,14.1L13.16,12.35L12.81,11H14.88L17.77,22M11.4,15L10.5,13.65L9.32,18.13L11.4,15M14.68,18.12L13.5,13.64L12.6,15L14.68,18.12Z'/%3E%3C/svg%3E");
      -webkit-mask-repeat: no-repeat;
      mask-repeat: no-repeat;
      -webkit-mask-position: center;
      mask-position: center;
      -webkit-mask-size: contain;
      mask-size: contain;
    }

    /* 燃气背景图标 - 火（mask 方式） */
    .elegas-card.bg-gas::before {
      -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='black' d='M17.66 11.2C17.43 10.9 17.15 10.64 16.89 10.38C16.22 9.78 15.46 9.35 14.82 8.72C13.33 7.26 13 4.85 13.95 3C13 3.23 12.17 3.75 11.46 4.32C8.87 6.4 7.85 10.07 9.07 13.22C9.11 13.32 9.15 13.42 9.15 13.55C9.15 13.77 9 13.97 8.8 14.05C8.57 14.15 8.33 14.09 8.14 13.93C8.08 13.88 8.04 13.83 8 13.76C6.87 12.33 6.69 10.28 7.45 8.64C5.78 10 4.87 12.3 5 14.47C5.06 14.97 5.12 15.47 5.29 15.97C5.43 16.57 5.7 17.17 6 17.7C7.08 19.43 8.95 20.67 10.96 20.92C13.1 21.19 15.39 20.8 17.03 19.32C18.86 17.66 19.5 15 18.56 12.72L18.43 12.46C18.22 12 17.66 11.2 17.66 11.2M14.5 17.5C14.22 17.74 13.76 18 13.4 18.1C12.28 18.5 11.16 17.94 10.5 17.28C11.69 17 12.4 16.12 12.61 15.23C12.78 14.43 12.46 13.77 12.33 13C12.21 12.26 12.23 11.63 12.5 10.94C12.69 11.32 12.89 11.7 13.13 12C13.9 13 15.11 13.44 15.37 14.8C15.41 14.94 15.43 15.08 15.43 15.23C15.46 16.05 15.1 16.95 14.5 17.5H14.5Z'/%3E%3C/svg%3E");
      mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='black' d='M17.66 11.2C17.43 10.9 17.15 10.64 16.89 10.38C16.22 9.78 15.46 9.35 14.82 8.72C13.33 7.26 13 4.85 13.95 3C13 3.23 12.17 3.75 11.46 4.32C8.87 6.4 7.85 10.07 9.07 13.22C9.11 13.32 9.15 13.42 9.15 13.55C9.15 13.77 9 13.97 8.8 14.05C8.57 14.15 8.33 14.09 8.14 13.93C8.08 13.88 8.04 13.83 8 13.76C6.87 12.33 6.69 10.28 7.45 8.64C5.78 10 4.87 12.3 5 14.47C5.06 14.97 5.12 15.47 5.29 15.97C5.43 16.57 5.7 17.17 6 17.7C7.08 19.43 8.95 20.67 10.96 20.92C13.1 21.19 15.39 20.8 17.03 19.32C18.86 17.66 19.5 15 18.56 12.72L18.43 12.46C18.22 12 17.66 11.2 17.66 11.2M14.5 17.5C14.22 17.74 13.76 18 13.4 18.1C12.28 18.5 11.16 17.94 10.5 17.28C11.69 17 12.4 16.12 12.61 15.23C12.78 14.43 12.46 13.77 12.33 13C12.21 12.26 12.23 11.63 12.5 10.94C12.69 11.32 12.89 11.7 13.13 12C13.9 13 15.11 13.44 15.37 14.8C15.41 14.94 15.43 15.08 15.43 15.23C15.46 16.05 15.1 16.95 14.5 17.5H14.5Z'/%3E%3C/svg%3E");
      -webkit-mask-repeat: no-repeat;
      mask-repeat: no-repeat;
      -webkit-mask-position: center;
      mask-position: center;
      -webkit-mask-size: contain;
      mask-size: contain;
    }

    /* 半透明主题：提高背景图标可见度 */
    .elegas-card.transparent-theme.bg-ele::before,
    .elegas-card.transparent-theme.bg-gas::before {
      opacity: 0.15;
      background-color: rgba(255, 255, 255, 0.5);
    }


    /* 更新文本颜色 */
    .user-info {
      color: var(--card-name-color);
    }

    .balance-label, .price-label, .date-info, .tier-label-left, .tier-label-right,
    .tier-range, .month-label, .year-label, .month-stat-unit, .year-stat-unit {
      color: var(--text-color);
    }

    .balance-amount {
      color: var(--balance-value-color, var(--card-value-color));
    }

    .elegas-price-display .price-value {
      color: var(--price-value-color, var(--card-value-color));
    }

    /* 电单价/气单价：元/°、元/m³ 与标签同色 */
    .elegas-price-display .price-unit {
      color: var(--text-color);
      opacity: 0.7;
    }

    .remaining-days-display .price-value {
      color: var(--remaining-days-value-color, var(--card-value-color));
    }

    /* 预计用：天、日期与标签同色 */
    .remaining-days-display .price-unit {
      color: var(--text-color);
      opacity: 0.7;
    }

    /* 国家电网主题：三个数据配色（红/绿/深绿） */
    .elegas-card.power-theme .balance-amount {
      color: #C41E1E;
      text-shadow: 0 2px 4px rgba(196, 30, 30, 0.2);
    }
    .elegas-card.power-theme .elegas-price-display .price-value {
      color: #008B45;
    }
    .elegas-card.power-theme .remaining-days-display .price-value {
      color: #006B47;
    }

    .month-stat-value.green {
      color: var(--usage-color);
    }

    .month-stat-value.yellow {
      color: var(--money-color);
    }

    .year-stat-value {
      color: var(--usage-color);
    }

    /* 更新统计区域背景 */
    .current-month-stats, .month-stats, .year-stats {
      background: var(--card-button-bg);
    }

    /* 更新阶梯内容背景色 */
    .tier-1 .tier-content {
      background: linear-gradient(to bottom, rgba(85, 197, 147, 0.2) 0%, rgba(85, 197, 147, 0.05) 100%);
    }

    .tier-2 .tier-content {
      background: linear-gradient(to bottom right, rgba(248, 195, 55, 0.2) 0%, rgba(248, 195, 55, 0.05) 100%);
    }

    .tier-3 .tier-content {
      background: linear-gradient(to bottom right, rgba(247, 147, 53, 0.2) 0%, rgba(247, 147, 53, 0.05) 100%);
    }

    /* ==================== 日历视图样式 ==================== */
    .calendar-view {
      width: 97%;
      padding: 8px;
      margin-top: 23px;
      min-height: 355px;
      animation: fadeIn 0.3s ease;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }

    /* 切换用户时的卡片过渡动画 */
    .elegas-card.switching {
      animation: cardSwitchOut 0.3s ease forwards;
    }

    .elegas-card.switching-in {
      animation: cardSwitchIn 0.4s ease forwards;
    }

    @keyframes cardSwitchOut {
      0% {
        opacity: 1;
        transform: scale(1) translateY(0);
      }
      100% {
        opacity: 0;
        transform: scale(0.98) translateY(-5px);
      }
    }

    @keyframes cardSwitchIn {
      0% {
        opacity: 0;
        transform: scale(0.98) translateY(5px);
      }
      100% {
        opacity: 1;
        transform: scale(1) translateY(0);
      }
    }

    .calendar-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 8px;
      font-size: 12px;
      padding: 0;
      flex-wrap: nowrap;
      gap: 8px;
    }

    .calendar-tabs {
      display: flex;
      gap: 5px;
      flex: 1;
    }

    .cal-tab-btn,
    .cal-control-btn {
      padding: 0 4px;
      border: 1px solid var(--button-color);
      border-radius: 4px;
      background: var(--card-button-bg);
      cursor: pointer;
      font-size: 12px;
      color: var(--text-color);
      white-space: nowrap;
      transition: all 0.2s;
      height: 25px;
    }

    .cal-tab-btn:hover,
    .cal-control-btn:hover {
      border-color: var(--button-color-active);
      color: var(--button-color-active);
      background: var(--button-primary-transparent);
    }

    .cal-tab-btn.active,
    .cal-control-btn.active {
      background: var(--button-color-active);
      color: var(--card-bg);
      border-color: var(--button-color-active);
      box-shadow: 0 0 0 1px var(--button-color-active);
    }

    .calendar-controls {
      display: flex;
      gap: 5px;
      align-items: center;
      white-space: nowrap;
      flex: 1;
      justify-content: flex-end;
      min-width: 0;
    }

    .calendar-back-control {
      display: flex;
      align-items: center;
    }

    .cal-control-select {
      background: var(--card-button-bg);
      color: var(--text-color);
      border: 1px solid var(--button-color);
      border-radius: 4px;
      padding: 0;
      cursor: pointer;
      height: 25px;
      min-width: 48px;
      font-size: 11px;
      text-align: center;
    }

    .cal-control-select:hover {
      border-color: var(--button-color-active);
    }

    .cal-control-select:focus {
      outline: none;
      border-color: var(--card-button-bg);
      background: var(--card-bg);
      color: var(--text-color);
      box-shadow: 0 0 0 1px var(--button-color-active);
    }

    .calendar-grid {
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 0;
      font-size: 0.7em;
      background: transparent;
      border: 2px solid var(--calendar-line-color);
      margin-bottom: 5px;
    }

    .day-of-week {
      text-align: center;
      font-weight: bold;
      padding: 4px;
      border-bottom: 2px solid var(--calendar-line-color);
      font-size: 11px;
      color: var(--text-color);
    }
    .day-of-week:not(:last-child) {
      border-right: 2px solid var(--calendar-line-color);
    }

    .calendar-day {
      position: relative;
      padding: 4px;
      background: rgba(0, 0, 0, 0);
      min-height: 30px;
      border-bottom: 2px solid var(--calendar-line-color);
      cursor: pointer;
      transition: all 0.2s;
    }

    .calendar-day:not(:nth-child(7n)) {
      border-right: 2px solid var(--calendar-line-color);
    }

    .calendar-day.has-date {
      background: var(--calendar-line-color);
    }

    .calendar-day.today {
      background: rgba(128, 164, 255, 0.3) !important;
    }

    .calendar-day.future-date {
      opacity: 0.4;
      cursor: not-allowed !important;
      background: rgba(128, 128, 128, 0.1) !important;
    }

    .calendar-day.future-date .date-circle {
      background: rgba(128, 128, 128, 0.3) !important;
      color: rgba(128, 128, 128, 0.6) !important;
    }

    /* 预计使用天数进度条 */
    .usage-progress-bar {
      position: absolute;
      bottom: 0;
      left: 0;
      width: 100%;
      height: 3px;
      background: #4CAF50;
      z-index: 5;
      pointer-events: none;
      box-shadow: 0 0 4px rgba(76, 175, 80, 0.5);
    }
    
    /* 当月用电量最大和最小的单元格背景色 - 最高优先级 */
    .calendar-day.has-date.max-usage-day {
      background: #f88988 !important;
    }
    
    .calendar-day.has-date.min-usage-day {
      background: #3eb370 !important;
    }
    
    /* 确保最大/最小用量样式优先于today样式 */
    .calendar-day.has-date.today.max-usage-day {
      background: #f88988 !important;
    }
    
    .calendar-day.has-date.today.min-usage-day {
      background: #3eb370 !important;
    }

    /* 国家电网主题：日历配色优化（国网绿/辅色红） */
    .elegas-card.power-theme .calendar-day.has-date {
      background: rgba(0, 139, 69, 0.12) !important;
    }
    .elegas-card.power-theme .calendar-day.has-date.today {
      background: rgba(0, 139, 69, 0.28) !important;
    }
    .elegas-card.power-theme .calendar-day.has-date.max-usage-day,
    .elegas-card.power-theme .calendar-day.has-date.today.max-usage-day {
      background: rgba(196, 30, 30, 0.2) !important;
    }
    .elegas-card.power-theme .calendar-day.has-date.min-usage-day,
    .elegas-card.power-theme .calendar-day.has-date.today.min-usage-day {
      background: rgba(0, 139, 69, 0.35) !important;
    }
    .elegas-card.power-theme .calendar-stats .kwh-value {
      background-color: #008B45;
      color: white;
    }
    .elegas-card.power-theme .calendar-stats .cost-value {
      background-color: #C41E1E;
      color: white;
    }
    .elegas-card.power-theme .calendar-day .data-value {
      background: #008B45;
      color: white;
    }
    .elegas-card.power-theme .calendar-day .calc-value {
      background: #C41E1E;
      color: white;
    }
    .elegas-card.power-theme .calendar-stats .stat-label {
      color: #1a3d2e;
    }

    .date-circle {
      background: var(--date-circle-bg, rgba(220, 53, 69, 1));
      color: var(--date-circle-color, white);
      border-radius: 50%;
      width: 16px;
      height: 16px;
      line-height: 18px;
      display: flex;
      align-items: center;
      justify-content: center;
      position: absolute;
      top: 2px;
      left: 2px;
      font-size: 10px;
    }

    .device-history-marker {
      position: absolute;
      top: 2px;
      right: 2px;
      font-size: 9px;
      font-weight: bold;
      color: var(--date-circle-color);
      background: var(--date-circle-bg);
      width: 12px;
      height: 12px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 2;
    }

    /* 统一的日历数据值样式 */
    .data-value,
    .calc-value {
      padding: 0px 2px;
      border-radius: 3px;
      margin-left: 10%;
      margin-right: 10%;
      font-size: 10px;
      width: 80%;
      text-align: center;
      display: inline-block;
    }

    .data-value {
      background: var(--usage-color);
      color: black;
      margin-top: 12px;
    }

    .calc-value {
      background: var(--money-color);
      color: white;
      margin-top: 1px;
    }

    .calendar-stats {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 11px;
      padding: 0;
      flex-wrap: nowrap;
    }

    .calendar-stat-item {
      display: flex;
      align-items: center;
      gap: 4px;
    }

    .stat-label {
      font-weight: bold;
      color: var(--secondary-text);
    }

    .stat-value {
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: bold;
    }

    .kwh-value {
      background-color: var(--usage-color);
      color: black;
    }

    .cost-value {
      background-color: var(--money-color);
      color: white;
    }

    /* 暗色主题下的日历样式 */

    /* ==================== 模态框样式 ==================== */
    .day-modal-overlay {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 1000;
      animation: fadeIn 0.2s ease;
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
    }

    .day-modal-content {
      position: relative;
      background: var(--card-bg);
      border-radius: 12px;
      padding: 5px 10px;
      max-width: 450px;
      width: 90%;
      max-height: 90vh;
      overflow-y: auto;
      box-shadow: 0 10px 40px rgba(0, 0, 0, 0.8);
      animation: scaleIn 0.3s ease;
      color: var(--text-color);
      /* 使用 transform 进行居中定位 */
      transform: scale(0.9);
      opacity: 0;
      transition: transform 0.3s ease, opacity 0.3s ease;
    }
    
    .day-modal-overlay[style*="display: flex"] .day-modal-content {
      transform: scale(1);
      opacity: 1;
    }

    @keyframes slideUp {
      from {
        opacity: 0;
        transform: translateY(20px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    .day-modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 5px;
      padding-bottom: 0px;
      border-bottom: 2px solid rgba(0, 0, 0, 0.1);
    }

    .day-modal-title {
      font-size: 14px;
      font-weight: 600;
      color: var(--text-color);
    }

    .day-modal-close {
      background: none;
      border: none;
      font-size: 28px;
      color: var(--text-color);
      cursor: pointer;
      line-height: 1;
      padding: 0;
      width: 30px;
      height: 30px;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 50%;
      transition: background 0.2s;
    }

    .day-modal-close:hover {
      background: rgba(0, 0, 0, 0.1);
    }

    /* 半透明主题：弹窗保持半透明毛玻璃效果，贴合主题特色 */
    .elegas-card.transparent-theme .day-modal-overlay {
      background: rgba(0, 0, 0, 0.35);
    }
    .elegas-card.transparent-theme .day-modal-content {
      background: rgba(35, 42, 55, 0.75);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
    }
    .elegas-card.transparent-theme .day-modal-header {
      border-bottom-color: rgba(255, 255, 255, 0.12);
    }
    .elegas-card.transparent-theme .day-modal-close:hover {
      background: rgba(255, 255, 255, 0.15);
    }

    /* 饼图区域样式 */
    .pie-chart-section {
      display: flex;
      flex-direction: column;
      background: var(--card-button-bg);
      border-radius: 8px;
      flex: 1;
      min-width: 0;
      height: 235px;
    }

    .pie-chart-title {
      font-size: 14px;
      font-weight: 600;
      color: var(--text-color);
      margin-left: 15px;
    }

    .pie-chart-content {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      flex: 1;
      min-height: 0;
    }

    .pie-chart-container {
      width: 100%;
      height: 205px;
      position: relative;
      top: -7px;
    }

    /* 缴费方式饼图在容器中完全居中，充满剩余空间 */
    .pay-history-pie-section {
      display: flex;
      flex-direction: column;
      align-items: stretch;
      justify-content: center;
      flex: 1;
      min-width: 0;
    }

    .pay-history-pie-section .pay-history-pie-title {
      text-align: center;
      margin: 0 0 8px 0;
      width: 100%;
    }

    .pay-history-pie-section .pie-chart-content {
      display: flex;
      justify-content: center;
      align-items: center;
      flex: 1;
      width: 100%;
      min-height: 0;
    }

    .pay-source-pie-container {
      width: 100%;
      height: 100%;
      min-height: 180px;
      margin: 0;
      flex: 1;
      display: flex;
      justify-content: center;
      align-items: center;
      position: relative;
      top: 0;
    }

    .usage-stats {
      flex: 0 0 35%;
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin-top: -20px;
    }

    .usage-stat-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 3px 3px;
      background: var(--button-primary-transparent);
      border-radius: 6px;
      border-left: 4px solid;
    }

    .usage-stat-label {
      font-weight: 600;
      font-size: 11px;
      color: var(--text-color);
    }

    .usage-stat-value {
      font-weight: bold;
      font-size: 11px;
      color: var(--text-color);
    }

    .valley-stat {
      border-left-color: #4CAF50;
    }

    .peak-stat {
      border-left-color: #FF9800;
    }

    .normal-stat {
      border-left-color: #2196F3;
    }

    .sharp-stat {
      border-left-color: #F44336;
    }

    .total-stat {
      border-left-color: #804AFF;
      background: rgba(128, 74, 255, 0.1) !important;
    }

    /* 设备轨道样式 */
    .timeline-aligner {
      display: grid;
      grid-template-columns: 67px 80px 1fr;
      align-items: center;
      margin-top: 10px;
      gap: 2px;
    }

    .timeline-aligner-spacer {
      width: 100px;
      flex-shrink: 0;
    }

    .timeline-aligner-spacer-2 {
      width: 60px;
      flex-shrink: 0;
    }

    .timeline-aligner-content {
      flex-grow: 1;
      margin-left: 0;
      width: 299px;
    }

    #timeline-container {
      margin-top: 10px;
      background: var(--card-button-bg);
      border-radius: 8px;
      padding: 10px;
    }

    .device-track {
      display: grid;
      grid-template-columns: 60px 85px 1fr;
      align-items: center;
      margin-bottom: 8px;
      height: 12px;
      width: 100%;
      gap: 2px;
      align-content: center;
    }

    .device-label {
      width: 100px;
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-right: 10px;
      padding: 0;
      border-radius: 4px;
      color: var(--button-color-active);
      flex-shrink: 0;
      text-align: left;
      box-sizing: border-box;
    }

    .power-usage-container {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 4px;
      font-size: 10px; /* 固定字体大小，不从配置读取 */
      font-weight: 600;
      white-space: nowrap;
      font-family: 'Courier New', monospace;
      transform-origin: center;
    }

    /* 统一的设备功率/时长样式 */
    .power-usage,
    .usage-duration,
    .power-usage-estimate {
      text-align: center;
      font-size: 10px;
      min-width: 40px;
      padding: 0;
      border-radius: 3px;
      font-weight: 600;
    }

    .power-usage {
      background: var(--usage-color);
      color: #000000;
    }

    .power-usage-estimate {
      background: #38b48b;
      color: #000000;
    }

    .usage-duration {
      background: var(--money-color);
      color: white;
    }

    /* 合计行样式 */
    .total-row {
      display: grid;
      grid-template-columns: 60px 85px 1fr;
      align-items: center;
      margin-top: 10px;
      padding-top: 8px;
      border-top: 1px solid var(--button-primary-transparent);
      font-weight: 600;
      font-size: 11px;
      color: var(--text-color);
      height: 12px;
      width: 100%;
      gap: 2px;
      align-content: center;
    }

    .total-label {
      width: 100px;
      font-size: 11px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-right: 10px;
      padding: 0;
      border-radius: 4px;
      color: var(--button-color-active);
      flex-shrink: 0;
      text-align: left;
      box-sizing: border-box;
    }

    .total-value {
      text-align: center;
      font-size: 10px;
      min-width: 40px;
      padding: 2px 4px;
      border-radius: 3px;
      font-weight: 600;
      background: var(--button-primary-transparent);
      color: var(--text-color);
    }

    /* timeline-aligner 中的 time-labels（绝对定位版本） */
    .timeline-aligner .time-labels {
      position: relative;
      width: 106%;
      height: 10px;
      margin-top: -5px;
      margin-left: -10px;
      font-size: 9px;
      color: var(--text-color);
      font-weight: 400;
    }

    .timeline-aligner .time-labels span {
      position: absolute;
      top: 0;
      white-space: nowrap;
      box-sizing: border-box;
      transform: translateX(-50%);
      font-weight: 400;
    }

    .timeline-aligner .time-labels span:nth-child(1) {
      left: 0%;
      transform: translateX(0%);
    }
    .timeline-aligner .time-labels span:nth-child(2) { left: 33.333%; }
    .timeline-aligner .time-labels span:nth-child(3) { left: 66.666%; }
    .timeline-aligner .time-labels span:nth-child(4) {
      left: 100%;
      transform: translateX(-100%);
    }



    .track-bar-wrapper {
      flex-grow: 1;
      position: relative;
      height: 15px;
      background: rgba(0,0,0,0.05);
      border-radius: 4px;
      cursor: pointer;
      margin-right: 0;
    }

    .track-bar {
      width: 100%;
      height: 15px;
      position: relative;
    }

    .track-fill {
      position: absolute;
      height: 15px;
      top: 0;
      opacity: 0.7;
      border-radius: 2px;
      transition: opacity 0.2s;
    }

    .track-fill.active {
      opacity: 1;
      box-shadow: 0 0 4px rgba(0,0,0,0.3);
      z-index: 2;
    }

    @keyframes stripe-move {
      from { background-position: 0 0; }
      to { background-position: 1rem 0; }
    }

    @keyframes rainbow-glow {
      0% { box-shadow: 0 0 6px 2px rgba(255, 0, 0, 0.8), 0 0 12px 4px rgba(255, 165, 0, 0.6); }
      14% { box-shadow: 0 0 6px 2px rgba(255, 165, 0, 0.8), 0 0 12px 4px rgba(255, 255, 0, 0.6); }
      28% { box-shadow: 0 0 6px 2px rgba(255, 255, 0, 0.8), 0 0 12px 4px rgba(0, 128, 0, 0.6); }
      42% { box-shadow: 0 0 6px 2px rgba(0, 128, 0, 0.8), 0 0 12px 4px rgba(0, 255, 255, 0.6); }
      57% { box-shadow: 0 0 6px 2px rgba(0, 255, 255, 0.8), 0 0 12px 4px rgba(0, 0, 255, 0.6); }
      71% { box-shadow: 0 0 6px 2px rgba(0, 0, 255, 0.8), 0 0 12px 4px rgba(128, 0, 128, 0.6); }
      85% { box-shadow: 0 0 6px 2px rgba(128, 0, 128, 0.8), 0 0 12px 4px rgba(255, 0, 0, 0.6); }
      100% { box-shadow: 0 0 6px 2px rgba(255, 0, 0, 0.8), 0 0 12px 4px rgba(255, 165, 0, 0.6); }
    }

    .track-fill.on-going {
      background-image: linear-gradient(45deg,rgba(255,255,255,.15) 25%,transparent 25%,transparent 50%,rgba(255,255,255,.15) 50%,rgba(255,255,255,.15) 75%,transparent 75%,transparent);
      background-size: 1rem 1rem;
      animation: stripe-move 1s linear infinite, rainbow-glow 8s linear infinite;
    }

    /* 正在运行事件列表项的动画样式 */
    #tooltip-events-list .on-going-event {
      position: relative;
      background-image: linear-gradient(45deg, rgba(255,255,255,0.1) 25%, transparent 25%, transparent 50%, rgba(255,255,255,0.1) 50%, rgba(255,255,255,0.1) 75%, transparent 75%, transparent);
      background-size: 0.8rem 0.8rem;
      animation: stripe-move 1.5s linear infinite;
      border-left: 3px solid var(--button-color-active);
      padding-left: 8px;
    }

    #tooltip-events-list .on-going-event::before {
      content: "●";
      position: absolute;
      left: -12px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--button-color-active);
      font-size: 8px;
      animation: pulse 1.5s infinite;
    }

    @keyframes pulse {
      0% { opacity: 1; }
      50% { opacity: 0.3; }
      100% { opacity: 1; }
    }

    /* 设备事件提示框样式 - 已移除，使用JavaScript动态创建的样式 */

    /* 缴费历史模态框样式 */
    .pay-history-top-section {
      display: flex;
      gap: 10px;
      margin-bottom: 10px;
      align-items: stretch;
    }

    .pay-history-summary {
      display: flex;
      justify-content: space-evenly;
      background: var(--card-button-bg);
      border-radius: 8px;
      width: 25%;
      flex-shrink: 0;
      flex-direction: column;
      height: 234px;
    }

    .pay-history-item {
      text-align: center;
    }

    .pay-history-label {
      font-size: 12px;
      color: var(--text-color);
      opacity: 0.8;
    }

    .pay-history-value {
      font-size: 18px;
      font-weight: 700;
      color: var(--card-value-color);
    }

    .pay-history-list {
      max-height: 350px;
      overflow-y: auto;
      padding-right: 5px;
    }

    .pay-history-list::-webkit-scrollbar {
      width: 6px;
    }

    .pay-history-list::-webkit-scrollbar-track {
      background: var(--button-primary-transparent);
      border-radius: 3px;
    }

    .pay-history-list::-webkit-scrollbar-thumb {
      background: var(--button-color);
      border-radius: 3px;
    }

    .pay-record {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 5px 15px;
      background: var(--card-button-bg);
      border-radius: 6px;
      margin-bottom: 8px;
      border-left: 3px solid var(--money-color);
      transition: all 0.2s ease;
    }

    .pay-record:hover {
      background: var(--button-primary-transparent);
      transform: translateX(3px);
    }

    .pay-record-source {
      font-size: 11px;
      color: white;
      opacity: 1;
      padding: 2px 6px;
      border-radius: 4px;
      display: inline-block;
      flex-shrink: 0;
    }

    .pay-record-time {
      font-size: 12px;
      color: var(--text-color);
      opacity: 0.85;
      min-width: 90px;
      text-align: center;
    }

    /* 支付宝 - 蓝色 */
    .pay-record-source.alipay {
      background: #1677FF;
    }

    /* 微信 - 绿色 */
    .pay-record-source.wechat {
      background: #52C41A;
    }

    /* 网上国网 - 深蓝色 */
    .pay-record-source.sgcc-online {
      background: #14806E;
    }

    /* 电e宝 - 金色 */
    .pay-record-source.eebao {
      background: #FAAD14;
    }

    /* 社会网点 - 橙色 */
    .pay-record-source.outlet {
      background: #FA8C16;
    }

    /* 银行卡 - 紫色 */
    .pay-record-source.bank {
      background: #9C27B0;
    }

    /* 其他 - 灰色 */
    .pay-record-source.other {
      background: #8C8C8C;
    }

    /* 默认 - 青色 */
    .pay-record-source.default {
      background: #13C2C2;
    }

    .pay-record-cost {
      font-size: 16px;
      font-weight: 700;
      color: var(--money-color);
      flex-shrink: 0;
    }

    .pay-record-cost::before {
      content: '¥';
      font-size: 12px;
      margin-right: 2px;
    }

    .no-pay-history {
      text-align: center;
      padding: 40px 20px;
      color: var(--text-color);
      opacity: 0.7;
    }

    .no-pay-history-icon {
      font-size: 48px;
      margin-bottom: 10px;
    }

    .no-pay-history-text {
      font-size: 14px;
    }

    .no-pay-history-subtext {
      font-size: 12px;
      margin-top: 5px;
      opacity: 0.7;
    }

    /* 年视图和月视图样式 */
    #year-content, #month-content {
      display: flex;
      flex-direction: column;
      height: calc(400px - 50px);
    }

    .year-chart-container {
      margin-bottom: 0;
      background: var(--stat-bg);
      border-radius: 8px;
      padding: 7px;
      display: flex;
      flex-direction: column;
      flex: 1;
      height: 100%;
    }

    .chart-title {
      font-size: 14px;
      font-weight: 600;
      color: var(--text-color);
      margin-bottom: 4px;
      text-align: center;
      flex-shrink: 0;
      margin-top: -15px;
    }

    .month-labels-container {
      display: flex;
      justify-content: center;
      gap: 2px;
      margin-bottom: 0;
      flex-shrink: 0;
    }

    .month-label-item {
      font-size: 11px;
      color: #666;
      text-align: center;
      flex: 1;
      max-width: 30px;
    }

    .year-filter {
      display: flex;
      justify-content: space-evenly;
      gap: 2px;
      flex-shrink: 0;
      flex-wrap: wrap;
    }

    .year-filter#day-filter {
      gap: 3px;
    }

    .year-filter#year-year-filter {
      margin-bottom: 8px;
    }

    .year-filter-main {
      display: flex;
      justify-content: center;
      gap: 5px;
      margin-bottom: 8px;
      flex-shrink: 0;
    }

    .year-filter-years {
      display: flex;
      justify-content: space-evenly;
      gap: 1px;
      flex-shrink: 0;
      flex-wrap: wrap;
    }

    .day-filter-main {
      display: flex;
      justify-content: center;
      gap: 5px;
      margin-bottom: 8px;
      flex-shrink: 0;
    }

    .day-filter-years {
      display: flex;
      justify-content: space-evenly;
      gap: 5px;
      flex-shrink: 0;
      flex-wrap: wrap;
    }

    .year-tag {
      padding: 2px 2px;
      border: 1px solid var(--button-color-active);
      border-radius: 16px;
      background: transparent;
      color: var(--button-color);
      font-size: 12px;
      cursor: pointer;
      transition: all 0.2s ease;
      white-space: nowrap;
    }

    .year-tag:hover {
      background: var(--button-primary-transparent);
    }

    .year-tag.active {
      background: var(--button-color-active);
      color: var(--button-active-text-color);
    }

    .month-tag {
      padding: 1px 1px;
      border: 1px solid var(--button-color);
      border-radius: 6px;
      background: transparent;
      color: var(--button-color);
      font-size: 12px;
      cursor: pointer;
      transition: all 0.2s ease;
      white-space: nowrap;
      width: 16px;
      margin-top: 5px;
      text-align: center;
    }

    .month-tag:hover {
      background: var(--button-primary-transparent);
    }

    .month-tag.active {
      background: var(--button-color-active);
      color: var(--card-bg);
    }

    .chart-wrapper {
      width: 100%;
      flex: 1;
      min-height: 270px;
    }
  </style>

  <div class="elegas-card">
    <div class="header">
      <div class="user-info" id="user-info">用电信息</div>
      <div class="multi-user-info" id="multi-user-info">
        <div class="slider-indicator" id="slider-indicator">
          <div class="spotlight-core"></div>
        </div>
        <div class="user-block active" data-index="0">
          <div class="user-block-balance">¥128.50</div>
          <div class="user-block-name">用户A</div>
        </div>
        <div class="user-block" data-index="1">
          <div class="user-block-balance">¥256.30</div>
          <div class="user-block-name">用户B</div>
        </div>
        <div class="user-block" data-index="2">
          <div class="user-block-balance">¥89.20</div>
          <div class="user-block-name">用户C</div>
        </div>
      </div>
    </div>

    <div class="balance-section">
      <div class="balance-price-row">
        <div class="balance-item">
          <div class="balance-label-container">
            <svg class="balance-icon" viewBox="0 0 24 24">
              <path fill="var(--svg-icon-color)" d="M2,5H22V20H2V5M20,18V7H4V18H20M17,8A2,2 0 0,0 19,10V15A2,2 0 0,0 17,17H7A2,2 0 0,0 5,15V10A2,2 0 0,0 7,8H17M17,13V12C17,10.9 16.33,10 15.5,10C14.67,10 14,10.9 14,12V13C14,14.1 14.67,15 15.5,15C16.33,15 17,14.1 17,13M15.5,11A0.5,0.5 0 0,1 16,11.5V13.5A0.5,0.5 0 0,1 15.5,14A0.5,0.5 0 0,1 15,13.5V11.5A0.5,0.5 0 0,1 15.5,11M13,13V12C13,10.9 12.33,10 11.5,10C10.67,10 10,10.9 10,12V13C10,14.1 10.67,15 11.5,15C12.33,15 13,14.1 13,13M11.5,11A0.5,0.5 0 0,1 12,11.5V13.5A0.5,0.5 0 0,1 11.5,14A0.5,0.5 0 0,1 11,13.5V11.5A0.5,0.5 0 0,1 11.5,11M8,15H9V10H8L7,10.5V11.5L8,11V15Z" />
            </svg>
            <span class="balance-label" id="balance-label">账户余额</span>
          </div>
          <div class="balance-amount">
            ¥<span id="balance">0.00</span>
            <span class="balance-unit"></span>
          </div>
        </div>
        <div class="price-display elegas-price-display">
          <div class="price-label-container">
            <svg class="price-icon" viewBox="0 0 24 24">
              <path fill="var(--svg-icon-color)" d="M7,15H9C9,16.08 10.37,17 12,17C13.63,17 15,16.08 15,15C15,13.9 13.96,13.5 11.76,12.97C9.64,12.44 7,11.78 7,9C7,7.21 8.47,5.69 10.5,5.18V3H13.5V5.18C15.53,5.69 17,7.21 17,9H15C15,7.92 13.63,7 12,7C10.37,7 9,7.92 9,9C9,10.1 10.04,10.5 12.24,11.03C14.36,11.56 17,12.22 17,15C17,16.79 15.53,18.31 13.5,18.82V21H10.5V18.82C8.47,18.31 7,16.79 7,15Z" />
            </svg>
            <span class="price-label">电价:</span>
          </div>
          <span class="price-value" id="elegas-price">0.4983</span>
          <span class="price-unit"></span>
        </div>
        <!-- 新增：剩余天数显示 -->
        <div class="price-display remaining-days-display">
          <div class="price-label-container">
            <svg class="price-icon" viewBox="0 0 24 24">
              <path fill="var(--svg-icon-color)" d="M16,11.78L20.24,4.45L21.97,5.45L16.74,14.5L10.23,10.75L5.46,19H22V21H2V3H4V17.54L9.5,8L16,11.78Z" />
            </svg>
            <span class="price-label">预计用:</span>
          </div>
          <span class="price-value" id="remaining-days">0</span>
          <span class="price-unit">天</span>
          <span class="price-unit" id="remaining-days-date"></span>
        </div>
      </div>
      <div class="date-info" id="date">更新时间 --</div>
      <div class="data-date-info" id="data-date">数据:--</div>
      <div class="relative-date-info" id="relative-date"></div>
    </div>

    <div class="tier-indicator">
      <div class="tier-label">
        <div class="tier-label-left">
          <svg class="icon tier-icon" viewBox="0 0 24 24">
            <path fill="var(--svg-icon-color)" d="M19,3H5C3.9,3 3,3.9 3,5V19C3,20.1 3.9,21 5,21H19C20.1,21 21,20.1 21,19V5C21,3.9 20.1,3 19,3M19,19H5V5H19V19M7,12H9V17H7V12M11,7H13V17H11V7M15,10H17V17H15V10Z" />
          </svg>
          用电阶梯
        </div>
        <div class="tier-label-right" id="tier-period">阶梯周期: 07.01-06.30</div>
      </div>
    </div>
    
    <!-- 添加一个内层容器，用于限制阶梯图的实际宽度 -->
    <div class="tier-indicator-container">
      <div class="tiers-container" id="tiers-container">
        <div class="tier tier-1" id="tier-1">
          <div class="tier-block" id="tier-1-block">第一阶梯</div>
          <div class="tier-content">
            <div class="tier-range" id="tier-1-range"></div>
            <div class="tier-price" id="tier-1-price"></div>
          </div>
        </div>
        
        <div class="tier tier-2" id="tier-2">
          <div class="tier-block" id="tier-2-block">第二阶梯</div>
          <div class="tier-content">
            <div class="tier-range" id="tier-2-range"></div>
            <div class="tier-price" id="tier-2-price"></div>
          </div>
        </div>
        
        <div class="tier tier-3" id="tier-3">
          <div class="tier-block" id="tier-3-block">第三阶梯</div>
          <div class="tier-content">
            <div class="tier-range" id="tier-3-range"></div>
            <div class="tier-price" id="tier-3-price"></div>
          </div>
        </div>
        
        <div class="current-indicator" id="current-indicator">
          <span id="current-tier">❶</span><span id="current-usage">0</span><span id="current-usage-unit"></span>
        </div>
        <div class="indicator-arrow" id="indicator-arrow"></div>
      </div>
    </div>
        <div class="data-container" id="data-container">
      <!-- 本月用电 -->
      <div class="current-month-stats" data-type="current-month">
        <div class="month-label">
          <svg class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" width="12" height="12">
            <path d="M512 1018C232.5 1018 6 791.5 6 512S232.5 6 512 6s506 226.5 506 506-226.5 506-506 506z m0-973.1C254 44.9 44.9 254 44.9 512S254 979.1 512 979.1 979.1 770 979.1 512 770 44.9 512 44.9zM395.2 862.3L473 589.8H278.5l350.3-428.2L551 434.1h194.6L395.2 862.3z" fill="var(--svg-icon-color)"></path>
          </svg>
          本月用电
        </div>
        
        <div class="month-grid">
          <div class="month-stat">
            <div class="month-stat-value green">
              <span id="current-month-electricity">0</span>
              <span class="month-stat-unit" id="current-month-ele-unit"></span>
            </div>
          </div>
          
          <div class="month-stat">
            <div class="month-stat-value yellow">
              <span id="current-month-cost">0</span>
              <span class="month-stat-unit" id="current-month-cost-unit"></span>
            </div>
          </div>
        </div>
      </div>

      <!-- 上月用电 -->
      <div class="month-stats" data-type="last-month">
        <div class="month-label">
          <svg class="icon" viewBox="0 0 24 24">
            <path fill="var(--svg-icon-color)" d="M19,19V8H5V19H19M16,1H18V3H19A2,2 0 0,1 21,5V19A2,2 0 0,1 19,21H5C3.89,21 3,20.1 3,19V5C3,3.89 3.89,3 5,3H6V1H8V3H16V1M7,10H9V12H7V10M15,10H17V12H15V10M11,14H13V16H11V14M15,14H17V16H15V14Z"/>
          </svg>
          上月用电
        </div>
        
        <div class="month-grid">
          <div class="month-stat">
            <div class="month-stat-value">
              <span id="last-month-electricity">0</span>
              <span class="month-stat-unit" id="last-month-ele-unit"></span>
            </div>
          </div>
          
          <div class="month-stat">
            <div class="month-stat-value yellow">
              <span id="last-month-cost">0</span>
              <span class="month-stat-unit" id="last-month-cost-unit"></span>
            </div>
          </div>
        </div>
      </div>

      <!-- 年度累计 -->
      <div class="year-stats" data-type="year">
        <div class="year-label">
          <svg class="icon" viewBox="0 0 24 24">
            <path fill="var(--svg-icon-color)" d="M9,10V12H7V10H9M13,10V12H11V10H13M17,10V12H15V10H17M19,3A2,2 0 0,1 21,5V19A2,2 0 0,1 19,21H5C3.89,21 3,20.1 3,19V5A2,2 0 0,1 5,3H6V1H8V3H16V1H18V3H19M19,19V8H5V19H19M9,14V16H7V14H9M13,14V16H11V14H13M17,14V16H15V14H17Z"/>
          </svg>
          <span id="current-year">2025</span>年用电
        </div>
        
        <div class="year-grid">
          <div class="year-stat">
            <div class="year-stat-value green">
              <span id="year-electricity">0</span>
              <span class="year-stat-unit" id="year-ele-unit"></span>
            </div>
          </div>
          
          <div class="year-stat">
            <div class="year-stat-value yellow">
              <span id="year-cost">0</span>
              <span class="year-stat-unit" id="year-cost-unit"></span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 日历视图（默认隐藏） -->
    <div class="calendar-view" id="calendar-view" style="display: none;">
      <!-- 选项卡和控制区域 -->
    <div class="calendar-header">
      <div class="calendar-tabs">
        <button class="cal-tab-btn" data-view="year">年</button>
        <button class="cal-tab-btn" data-view="month">月</button>
        <button class="cal-tab-btn" data-view="day">日</button>
        <button class="cal-tab-btn active" data-view="calendar">日历</button>
      </div>
      <div class="calendar-controls">
        <button class="cal-control-btn" id="current-month-btn">本月</button>
        <select id="cal-year-select" class="cal-control-select">
          <!-- 年份选项动态生成 -->
        </select>
        <select id="cal-month-select" class="cal-control-select">
          <option value="1">1月</option>
          <option value="2">2月</option>
          <option value="3">3月</option>
          <option value="4">4月</option>
          <option value="5">5月</option>
          <option value="6">6月</option>
          <option value="7">7月</option>
          <option value="8">8月</option>
          <option value="9">9月</option>
          <option value="10">10月</option>
          <option value="11">11月</option>
          <option value="12">12月</option>
        </select>
      </div>
      <div class="calendar-back-control">
        <button class="cal-control-btn" id="back-to-main">返回</button>
      </div>
    </div>

      <!-- 日历视图内容 -->
      <div id="calendar-content">
        <!-- 日历网格 -->
        <div class="calendar-grid" id="calendar-grid">
          <div class="day-of-week">周一</div>
          <div class="day-of-week">周二</div>
          <div class="day-of-week">周三</div>
          <div class="day-of-week">周四</div>
          <div class="day-of-week">周五</div>
          <div class="day-of-week">周六</div>
          <div class="day-of-week">周日</div>
          <!-- 日期单元格动态生成 -->
        </div>

        <!-- 统计信息 -->
        <div class="calendar-stats">
          <div class="calendar-stat-item">
            <span class="stat-label">月用量：</span>
            <span class="stat-value kwh-value" id="cal-month-usage">0</span>
            <span class="stat-value cost-value" id="cal-month-cost">¥0</span>
          </div>
          <div class="calendar-stat-item">
            <span class="stat-label">年用量：</span>
            <span class="stat-value kwh-value" id="cal-year-usage">0</span>
            <span class="stat-value cost-value" id="cal-year-cost">¥0</span>
          </div>
        </div>
      </div>

      <!-- 年视图内容（默认隐藏） -->
      <div id="year-content" style="display: none;">
        <!-- 年度电费与用电量组合图 -->
        <div class="year-chart-container">
          <div class="chart-title">年度电费与用电量趋势</div>
          <div class="year-filter-main" id="year-filter-main"></div>
          <div class="year-filter-years" id="year-filter-years"></div>
          <div class="chart-wrapper" id="year-combo-chart"></div>
        </div>
      </div>

      <!-- 月视图内容（默认隐藏） -->
      <div id="month-content" style="display: none;">
        <!-- 月度电费与用电量组合图 -->
        <div class="year-chart-container">
          <div class="chart-title">月度电费与用电量趋势</div>
          <div class="year-filter" id="month-year-filter"></div>
          <div class="chart-wrapper" id="month-combo-chart"></div>
        </div>
      </div>

      <!-- 日视图内容（默认隐藏） -->
      <div id="day-content" style="display: none;">
        <!-- 日用电详情组合图 -->
        <div class="year-chart-container">
          <div class="chart-title">日用电详情</div>
          <div class="day-filter-main" id="day-filter-main"></div>
          <div class="day-filter-years" id="day-filter-years"></div>
          <div class="chart-wrapper" id="day-combo-chart"></div>
          <!-- 未来月份提示文字 -->
          <div class="future-month-message" id="future-month-message" style="display: none; text-align: center; padding: 60px 20px; font-size: 16px; color: var(--card-value-color); font-style: italic; line-height: 1.8;">
            愿为期之可待，化北辰以恒明，引千帆而向曙。
          </div>
        </div>
      </div>
    </div>

    <!-- 日详情模态框 -->
    <div class="day-modal-overlay" id="day-modal" style="display: none;">
      <div class="day-modal-content">
        <div class="day-modal-header">
          <div class="day-modal-title" id="day-modal-title">用电详情</div>
          <button class="day-modal-close" id="day-modal-close">×</button>
        </div>
        <div id="day-modal-body">
          <!-- 动态内容 -->
        </div>
      </div>
    </div>

    <!-- 缴费历史模态框 -->
    <div class="day-modal-overlay" id="pay-history-modal" style="display: none;">
      <div class="day-modal-content">
        <div class="day-modal-header">
          <div class="day-modal-title" id="pay-history-title">缴费历史</div>
          <button class="day-modal-close" id="pay-history-close">×</button>
        </div>
        <div id="pay-history-body">
          <!-- 动态内容 -->
        </div>
      </div>
    </div>

    <!-- 设备事件提示框（移到根级别，不受模态框影响） -->
    <div id="event-tooltip">
      <strong id="tooltip-device-name"></strong>
      <ul id="tooltip-events-list"></ul>
    </div>
  </div>
`;

/**
 * ECharts 加载函数
 * 优先从同目录 echarts.min.js 加载（/xjelegas/echarts.min.js），失败则从 jsDelivr CDN 加载
 * 使用单例 Promise 避免重复加载
 */
function loadECharts() {
  if (window._echartsLoadPromise) {
    return window._echartsLoadPromise;
  }

  window._echartsLoadPromise = new Promise((resolve, reject) => {
    // 如果ECharts已加载，直接返回
    if (typeof echarts !== "undefined") {
      resolve(echarts);
      return;
    }

    // 尝试加载同目录下的 echarts.min.js（与 xjelegas-card.js 同目录）
    const localScript = document.createElement("script");
    const cardScript = document.querySelector('script[src*="xjelegas-card"]');
    const baseUrl = cardScript?.src?.replace(/\/[^/]*$/, "/") || "/xjelegas/";
    localScript.src = baseUrl + "echarts.min.js";

    localScript.onload = () => {
      if (typeof echarts !== "undefined") {
        // console.log('✓ ECharts 从本地加载成功');
        resolve(echarts);
      } else {
        console.warn("✗ 本地ECharts加载成功但全局变量未定义，尝试CDN...");
        loadFromCDN(resolve, reject);
      }
    };

    localScript.onerror = () => {
      console.warn("✗ 本地ECharts加载失败，尝试CDN...");
      loadFromCDN(resolve, reject);
    };

    document.head.appendChild(localScript);
  });

  return window._echartsLoadPromise;
}

/** 从 jsDelivr CDN 加载 ECharts，作为本地加载失败时的备用方案 */
function loadFromCDN(resolve, reject) {
  const cdnScript = document.createElement("script");
  cdnScript.src =
    "https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js";

  cdnScript.onload = () => {
    if (typeof echarts !== "undefined") {
      console.log("✓ ECharts 从CDN加载成功");
      resolve(echarts);
    } else {
      reject(new Error("ECharts CDN加载后全局变量未定义"));
    }
  };

  cdnScript.onerror = () => {
    reject(new Error("ECharts CDN加载失败"));
  };

  document.head.appendChild(cdnScript);
}

/**
 * 电力燃气信息卡片主类
 * 继承 HTMLElement，作为 Home Assistant 自定义卡片组件
 */
// ==================== SECTION 4: EleGasCard 主卡片类 ====================
class EleGasCard extends HTMLElement {
  /** 返回配置编辑器元素，HA 在编辑卡片时调用 */
  static getConfigElement() {
    return document.createElement("xjelegas-card-editor");
  }

  /** 返回默认配置模板，用于新建卡片时的初始配置 */
  static getStubConfig() {
    return {
      multiclass: {
        1: {
          entity: "",
          utility_type: "ele",
        },
      },
    };
  }

  // =================================== 统一数据格式转换方法 ========================================

  /**
   * 格式化价格，去除尾部多余的0
   * @param {number} price - 价格数值
   * @param {number} maxDecimals - 最大小数位数，默认4
   * @returns {string} 格式化后的价格字符串
   */
  formatPrice(price, maxDecimals = 4) {
    if (price === undefined || price === null) return "0";
    const num = parseFloat(price);
    if (isNaN(num)) return "0";

    // 使用 parseFloat 去除尾部多余的 0
    // toFixed 保证最多 maxDecimals 位小数，然后 parseFloat 转回数字去除无效 0，最后转字符串
    return parseFloat(num.toFixed(maxDecimals)).toString();
  }

  /**
   * 安全转换数值，处理字符串、null、undefined
   */
  safeParseFloat(value, defaultValue = 0) {
    if (value === null || value === undefined || value === "") {
      return defaultValue;
    }
    const num = parseFloat(value);
    return isNaN(num) ? defaultValue : num;
  }

  /**
   * 计算并显示相对日期（今天、昨天、前天、x天前）
   */
  updateRelativeDate(dateStr) {
    if (!this.relativeDateEl || !dateStr || dateStr === "--") {
      return;
    }

    // 解析日期
    const date = new Date(dateStr);
    if (isNaN(date.getTime())) {
      this.relativeDateEl.textContent = "";
      this.relativeDateEl.className = "relative-date-info";
      return;
    }

    // 获取今天的日期（去除时间部分）
    const today = new Date();
    today.setHours(0, 0, 0, 0);

    // 获取数据日期（去除时间部分）
    const dataDate = new Date(date);
    dataDate.setHours(0, 0, 0, 0);

    const diffDays = Math.floor(
      (today.getTime() - dataDate.getTime()) / (1000 * 60 * 60 * 24),
    );
    const match = RELATIVE_DATE_MAP.find((m) => m.diff === diffDays);
    const text = match ? match.text : diffDays > 2 ? `${diffDays}天前` : "";
    const className = match
      ? `relative-date-info ${match.class}`
      : diffDays > 2
        ? "relative-date-info relative-date-other"
        : "relative-date-info";
    this.relativeDateEl.textContent = text;
    this.relativeDateEl.className = className;
  }

  /**
   * 根据 utility_type 获取单位
   * 电力用"°"(kWh)，燃气用 m³
   */
  getUnitByUtilityType(utilityType) {
    switch (utilityType) {
      case "ele":
        return "°";
      case "gas":
        return "m³";
      default:
        return "°"; // 默认使用电的单位
    }
  }

  /**
   * 根据 utility_type 更新动态文本
   */
  updateDynamicTexts(utilityType) {
    // 定义文本映射
    const textMap = {
      ele: {
        priceLabel: "电单价:",
        monthCurrent: "本月用电",
        monthLast: "上月用电",
        year: "年用电",
        tierLabel: "用电阶梯",
        chartTitleYear: "年度电费与用电量趋势",
        chartTitleMonth: "月度电费与用电量趋势",
        chartTitleDay: "日用电详情",
        dayModalTitle: "用电详情",
        pieChartTitle: "用电分布",
      },
      gas: {
        priceLabel: "气单价:",
        monthCurrent: "本月用气",
        monthLast: "上月用气",
        year: "年用气",
        tierLabel: "用气阶梯",
        chartTitleYear: "年度天然气费与用气量趋势",
        chartTitleMonth: "月度天然气费与用气量趋势",
        chartTitleDay: "日用气详情",
        dayModalTitle: "用气详情",
        pieChartTitle: "用气分布",
      },
    };

    // 存储文本映射为实例属性，供其他方法使用
    this.textMap = textMap[utilityType] || textMap.ele;

    const texts = this.textMap;

    // 1. 更新价格标签
    if (this.priceLabelEl) {
      this.priceLabelEl.textContent = texts.priceLabel;
    }

    // 2. 更新月份标签
    if (this.monthLabels && this.monthLabels.length >= 2) {
      // 清除第一个月份标签的所有文本节点，然后添加新文本
      const textNodes0 = Array.from(this.monthLabels[0].childNodes).filter(
        (node) => node.nodeType === Node.TEXT_NODE,
      );
      textNodes0.forEach((node) => node.remove());
      this.monthLabels[0].appendChild(
        document.createTextNode(texts.monthCurrent),
      );

      // 清除第二个月份标签的所有文本节点，然后添加新文本
      const textNodes1 = Array.from(this.monthLabels[1].childNodes).filter(
        (node) => node.nodeType === Node.TEXT_NODE,
      );
      textNodes1.forEach((node) => node.remove());
      this.monthLabels[1].appendChild(document.createTextNode(texts.monthLast));
    }

    // 3. 更新年度标签
    if (this.yearLabels && this.yearLabels.length > 0) {
      // 清除所有文本节点（保留 span#current-year 元素）
      const textNodes = Array.from(this.yearLabels[0].childNodes).filter(
        (node) => node.nodeType === Node.TEXT_NODE,
      );
      textNodes.forEach((node) => node.remove());

      // 只添加文本部分，不包含年份（年份由 span#current-year 提供）
      this.yearLabels[0].appendChild(document.createTextNode(texts.year));
    }

    // 4. 更新阶梯标签
    if (this.tierLabelLeftEl) {
      // 清除所有文本节点
      const textNodes = Array.from(this.tierLabelLeftEl.childNodes).filter(
        (node) => node.nodeType === Node.TEXT_NODE,
      );
      textNodes.forEach((node) => node.remove());

      // 添加新文本
      this.tierLabelLeftEl.appendChild(
        document.createTextNode(texts.tierLabel),
      );
    }

    // 5. 更新图表标题
    const chartTitles = this.shadowRoot.querySelectorAll(".chart-title");
    chartTitles.forEach((titleEl) => {
      if (titleEl) {
        if (
          titleEl.textContent.includes("年度") &&
          titleEl.textContent.includes("趋势")
        ) {
          titleEl.textContent = texts.chartTitleYear;
        } else if (
          titleEl.textContent.includes("月度") &&
          titleEl.textContent.includes("趋势")
        ) {
          titleEl.textContent = texts.chartTitleMonth;
        } else if (
          titleEl.textContent.includes("日") &&
          titleEl.textContent.includes("详情")
        ) {
          titleEl.textContent = texts.chartTitleDay;
        }
      }
    });
  }

  /**
   * 初始化多户滑动条
   */
  setupMultiUserBar() {
    if (!this.multiUserInfoEl) return;

    // 根据是否显示多用户条来设置样式
    if (this.showMultiUserBar) {
      this.multiUserInfoEl.classList.remove("hidden");
    } else {
      this.multiUserInfoEl.classList.add("hidden");
      return;
    }

    // 清空现有的用户块
    this.multiUserInfoEl.innerHTML = "";

    // 重新创建滑块指示器
    const sliderIndicator = document.createElement("div");
    sliderIndicator.className = "slider-indicator";
    sliderIndicator.id = "slider-indicator";
    this.multiUserInfoEl.appendChild(sliderIndicator);
    this.sliderIndicatorEl = sliderIndicator;

    // 创建用户块
    const userKeys = Object.keys(this.multiClassConfig);
    userKeys.forEach((key, index) => {
      const config = this.multiClassConfig[key];
      const userBlock = document.createElement("div");
      userBlock.className = `user-block ${index === this.currentUserIndex ? "active" : ""}`;
      userBlock.dataset.index = index;

      // 创建余额显示（初始显示'--'，加载后更新）
      const balanceDiv = document.createElement("div");
      balanceDiv.className = "user-block-balance";
      balanceDiv.textContent = "--";
      balanceDiv.dataset.entity = config.entity
        ? config.entity.split(",")[0].trim()
        : "";

      // 创建名称显示
      const nameDiv = document.createElement("div");
      nameDiv.className = "user-block-name";

      let displayName;
      if (config.info) {
        // 如果配置了 info，直接使用 info 作为显示名称
        displayName = config.info;
      } else {
        // 否则使用默认格式：ID_类型
        const utilityType = config.utility_type || "ele";
        const typeName = this.getUtilityTypeName(utilityType);
        displayName = `${key}_${typeName}`;
      }

      nameDiv.textContent = displayName;

      userBlock.appendChild(balanceDiv);
      userBlock.appendChild(nameDiv);

      // 添加点击事件
      userBlock.addEventListener("click", () => {
        this.switchUser(index);
      });

      this.multiUserInfoEl.appendChild(userBlock);
    });

    // 初始化所有用户的余额显示
    this.updateAllUsersBalance();

    // 更新滑块位置
    this.userBlocks = this.multiUserInfoEl.querySelectorAll(".user-block");

    // 确保当前用户块的 active 状态正确设置（首次进入卡片时）
    if (
      this.userBlocks.length > 0 &&
      this.currentUserIndex < this.userBlocks.length
    ) {
      // 使用 requestAnimationFrame 确保 DOM 已经渲染完成
      requestAnimationFrame(() => {
        // 先移除所有用户块的 active 类
        this.userBlocks.forEach((block) => block.classList.remove("active"));
        // 为当前用户块添加 active 类
        this.userBlocks[this.currentUserIndex].classList.add("active");
        // 更新滑块位置
        this.updateSliderPosition();
      });
    } else {
      // 如果没有用户块，直接更新滑块位置
      this.updateSliderPosition();
    }
  }

  /** 获取 utility_type 对应的中文名称（电费/燃气） */
  getUtilityTypeName(utilityType) {
    const typeMap = {
      ele: "电费",
      gas: "燃气",
    };
    return typeMap[utilityType] || "未知";
  }

  /** 根据 utility_type 更新卡片背景图标（电力高压塔/燃气火焰） */
  updateCardBackgroundIcon(utilityType) {
    if (!this.electricityCardEl) return;

    // 先移除所有背景图标类
    this.electricityCardEl.classList.remove("bg-ele", "bg-gas");

    // 根据utility_type添加对应的背景图标类
    if (utilityType === "ele") {
      this.electricityCardEl.classList.add("bg-ele");
    } else if (utilityType === "gas") {
      this.electricityCardEl.classList.add("bg-gas");
    }
  }

  /** 更新多用户切换条的滑块指示器位置，使其对齐当前选中用户块 */
  updateSliderPosition() {
    if (
      !this.sliderIndicatorEl ||
      !this.userBlocks ||
      this.userBlocks.length === 0
    )
      return;

    const activeBlock = this.userBlocks[this.currentUserIndex];
    if (activeBlock) {
      // 使用 requestAnimationFrame 确保 DOM 已渲染
      requestAnimationFrame(() => {
        const offsetLeft = activeBlock.offsetLeft;
        const offsetWidth = activeBlock.offsetWidth;

        // 确保获取到了有效的尺寸（宽度大于0）
        if (offsetWidth > 0) {
          this.sliderIndicatorEl.style.left = `${offsetLeft}px`;
          this.sliderIndicatorEl.style.width = `${offsetWidth}px`;
        } else {
          // 如果首次渲染时宽度为0，等待下一帧再尝试
          requestAnimationFrame(() => {
            const retryOffsetLeft = activeBlock.offsetLeft;
            const retryOffsetWidth = activeBlock.offsetWidth;
            this.sliderIndicatorEl.style.left = `${retryOffsetLeft}px`;
            this.sliderIndicatorEl.style.width = `${retryOffsetWidth}px`;
          });
        }
      });
    }
  }

  /** 确保当前用户块具有 active 类，并同步滑块位置 */
  ensureCurrentUserActive() {
    if (!this.userBlocks || this.userBlocks.length === 0) return;

    // 确保当前用户索引在有效范围内
    if (
      this.currentUserIndex < 0 ||
      this.currentUserIndex >= this.userBlocks.length
    ) {
      this.currentUserIndex = 0;
    }

    // 先移除所有用户块的 active 类
    this.userBlocks.forEach((block) => block.classList.remove("active"));

    // 为当前用户块添加 active 类
    this.userBlocks[this.currentUserIndex].classList.add("active");

    // 更新滑块位置
    this.updateSliderPosition();
  }

  /**
   * 切换用户（完整流程）
   */
  switchUser(index) {
    if (index === this.currentUserIndex) return;

    const blocks = Array.from(this.userBlocks);
    if (!blocks[index]) return;

    // 【关闭】隐藏日历/关闭模态框/销毁图表/清空动态内容
    this.closeAllUI();

    // 【重置】卡片回到最初状态
    this.resetCardState();

    // 【动画】卡片淡出效果
    if (this.electricityCardEl) {
      this.electricityCardEl.classList.add("switching");
      this.electricityCardEl.classList.remove("switching-in");
    }

    // 等待淡出动画完成（300ms）
    setTimeout(() => {
      // 【切换配置】加载新户的配置
      this.currentUserIndex = index;
      // 持久化用户选择，刷新后恢复
      const storageKey = this._getUserIndexStorageKey();
      if (storageKey) {
        try {
          localStorage.setItem(storageKey, String(index));
        } catch (_) {}
      }
      this.initializeCurrentUserConfig();

      // 添加 active 类到当前块
      blocks.forEach((block) => block.classList.remove("active"));
      blocks[index].classList.add("active");

      // 更新滑块位置
      this.updateSliderPosition();

      // 【动画】卡片淡入效果
      if (this.electricityCardEl) {
        this.electricityCardEl.classList.remove("switching");
        this.electricityCardEl.classList.add("switching-in");
      }

      // 等待淡入动画完成（400ms）后移除动画类
      setTimeout(() => {
        if (this.electricityCardEl) {
          this.electricityCardEl.classList.remove("switching-in");
        }
      }, 400);

      // 【加载数据】加载新用户的数据（重要：切换用户后必须重新加载数据）
      // 等待数据加载完成后再更新显示，确保 standardData 已切换到新用户数据
      this.loadDataForCurrentUser().then(() => {
        // 数据加载完成后再更新余额显示
        this.updateBalanceDisplay();

        // 应用当前用户的隐藏配置
        this.applyHiddenConfig();

        // 【更新显示】数据加载完成后更新卡片
        this.updateCard();

        // 【重要】如果当前在日历视图，重新渲染日历以确保设备历史数据标记正确显示
        if (this.isCalendarView) {
          this.updateCalendar();
        }
      });
    });
  }

  // 关闭所有UI元素
  /** 关闭所有 UI：隐藏日历/总计视图、关闭模态框、销毁图表 */
  closeAllUI() {
    // 如果当前是日历视图，调用 hideCalendarView() 来完整关闭
    if (this.isCalendarView) {
      this.hideCalendarView();
    }

    // 关闭模态框
    this.closeAllModals();

    // 销毁图表实例（但不销毁ECharts本身）
    this.destroyCharts();

    // 清除阶梯指示器的动态元素（重要：切换用户时必须清除旧指示器）
    if (this.tiersContainerEl) {
      // 移除红色竖线指示器
      const redLines = this.tiersContainerEl.querySelectorAll(
        ".red-line-indicator",
      );
      redLines.forEach((line) => line.remove());

      // 移除倒三角指示器
      const triangles = this.tiersContainerEl.querySelectorAll(
        ".current-indicator-triangle",
      );
      triangles.forEach((triangle) => triangle.remove());
    }
  }

  /** 重置卡片状态：清空视图、恢复默认 tab、清除缓存等 */
  resetCardState() {
    // 清空数据缓存中的当前用户数据
    const cacheKey = this.getCurrentCacheKey();
    if (this._dataCache.has(cacheKey)) {
      this._dataCache.delete(cacheKey);
    }

    // 重置标准数据
    this.standardData = {
      dayUsage: [],
      monthUsage: [],
      yearUsage: [],
      payRecords: [],
      unit: "",
    };

    // 重置阶梯配置，避免切换用户时电力串用燃气的 m³/阶梯范围/单价
    this.tierConfig = {
      tiers: [
        { id: 1, max: null, price: null, color: "#55c593", title: "第一阶梯" },
        { id: 2, max: null, price: null, color: "#f8c337", title: "第二阶梯" },
        {
          id: 3,
          max: Infinity,
          price: null,
          color: "#f79335",
          title: "第三阶梯",
        },
      ],
      periodStartMonth: null,
      periodStartDay: null,
      periodEndMonth: null,
      periodEndDay: null,
    };

    // 重置历史数据加载标志
    this.historicalDataLoaded = false;

    // 重置阶梯用量，避免切换后残留燃气的 67 等错误值
    this.currentPeriodUsage = 0;
    this.currentTier = 1;
  }

  /** 关闭所有弹窗（日详情、缴费弹窗等） */
  closeAllModals() {
    // 关闭日历日期详情模态框
    const dayModal = this.shadowRoot.querySelector(".day-modal-overlay");
    if (dayModal) {
      dayModal.style.display = "none";
    }

    // 关闭缴费历史模态框
    const payModal = this.shadowRoot.querySelector(
      ".pay-history-modal-overlay",
    );
    if (payModal) {
      payModal.style.display = "none";
    }
  }

  /** 检查 ECharts 是否可用（可选传入容器元素；requireLoaded 时需 echartsLoaded） */
  _isEchartsAvailable(containerEl, requireLoaded = false) {
    if (typeof echarts === "undefined") return false;
    if (requireLoaded && !this.echartsLoaded) return false;
    if (containerEl !== undefined && !containerEl) return false;
    return true;
  }

  /** 销毁单个图表实例（若存在） */
  _disposeChart(chartRef) {
    const chart = this[chartRef];
    if (chart) {
      chart.dispose();
      this[chartRef] = null;
    }
  }

  // 销毁图表（保留ECharts库）
  destroyCharts() {
    this._disposeChart("yearChart");
    this._disposeChart("monthChart");
    this._disposeChart("dayChart");
    this._disposeChart("paySourceChart");
    this._disposeChart("dayViewChart");
  }

  /**
   * 转换日用量数据为统一格式
   */
  convertDayList(daylist, user = "ele_01") {
    if (!Array.isArray(daylist)) return [];

    // 获取字段映射（如果有）
    const mapping = this.fieldMapping || {};
    const dateField = mapping.date || "day";
    const usageField = mapping.usage;
    const amountField = mapping.amount;

    return daylist.map((item) => {
      let converted;

      if (this.utilityType === "ele") {
        // 电力类型使用原有字段
        converted = {
          user: user,
          utility_type: this.utilityType,
          data_category: "usage",
          date_granularity: "day",
          time: item.day || "",
          data_source: "entity",
          total_usage: this.safeParseFloat(item.dayEleNum),
          total_amount: this.safeParseFloat(item.dayEleCost),
          unit: item.unit || this.getUnitByUtilityType(this.utilityType),
        };
      } else {
        // 非电力类型（如燃气）尝试读取 readingTime/usage/amount
        const dateValue = item[dateField] || item.readingTime || item.day || "";
        // 处理日期字符串，移除可能的双引号
        const cleanDate =
          typeof dateValue === "string"
            ? dateValue.replace(/"/g, "")
            : dateValue;

        // 尝试获取用量和金额，优先使用 mapping，其次是通用字段 usage/amount，最后是兼容字段 dayEleNum/dayEleCost
        const usageVal = usageField
          ? item[usageField]
          : item.usage !== undefined
            ? item.usage
            : item.dayEleNum;
        const amountVal = amountField
          ? item[amountField]
          : item.amount !== undefined
            ? item.amount
            : item.dayEleCost;

        converted = {
          user: user,
          utility_type: this.utilityType,
          data_category: "usage",
          date_granularity: "day",
          time: cleanDate,
          data_source: "entity",
          total_usage: this.safeParseFloat(usageVal),
          total_amount: this.safeParseFloat(amountVal),
          unit: item.unit || this.getUnitByUtilityType(this.utilityType),
        };
      }

      converted.usage_ele_no = converted.total_usage || 0;

      return converted;
    });
  }

  /**
   * 标准化月份字符串为 YYYY-MM 格式，兼容电力集成 API 返回的 YYYYMM 格式
   */
  normalizeMonthStr(monthStr) {
    if (!monthStr) return "";
    const s = String(monthStr).replace(/-/g, "");
    if (s.length === 6 && /^\d{6}$/.test(s)) {
      return `${s.slice(0, 4)}-${s.slice(4, 6)}`;
    }
    return monthStr;
  }

  /**
   * 转换月用量数据为统一格式
   */
  convertMonthList(monthlist, user = "ele_01") {
    if (!Array.isArray(monthlist)) return [];

    return monthlist.map((item) => {
      // 兼容 monthEleNum/monthEleCost (电力) 和 usage/amount (燃气/水务)
      const usageVal =
        item.monthEleNum !== undefined ? item.monthEleNum : item.usage;
      const amountVal =
        item.monthEleCost !== undefined ? item.monthEleCost : item.amount;
      // 电力集成 API 可能返回 YYYYMM 格式，统一为 YYYY-MM 便于与 getCurrentMonthStr 匹配
      const monthTime = this.normalizeMonthStr(item.month || "");

      const converted = {
        user: user,
        utility_type: this.utilityType,
        data_category: "usage",
        date_granularity: "month",
        time: monthTime,
        data_source: "entity",
        total_usage: this.safeParseFloat(usageVal),
        total_amount: this.safeParseFloat(amountVal),
        unit: item.unit || this.getUnitByUtilityType(this.utilityType),
      };

      converted.usage_ele_no = converted.total_usage || 0;

      return converted;
    });
  }

  /**
   * 转换年用量数据为统一格式
   */
  convertYearList(yearlist, user = "ele_01") {
    if (!Array.isArray(yearlist)) return [];

    return yearlist.map((item) => {
      // 兼容 yearEleNum/yearEleCost (电力) 和 usage/amount (燃气/水务)
      const usageVal =
        item.yearEleNum !== undefined ? item.yearEleNum : item.usage;
      const amountVal =
        item.yearEleCost !== undefined ? item.yearEleCost : item.amount;

      const converted = {
        user: user,
        utility_type: this.utilityType,
        data_category: "usage",
        date_granularity: "year",
        time: item.year || "",
        data_source: "entity",
        total_usage: this.safeParseFloat(usageVal),
        total_amount: this.safeParseFloat(amountVal),
        unit: item.unit || this.getUnitByUtilityType(this.utilityType),
      };

      converted.usage_ele_no = converted.total_usage || 0;

      return converted;
    });
  }

  /** 预定义主题配色方案（power=国家电网、transparent=半透明等） */
  static get COLOR_SCHEMES() {
    return {
      // 国家电网主题方案（国网绿 C100M5Y50K40 ≈ #008B45，辅色红/黄）
      power: {
        "--button-color-active": "#008B45",
        "--button-active-text-color": "rgb(255, 255, 255)",
        "--button-color": "#008B45",
        "--button-text-color": "#1a3d2e",
        "--text-color": "#1a3d2e",
        "--svg-icon-color": "#1a3d2e",
        "--card-primary-hover": "#00A651",
        "--button-primary-transparent": "rgba(0, 139, 69, 0.15)",
        "--card-on-state-hover": "rgba(0, 139, 69, 0.2)",
        "--card-button-bg": "rgba(255, 255, 255, 0.6)",
        "--card-name-color": "rgba(0, 0, 0, 0.85)",
        "--card-value-color": "#006B47",
        "--balance-value-color": "#C41E1E",
        "--price-value-color": "#008B45",
        "--remaining-days-value-color": "#006B47",
        "--card-slider-value-color": "rgba(255, 255, 255, 0.95)",
        "--card-bg":
          "linear-gradient(135deg, rgba(230, 248, 238, 1) 0%, rgba(210, 240, 225, 1) 50%, rgba(195, 232, 215, 1) 100%)",
        "--date-circle-bg": "#C41E1E",
        "--date-circle-color": "white",
        "--usage-color": "#008B45",
        "--money-color": "#C41E1E",
        "--tooltip-bg": "rgba(230, 248, 238, 0.98)",
        "--tooltip-text-color": "var(--text-color)",
        "--calendar-line-color": "rgba(0, 139, 69, 0.2)",
        "--tooltip-border-color": "rgba(0, 139, 69, 0.3)",
        "--tooltip-highlight-bg": "var(--button-color-active)",
      },
      // 半透明主题方案（优化：语义统一、对比度与色彩协调）
      transparent: {
        "--button-color-active": "rgba(255, 255, 255, 0.85)",
        "--button-active-text-color": "rgb(255, 255, 255)",
        "--button-color": "rgba(255, 255, 255, 0.65)",
        "--text-color": "rgba(255, 255, 255, 0.75)",
        "--svg-icon-color": "rgba(255, 255, 255, 0.7)",
        "--card-primary-hover": "rgba(255, 255, 255, 0.9)",
        "--button-primary-transparent": "rgba(255, 255, 255, 0.12)",
        "--card-on-state-hover": "rgba(255, 255, 255, 0.25)",
        "--card-button-bg": "rgba(255, 255, 255, 0.06)",
        "--card-name-color": "rgba(235, 235, 235, 0.95)",
        "--card-value-color": "rgba(230, 230, 230, 0.95)",
        "--balance-value-color": "#f87171",
        "--price-value-color": "#34d399",
        "--remaining-days-value-color": "#38bdf8",
        "--card-slider-value-color": "rgba(255, 255, 255, 0.9)",
        "--card-bg": "rgba(0, 0, 0, 0.35)",
        "--date-circle-bg": "#f87171",
        "--date-circle-color": "white",
        "--usage-color": "#34d399",
        "--money-color": "#f87171",
        "--calendar-line-color": "rgba(255, 255, 255, 0.15)",
        "--tooltip-bg": "var(--card-bg)",
        "--tooltip-text-color": "var(--text-color)",
        "--tooltip-border-color": "rgba(255, 255, 255, 0.2)",
        "--tooltip-highlight-bg": "var(--button-color-active)",
      },
    };
  }

  // ==================== 主题相关方法（优化版） ====================
  /** 解析 dark_light_theme 配置，返回 [暗色主题名, 亮色主题名] */
  parseDarkLightTheme() {
    const darkLightTheme = this._config?.dark_light_theme;
    if (darkLightTheme && typeof darkLightTheme === "string") {
      const parts = darkLightTheme.split(",").map((p) => p.trim());
      if (parts.length >= 2) {
        return [parts[0] || "transparent", parts[1] || "power"];
      }
    }
    return ["transparent", "power"]; // 默认：暗色半透明、亮色电力主题
  }

  /** 根据当前时间返回主题：6:00–18:00 为亮色，其余为暗色 */
  getThemeByTime() {
    const hour = new Date().getHours();
    const [darkTheme, lightTheme] = this.parseDarkLightTheme();
    return hour >= 6 && hour < 18 ? lightTheme : darkTheme;
  }

  /** 获取系统主题：根据 prefers-color-scheme 返回暗色或亮色主题 */
  getSystemTheme() {
    const [darkTheme, lightTheme] = this.parseDarkLightTheme();
    if (
      window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches
    ) {
      return darkTheme;
    }
    return lightTheme; // 默认亮色
  }

  /** 设置主题监听器：time 模式用定时器，phone 模式用 matchMedia */
  setupThemeListeners() {
    // 如果已经设置了监听器，先清理
    if (this._themeObserver) {
      this._themeObserver.disconnect();
    }

    // 监听主题实体变化（在hass设置时触发）
  }

  // 停止主题定时器
  stopThemeTimer() {
    if (this.themeTimer) {
      clearInterval(this.themeTimer);
      this.themeTimer = null;
    }
  }

  // 停止手机主题监听器
  stopPhoneThemeListener() {
    this.toggleThemeListener(false);
  }

  // 启动或关闭主题监听器
  toggleThemeListener(enable = true) {
    // 停止主题定时器
    this.stopThemeTimer();

    if (this.systemThemeMediaQuery && this.systemThemeChangeHandler) {
      if (this.systemThemeMediaQuery.removeEventListener) {
        this.systemThemeMediaQuery.removeEventListener(
          "change",
          this.systemThemeChangeHandler,
        );
      }
      this.systemThemeMediaQuery = null;
      this.systemThemeChangeHandler = null;
    }

    if (enable) {
      // 设置系统主题监听（使用具名引用以便正确移除）
      if (window.matchMedia) {
        this.systemThemeMediaQuery = window.matchMedia(
          "(prefers-color-scheme: dark)",
        );
        this.systemThemeChangeHandler = (e) => {
          const themeName = e.matches
            ? this.parseDarkLightTheme()[0]
            : this.parseDarkLightTheme()[1];
          this.applyThemeInternal(themeName);
        };
        this.systemThemeMediaQuery.addEventListener(
          "change",
          this.systemThemeChangeHandler,
        );
      }

      // 启动时间主题定时器（每分钟检查，6点/18点边界切换）
      this.themeTimer = setInterval(() => {
        const currentTheme = this.getThemeByTime();
        if (this.lastThemeName !== currentTheme) {
          this.lastThemeName = currentTheme;
          this.lastThemeUpdate = Date.now();
          this.applyThemeInternal(currentTheme);
        }
      }, THEME_CHECK_INTERVAL_MS);
    }
  }

  /** 将主题配色变量应用到卡片根元素及 documentElement（供全局 tooltip 使用） */
  applyThemeVariables(themeName) {
    // 兼容旧配置：black/light 映射到已存在的主题
    const themeAlias = { black: "transparent", light: "power" };
    const resolvedName = themeAlias[themeName] || themeName;
    const theme =
      EleGasCard.COLOR_SCHEMES[resolvedName] || EleGasCard.COLOR_SCHEMES.power;
    const root = this.electricityCardEl;

    // 应用所有CSS变量到卡片元素
    Object.keys(theme).forEach((key) => {
      root.style.setProperty(key, theme[key]);
    });

    // 同时应用到documentElement，让全局tooltip可以访问这些变量
    if (this.eventTooltip && document.documentElement) {
      Object.keys(theme).forEach((key) => {
        document.documentElement.style.setProperty(key, theme[key]);
      });
    }
  }

  /** 应用主题：移除旧主题类、设置 CSS 变量、添加新主题类 */
  applyThemeInternal(themeName) {
    // 兼容旧配置：black/light 映射到已存在的主题
    const themeAlias = { black: "transparent", light: "power" };
    const resolvedName = themeAlias[themeName] || themeName;
    // 移除所有主题类
    const themeClasses = [
      ...new Set([
        ...Object.keys(EleGasCard.COLOR_SCHEMES || {}),
        "dark",
        "light",
        "black",
        "darkgray",
        "blue",
        "green",
        "red",
        "purple",
        "yellow",
        "cyan",
        "pink",
        "orange",
      ]),
    ];
    this.electricityCardEl.classList.remove(
      ...themeClasses.map((t) => t + "-theme"),
    );
    // 应用主题变量和类（使用解析后的名称，确保类名与 COLOR_SCHEMES 一致）
    this.applyThemeVariables(themeName);
    this.electricityCardEl.classList.add(resolvedName + "-theme");
  }

  /** 处理特殊主题模式：off/on/time/phone，返回主题名与是否需要监听器 */
  handleSpecialThemeMode(theme) {
    const [darkTheme, lightTheme] = this.parseDarkLightTheme();

    if (theme === "off") return { themeName: darkTheme, needsListener: false };
    if (theme === "on") return { themeName: lightTheme, needsListener: false };
    if (theme === "time")
      return { themeName: this.getThemeByTime(), needsListener: true };
    if (theme === "phone")
      return { themeName: this.getSystemTheme(), needsListener: true };

    return null;
  }

  /** 从 HA 实体状态解析主题名（实体 state 为主题名） */
  handleEntityTheme(theme) {
    if (this._hass && this._hass.states[theme]) {
      return this.determineTheme(this._hass.states[theme].state);
    }
    return null;
  }

  /** 根据配置确定最终主题名：支持特殊模式、实体、对象、字符串 */
  determineTheme(theme) {
    // 处理特殊模式
    const specialMode = this.handleSpecialThemeMode(theme);
    if (specialMode) return specialMode.themeName;

    // 处理对象配置
    if (typeof theme === "object" && theme !== null) {
      if (theme.value && EleGasCard.COLOR_SCHEMES[theme.value]) {
        return theme.value;
      }
      if (theme.entity && this._hass) {
        const entityTheme = this.handleEntityTheme(theme.entity);
        if (entityTheme) return entityTheme;
      }
    }

    // 处理字符串（实体或主题名）
    if (typeof theme === "string") {
      // 检查是否是实体
      const entityTheme = this.handleEntityTheme(theme);
      if (entityTheme) return entityTheme;

      // 检查是否是预定义主题
      if (EleGasCard.COLOR_SCHEMES[theme]) {
        return theme;
      }
    }

    return this.parseDarkLightTheme()[1]; // 默认返回亮色主题
  }

  // 根据实体状态确定主题名称（复用determineTheme逻辑）
  determineThemeFromEntityState(entityState, isSelectEntity) {
    // 下拉选择实体：检查是否是预定义的主题名称
    if (isSelectEntity && EleGasCard.COLOR_SCHEMES[entityState]) {
      return entityState;
    }

    // 复用determineTheme逻辑处理特殊值
    return this.determineTheme(entityState);
  }

  // 更新主题配置（优化后的主入口）
  updateTheme(config) {
    const theme = config.theme;

    // 停止所有监听器
    this.toggleThemeListener(false);

    let themeName = "power"; // 默认主题
    let needsListener = false;

    // 未配置或空字符串：使用「自动（根据时间）」模式
    if (
      theme === undefined ||
      theme === "" ||
      (typeof theme === "object" && theme !== null && theme.value === "")
    ) {
      const specialMode = this.handleSpecialThemeMode("time");
      if (specialMode) {
        themeName = specialMode.themeName;
        needsListener = specialMode.needsListener;
      }
      this.lastThemeName = themeName;
    } else if (theme !== undefined) {
      // 处理特殊模式
      const specialMode = this.handleSpecialThemeMode(theme);
      if (specialMode) {
        themeName = specialMode.themeName;
        needsListener = specialMode.needsListener;
      } else {
        themeName = this.determineTheme(theme);
      }
      this.lastThemeName = themeName;
    }

    // 启用监听器（如果需要）
    if (needsListener) {
      this.toggleThemeListener(true);
    }

    // 应用主题
    this.applyThemeInternal(themeName);
  }
  // ==================== 主题相关方法结束 ====================

  /** 构造函数：创建 Shadow DOM、初始化数据结构和 DOM 引用 */
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.appendChild(template.content.cloneNode(true));

    // 初始化统一数据格式存储
    this.standardData = {
      dayUsage: [],
      monthUsage: [],
      yearUsage: [],
      payRecords: [],
      unit: "", // 单位将根据 utility_type 自动设置
    };

    // 默认 utility_type
    this.utilityType = "ele"; // 默认为电力

    // 多用户相关初始化
    this.multiClassConfig = {}; // 多用户配置
    this.currentUserIndex = 0; // 当前用户索引
    this.userCount = 0; // 用户数量
    this.showMultiUserBar = false; // 是否显示多用户切换条
    this.currentUserKey = ""; // 当前用户键
    this.currentConfig = {}; // 当前用户配置
    this.entityId = ""; // 当前实体ID
    this.fieldMapping = {}; // 字段映射
    this.jiaofeiEntityId = null; // 缴费实体ID
    this.jiaofeiFieldMapping = {}; // 缴费实体字段映射

    // 数据缓存相关（1分钟缓存，减少旧数据滞留）
    this._dataCache = new Map(); // 数据缓存，键：{info}_{utility_type}
    this._cacheExpiry = new Map(); // 缓存过期时间
    this._isCardVisible = false; // 卡片可见性
    this._cacheDuration = 1 * 60 * 1000; // 缓存时长：1分钟

    // 余额刷新定时器（10秒）
    this._balanceUpdateInterval = null;

    // 调试信息开关（默认不显示调试信息）
    this.showDebug = false;

    // 只显示一次的调试信息（版本信息，不受showDebug控制）
    if (!window._EleGasCardDebugShown) {
      window._EleGasCardDebugShown = true;

      // 使用新的ECharts加载函数
      loadECharts()
        .then(() => {
          // ECharts加载成功后检测来源
          const scripts = document.querySelectorAll("script[src]");
          let echartsSource = "未知";

          for (const script of scripts) {
            if (script.src.includes("echarts")) {
              if (script.src.includes("/local/")) {
                echartsSource = "本地";
              } else if (script.src.includes("cdn")) {
                echartsSource = "CDN";
              }
              break;
            }
          }

          // 显示卡片信息（字体已调小）
          console.log(
            "%c⚡xjelegas-card\n%cVersion 1.0.0 ｜  ECharts: " + echartsSource,
            `background: #ff5722; color: white; padding: 3px 8px; margin: 0; display: inline-block; text-align: center; font-weight: bold; font-size: 12px; font-family: monospace; line-height: 1.3; border-radius: 3px 3px 0 0;`,
            `background: white; color: #ff5722; padding: 3px 8px; margin: 0; display: inline-block; text-align: center; font-weight: bold; font-size: 11px; font-family: monospace; line-height: 1.3; border-radius: 0 0 3px 3px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);`,
          );
        })
        .catch((err) => {
          // 如果ECharts加载失败，仍然显示调试信息
          console.log(
            "%c⚡xjelegas-card\n%cVersion 1.0.0 ｜  ECharts: 加载失败",
            `background: #ff5722; color: white; padding: 3px 8px; margin: 0; display: inline-block; text-align: center; font-weight: bold; font-size: 12px; font-family: monospace; line-height: 1.3; border-radius: 3px 3px 0 0;`,
            `background: white; color: #ff5722; padding: 3px 8px; margin: 0; display: inline-block; text-align: center; font-weight: bold; font-size: 11px; font-family: monospace; line-height: 1.3; border-radius: 0 0 3px 3px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);`,
          );
          console.error("ECharts加载失败:", err);
        });
    }

    // 存储元素引用
    this.userInfoEl = this.shadowRoot.getElementById("user-info");
    this.multiUserInfoEl = this.shadowRoot.getElementById("multi-user-info");
    this.sliderIndicatorEl = this.shadowRoot.getElementById("slider-indicator");
    this.userBlocks = this.shadowRoot.querySelectorAll(".user-block");
    this.balanceEl = this.shadowRoot.getElementById("balance");
    this.balanceLabelEl = this.shadowRoot.getElementById("balance-label");
    this.dateEl = this.shadowRoot.getElementById("date");
    this.dataDateEl = this.shadowRoot.getElementById("data-date");
    this.relativeDateEl = this.shadowRoot.getElementById("relative-date");
    this.electricityCardEl = this.shadowRoot.querySelector(".elegas-card");
    this.dataContainerEl = this.shadowRoot.getElementById("data-container");

    // 本月用电元素
    this.currentMonthElectricityEl = this.shadowRoot.getElementById(
      "current-month-electricity",
    );
    this.currentMonthCostEl =
      this.shadowRoot.getElementById("current-month-cost");
    this.currentMonthEleUnitEl = this.shadowRoot.getElementById(
      "current-month-ele-unit",
    );
    this.currentMonthCostUnitEl = this.shadowRoot.getElementById(
      "current-month-cost-unit",
    );
    // 上月用电元素
    this.lastMonthElectricityEl = this.shadowRoot.getElementById(
      "last-month-electricity",
    );
    this.lastMonthCostEl = this.shadowRoot.getElementById("last-month-cost");
    this.lastMonthEleUnitEl = this.shadowRoot.getElementById(
      "last-month-ele-unit",
    );
    this.lastMonthCostUnitEl = this.shadowRoot.getElementById(
      "last-month-cost-unit",
    );
    // 年度用电元素
    this.currentYearEl = this.shadowRoot.getElementById("current-year");
    this.yearElectricityEl = this.shadowRoot.getElementById("year-electricity");
    this.yearCostEl = this.shadowRoot.getElementById("year-cost");
    this.yearEleUnitEl = this.shadowRoot.getElementById("year-ele-unit");
    this.yearCostUnitEl = this.shadowRoot.getElementById("year-cost-unit");
    this.electricityPriceEl = this.shadowRoot.getElementById("elegas-price");
    this.priceUnitEl = this.shadowRoot.querySelector(".price-unit");
    this.priceLabelEl = this.shadowRoot.querySelector(".price-label");
    // 新增剩余天数元素引用
    this.remainingDaysEl = this.shadowRoot.getElementById("remaining-days");
    this.remainingDaysDateEl = this.shadowRoot.getElementById(
      "remaining-days-date",
    );

    // 阶梯电价相关元素
    this.tierPeriodEl = this.shadowRoot.getElementById("tier-period");
    this.tier1El = this.shadowRoot.getElementById("tier-1");
    this.tier2El = this.shadowRoot.getElementById("tier-2");
    this.tier3El = this.shadowRoot.getElementById("tier-3");
    this.currentIndicatorEl =
      this.shadowRoot.getElementById("current-indicator");
    this.indicatorArrowEl = this.shadowRoot.getElementById("indicator-arrow");
    this.currentTierEl = this.shadowRoot.getElementById("current-tier");
    this.currentUsageEl = this.shadowRoot.getElementById("current-usage");
    this.currentUsageUnitEl =
      this.shadowRoot.getElementById("current-usage-unit");
    this.tiersContainerEl = this.shadowRoot.querySelector(".tiers-container");

    // 阶梯块中的文字元素
    this.tier1BlockEl = this.shadowRoot.getElementById("tier-1-block");
    this.tier2BlockEl = this.shadowRoot.getElementById("tier-2-block");
    this.tier3BlockEl = this.shadowRoot.getElementById("tier-3-block");

    // 阶梯范围DOM元素
    this.tier1RangeEl = this.shadowRoot.getElementById("tier-1-range");
    this.tier2RangeEl = this.shadowRoot.getElementById("tier-2-range");
    this.tier3RangeEl = this.shadowRoot.getElementById("tier-3-range");
    this.tier1PriceEl = this.shadowRoot.getElementById("tier-1-price");
    this.tier2PriceEl = this.shadowRoot.getElementById("tier-2-price");
    this.tier3PriceEl = this.shadowRoot.getElementById("tier-3-price");

    // 所有tier-content元素
    this.tierContentElements =
      this.shadowRoot.querySelectorAll(".tier-content");

    // 动态文本元素
    this.tierLabelLeftEl = this.shadowRoot.querySelector(".tier-label-left");
    this.monthLabels = this.shadowRoot.querySelectorAll(".month-label");
    this.yearLabels = this.shadowRoot.querySelectorAll(".year-label");

    // 日历视图相关元素
    this.dataContainerEl = this.shadowRoot.getElementById("data-container");
    this.calendarViewEl = this.shadowRoot.getElementById("calendar-view");
    this.calendarHeaderEl = this.shadowRoot.querySelector(".calendar-header");
    this.calendarTabsEl = this.shadowRoot.querySelector(".calendar-tabs");
    this.calendarControlsEl =
      this.shadowRoot.querySelector(".calendar-controls");
    this.calendarGridEl = this.shadowRoot.getElementById("calendar-grid");
    this.calYearSelectEl = this.shadowRoot.getElementById("cal-year-select");
    this.calMonthSelectEl = this.shadowRoot.getElementById("cal-month-select");
    this.currentMonthBtnEl =
      this.shadowRoot.getElementById("current-month-btn");
    this.backToMainBtnEl = this.shadowRoot.getElementById("back-to-main");

    // 日历统计元素
    this.calMonthUsageEl = this.shadowRoot.getElementById("cal-month-usage");
    this.calMonthCostEl = this.shadowRoot.getElementById("cal-month-cost");
    this.calYearUsageEl = this.shadowRoot.getElementById("cal-year-usage");
    this.calYearCostEl = this.shadowRoot.getElementById("cal-year-cost");

    // 年视图元素
    this.calendarContentEl = this.shadowRoot.getElementById("calendar-content");
    this.yearContentEl = this.shadowRoot.getElementById("year-content");
    this.yearComboChartEl = this.shadowRoot.getElementById("year-combo-chart");
    this.yearFilterMainEl = this.shadowRoot.getElementById("year-filter-main");
    this.yearFilterYearsEl =
      this.shadowRoot.getElementById("year-filter-years");
    this.yearChart = null;
    // 年视图数据缓存
    this._yearChartData = null;
    this._daylistData = null;
    this.selectedYearView = "年度"; // 年视图当前选中的按钮：总计、年度、xx年

    // 月视图元素
    this.monthContentEl = this.shadowRoot.getElementById("month-content");
    this.monthComboChartEl =
      this.shadowRoot.getElementById("month-combo-chart");
    this.monthYearFilterEl =
      this.shadowRoot.getElementById("month-year-filter");
    this.monthChart = null;
    this.selectedYear = null; // 当前选中的年份
    // 月视图数据缓存
    this._monthChartData = null;

    // 日视图元素
    this.dayContentEl = this.shadowRoot.getElementById("day-content");
    this.dayComboChartEl = this.shadowRoot.getElementById("day-combo-chart");
    this.dayFilterMainEl = this.shadowRoot.getElementById("day-filter-main");
    this.dayFilterYearsEl = this.shadowRoot.getElementById("day-filter-years");
    this.futureMonthMessageEl = this.shadowRoot.getElementById(
      "future-month-message",
    );
    this.dayViewChart = null;
    this.selectedDayYear = null; // 日视图中当前选中的年份
    this.selectedDayMonth = null; // 日视图中当前选中的月份
    // 日视图数据缓存
    this._dayChartData = null;

    // 日历状态
    this.isCalendarView = false;
    this.currentView = "calendar";
    this.calCurrentYear = new Date().getFullYear();
    this.calCurrentMonth = new Date().getMonth() + 1;
    this.availableYears = [];
    this.daylistData = [];
    this.yearlistData = [];
    this.monthlistData = [];

    // 日详情模态框元素
    this.dayModalEl = this.shadowRoot.getElementById("day-modal");
    this.dayModalTitleEl = this.shadowRoot.getElementById("day-modal-title");
    this.dayModalBodyEl = this.shadowRoot.getElementById("day-modal-body");
    this.dayModalCloseEl = this.shadowRoot.getElementById("day-modal-close");
    this.dayModalContentEl =
      this.shadowRoot.querySelector(".day-modal-content");

    // 缴费历史模态框元素
    this.payHistoryModalEl =
      this.shadowRoot.getElementById("pay-history-modal");
    this.payHistoryTitleEl =
      this.shadowRoot.getElementById("pay-history-title");
    this.payHistoryBodyEl = this.shadowRoot.getElementById("pay-history-body");
    this.payHistoryCloseEl =
      this.shadowRoot.getElementById("pay-history-close");
    this.payHistoryContentEl = this.shadowRoot.querySelector(
      "#pay-history-modal .day-modal-content",
    );

    // 设备事件提示框元素 - 将其移到document.body以避免shadow DOM定位问题
    this.eventTooltip = this.shadowRoot.getElementById("event-tooltip");
    this.tooltipDeviceName = this.shadowRoot.getElementById(
      "tooltip-device-name",
    );
    this.tooltipEventsList = this.shadowRoot.getElementById(
      "tooltip-events-list",
    );

    // 将tooltip从shadow DOM移到document.body，使其成为真正的固定定位弹出窗口
    if (this.eventTooltip) {
      // 添加主题化的tooltip样式到document.head
      const tooltipStyle = document.createElement("style");
      tooltipStyle.textContent = `
        #event-tooltip {
          position: absolute !important;
          background-color: var(--tooltip-bg, var(--card-button-bg));
          color: var(--tooltip-text-color, var(--text-color));
          border: 1px solid var(--tooltip-border-color, var(--button-primary-transparent));
          border-radius: 6px;
          padding: 10px;
          box-shadow: 0 4px 10px rgba(0,0,0,0.3);
          z-index: 99999;
          display: none;
          width: auto;
          min-width: 100px;
          max-width: 300px;
          font-size: 11px;
          line-height: 1.4;
          --arrow-y: 50%;
          max-height: 150px;
          pointer-events: auto !important;
        }
        #event-tooltip strong {
          display: block;
          margin-bottom: 5px;
          color: var(--tooltip-text-color, var(--text-color));
        }
        #event-tooltip ul {
          list-style: none;
          padding: 0;
          margin: 0;
          max-height: 120px;
          overflow-y: auto;
        }
        #event-tooltip ul li {
          margin-bottom: 3px;
          border-bottom: 1px dashed var(--tooltip-border-color, var(--button-primary-transparent));
          padding: 3px 0;
          font-size: 11px;
          transition: background-color 0.2s;
        }
        #event-tooltip ul li.highlighted-event {
          background-color: var(--tooltip-highlight-bg, var(--button-color-active));
          padding: 3px 5px;
          border-radius: 4px;
          border-bottom: none;
        }
        #event-tooltip ul li.highlighted-event span,
        #event-tooltip ul li.highlighted-event {
          color: var(--button-active-text-color);
        }
        #event-tooltip ul li span {
          color: var(--tooltip-text-color, var(--text-color));
          opacity: 0.8;
        }
        #event-tooltip[data-arrow]::after {
          content: '';
          position: absolute;
          top: var(--arrow-y);
          transform: translateY(-50%);
          width: 0;
          height: 0;
          bottom: auto;
          border-color: transparent;
        }
        #event-tooltip[data-arrow="left"]::after {
          left: -7px;
          border-top: 7px solid transparent;
          border-bottom: 7px solid transparent;
          border-right: 7px solid var(--tooltip-bg);
        }
        #event-tooltip[data-arrow="right"]::after {
          right: -7px;
          border-top: 7px solid transparent;
          border-bottom: 7px solid transparent;
          border-left: 7px solid var(--tooltip-bg);
        }
      `;
      document.head.appendChild(tooltipStyle);

      // 将tooltip移到document.body（固定定位，浮动在所有内容之上）
      document.body.appendChild(this.eventTooltip);
      this.eventTooltip.style.display = "none";
    }

    // ECharts相关
    this.echartsLoaded = false;
    this.echartsPath = "/xjelegas/echarts.min.js";
    this.echartsFallbackPath =
      "https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js";
    this.dayChart = null;
    this.echartsScriptElement = null;

    // 设备事件数据
    this.deviceEvents = [];

    // 默认配置
    this.defaultConfig = {
      // 背景色配置（由主题系统控制）
      // background: null,
      // 主题配置:
      // - 不配置时: 使用power主题
      // - 配置为"time": 根据时间自动切换主题 (白天使用dark_light_theme配置的亮色主题(默认light)，晚上使用dark_light_theme配置的暗色主题(默认black))
      // - 配置为"phone": 跟随手机系统主题自动切换 (亮色使用dark_light_theme配置的亮色主题(默认light)，暗色使用dark_light_theme配置的暗色主题(默认black))
      // - 开关实体配置: 仅在dark_light_theme配置的亮色主题和暗色主题之间切换 (默认light和black)
      // - 下拉选择实体配置: 根据实体值切换对应主题 (light、dark、transparent、blue、green、red、purple、yellow、cyan、pink、orange)
      // - 特殊值 "time": 根据时间自动切换主题
      // - 特殊值 "phone": 跟随手机系统主题
      theme: undefined,
      // 暗色和亮色主题配置:
      // - 当theme配置为time或phone，或者使用开关实体时，使用该配置的主题进行切换
      // - 配置格式: "暗色主题,亮色主题"，使用逗号分隔，例如: "black,red"
      // - 不配置时默认使用"black,light" (暗色black，亮色light)
      // - 可选值: dark、black、transparent、blue、green、red、purple、yellow、cyan、pink、orange、light
      dark_light_theme: undefined,
      // 设备实体配置（用于显示设备使用情况）
      device_entity: null,
      // 隐藏配置：可以隐藏指定的UI组件，支持多个值，用逗号分隔
      // 可选值：
      // - price-display: 隐藏所有价格显示区域
      // - elegas-price-display: 隐藏电价显示区域
      // - remaining-days-display: 隐藏剩余天数显示区域
      // - tier-indicator: 隐藏用电阶梯指示器
      // - data-container: 隐藏统计数据容器（本月/上月/年度统计）
      // - user-info: 隐藏用户信息标题
      // 可以同时填写多个，用逗号分隔，例如：'tier-indicator,time-distribution-bar'
      hide: "",
    };

    // 阶梯电价配置（初始化为空，将由配置文件或实体数据填充）
    this.tierConfig = {
      tiers: [
        { id: 1, max: null, price: null, color: "#55c593", title: "第一阶梯" },
        { id: 2, max: null, price: null, color: "#f8c337", title: "第二阶梯" },
        {
          id: 3,
          max: Infinity,
          price: null,
          color: "#f79335",
          title: "第三阶梯",
        },
      ],
      // 阶梯电价计算周期 (默认: 7月1日到6月30日，但仅用于非电力类型)
      periodStartMonth: null, // 开始月份
      periodStartDay: null, // 开始日
      periodEndMonth: null, // 结束月份
      periodEndDay: null, // 结束日
    };

    // 初始化变量
    this._hass = null;
    this._config = null;
    this.currentPeriodUsage = 0; // 当前周期用电量
    this.currentTier = 1; // 当前阶梯
    this.lastUpdateTime = 0; // 上次更新时间戳
    this.updateInterval = 60 * 60 * 1000; // 60分钟节流更新间隔（实体无变化时）
    this.deviceEntityConfig = null; // 设备实体配置
    this.themeTimer = null; // 主题定时器
    this.lastThemeUpdate = 0; // 上次主题更新时间
    this.historicalData = null; // 历史数据缓存
    this.historicalDataLoaded = false; // 历史数据是否已加载完成
    this.paySourceChart = null; // 缴费方式饼图实例
  }

  /**
   * 接收并应用卡片配置，由 HA 在配置加载或变更时调用
   * 负责解析 multiclass、主题、实体等配置并初始化卡片状态
   */
  setConfig(config) {
    // 检查并设置默认的multiclass配置
    if (!config.multiclass || Object.keys(config.multiclass).length === 0) {
      // 如果没有配置multiclass，创建一个默认的用户配置
      // 尝试使用顶层配置中的entity
      const defaultEntity = config.entity || "";
      const defaultUtilityType = config.utility_type || "ele";

      // 修改config对象（注意：直接修改传入的config对象可能会影响HA的其他部分，最好是深拷贝一份，但这里为了简单起见，我们修改this._config）
      // 默认使用数字ID（1、2、3...），不用 default_user
      config.multiclass = {
        1: {
          info: defaultUtilityType === "gas" ? "燃气户号" : "用电户号",
          entity: defaultEntity,
          utility_type: defaultUtilityType,
        },
      };

      console.log(
        `xjelegas-card: 未检测到multiclass配置，已自动创建默认${defaultUtilityType === "gas" ? "燃气" : "电力"}用户配置`,
      );
    }

    this._config = config;

    // 保存顶层配置
    this.topName = config.name || ""; // 顶层的名称
    this.showName = config.show_name !== false; // 是否显示名称，默认为true

    // 读取调试开关配置（默认为false，不显示调试信息）
    this.showDebug = config.show_debug === true;

    // 初始化多用户配置
    this.multiClassConfig = config.multiclass;
    this.userCount = Object.keys(this.multiClassConfig).length;

    // 从 localStorage 恢复上次选中的用户（刷新后保持用户选择）
    const storageKey = this._getUserIndexStorageKey();
    if (storageKey && this.userCount >= 2) {
      try {
        const saved = parseInt(localStorage.getItem(storageKey), 10);
        if (!isNaN(saved) && saved >= 0 && saved < this.userCount) {
          this.currentUserIndex = saved;
        } else {
          this.currentUserIndex = 0;
        }
      } catch (_) {
        this.currentUserIndex = 0;
      }
    } else {
      this.currentUserIndex = 0;
    }

    // 根据用户数量设置单类样式（调整背景图标位置）
    this.updateSingleClassStyle();

    // 根据用户数量决定是否显示切换条
    this.showMultiUserBar = this.userCount >= 2;

    // 初始化当前用户配置
    this.initializeCurrentUserConfig();

    // 初始化ECharts（仅在首次加载时）
    if (!this.echartsInitialized) {
      this.initializeECharts();
      this.echartsInitialized = true;
    }

    // 应用隐藏配置
    this.applyHiddenConfig();

    // 初始化日历事件监听
    this.initCalendarEvents();

    // 设置主题相关的监听器
    this.setupThemeListeners();
  }

  /** 调试日志（仅当 showDebug 为 true 时输出） */
  debugLog(...args) {
    if (this.showDebug) {
      console.log(...args);
    }
  }

  /** 调试警告（仅当 showDebug 为 true 时输出） */
  debugWarn(...args) {
    if (this.showDebug) {
      console.warn(...args);
    }
  }

  /** 调试错误（仅当 showDebug 为 true 时输出） */
  debugError(...args) {
    if (this.showDebug) {
      console.error(...args);
    }
  }

  /** 从实体获取「更新时间」显示值（用于判断数据是否变化，决定是否刷新） */
  _getEntityUpdateTime(entity) {
    if (!entity?.attributes) return undefined;
    const key = this.fieldMapping?.syn || (this.utilityType === "ele" ? "date" : "syn");
    return this.getEntityAttribute(entity, key);
  }

  /** 生成用于持久化用户选择的 localStorage 键（按卡片配置唯一） */
  _getUserIndexStorageKey() {
    if (!this.multiClassConfig || Object.keys(this.multiClassConfig).length === 0)
      return null;
    const entityIds = Object.values(this.multiClassConfig)
      .map((c) => (c.entity || "").split(",")[0].trim())
      .filter(Boolean)
      .sort();
    if (entityIds.length === 0) return null;
    return `xjelegas-card-user-index-${entityIds.join(",")}`;
  }

  /** 根据用户数量更新单类样式：单用户时添加 single-class 以调整背景图标位置 */
  updateSingleClassStyle() {
    if (!this.electricityCardEl) return;

    if (this.userCount === 1) {
      this.electricityCardEl.classList.add("single-class");
    } else {
      this.electricityCardEl.classList.remove("single-class");
    }
  }

  /** 初始化当前选中用户的配置：实体、缴费、阶梯、主题、多用户条等 */
  initializeCurrentUserConfig() {
    const userKeys = Object.keys(this.multiClassConfig);
    const currentKey = userKeys[this.currentUserIndex];
    const currentConfig = this.multiClassConfig[currentKey];

    if (!currentConfig) {
      throw new Error("无法获取当前用户配置");
    }

    // 设置当前用户信息
    this.currentUserKey = currentKey;
    this.currentConfig = currentConfig;

    // 更新 user-info 显示（使用顶层配置的 name）
    if (this.userInfoEl && this.topName) {
      this.userInfoEl.textContent = this.topName;
    }

    // 根据 show_name 控制 user-info 和 multi-user-info 的显示
    if (this.userInfoEl && this.multiUserInfoEl) {
      if (this.showName) {
        // 显示名称
        this.userInfoEl.classList.remove("hidden");
        this.multiUserInfoEl.classList.remove("hidden");
      } else {
        // 隐藏名称
        this.userInfoEl.classList.add("hidden");
        this.multiUserInfoEl.classList.add("hidden");
      }
    }

    // 读取 utility_type 配置（默认为 'ele'）
    this.utilityType = currentConfig.utility_type || "ele";

    // 根据 utility_type 设置单位
    this.standardData.unit = this.getUnitByUtilityType(this.utilityType);

    // 根据 utility_type 更新动态文本
    this.updateDynamicTexts(this.utilityType);

    // 根据 utility_type 更新背景图标
    this.updateCardBackgroundIcon(this.utilityType);

    // 解析实体配置（支持字段映射）
    this.parseEntityConfig(currentConfig);

    // 解析缴费实体配置
    this.parseJiaofeiConfig(currentConfig);

    // 当 utility_type 不是 'ele' 时，才使用手动配置的阶梯电价和计费周期
    // 'ele' 类型完全从实体获取数据，不使用任何手动配置
    if (this.utilityType !== "ele") {
      // 更新阶梯电价配置和计费周期配置（仅非电力类型使用）
      this.updateTierConfig(currentConfig);
      this.updatePeriodConfig(currentConfig);
    }

    // 更新背景色配置
    this.updateBackgroundConfig(currentConfig);

    // 更新卡片宽度配置（从顶层配置读取）
    this.updateCardWidth();

    // 保存设备实体配置
    this.deviceEntityConfig = currentConfig.device_entity || null;

    // 初始化多用户切换条
    this.setupMultiUserBar();

    // 初始化所有用户的余额显示
    this.updateAllUsersBalance();

    // 应用当前用户的隐藏配置
    this.applyHiddenConfig();

    // 确保当前用户块的 active 状态正确设置（首次进入卡片时）
    this.ensureCurrentUserActive();

    // 如果卡片可见，加载数据
    if (this._isCardVisible) {
      this.loadDataForCurrentUser();
    }
  }

  /** 解析实体配置：支持 entity 或 entity,[fieldMapping] 语法，提取 entityId 与 fieldMapping */
  parseEntityConfig(config) {
    const entityStr = config.entity || "";

    // 解析实体ID和字段映射（支持逗号前后有空格）
    const entityMatch = entityStr.match(/^([^,\s[]+)(?:\s*,\s*\[(.+)\])?$/);
    if (entityMatch) {
      this.entityId = entityMatch[1].trim();
      this.fieldMapping = {};

      // 解析字段映射
      if (entityMatch[2]) {
        const mappingStr = entityMatch[2];
        const mappings = mappingStr.split(",");

        mappings.forEach((mapping) => {
          const kv = mapping.trim().split(":");
          if (kv.length === 2) {
            const key = kv[0].trim();
            const value = kv[1].trim();
            this.fieldMapping[key] = value;
          }
        });
      }
    } else {
      this.entityId = entityStr.trim();
      this.fieldMapping = {};
    }

    // 根据 utility_type 设置默认字段映射（如果没有自定义映射）
    if (
      this.utilityType === "gas" &&
      Object.keys(this.fieldMapping).length === 0
    ) {
      // 燃气默认映射（对应集成 sensor.py 中的数据结构）
      this.fieldMapping = {
        daylist: "daylist",
        monthlist: "monthlist",
        yearlist: "yearlist",
        date: "readingTime",
        usage: "usage",
        amount: "amount",
        syn: "syn",
        latest_data: "latest_data",
      };
    }
    if (
      this.utilityType === "ele" &&
      Object.keys(this.fieldMapping).length === 0
    ) {
      // 电力默认映射（对应 ele/sensor.py 中的 daylist 结构：day, dayEleNum, dayEleCost）
      this.fieldMapping = {
        daylist: "daylist",
        monthlist: "monthlist",
        yearlist: "yearlist",
        date: "day",
        usage: "dayEleNum",
        amount: "dayEleCost",
        syn: "date",
        latest_data: "date",
      };
    }
  }

  /** 解析缴费实体配置：从 jiaofei_entity 提取实体 ID 与字段映射 */
  parseJiaofeiConfig(config) {
    const jiaofeiStr = config.jiaofei_entity || "";
    this.jiaofeiEntityId = null;
    this.jiaofeiFieldMapping = {};

    if (jiaofeiStr) {
      // 解析缴费实体ID和字段映射（支持逗号前后有空格）
      const match = jiaofeiStr.match(/^([^,\s[]+)(?:\s*,\s*\[(.+)\])?$/);
      if (match) {
        this.jiaofeiEntityId = match[1].trim();

        // 解析字段映射
        if (match[2]) {
          const mappingStr = match[2];
          const mappings = mappingStr.split(",");

          mappings.forEach((mapping) => {
            const kv = mapping.trim().split(":");
            if (kv.length === 2) {
              const key = kv[0].trim();
              const value = kv[1].trim();
              this.jiaofeiFieldMapping[key] = value;
            }
          });
        }
      } else {
        this.jiaofeiEntityId = jiaofeiStr.trim();
        this.jiaofeiFieldMapping = {};
      }
    }
  }

  /** 加载当前用户数据：从 HA 实体获取并合并历史数据，更新 standardData */
  loadDataForCurrentUser() {
    if (!this._hass || !this.entityId) {
      return Promise.resolve();
    }

    const cacheKey = this.getCurrentCacheKey();

    // 检查缓存是否有效
    if (this.isCacheValid(cacheKey)) {
      // 使用缓存数据
      const cachedData = this._dataCache.get(cacheKey);
      this.processEntityData(cachedData);
      return Promise.resolve();
    }

    // 从实体获取数据
    const entity = this._hass.states[this.entityId];
    if (
      !entity ||
      entity.state === "unknown" ||
      entity.state === "unavailable"
    ) {
      // 实体不存在或状态为unknown/unavailable时，设置默认数据并更新卡片显示
      this.setDefaultData();
      // 更新所有用户的余额显示为"--"
      this.updateAllUsersBalance();
      return Promise.resolve();
    }

    // 根据字段映射获取数据
    const entityData = this.extractDataFromEntity(entity);

    // 加载并合并历史数据，返回 Promise
    return this.loadAndMergeData(entityData).then(() => {
      // 缓存数据
      this._dataCache.set(cacheKey, entityData);
      this._cacheExpiry.set(cacheKey, Date.now() + this._cacheDuration);

      // 确保切换用户后更新卡片显示（重要：解决切换用户时数据不显示的问题）
      this.updateCard();
    });
  }

  // 设置默认数据（当实体不存在时使用）
  setDefaultData() {
    // 设置默认的实体数据
    const defaultEntityData = {
      yearlist: [],
      monthlist: [],
      daylist: [],
      syn: "未知",
      latest_data: "未知",
    };

    // 处理默认数据
    this.processEntityData(defaultEntityData);

    // 更新卡片显示
    this.updateCardWithDefaultData();
  }

  // 使用默认数据更新卡片显示
  updateCardWithDefaultData() {
    // 设置余额为0
    if (this.balanceEl) {
      this.balanceEl.textContent = "0.00";
    }

    // 设置日期信息
    if (this.dateEl) {
      this.dateEl.textContent = "更新时间 未知";
    }

    if (this.dataDateEl) {
      this.dataDateEl.textContent = "数据: 未知";
    }

    // 设置默认的用电数据
    if (this.currentMonthEleEl) {
      this.currentMonthEleEl.textContent = "0";
    }

    if (this.currentMonthCostEl) {
      this.currentMonthCostEl.textContent = "0";
    }

    if (this.lastMonthEleEl) {
      this.lastMonthEleEl.textContent = "0";
    }

    if (this.lastMonthCostEl) {
      this.lastMonthCostEl.textContent = "0";
    }

    if (this.yearEleEl) {
      this.yearEleEl.textContent = "0";
    }

    if (this.yearCostEl) {
      this.yearCostEl.textContent = "0";
    }
  }

  // 获取当前缓存键
  getCurrentCacheKey() {
    const info = this.currentUserKey || "default";
    const utilityType = this.utilityType || "ele";
    return `${info}_${utilityType}`;
  }

  // 检查缓存是否有效
  isCacheValid(cacheKey) {
    if (!this._dataCache.has(cacheKey)) return false;

    const expiryTime = this._cacheExpiry.get(cacheKey);
    if (!expiryTime) return false;

    return Date.now() < expiryTime;
  }

  /** 从 HA 实体 attributes 中提取数据，支持 fieldMapping 字段映射 */
  extractDataFromEntity(entity) {
    const attributes = entity.attributes || {};
    const mapping = this.fieldMapping || {};

    // 【重要】优先判断是否使用data节点（电力数据可能在data节点下）
    const useDataNode = this.isUsingDataNode(entity);

    // 判断是否有node（有node表示数据在attributes的某个节点下）
    const hasNode = mapping.node;
    // 确定实际的数据源：如果使用data节点，则从attributes.data获取，否则从attributes获取
    const attributesSource = useDataNode
      ? attributes.data || attributes
      : attributes;
    const dataNode = hasNode
      ? attributesSource[mapping.node]
      : attributesSource;

    // 提取数据（如果无node，则直接从attributesSource取）
    const rawData = hasNode ? dataNode : attributesSource;

    // 根据字段映射转换数据
    let convertedData;

    // 如果 rawData 本身就是数组（说明我们已经定位到具体节点，如 daylist）
    if (hasNode && Array.isArray(rawData)) {
      const nodeKey = mapping.node; // 例如 'daylist'

      convertedData = {
        yearlist: [],
        monthlist: [],
        daylist: [],
        syn: this.getEntityAttribute(entity, mapping.syn),
        latest_data: this.getEntityAttribute(entity, mapping.latest_data),
      };

      // 根据 node 类型设置对应的数组
      if (nodeKey === "daylist") {
        convertedData.daylist = rawData;
      } else if (nodeKey === "monthlist") {
        convertedData.monthlist = rawData;
      } else if (nodeKey === "yearlist") {
        convertedData.yearlist = rawData;
      }
    } else {
      // 正常情况：从对象中提取各个字段
      convertedData = {
        yearlist: this.extractArrayData(
          rawData,
          mapping,
          "yearlist",
          "yearlist",
        ),
        monthlist: this.extractArrayData(
          rawData,
          mapping,
          "monthlist",
          "monthlist",
        ),
        daylist: this.extractArrayData(rawData, mapping, "daylist", "daylist"),
        syn: this.getEntityAttribute(entity, mapping.syn),
        latest_data: this.getEntityAttribute(entity, mapping.latest_data),
      };
    }

    // 如果 utility_type 不是 'ele' 且没有 monthlist/yearlist，从 daylist 自动计算
    if (
      this.utilityType !== "ele" &&
      convertedData.daylist &&
      convertedData.daylist.length > 0
    ) {
      // 如果没有 monthlist，从 daylist 计算
      if (!convertedData.monthlist || convertedData.monthlist.length === 0) {
        convertedData.monthlist = this.calculateMonthlyData(
          convertedData.daylist,
          mapping,
        );
      }

      // 如果没有 yearlist，从 daylist 计算
      if (!convertedData.yearlist || convertedData.yearlist.length === 0) {
        convertedData.yearlist = this.calculateYearlyData(
          convertedData.daylist,
          mapping,
        );
      }
    }

    // 如果配置了syn字段，显示date-info
    if (mapping.syn && this.dateEl) {
      const synValue = this.getEntityAttribute(entity, mapping.syn);
      if (synValue !== undefined) {
        this.dateEl.textContent = `更新时间 ${synValue}`;
      }
    } else if (this.utilityType === "ele" && this.dateEl) {
      // 电力类型：显示同步时间
      // 优先从 entity 的顶层节点的 date 字段获取
      let date = attributes.date;

      // 如果获取不到，再从 entity 的 data 节点的 date 字段获取（data.date）
      if (!date) {
        try {
          date = attributes.data?.date || "";
        } catch (e) {
          date = "";
        }
      }

      // 显示为"同步：yyyy-mm-dd hh:mm:ss"
      this.dateEl.textContent = `同步: ${date}`;
    } else if (this.utilityType !== "ele" && this.dateEl) {
      // 非电力类型：显示为"同步: yyyy-mm-dd hh:mm:ss"，从entity的date字段获取
      const dateValue = this.getEntityAttribute(entity, "date");
      if (dateValue !== undefined) {
        this.dateEl.textContent = `同步: ${dateValue}`;
      }
    }

    // 电力类型：数据日期必须来自 daylist 最新日用电日期，而非刷新时间（entity.date）
    if (this.utilityType === "ele" && this.dataDateEl) {
      const daylist = convertedData.daylist || [];
      const dateField = mapping.date || "day";
      const dataDate =
        daylist && daylist.length > 0 && daylist[0][dateField]
          ? daylist[0][dateField]
          : "--";
      this.dataDateEl.textContent = `数据: ${dataDate}`;
      this.updateRelativeDate(dataDate);
      if (dataDate === "--" && this.relativeDateEl) {
        this.relativeDateEl.textContent = "";
        this.relativeDateEl.className = "relative-date-info";
      }
    } else if (
      mapping.latest_data &&
      this.utilityType !== "ele" &&
      this.dataDateEl
    ) {
      // 非电力类型：若配置了 latest_data 则使用，不可用时回退到 daylist
      const latestDataValue = this.getEntityAttribute(
        entity,
        mapping.latest_data,
      );
      if (latestDataValue !== undefined) {
        this.dataDateEl.textContent = `数据:${latestDataValue}`;
        this.updateRelativeDate(latestDataValue);
      } else {
        const daylist = convertedData.daylist || [];
        if (daylist && daylist.length > 0) {
          const latestDayData = daylist[0];
          const dateField = mapping.date || "day";
          const dataDate = latestDayData[dateField] || "--";
          this.dataDateEl.textContent = `数据: ${dataDate}`;
          this.updateRelativeDate(dataDate);
        } else {
          this.dataDateEl.textContent = "数据: --";
          if (this.relativeDateEl) {
            this.relativeDateEl.textContent = "";
            this.relativeDateEl.className = "relative-date-info";
          }
        }
      }
    } else if (this.utilityType !== "ele" && this.dataDateEl) {
      // 非电力类型：显示为"数据: yyyy-mm-dd"，从entity的daylist字段中的最新day处获取
      const daylist = convertedData.daylist || [];
      if (daylist && daylist.length > 0) {
        // 获取最新的day数据（第一条是最新的）
        const latestDayData = daylist[0];
        const dateField = mapping.date || "day";
        const dataDate = latestDayData[dateField] || "--";
        this.dataDateEl.textContent = `数据: ${dataDate}`;
        // 计算并显示相对日期
        this.updateRelativeDate(dataDate);
      } else {
        this.dataDateEl.textContent = "数据: --";
        // 清空相对日期
        if (this.relativeDateEl) {
          this.relativeDateEl.textContent = "";
          this.relativeDateEl.className = "relative-date-info";
        }
      }
    }

    return convertedData;
  }

  // 提取数组数据
  extractArrayData(rawData, mapping, defaultKey, targetKey) {
    // 如果 rawData 本身就是数组，直接返回（例如燃气数据：daylist 节点直接是数组）
    if (Array.isArray(rawData)) {
      return rawData;
    }

    const value = rawData[mapping[targetKey] || defaultKey];
    if (Array.isArray(value)) {
      return value;
    }

    // 如果是字符串，尝试解析为JSON
    if (typeof value === "string") {
      try {
        return JSON.parse(value);
      } catch (e) {
        return [];
      }
    }

    return [];
  }

  // 从日数据计算月合计数据
  calculateMonthlyData(daylist, mapping) {
    if (!daylist || !Array.isArray(daylist) || daylist.length === 0) {
      return [];
    }

    const monthlyMap = new Map();
    const dateField = mapping.date || "day";
    // 根据 utility_type 动态获取默认字段名
    const usageField =
      mapping.usage || (this.utilityType === "ele" ? "dayEleNum" : "usage");
    const amountField =
      mapping.amount || (this.utilityType === "ele" ? "dayEleCost" : "amount");

    daylist.forEach((item) => {
      const dateStr = item[dateField];
      if (!dateStr) return;

      // 处理日期字符串，移除可能的双引号
      const cleanDateStr = dateStr.replace(/"/g, "");
      const date = new Date(cleanDateStr);
      if (isNaN(date.getTime())) return;

      const monthKey = `${date.getFullYear()}-${pad2(date.getMonth() + 1)}`;

      const usage = this.safeParseFloat(item[usageField]);
      const amount = this.safeParseFloat(item[amountField]);

      if (!monthlyMap.has(monthKey)) {
        monthlyMap.set(monthKey, {
          month: monthKey,
          monthEleNum: 0,
          monthEleCost: 0,
          unit: this.getUnitByUtilityType(this.utilityType),
        });
      }

      const monthData = monthlyMap.get(monthKey);
      monthData.monthEleNum += usage;
      monthData.monthEleCost += amount;
    });

    // 转换为数组并保留两位小数
    const result = Array.from(monthlyMap.values()).map((item) => ({
      ...item,
      monthEleNum: parseFloat((item.monthEleNum || 0).toFixed(2)),
      monthEleCost: parseFloat((item.monthEleCost || 0).toFixed(2)),
    }));

    // 按月份排序（降序）
    return result.sort((a, b) => b.month.localeCompare(a.month));
  }

  // 从日数据计算年合计数据
  calculateYearlyData(daylist, mapping) {
    if (!daylist || !Array.isArray(daylist) || daylist.length === 0) {
      return [];
    }

    const yearlyMap = new Map();
    const dateField = mapping.date || "day";
    // 根据 utility_type 动态获取默认字段名
    const usageField =
      mapping.usage || (this.utilityType === "ele" ? "dayEleNum" : "usage");
    const amountField =
      mapping.amount || (this.utilityType === "ele" ? "dayEleCost" : "amount");

    daylist.forEach((item) => {
      const dateStr = item[dateField];
      if (!dateStr) return;

      // 处理日期字符串，移除可能的双引号
      const cleanDateStr = dateStr.replace(/"/g, "");
      const date = new Date(cleanDateStr);
      if (isNaN(date.getTime())) return;

      const yearKey = date.getFullYear().toString();

      const usage = this.safeParseFloat(item[usageField]);
      const amount = this.safeParseFloat(item[amountField]);

      if (!yearlyMap.has(yearKey)) {
        yearlyMap.set(yearKey, {
          year: yearKey,
          yearEleNum: 0,
          yearEleCost: 0,
          unit: this.getUnitByUtilityType(this.utilityType),
        });
      }

      const yearData = yearlyMap.get(yearKey);
      yearData.yearEleNum += usage;
      yearData.yearEleCost += amount;
    });

    // 转换为数组并保留两位小数
    const result = Array.from(yearlyMap.values()).map((item) => ({
      ...item,
      yearEleNum: parseFloat((item.yearEleNum || 0).toFixed(2)),
      yearEleCost: parseFloat((item.yearEleCost || 0).toFixed(2)),
    }));

    // 按年份排序（降序）
    return result.sort((a, b) => b.year.localeCompare(a.year));
  }

  // 加载并合并数据（包括JSON文件）
  async loadAndMergeData(entityData) {
    // 转换实体数据为标准格式
    const user = this.getCurrentCacheKey();
    this.standardData = {
      dayUsage: this.convertDayList(entityData.daylist || [], user),
      monthUsage: this.convertMonthList(entityData.monthlist || [], user),
      yearUsage: this.convertYearList(entityData.yearlist || [], user),
      payRecords: [],
      unit: this.getUnitByUtilityType(this.utilityType),
    };

    // 保存数据到实例变量
    this.daylistData = entityData.daylist || [];
    this.monthlistData = entityData.monthlist || [];
    this.yearlistData = entityData.yearlist || [];

    // 标记历史数据已加载
    this.historicalDataLoaded = true;

    // 更新卡片显示
    this.updateCard();
  }

  // 处理实体数据（从缓存）
  processEntityData(entityData) {
    // 转换数据（与loadAndMergeData类似，但不加载历史数据）
    const user = this.getCurrentCacheKey();
    this.standardData = {
      dayUsage: this.convertDayList(entityData.daylist || [], user),
      monthUsage: this.convertMonthList(entityData.monthlist || [], user),
      yearUsage: this.convertYearList(entityData.yearlist || [], user),
      payRecords: [],
      unit: this.getUnitByUtilityType(this.utilityType),
    };

    // 保存数据到实例变量
    this.daylistData = entityData.daylist || [];
    this.monthlistData = entityData.monthlist || [];
    this.yearlistData = entityData.yearlist || [];

    // 标记数据已加载（重要：确保updateCard可以正常显示数据）
    this.historicalDataLoaded = true;

    // 更新卡片显示
    this.updateCard();
  }

  /** 元素挂载到 DOM 时调用，初始化可见性观察、余额刷新等 */
  connectedCallback() {
    this._isCardVisible = true;

    // 设置IntersectionObserver来检测卡片可见性
    this._setupVisibilityObserver();

    // 如果配置已设置，初始化余额显示
    if (this._config) {
      this.updateAllUsersBalance();
    }

    // 如果配置已设置且数据未加载，加载数据
    if (this._config && this.currentConfig && !this.historicalDataLoaded) {
      this.loadDataForCurrentUser();
    }

    // 数据每日更新，不启动定时轮询，仅随实体变化刷新
  }

  /** 使用 IntersectionObserver 检测卡片是否在视口内，用于按需加载数据 */
  _setupVisibilityObserver() {
    if (!window.IntersectionObserver) return;

    if (this._visibilityObserver) {
      this._visibilityObserver.disconnect();
    }

    this._visibilityObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        this._isCardVisible = entry.isIntersecting;

        // 如果卡片变为可见且需要加载数据，则加载
        if (
          this._isCardVisible &&
          this._config &&
          this.currentConfig &&
          !this.historicalDataLoaded
        ) {
          this.loadDataForCurrentUser();
        }
      });
    });

    this._visibilityObserver.observe(this);
  }

  /** 元素从 DOM 移除时调用，清理定时器、观察器等资源 */
  disconnectedCallback() {
    this._isCardVisible = false;

    // 移除tooltip点击监听
    if (this._tooltipClickHandler) {
      document.removeEventListener("click", this._tooltipClickHandler);
      this._tooltipClickHandler = null;
    }

    // 清理所有定时器和监听器
    this.stopThemeTimer();
    this.stopPhoneThemeListener();

    // 停止余额刷新定时器
    this.stopBalanceUpdateTimer();

    // 清除年/月视图数据检查定时器
    if (this._viewCheckIntervals) {
      this._viewCheckIntervals.forEach((id) => clearInterval(id));
      this._viewCheckIntervals = [];
    }

    // 清除设备运行事件定时器
    this.clearOngoingEventTimers();

    // 销毁所有图表
    this.destroyCharts();

    // 断开可见性观察器
    if (this._visibilityObserver) {
      this._visibilityObserver.disconnect();
      this._visibilityObserver = null;
    }
  }

  // 启动余额刷新定时器（10秒）
  startBalanceUpdateTimer() {
    if (this._balanceUpdateInterval) return;

    this._balanceUpdateInterval = setInterval(() => {
      if (this._isCardVisible && this._hass && this.entityId) {
        this.updateBalanceDisplay();
      }
    }, BALANCE_UPDATE_INTERVAL_MS);
  }

  // 停止余额刷新定时器
  stopBalanceUpdateTimer() {
    if (this._balanceUpdateInterval) {
      clearInterval(this._balanceUpdateInterval);
      this._balanceUpdateInterval = null;
    }
  }

  // 更新余额显示（当前用户）
  updateBalanceDisplay() {
    if (!this._hass || !this.entityId || !this.balanceEl) return;

    const entity = this._hass.states[this.entityId];
    if (entity) {
      // 优先从entity.state获取余额，如果state不是数值，则尝试从data节点获取
      let balance = parseFloat(entity.state);
      if (isNaN(balance)) {
        // 如果state不是数值，尝试从data.balance获取
        balance = parseFloat(this.getEntityAttribute(entity, "balance")) || 0;
      }
      this.balanceEl.textContent = (balance || 0).toFixed(2);
    } else {
      // 实体不存在，设置默认余额
      this.balanceEl.textContent = "0.00";
    }

    // 同时更新所有用户的余额（包括当前用户）
    this.updateAllUsersBalance();
  }

  // 更新所有用户的余额显示
  updateAllUsersBalance() {
    if (!this._hass || !this.showMultiUserBar || !this.userBlocks) return;

    const userKeys = Object.keys(this.multiClassConfig);

    userKeys.forEach((key, index) => {
      const config = this.multiClassConfig[key];
      const entityId = config.entity ? config.entity.split(",")[0].trim() : "";

      if (!entityId) return;

      const entity = this._hass.states[entityId];
      const userBlock = this.userBlocks[index];

      if (userBlock) {
        const balanceDiv = userBlock.querySelector(".user-block-balance");
        if (balanceDiv) {
          if (
            entity &&
            entity.state !== "unknown" &&
            entity.state !== "unavailable"
          ) {
            // 实体存在且状态正常，显示实际余额
            const balance = parseFloat(entity.state);
            balanceDiv.textContent = `¥${(balance || 0).toFixed(2)}`;
          } else {
            // 实体不存在或状态为unknown/unavailable，显示"--"
            balanceDiv.textContent = "--";
          }
        }
      }
    });
  }

  // 更新卡片宽度配置
  updateCardWidth() {
    // 从顶层配置获取卡片宽度，如果未配置则自适应宽度
    const cardWidth = this._config.card_width;

    if (cardWidth) {
      // 如果配置了具体的宽度，则使用该宽度
      this.electricityCardEl.style.maxWidth = cardWidth;
      this.electricityCardEl.style.width = cardWidth;
      // 模态框内容宽度为卡片宽度的95%
      this.dayModalContentEl.style.width = `calc(${cardWidth} * 0.95)`;
    } else {
      // 如果未配置宽度，则恢复自适应行为（移除最大宽度限制）
      this.electricityCardEl.style.maxWidth = "none";
      this.electricityCardEl.style.width = "auto";
      // 模态框内容恢复默认的90%
      this.dayModalContentEl.style.width = "90%";
    }
  }

  // 重置所有隐藏元素的显示状态（切换用户时调用）
  resetHiddenElements() {
    // 重置 price-display
    const priceDisplays = this.shadowRoot.querySelectorAll(".price-display");
    priceDisplays.forEach((el) => (el.style.display = ""));

    // 重置 elegas-price-display
    const electricityPriceDisplay = this.shadowRoot.querySelector(
      ".elegas-price-display",
    );
    if (electricityPriceDisplay) electricityPriceDisplay.style.display = "";

    // 重置 remaining-days-display
    const remainingDaysDisplay = this.shadowRoot.querySelector(
      ".remaining-days-display",
    );
    if (remainingDaysDisplay) remainingDaysDisplay.style.display = "";

    // 重置 tier-indicator
    const tierIndicator = this.shadowRoot.querySelector(".tier-indicator");
    const tierIndicatorContainer = this.shadowRoot.querySelector(
      ".tier-indicator-container",
    );
    if (tierIndicator) tierIndicator.style.display = "";
    if (tierIndicatorContainer) tierIndicatorContainer.style.display = "";

    // 重置 data-container
    const dataContainer = this.shadowRoot.querySelector(".data-container");
    if (dataContainer) dataContainer.style.display = "";

    // 重置 pie-chart-section
    const pieChartSections =
      this.shadowRoot.querySelectorAll(".pie-chart-section");
    pieChartSections.forEach((el) => (el.style.display = ""));

    // 重置 timeline-container
    const timelineContainers = this.shadowRoot.querySelectorAll(
      "#timeline-container",
    );
    timelineContainers.forEach((el) => (el.style.display = ""));

    // 重置 calendar-stats
    const calendarStatsList = [
      this.shadowRoot.getElementById("cal-month-usage"),
      this.shadowRoot.getElementById("cal-month-cost"),
      this.shadowRoot.getElementById("cal-year-usage"),
      this.shadowRoot.getElementById("cal-year-cost"),
    ];
    calendarStatsList.forEach((el) => {
      if (el && el.parentElement) el.parentElement.style.display = "";
    });

    // 重置统计信息的标签
    const calendarStatsSection =
      this.shadowRoot.querySelector(".calendar-stats");
    if (calendarStatsSection) {
      calendarStatsSection.style.display = "";
    }
  }

  // 应用隐藏配置
  applyHiddenConfig() {
    // 优先使用当前用户的hide配置，如果没有则使用顶层配置
    const hideConfig =
      this.currentConfig && this.currentConfig.hide
        ? this.currentConfig.hide
        : this._config && this._config.hide
          ? this._config.hide
          : "";

    if (!hideConfig) return;

    // 先重置所有隐藏元素的显示状态（重要：切换用户时需要重置）
    this.resetHiddenElements();

    // 解析隐藏配置（支持多个值，用逗号分隔）
    const hiddenItems = hideConfig
      .split(",")
      .map((item) => item.trim())
      .filter((item) => item);

    // 隐藏price-display（所有价格显示区域）
    if (hiddenItems.includes("price-display")) {
      const priceDisplays = this.shadowRoot.querySelectorAll(".price-display");
      priceDisplays.forEach((el) => (el.style.display = "none"));
    }

    // 隐藏elegas-price-display（电价显示区域）
    if (hiddenItems.includes("elegas-price-display")) {
      const electricityPriceDisplay = this.shadowRoot.querySelector(
        ".elegas-price-display",
      );
      if (electricityPriceDisplay)
        electricityPriceDisplay.style.display = "none";
    }

    // 隐藏remaining-days-display（剩余天数显示区域）
    if (hiddenItems.includes("remaining-days-display")) {
      const remainingDaysDisplay = this.shadowRoot.querySelector(
        ".remaining-days-display",
      );
      if (remainingDaysDisplay) remainingDaysDisplay.style.display = "none";
    }

    // 隐藏tier-indicator（用电阶梯指示器）
    if (hiddenItems.includes("tier-indicator")) {
      const tierIndicator = this.shadowRoot.querySelector(".tier-indicator");
      const tierIndicatorContainer = this.shadowRoot.querySelector(
        ".tier-indicator-container",
      );
      if (tierIndicator) tierIndicator.style.display = "none";
      if (tierIndicatorContainer) tierIndicatorContainer.style.display = "none";
    }

    // 隐藏data-container（统计数据容器）
    if (hiddenItems.includes("data-container")) {
      const dataContainer = this.shadowRoot.querySelector(".data-container");
      if (dataContainer) dataContainer.style.display = "none";
    }

    // 注意：user-info 和 multi-user-info 的显示现在由顶层的 show_name 控制，不再通过 hide 配置

    // 隐藏pie-chart-section（饼图区域）
    if (hiddenItems.includes("pie-chart-section")) {
      const pieChartSections =
        this.shadowRoot.querySelectorAll(".pie-chart-section");
      pieChartSections.forEach((el) => (el.style.display = "none"));
    }

    // 隐藏timeline-container（设备时间线容器）
    if (hiddenItems.includes("timeline-container")) {
      const timelineContainers = this.shadowRoot.querySelectorAll(
        "#timeline-container",
      );
      timelineContainers.forEach((el) => (el.style.display = "none"));
    }

    // 隐藏calendar-stats（日历统计信息）
    if (hiddenItems.includes("calendar-stats")) {
      const calendarStatsList = [
        this.shadowRoot.getElementById("cal-month-usage"),
        this.shadowRoot.getElementById("cal-month-cost"),
        this.shadowRoot.getElementById("cal-year-usage"),
        this.shadowRoot.getElementById("cal-year-cost"),
      ];
      calendarStatsList.forEach((el) => {
        if (el) el.parentElement.style.display = "none";
      });

      // 也隐藏统计信息的标签
      const calendarStatsSection =
        this.shadowRoot.querySelector(".calendar-stats");
      if (calendarStatsSection) {
        calendarStatsSection.style.display = "none";
      }
    }
  }

  // 根据主题实体实时更新主题
  updateThemeFromEntity() {
    if (!this._config || !this._hass) return;

    const theme = this._config.theme;
    let entityState, isSelectEntity;

    // 新的配置格式：实体模式
    if (
      typeof theme === "object" &&
      theme !== null &&
      theme.entity &&
      this._hass.states[theme.entity]
    ) {
      const themeEntity = this._hass.states[theme.entity];
      entityState = themeEntity.state;
      isSelectEntity = theme.entity.toLowerCase().includes("select");
    }
    // 旧的实体ID格式（向后兼容）
    else if (typeof theme === "string" && this._hass.states[theme]) {
      const themeEntity = this._hass.states[theme];
      entityState = themeEntity.state;
      isSelectEntity = theme.toLowerCase().includes("select");
    } else {
      return;
    }

    // 确定主题名称
    const themeName = this.determineThemeFromEntityState(
      entityState,
      isSelectEntity,
    );

    // 如果主题没有变化，则不执行后续操作（优化性能）
    if (this.lastThemeName === themeName) {
      return;
    }

    // 保存主题名称
    this.lastThemeName = themeName;

    // 应用主题
    this.applyThemeInternal(themeName);
  }

  // ==================== 日历视图相关方法 ====================

  // 初始化日历事件监听
  initCalendarEvents() {
    if (this._calendarEventsInitialized) return;
    this._calendarEventsInitialized = true;

    // 为data-container中的统计卡片添加点击事件
    const statCards = this.shadowRoot.querySelectorAll("[data-type]");
    statCards.forEach((card) => {
      card.addEventListener("click", (e) => {
        // 阻止移动端双击缩放
        e.preventDefault();
        this.showCalendarView(card.dataset.type);
      });
      card.style.cursor = "pointer";

      // 添加touch事件处理，防止移动端双击缩放
      let lastTouchEnd = 0;
      card.addEventListener(
        "touchend",
        (e) => {
          const now = Date.now();
          if (now - lastTouchEnd <= 300) {
            e.preventDefault();
          }
          lastTouchEnd = now;
        },
        false,
      );
    });

    // 日详情模态框关闭按钮
    this.dayModalCloseEl.addEventListener("click", () => this.hideDayModal());
    this.dayModalEl.addEventListener("click", (e) => {
      if (e.target === this.dayModalEl) {
        this.hideDayModal();
      }
    });

    // 缴费历史模态框关闭按钮
    this.payHistoryCloseEl.addEventListener("click", () =>
      this.hidePayHistoryModal(),
    );
    this.payHistoryModalEl.addEventListener("click", (e) => {
      if (e.target === this.payHistoryModalEl) {
        this.hidePayHistoryModal();
      }
    });

    // 点击余额金额显示缴费历史（与燃气卡片一致）
    const balanceAmountEl = this.shadowRoot.querySelector(".balance-amount");
    if (balanceAmountEl) {
      balanceAmountEl.addEventListener("click", () =>
        this.showPayHistoryModal(),
      );
      balanceAmountEl.style.cursor = "pointer";
    }

    // 点击tooltip外部时隐藏tooltip
    this._tooltipClickHandler = (e) => {
      if (!this.eventTooltip) return;
      const isClickInsideTooltip = e
        .composedPath()
        .some((el) => el === this.eventTooltip);
      if (!isClickInsideTooltip) {
        this.hideTooltip();
      }
    };
    document.addEventListener("click", this._tooltipClickHandler);

    // 返回按钮
    this.backToMainBtnEl.addEventListener("click", () =>
      this.hideCalendarView(),
    );

    // 本月按钮
    this.currentMonthBtnEl.addEventListener("click", () => {
      const now = new Date();
      this.calCurrentYear = now.getFullYear();
      this.calCurrentMonth = now.getMonth() + 1;
      this.calYearSelectEl.value = this.calCurrentYear;
      this.calMonthSelectEl.value = this.calCurrentMonth;
      this.updateCalendarView();
    });

    // 年份和月份选择
    this.calYearSelectEl.addEventListener("change", () => {
      this.calCurrentYear = parseInt(this.calYearSelectEl.value);
      this.updateCalendarView();
    });

    this.calMonthSelectEl.addEventListener("change", () => {
      this.calCurrentMonth = parseInt(this.calMonthSelectEl.value);
      this.updateCalendarView();
    });

    // 选项卡切换
    const tabBtns = this.shadowRoot.querySelectorAll(".cal-tab-btn");
    tabBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        tabBtns.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        this.currentView = btn.dataset.view;
        this.updateViewByType(btn.dataset.view);
      });
    });

    // 初始化年份选择器
    this.initYearSelector();

    // 设置初始年月
    const now = new Date();
    this.calCurrentYear = now.getFullYear();
    this.calCurrentMonth = now.getMonth() + 1;
    this.calYearSelectEl.value = this.calCurrentYear;
    this.calMonthSelectEl.value = this.calCurrentMonth;
  }

  // 初始化年份选择器
  initYearSelector() {
    if (!this.daylistData || this.daylistData.length === 0) {
      const now = new Date();
      this.availableYears = [now.getFullYear()];
    } else {
      // 从daylist中提取所有年份
      const years = new Set();
      this.daylistData.forEach((dayData) => {
        if (dayData.day) {
          const year = parseInt(dayData.day.split("-")[0]);
          years.add(year);
        }
      });
      this.availableYears = Array.from(years).sort((a, b) => b - a);

      // 确保当前年份也在列表中
      const currentYear = new Date().getFullYear();
      if (!this.availableYears.includes(currentYear)) {
        this.availableYears.push(currentYear);
        this.availableYears.sort((a, b) => b - a);
      }
    }

    // 填充年份选择器
    this.calYearSelectEl.innerHTML = "";
    this.availableYears.forEach((year) => {
      const option = document.createElement("option");
      option.value = year;
      option.textContent = year;
      this.calYearSelectEl.appendChild(option);
    });
  }

  // 显示日历视图
  showCalendarView(type) {
    this.isCalendarView = true;
    this.dataContainerEl.style.display = "none";
    this.calendarViewEl.style.display = "block";

    // 根据点击的类型设置年月
    const now = new Date();
    if (type === "current-month") {
      this.calCurrentYear = now.getFullYear();
      this.calCurrentMonth = now.getMonth() + 1;
    } else if (type === "last-month") {
      const lastMonth = new Date(now.getFullYear(), now.getMonth() - 1);
      this.calCurrentYear = lastMonth.getFullYear();
      this.calCurrentMonth = lastMonth.getMonth() + 1;
    } else if (type === "year") {
      this.calCurrentYear = now.getFullYear();
      this.calCurrentMonth = 1;
    }

    // 更新年份选择器（以防数据有更新）
    this.initYearSelector();

    // 在initYearSelector之后设置选中的年份和月份，避免被覆盖
    this.calYearSelectEl.value = this.calCurrentYear;
    this.calMonthSelectEl.value = this.calCurrentMonth;

    // 使用 requestAnimationFrame 确保 calendarViewEl 完全显示后再切换视图
    requestAnimationFrame(() => {
      // 根据类型显示不同的视图
      if (type === "year") {
        // 点击年度统计时，显示年度图表
        this.currentView = "year";
        // 更新选项卡的 active 状态
        const tabBtns = this.shadowRoot.querySelectorAll(".cal-tab-btn");
        tabBtns.forEach((btn) => {
          btn.classList.remove("active");
          if (btn.dataset.view === "year") {
            btn.classList.add("active");
          }
        });
        this.updateViewByType("year");

        // 直接点击"年"按钮，避免有时不渲染的问题
        setTimeout(() => {
          const yearTab = this.shadowRoot.querySelector(
            '.cal-tab-btn[data-view="year"]',
          );
          if (yearTab) {
            yearTab.click();
            // 再点击年视图中的"年度"按钮
            setTimeout(() => {
              // 查找所有包含"年度"文本的按钮
              const yearButtons = this.shadowRoot.querySelectorAll(".year-tag");
              const yearViewAllBtn = Array.from(yearButtons).find(
                (btn) => btn.textContent === "年度",
              );
              if (yearViewAllBtn) {
                yearViewAllBtn.click();
              }
            }, 100);
          }
        }, 500);
      } else {
        // 其他情况显示日历视图（本月用电和上月用电）
        this.currentView = "calendar";
        // 更新选项卡的 active 状态
        const tabBtns = this.shadowRoot.querySelectorAll(".cal-tab-btn");
        tabBtns.forEach((btn) => {
          btn.classList.remove("active");
          if (btn.dataset.view === "calendar") {
            btn.classList.add("active");
          }
        });
        this.updateCalendarView();

        // 直接点击"日历"按钮，避免有时不渲染的问题
        setTimeout(() => {
          const calendarTab = this.shadowRoot.querySelector(
            '.cal-tab-btn[data-view="calendar"]',
          );
          if (calendarTab) {
            calendarTab.click();
          }
        }, 500);
      }
    });
  }

  // 隐藏日历视图
  hideCalendarView() {
    this.isCalendarView = false;
    this.dataContainerEl.style.display = "flex";
    this.calendarViewEl.style.display = "none";
    // 销毁所有图表
    this.destroyYearCharts();
    this.destroyMonthCharts();
    this.destroyDayCharts();
  }

  /** 切换年视图容器显示（combo=组合图） */
  _showYearChartContainer(mode) {
    if (this.yearComboChartEl?.parentElement) {
      this.yearComboChartEl.parentElement.style.display = "block";
    }
  }

  /** 渲染图表并延迟 resize（确保容器布局完成） */
  _renderChartWithResize(renderFn, chartRef) {
    requestAnimationFrame(() => {
      renderFn();
      setTimeout(() => {
        const chart = chartRef ? this[chartRef] : null;
        if (chart) chart.resize();
      }, CHART_RESIZE_DELAY_MS);
    });
  }

  /** 显示年/月视图（含数据加载检查与轮询） */
  _showViewWithDataCheck(viewType) {
    const isYear = viewType === "year";
    const contentEl = isYear ? this.yearContentEl : this.monthContentEl;
    const chartEl = isYear ? this.yearComboChartEl : this.monthComboChartEl;
    const dataGetter = () => (isYear ? this.yearlistData : this.monthlistData);
    const renderFn = isYear
      ? () => this.renderYearView()
      : () => this.renderMonthView();
    const chartRef = isYear ? "yearChart" : "monthChart";

    contentEl.style.display = "block";
    const hasData = this.historicalDataLoaded && dataGetter()?.length > 0;

    if (!hasData) {
      if (chartEl) chartEl.innerHTML = LOADING_HTML;
      const checkData = setInterval(() => {
        if (this.historicalDataLoaded && dataGetter()?.length > 0) {
          clearInterval(checkData);
          this._viewCheckIntervals = (this._viewCheckIntervals || []).filter(
            (id) => id !== checkData,
          );
          if (chartEl) chartEl.innerHTML = "";
          this._renderChartWithResize(renderFn, chartRef);
        }
      }, DATA_CHECK_INTERVAL_MS);
      this._viewCheckIntervals = this._viewCheckIntervals || [];
      this._viewCheckIntervals.push(checkData);
      setTimeout(() => {
        clearInterval(checkData);
        this._viewCheckIntervals = (this._viewCheckIntervals || []).filter(
          (id) => id !== checkData,
        );
      }, DATA_LOAD_TIMEOUT_MS);
    } else {
      this._renderChartWithResize(renderFn, chartRef);
    }
  }

  // 根据视图类型更新显示
  updateViewByType(viewType) {
    // 隐藏所有视图内容
    this.calendarContentEl.style.display = "none";
    this.yearContentEl.style.display = "none";
    this.monthContentEl.style.display = "none";
    this.dayContentEl.style.display = "none";
    this.calendarControlsEl.style.display = "none";

    if (viewType === "year") {
      this._showViewWithDataCheck("year");
    } else if (viewType === "month") {
      this._showViewWithDataCheck("month");
    } else if (viewType === "day") {
      this.dayContentEl.style.display = "block";
      this._renderChartWithResize(() => this.renderDayView(), "dayViewChart");
    } else {
      // 显示日历视图
      this.calendarContentEl.style.display = "block";
      this.calendarControlsEl.style.display = "flex";
      this.updateCalendarView();
    }
  }

  // 渲染年视图
  async renderYearView() {
    // 从统一数据接口获取数据
    if (
      !this.standardData ||
      !this.standardData.yearUsage ||
      this.standardData.yearUsage.length === 0
    ) {
      return;
    }

    // 销毁现有图表
    this.destroyYearCharts();

    // 从统一格式准备数据
    const yearData = this.standardData.yearUsage;
    const years = yearData.map((item) => item.time);
    const costs = yearData.map((item) => item.total_amount);
    const peakData = yearData.map((item) => item.usage_ele_peak || 0);
    const valleyData = yearData.map((item) => item.usage_ele_valley || 0);
    const normalData = yearData.map((item) => item.usage_ele_norm || 0);
    const sharpData = yearData.map((item) => item.usage_ele_tip || 0);
    const noTimeData = yearData.map((item) => item.usage_ele_no || 0);

    // 获取单位符号（从统一数据格式中获取，不设置默认值）
    const usageUnit = yearData.length > 0 ? yearData[0].unit : "";

    // 保存数据到缓存，用于重试
    this._yearChartData = {
      years,
      costs,
      peakData,
      valleyData,
      normalData,
      sharpData,
      noTimeData,
      usageUnit,
    };

    // 生成年份选择器按钮
    this.generateYearFilterButtons(years);
    this.updateYearFilterButtons();

    // 根据当前选中的视图类型渲染图表
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (this.selectedYearView === "总计") {
          this.renderYearTotalChart(usageUnit);
        } else if (this.selectedYearView === "年度") {
          this.renderYearComboChart(
            years,
            costs,
            peakData,
            valleyData,
            normalData,
            sharpData,
            noTimeData,
            usageUnit,
          );
        } else {
          // 单个年份，渲染饼图
          this.renderYearSingleChart(this.selectedYearView, usageUnit);
        }
      });
    });
  }

  // 生成年份选择器按钮
  generateYearFilterButtons(years) {
    // 获取两个容器
    const mainFilterEl = this.yearFilterMainEl;
    const yearsFilterEl = this.yearFilterYearsEl;

    if (!mainFilterEl || !yearsFilterEl) {
      return;
    }

    // 清空两个容器
    mainFilterEl.innerHTML = "";
    yearsFilterEl.innerHTML = "";

    // 添加"总计"按钮到主容器
    const totalBtn = document.createElement("button");
    totalBtn.className = "year-tag";
    totalBtn.textContent = "总计";
    if (this.selectedYearView === "总计") {
      totalBtn.classList.add("active");
    }
    totalBtn.onclick = () => {
      this.selectedYearView = "总计";
      this.updateYearFilterButtons();
      this.renderYearTotalChart();
    };
    mainFilterEl.appendChild(totalBtn);

    // 添加"年度"按钮到主容器
    const allBtn = document.createElement("button");
    allBtn.className = "year-tag";
    allBtn.textContent = "年度";
    if (this.selectedYearView === "年度") {
      allBtn.classList.add("active");
    }
    allBtn.onclick = () => {
      this.selectedYearView = "年度";
      this.updateYearFilterButtons();
      const {
        years,
        costs,
        peakData,
        valleyData,
        normalData,
        sharpData,
        noTimeData,
      } = this._yearChartData;
      this.renderYearComboChart(
        years,
        costs,
        peakData,
        valleyData,
        normalData,
        sharpData,
        noTimeData,
      );
    };
    mainFilterEl.appendChild(allBtn);

    // 添加各年份按钮到年份容器（倒序排列）
    [...years].reverse().forEach((year) => {
      const btn = document.createElement("button");
      btn.className = "year-tag";
      btn.textContent = year;
      if (this.selectedYearView === year) {
        btn.classList.add("active");
      }
      btn.onclick = () => {
        this.selectedYearView = year;
        this.updateYearFilterButtons();
        this.renderYearSingleChart(year);
      };
      yearsFilterEl.appendChild(btn);
    });
  }

  // 更新年份选择器按钮的激活状态
  updateYearFilterButtons() {
    // 总计下隐藏底部年度按钮（2026、2025 等）
    if (this.yearFilterYearsEl) {
      this.yearFilterYearsEl.style.display =
        this.selectedYearView === "总计" ? "none" : "flex";
    }
    // 从两个容器中查找所有按钮
    const mainButtons = this.yearFilterMainEl.querySelectorAll(".year-tag");
    const yearButtons = this.yearFilterYearsEl.querySelectorAll(".year-tag");

    // 合并所有按钮
    const allButtons = [...mainButtons, ...yearButtons];

    allButtons.forEach((btn) => {
      if (btn.textContent === this.selectedYearView) {
        btn.classList.add("active");
      } else {
        btn.classList.remove("active");
      }
    });
  }

  // 渲染总计饼图
  renderYearTotalChart() {
    if (!this._isEchartsAvailable(this.yearComboChartEl, true)) return;

    this._showYearChartContainer("combo");
    this._disposeChart("yearChart");
    this.yearChart = echarts.init(this.yearComboChartEl);

    const {
      years = [],
      peakData,
      valleyData,
      normalData,
      sharpData,
      noTimeData,
      costs,
      usageUnit,
    } = this._yearChartData;

    // 按年度计算用量，每年一色形成闭环
    const pieData = [];
    let totalCost = costs.reduce((a, b) => a + b, 0);
    let totalUsageFallback = 0;
    for (let i = 0; i < years.length; i++) {
      let yearUsage = noTimeData[i] || 0;
      if (yearUsage === 0) {
        yearUsage =
          (peakData[i] || 0) +
          (valleyData[i] || 0) +
          (normalData[i] || 0) +
          (sharpData[i] || 0);
      }
      if (yearUsage === 0 && this.standardData && this.standardData.yearUsage) {
        const yearKey = String(years[i]);
        const yearItem = this.standardData.yearUsage.find(
          (item) => String(item.time || "") === yearKey,
        );
        if (yearItem) yearUsage = yearItem.total_usage || 0;
      }
      totalUsageFallback += yearUsage;
      if (yearUsage > 0) {
        const color =
          YEAR_TOTAL_CHART_COLORS[i % YEAR_TOTAL_CHART_COLORS.length];
        pieData.push({
          name: String(years[i]),
          value: yearUsage,
          itemStyle: { color },
        });
      }
    }

    // 若按年分段后无数据，回退为单段「用量」显示（确保图表能显示）
    if (pieData.length === 0 && totalUsageFallback > 0) {
      pieData.push({
        name: "用量",
        value: totalUsageFallback,
        itemStyle: { color: USAGE_CHART_COLOR },
      });
    }

    const totalElectricity = pieData.reduce((sum, item) => sum + item.value, 0);

    const option = {
      tooltip: {
        trigger: "item",
        textStyle: {
          fontSize: 12,
        },
        formatter: (function (totalElectricity, usageUnit) {
          return function (params) {
            const val = params.value || 0;
            const total = totalElectricity || 1; // 防止除以0
            const percentage = formatPercentage(val, total);
            const label = /^\d{4}$/.test(params.name)
              ? params.name + "年"
              : params.name;
            return `${label}: ${(val || 0).toFixed(2)} ${usageUnit}<br/>占比: ${percentage}%`;
          };
        })(totalElectricity, usageUnit),
      },
      legend: { show: false },
      series: [
        {
          name: this.utilityType === "gas" ? "用气量" : "用电量",
          type: "pie",
          radius: ["40%", "65%"],
          center: ["48%", "48%"],
          avoidLabelOverlap: false,
          label: { show: false },
          labelLine: { show: false },
          emphasis: {
            scale: true,
            scaleSize: 5,
          },
          data: pieData,
        },
      ],
      graphic: [
        {
          type: "text",
          left: "40%",
          top: "43%",
          style: {
            text:
              "总计\n" + (totalElectricity || 0).toFixed(1) + " " + usageUnit,
            textAlign: "left",
            fill: "#666",
            fontSize: 13,
            fontWeight: "bold",
          },
          z: 10,
        },
        {
          type: "text",
          left: "40%",
          top: "53%",
          style: {
            text: "¥" + (totalCost || 0).toFixed(2),
            textAlign: "center",
            fill: "#804AFF",
            fontSize: 13,
            fontWeight: "bold",
          },
          z: 10,
        },
      ],
    };

    this.yearChart.setOption(option);
    requestAnimationFrame(() => {
      if (this.yearChart) this.yearChart.resize();
    });
  }

  // 渲染单个年份饼图
  renderYearSingleChart(year) {
    if (
      !this.echartsLoaded ||
      typeof echarts === "undefined" ||
      !this.yearComboChartEl
    ) {
      return;
    }

    this._showYearChartContainer("combo");

    const {
      years,
      peakData,
      valleyData,
      normalData,
      sharpData,
      noTimeData,
      costs,
      usageUnit,
    } = this._yearChartData;
    const yearIndex = years.indexOf(year);

    if (yearIndex === -1) {
      console.error("未找到年份:", year);
      return;
    }

    this._disposeChart("yearChart");
    this.yearChart = echarts.init(this.yearComboChartEl);

    // 获取该年份的数据
    const yearData = {
      peak: peakData[yearIndex],
      valley: valleyData[yearIndex],
      normal: normalData[yearIndex],
      sharp: sharpData[yearIndex],
      no: noTimeData[yearIndex],
      cost: costs[yearIndex],
    };

    // 根据 utility_type 设置颜色
    const isElectricity = this.utilityType === "ele";
    const pieColor = USAGE_CHART_COLOR;

    // 准备饼图数据
    const pieData = [];

    // 电力与燃气一致：只显示用量
    let usage = yearData.no;
    if (usage === 0) {
      usage =
        yearData.valley + yearData.peak + yearData.normal + yearData.sharp;
    }
    if (usage === 0 && this.standardData && this.standardData.yearUsage) {
      const yearItem = this.standardData.yearUsage.find(
        (item) => item.time === year,
      );
      if (yearItem) usage = yearItem.total_usage || 0;
    }
    if (usage > 0) {
      pieData.push({
        name: "用量",
        value: usage,
        itemStyle: { color: pieColor },
      });
    }

    const totalElectricity = pieData.reduce((sum, item) => sum + item.value, 0);

    const option = {
      tooltip: {
        trigger: "item",
        textStyle: {
          fontSize: 12,
        },
        formatter: (function (totalElectricity, usageUnit) {
          return function (params) {
            const val = params.value || 0;
            const total = totalElectricity || 1; // 防止除以0
            const percentage = formatPercentage(val, total);
            return `${params.name}: ${(val || 0).toFixed(2)} ${usageUnit}<br/>占比: ${percentage}%`;
          };
        })(totalElectricity, usageUnit),
      },
      legend: {
        orient: "horizontal",
        bottom: 0,
        left: "center",
        itemWidth: 10,
        itemHeight: 14,
        itemGap: 8,
        data: pieData.map((item) => ({
          name: item.name,
          icon: "none",
          textStyle: {
            rich: {
              title: {
                color: "#fff",
                backgroundColor: item.itemStyle.color,
                padding: [2, 5],
                borderRadius: 2,
                fontSize: 11,
                fontWeight: "bold",
              },
            },
          },
        })),
        textStyle: {
          fontSize: 11,
          fontWeight: "bold",
        },
        formatter: function (name) {
          return "{title|" + name + "}";
        },
      },
      series: [
        {
          name: "用电量",
          type: "pie",
          radius: ["40%", "65%"],
          center: ["48%", "48%"],
          avoidLabelOverlap: false,
          label: {
            show: true,
            position: "outside",
            formatter: function (params) {
              const val = params.value || 0;
              const total = totalElectricity || 1;
              const percentage = formatPercentage(val, total);
              return `${params.name}\n${percentage}%`;
            },
            fontSize: 11,
          },
          emphasis: {
            label: {
              show: true,
              fontSize: 13,
              fontWeight: "bold",
            },
          },
          labelLine: {
            show: true,
          },
          data: pieData,
        },
      ],
      graphic: [
        {
          type: "text",
          left: "40%",
          top: "40%",
          style: {
            text:
              year +
              "年\n" +
              (totalElectricity || 0).toFixed(0) +
              " " +
              usageUnit,
            textAlign: "left",
            fill: "#666",
            fontSize: 13,
            fontWeight: "bold",
          },
          z: 10,
        },
        {
          type: "text",
          left: "40%",
          top: "53%",
          style: {
            text: "¥" + (yearData.cost || 0).toFixed(0),
            textAlign: "left",
            fill: "#804AFF",
            fontSize: 13,
            fontWeight: "bold",
          },
          z: 10,
        },
      ],
    };

    this.yearChart.setOption(option);
  }

  // 渲染年度电费与用电量组合图
  async renderYearComboChart(
    years,
    costs,
    peakData,
    valleyData,
    normalData,
    sharpData,
    noTimeData,
    usageUnit = "",
  ) {
    if (!this._isEchartsAvailable(this.yearComboChartEl, true)) return;

    this._showYearChartContainer("combo");

    // 强制触发浏览器重排，确保容器有正确的尺寸
    const forceReflow = this.yearComboChartEl.offsetWidth;

    // 检查容器尺寸是否有效
    if (
      this.yearComboChartEl.offsetWidth === 0 ||
      this.yearComboChartEl.offsetHeight === 0
    ) {
      setTimeout(() => {
        // 使用缓存数据重试
        if (this._yearChartData) {
          const {
            years,
            costs,
            peakData,
            valleyData,
            normalData,
            sharpData,
            noTimeData,
            usageUnit,
          } = this._yearChartData;
          this.renderYearComboChart(
            years,
            costs,
            peakData,
            valleyData,
            normalData,
            sharpData,
            noTimeData,
            usageUnit,
          );
        } else {
          // 如果没有缓存数据，调用 renderYearView 重新获取数据
          this.renderYearView();
        }
      }, 100);
      return;
    }

    this._disposeChart("yearChart");
    this.yearChart = echarts.init(this.yearComboChartEl);

    // 获取缴费数据并按年聚合（从缴费实体或余额实体的 history_charges 获取）
    let paymentData = {};
    try {
      const payHistory = await this.fetchPayHistory();
      if (payHistory && payHistory.length > 0) {
        // 按年聚合缴费数据
        payHistory.forEach((item) => {
          if (item.time && item.cost) {
            const year = new Date(item.time).getFullYear().toString();
            if (!paymentData[year]) {
              paymentData[year] = 0;
            }
            paymentData[year] += item.cost;
          }
        });
      }
    } catch (error) {
      console.log("获取缴费数据失败:", error);
    }

    // 根据缴费数据确定完整的年份范围
    let allYears = [...years];
    if (Object.keys(paymentData).length > 0) {
      const paymentYears = Object.keys(paymentData).sort();
      // 合并年份范围
      allYears = [...new Set([...years, ...paymentYears])].sort();
    }

    // 重新调整数据以匹配完整的年份范围
    const adjustedYears = allYears;
    const adjustedCosts = adjustedYears.map((year) => {
      const index = years.indexOf(year);
      return index !== -1 ? costs[index] : 0;
    });
    const adjustedPeakData = adjustedYears.map((year) => {
      const index = years.indexOf(year);
      return index !== -1 ? peakData[index] : 0;
    });
    const adjustedValleyData = adjustedYears.map((year) => {
      const index = years.indexOf(year);
      return index !== -1 ? valleyData[index] : 0;
    });
    const adjustedNormalData = adjustedYears.map((year) => {
      const index = years.indexOf(year);
      return index !== -1 ? normalData[index] : 0;
    });
    const adjustedSharpData = adjustedYears.map((year) => {
      const index = years.indexOf(year);
      return index !== -1 ? sharpData[index] : 0;
    });
    const adjustedNoTimeData = adjustedYears.map((year) => {
      const index = years.indexOf(year);
      return index !== -1 ? noTimeData[index] : 0;
    });
    const adjustedPaymentData = adjustedYears.map(
      (year) => paymentData[year] || 0,
    );

    // 根据 utility_type 设置系列和图例
    const isElectricity = this.utilityType === "ele";
    const barColor = USAGE_CHART_COLOR;

    const series = [];
    const legendData = [];

    // 电力与燃气一致：只显示用量系列
    const totalUsageData = adjustedYears.map((year) => {
      const index = years.indexOf(year);
      if (index === -1) return 0;
      let usage = noTimeData[index];
      if (usage === 0) {
        usage =
          (peakData[index] || 0) +
          (valleyData[index] || 0) +
          (normalData[index] || 0) +
          (sharpData[index] || 0);
      }
      if (usage === 0 && this.standardData && this.standardData.yearUsage) {
        const yearItem = this.standardData.yearUsage.find(
          (item) => item.time === year,
        );
        if (yearItem) usage = yearItem.total_usage || 0;
      }
      return usage;
    });

    series.push({
      name: "用量",
      type: "bar",
      stack: "usage",
      data: totalUsageData,
      yAxisIndex: 0,
      itemStyle: { color: barColor },
    });
    legendData.push({ name: "用量", icon: "none" });

    // 所有类型都显示费用系列
    series.push({
      name: "消费",
      type: "line",
      data: adjustedCosts,
      yAxisIndex: 1,
      smooth: true,
      symbol: "circle",
      symbolSize: 6,
      showSymbol: true,
      lineStyle: {
        width: 3,
        color: "#804AFF",
      },
      itemStyle: {
        color: "#804AFF",
        borderColor: "#fff",
        borderWidth: 1.5,
        shadowBlur: 10,
        shadowColor: "rgba(128, 74, 255, 0.5)",
      },
      label: {
        show: true,
        position: "top",
        fontSize: 10,
        color: "#804AFF",
        fontWeight: 600,
        formatter: function (params) {
          if (params.value === 0) {
            return "";
          }
          return (params.value || 0).toFixed(2);
        },
        offset: [0, -8],
      },
      emphasis: {
        focus: "series",
        scale: true,
        scaleSize: 1.3,
        itemStyle: {
          borderColor: "#804AFF",
          borderWidth: 2,
        },
      },
    });

    legendData.push({ name: "消费", icon: "none" });

    // 如果有缴费数据，添加阶梯面积图系列（放在柱状图下层）
    if (Object.keys(paymentData).length > 0) {
      // 将缴费系列插入到series数组的开头，这样它会先渲染（在底层）
      series.unshift({
        name: "缴费",
        type: "line",
        step: "start",
        data: adjustedPaymentData,
        yAxisIndex: 1,
        z: 0, // 设置zIndex确保在柱状图下层
        symbol: "none",
        showSymbol: false,
        lineStyle: {
          width: 0,
          opacity: 0,
        },
        areaStyle: {
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(0, 200, 83, 0.3)" },
              { offset: 1, color: "rgba(0, 200, 83, 0.05)" },
            ],
          },
        },
        label: {
          show: false,
        },
        emphasis: {
          disabled: true,
        },
      });
      // 同样将缴费图例插入到开头
      legendData.unshift({
        name: "缴费",
        icon: "none",
        itemStyle: { color: "#00C853" },
      });
    }

    const option = {
      tooltip: {
        trigger: "axis",
        axisPointer: {
          type: "cross",
          label: {
            backgroundColor: "#6a7985",
          },
        },
        textStyle: {
          fontSize: 12,
        },
        formatter: function (params) {
          let result = `${params[0].axisValue}年<br/><br/>`;

          // 先显示费用信息
          const costParam = params.find((param) => param.seriesName === "消费");
          if (costParam) {
            result += `<span style="color:#804AFF">●</span> 消费: <strong>¥${(costParam.value || 0).toFixed(2)}</strong><br/>`;
            result +=
              '<hr style="margin:3px 0;border:none;border-top:1px solid #ddd;">';
          }

          // 显示缴费信息
          const paymentParam = params.find(
            (param) => param.seriesName === "缴费",
          );
          if (paymentParam && paymentParam.value > 0) {
            result += `<span style="color:#00C853">●</span> 缴费: <strong>¥${(paymentParam.value || 0).toFixed(2)}</strong><br/>`;
            result +=
              '<hr style="margin:3px 0;border:none;border-top:1px solid #ddd;">';
          }

          // 显示用量信息
          let hasUsage = false;
          let totalUsage = 0;
          params.forEach((param) => {
            if (param.seriesName === "用量" && param.value > 0) {
              hasUsage = true;
              result += `<span style="display:inline-block;width:10px;height:10px;background:${param.color};margin-right:5px;"></span><strong>${param.seriesName}: ${(param.value || 0).toFixed(2)} ${usageUnit}</strong><br/>`;
              totalUsage += param.value;
            } else if (
              param.seriesName !== "消费" &&
              param.seriesName !== "用量" &&
              param.seriesName !== "缴费" &&
              param.value > 0
            ) {
              hasUsage = true;
              result += `<span style="display:inline-block;width:10px;height:10px;background:${param.color};margin-right:5px;"></span>${param.seriesName}: ${(param.value || 0).toFixed(2)} ${usageUnit}<br/>`;
              totalUsage += param.value;
            }
          });

          if (hasUsage) {
            result += `<hr style="margin:3px 0;border:none;border-top:1px solid #ddd;">`;
            result += `<strong>总用量: ${(totalUsage || 0).toFixed(2)} ${usageUnit}</strong>`;
          }

          return result;
        },
      },
      legend: {
        data: legendData,
        bottom: 0,
        itemWidth: 10,
        itemHeight: 20,
        itemGap: 2,
        textStyle: {
          fontSize: 10,
          fontWeight: "bold",
        },
        formatter: function (name) {
          return "{title|" + name + "}";
        },
        textStyle: {
          rich: {
            title: {
              color: "#fff",
              fontSize: 11,
              fontWeight: "bold",
              padding: [3, 3],
              borderRadius: 2,
              backgroundColor: function (params) {
                if (params.name === "用量") {
                  return barColor;
                } else if (params.name === "消费") {
                  return "#804AFF";
                } else if (params.name === "缴费") {
                  return "#00C853";
                }
                return barColor;
              },
            },
          },
        },
      },
      grid: {
        left: "0%",
        right: "0%",
        bottom: "10%",
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: adjustedYears,
        axisLine: {
          lineStyle: { color: "#666" },
        },
        axisLabel: {
          color: "#666",
          interval: 0,
          rotate: 45,
          fontSize: 10,
        },
      },
      yAxis: [
        {
          type: "value",
          name: usageUnit,
          nameLocation: "end",
          position: "left",
          axisLine: {
            lineStyle: { color: "#666" },
          },
          axisLabel: {
            color: "#666",
            formatter: axisValueFormatter,
          },
          splitLine: {
            lineStyle: { color: "rgba(0,0,0,0.1)" },
          },
        },
        {
          type: "value",
          name: "元",
          position: "right",
          axisLine: {
            lineStyle: { color: "#804AFF" },
          },
          axisLabel: {
            color: "#804AFF",
            formatter: axisValueFormatter,
          },
          splitLine: {
            show: false,
          },
        },
      ],
      series: series,
    };

    this.yearChart.setOption(option);
  }

  // 渲染月视图
  renderMonthView() {
    // 从统一数据接口获取数据
    if (
      !this.standardData ||
      !this.standardData.monthUsage ||
      this.standardData.monthUsage.length === 0
    ) {
      return;
    }

    // 销毁现有图表
    this.destroyMonthCharts();

    // 从统一格式提取所有可用的年份
    const availableYears = [
      ...new Set(
        this.standardData.monthUsage.map((item) =>
          parseInt(item.time.split("-")[0]),
        ),
      ),
    ].sort((a, b) => b - a); // 降序排列

    // 如果没有选中的年份，默认选择第一个（最新的年份）
    if (!this.selectedYear || !availableYears.includes(this.selectedYear)) {
      this.selectedYear = availableYears[0];
    }

    // 渲染年份筛选标签
    this.renderYearFilter(availableYears, this.selectedYear);

    // 从统一格式过滤出当前选中年份的数据
    const yearData = this.standardData.monthUsage.filter((item) => {
      const itemYear = parseInt(item.time.split("-")[0]);
      return itemYear === this.selectedYear;
    });

    // 如果没有数据，直接返回
    if (yearData.length === 0) {
      return;
    }

    // 从统一格式准备图表数据
    const months = yearData.map((item) => {
      const date = new Date(item.time + "-01");
      return `${pad2(date.getMonth() + 1)}月`;
    });
    const costs = yearData.map((item) => item.total_amount);
    const peakData = yearData.map((item) => item.usage_ele_peak || 0);
    const valleyData = yearData.map((item) => item.usage_ele_valley || 0);
    const normalData = yearData.map((item) => item.usage_ele_norm || 0);
    const sharpData = yearData.map((item) => item.usage_ele_tip || 0);
    const noTimeData = yearData.map((item) => item.usage_ele_no || 0);

    // 获取单位符号（从统一数据格式中获取，不设置默认值）
    const usageUnit = yearData.length > 0 ? yearData[0].unit : "";

    // 保存数据到缓存，用于重试
    this._monthChartData = {
      months,
      costs,
      peakData,
      valleyData,
      normalData,
      sharpData,
      noTimeData,
      usageUnit,
    };

    // 渲染组合图表 - 使用双重 requestAnimationFrame 确保容器布局完成
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        this.renderMonthComboChart(
          months,
          costs,
          peakData,
          valleyData,
          normalData,
          sharpData,
          noTimeData,
          usageUnit,
        );
      });
    });
  }

  // 渲染年份筛选标签
  renderYearFilter(years, selectedYear) {
    this.monthYearFilterEl.innerHTML = "";

    years.forEach((year) => {
      const yearTag = document.createElement("div");
      yearTag.className = "year-tag";
      yearTag.textContent = `${year}`;

      if (year === selectedYear) {
        yearTag.classList.add("active");
      }

      yearTag.addEventListener("click", () => {
        this.selectedYear = year;
        this.renderMonthView();
      });

      this.monthYearFilterEl.appendChild(yearTag);
    });
  }

  // 渲染月度电费与用电量组合图
  renderMonthComboChart(
    months,
    costs,
    peakData,
    valleyData,
    normalData,
    sharpData,
    noTimeData,
    usageUnit = "",
  ) {
    if (!this._isEchartsAvailable(this.monthComboChartEl, true)) return;

    // 强制触发浏览器重排，确保容器有正确的尺寸
    const forceReflow = this.monthComboChartEl.offsetWidth;

    // 检查容器尺寸是否有效
    if (
      this.monthComboChartEl.offsetWidth === 0 ||
      this.monthComboChartEl.offsetHeight === 0
    ) {
      setTimeout(() => {
        // 使用缓存数据重试
        if (this._monthChartData) {
          const {
            months,
            costs,
            peakData,
            valleyData,
            normalData,
            sharpData,
            noTimeData,
            usageUnit,
          } = this._monthChartData;
          this.renderMonthComboChart(
            months,
            costs,
            peakData,
            valleyData,
            normalData,
            sharpData,
            noTimeData,
            usageUnit,
          );
        } else {
          // 如果没有缓存数据，调用 renderMonthView 重新获取数据
          this.renderMonthView();
        }
      }, 100);
      return;
    }

    // 销毁现有图表实例
    this._disposeChart("monthChart");
    this.monthChart = echarts.init(this.monthComboChartEl);

    // 根据 utility_type 设置系列和图例
    const isElectricity = this.utilityType === "ele";
    const barColor = USAGE_CHART_COLOR;

    const series = [];
    const legendData = [];

    // 电力与燃气一致：只显示用量系列
    const totalUsageData = noTimeData.map((val, idx) => {
      let usage = val;
      if (usage === 0) {
        usage =
          (peakData[idx] || 0) +
          (valleyData[idx] || 0) +
          (normalData[idx] || 0) +
          (sharpData[idx] || 0);
      }
      return usage;
    });

    series.push({
      name: "用量",
      type: "bar",
      stack: "usage",
      data: totalUsageData,
      yAxisIndex: 0,
      itemStyle: { color: barColor },
    });
    legendData.push({ name: "用量", icon: "none" });

    // 所有类型都显示费用系列
    series.push({
      name: "消费",
      type: "line",
      data: costs,
      yAxisIndex: 1,
      smooth: true,
      symbol: "circle",
      symbolSize: 6,
      showSymbol: true,
      lineStyle: {
        width: 3,
        color: "#804AFF",
      },
      itemStyle: {
        color: "#804AFF",
        borderColor: "#fff",
        borderWidth: 1.5,
        shadowBlur: 10,
        shadowColor: "rgba(128, 74, 255, 0.5)",
      },
      label: {
        show: true,
        position: "top",
        fontSize: 10,
        color: "#804AFF",
        fontWeight: 600,
        formatter: function (params) {
          if (params.value === 0) {
            return "";
          }
          return (params.value || 0).toFixed(2);
        },
        offset: [0, -8],
      },
      emphasis: {
        focus: "series",
        scale: true,
        scaleSize: 1.3,
        itemStyle: {
          borderColor: "#804AFF",
          borderWidth: 2,
        },
      },
    });

    legendData.push({ name: "消费", icon: "none" });

    const option = {
      tooltip: {
        trigger: "axis",
        axisPointer: {
          type: "cross",
          label: {
            backgroundColor: "#6a7985",
          },
        },
        textStyle: {
          fontSize: 12,
        },
        formatter: (function (selectedYear, usageUnit, isElectricity) {
          return function (params) {
            // 月视图tooltip - 显示年-月格式
            const monthLabel = params[0].axisValue;
            const month = monthLabel.replace("月", "");
            const fullDate = `${selectedYear}-${month}`;
            let result = `${fullDate}<br/><br/>`;

            // 先显示费用信息
            const costParam = params.find(
              (param) => param.seriesName === "消费",
            );
            if (costParam) {
              result += `<span style="color:#804AFF">●</span> 消费: <strong>¥${(costParam.value || 0).toFixed(2)}</strong><br/>`;
              result +=
                '<hr style="margin:3px 0;border:none;border-top:1px solid #ddd;">';
            }

            // 显示用量信息
            let hasUsage = false;
            let totalUsage = 0;
            params.forEach((param) => {
              if (param.seriesName === "用量" && param.value > 0) {
                hasUsage = true;
                result += `<span style="display:inline-block;width:10px;height:10px;background:${param.color};margin-right:5px;"></span><strong>${param.seriesName}: ${(param.value || 0).toFixed(2)} ${usageUnit}</strong><br/>`;
                totalUsage += param.value;
              } else if (
                param.seriesName !== "消费" &&
                param.seriesName !== "用量" &&
                param.value > 0
              ) {
                hasUsage = true;
                result += `<span style="display:inline-block;width:10px;height:10px;background:${param.color};margin-right:5px;"></span>${param.seriesName}: ${(param.value || 0).toFixed(2)} ${usageUnit}<br/>`;
                totalUsage += param.value;
              }
            });

            if (hasUsage) {
              result += `<hr style="margin:3px 0;border:none;border-top:1px solid #ddd;">`;
              result += `<strong>总用量: ${(totalUsage || 0).toFixed(2)} ${usageUnit}</strong>`;
            }

            return result;
          };
        })(this.selectedYear, usageUnit, isElectricity),
      },
      legend: {
        data: legendData,
        bottom: 0,
        itemWidth: 10,
        itemHeight: 20,
        itemGap: 10,
        textStyle: {
          fontSize: 10,
          fontWeight: "bold",
        },
        formatter: function (name) {
          var colorMap = {
            用量: "#FF9800",
            电费: "#804AFF",
          };
          return "{title|" + name + "}";
        },
        textStyle: {
          rich: {
            title: {
              color: "#fff",
              fontSize: 11,
              fontWeight: "bold",
              padding: [3, 3],
              borderRadius: 2,
              backgroundColor: function (params) {
                var colorMap = {
                  用量: "#FF9800",
                };
                return colorMap[params.name] || "#FF9800";
              },
            },
          },
        },
      },
      grid: {
        left: "0%",
        right: "0%",
        bottom: "10%",
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: months,
        axisLine: {
          lineStyle: { color: "#666" },
        },
        axisLabel: {
          color: "#666",
          interval: 0,
          rotate: 45,
          fontSize: 10,
        },
      },
      yAxis: [
        {
          type: "value",
          name: usageUnit,
          nameLocation: "end",
          position: "left",
          axisLine: {
            lineStyle: { color: "#666" },
          },
          axisLabel: {
            color: "#666",
            formatter: axisValueFormatter,
          },
          splitLine: {
            lineStyle: { color: "rgba(0,0,0,0.1)" },
          },
        },
        {
          type: "value",
          name: "元",
          position: "right",
          axisLine: {
            lineStyle: { color: "#804AFF" },
          },
          axisLabel: {
            color: "#804AFF",
            formatter: axisValueFormatter,
          },
          splitLine: {
            show: false,
          },
        },
      ],
      series: series,
    };

    this.monthChart.setOption(option);
  }

  // 渲染日视图
  renderDayView() {
    // 从统一数据接口获取数据
    if (
      !this.standardData ||
      !this.standardData.dayUsage ||
      this.standardData.dayUsage.length === 0
    ) {
      return;
    }

    this._disposeChart("dayViewChart");

    // 从统一格式提取所有可用的年份和月份
    const dates = this.standardData.dayUsage
      .filter((item) => item.time)
      .map((item) => {
        const [year, month] = item.time.split("-");
        return { year: parseInt(year), month: parseInt(month) };
      });

    // 提取所有可用的年份
    const availableYears = [...new Set(dates.map((d) => d.year))].sort(
      (a, b) => b - a,
    );

    // 获取当前年月
    const now = new Date();
    const currentYear = now.getFullYear();
    const currentMonth = now.getMonth() + 1;

    // 根据选中的年份过滤月份，只显示该年有数据的月份
    const selectedYear = this.selectedDayYear || availableYears[0];
    let availableMonths = [
      ...new Set(
        dates.filter((d) => d.year === selectedYear).map((d) => d.month),
      ),
    ].sort((a, b) => a - b);

    // 如果选中年份是当前年份，只显示到当前月（不包含未来月份）
    if (selectedYear === currentYear) {
      availableMonths = availableMonths.filter(
        (month) => month <= currentMonth,
      );
    }

    // 检查是否为未来月份
    // 如果选择了未来月份，显示提示文字并隐藏图表
    if (
      this.selectedDayYear > currentYear ||
      (this.selectedDayYear === currentYear &&
        this.selectedDayMonth > currentMonth)
    ) {
      // 隐藏图表，显示提示文字
      this.dayComboChartEl.style.display = "none";
      this.futureMonthMessageEl.style.display = "block";
      return;
    } else {
      // 显示图表，隐藏提示文字
      this.dayComboChartEl.style.display = "block";
      this.futureMonthMessageEl.style.display = "none";
    }

    // 如果没有选中的年份或不在可用列表中，选择当前年份（如果可用）或第一个可用年份
    if (
      !this.selectedDayYear ||
      !availableYears.includes(this.selectedDayYear)
    ) {
      // 如果当前年份在可用列表中，优先使用当前年份
      if (availableYears.includes(currentYear)) {
        this.selectedDayYear = currentYear;
      } else {
        this.selectedDayYear = availableYears[0];
      }
    }

    // 对于月份，必须在确定了年份后，从该年份的可用月份中选择
    // 重新获取该年份的可用月份（因为年份可能刚改变）
    availableMonths = [
      ...new Set(
        dates
          .filter((d) => d.year === this.selectedDayYear)
          .map((d) => d.month),
      ),
    ].sort((a, b) => a - b);

    // 如果选中年份是当前年份，只显示到当前月
    if (this.selectedDayYear === currentYear) {
      availableMonths = availableMonths.filter(
        (month) => month <= currentMonth,
      );
    }

    // ===== 修复年份切换时月份不同步的bug =====
    // 检查当前选中的月份是否在新年份的可用月份中
    const isSelectedMonthAvailable = availableMonths.includes(
      this.selectedDayMonth,
    );

    // 如果当前选中的月份不在新年份的可用月份中，或者没有选中月份，则重新选择月份
    if (!this.selectedDayMonth || !isSelectedMonthAvailable) {
      if (
        this.selectedDayYear === currentYear &&
        availableMonths.includes(currentMonth)
      ) {
        // 如果选中的是当前年份且当前月有数据，优先选择当前月
        this.selectedDayMonth = currentMonth;
      } else {
        // 否则选择最近的可用月份（最大的月份，因为是降序排列）
        this.selectedDayMonth = availableMonths[availableMonths.length - 1];
      }
    }
    // ===== 修复结束 =====

    // 渲染年月筛选标签
    this.renderDayFilter(
      availableYears,
      availableMonths,
      this.selectedDayYear,
      this.selectedDayMonth,
    );

    // 从统一格式过滤出当前选中年月的数据
    const monthDays = this.standardData.dayUsage
      .filter((item) => {
        if (!item.time) return false;
        const [year, month] = item.time.split("-").map(Number);
        return year === this.selectedDayYear && month === this.selectedDayMonth;
      })
      .sort((a, b) => a.time.localeCompare(b.time));

    // 如果没有数据，直接返回
    if (monthDays.length === 0) {
      return;
    }

    // 从统一格式准备图表数据
    const days = monthDays.map((item) => {
      const day = parseInt(item.time.split("-")[2]);
      return `${day}`;
    });
    const costs = monthDays.map((item) => item.total_amount || 0);
    const peakData = monthDays.map((item) => item.usage_ele_peak || 0);
    const valleyData = monthDays.map((item) => item.usage_ele_valley || 0);
    const normalData = monthDays.map((item) => item.usage_ele_norm || 0);
    const sharpData = monthDays.map((item) => item.usage_ele_tip || 0);
    const noTimeData = monthDays.map((item) => item.usage_ele_no || 0);

    // 获取单位符号（从统一数据格式中获取，不设置默认值）
    const usageUnit = monthDays.length > 0 ? monthDays[0].unit : "";

    // 保存数据到缓存，用于重试
    this._dayChartData = {
      days,
      costs,
      peakData,
      valleyData,
      normalData,
      sharpData,
      noTimeData,
      usageUnit,
    };

    // 渲染组合图表 - 使用双重 requestAnimationFrame 确保容器布局完成
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        this.renderDayComboChart(
          days,
          costs,
          peakData,
          valleyData,
          normalData,
          sharpData,
          noTimeData,
          usageUnit,
        );
      });
    });
  }

  // 渲染年月筛选标签
  renderDayFilter(years, months, selectedYear, selectedMonth) {
    // 清空两个容器
    this.dayFilterMainEl.innerHTML = "";
    this.dayFilterYearsEl.innerHTML = "";

    // 创建年份筛选（放在第一行，居中）
    years.forEach((year) => {
      const yearTag = document.createElement("div");
      yearTag.className = "year-tag";
      yearTag.textContent = `${year}`;

      if (year === selectedYear) {
        yearTag.classList.add("active");
      }

      yearTag.addEventListener("click", () => {
        this.selectedDayYear = year;

        // 当切换年份时，检查当前选中的月份是否在新年份中可用
        // 从数据中提取该年份的所有可用月份
        const dates = this.standardData.dayUsage
          .filter((item) => item.time)
          .map((item) => {
            const [year, month] = item.time.split("-");
            return { year: parseInt(year), month: parseInt(month) };
          });

        const now = new Date();
        const currentYear = now.getFullYear();
        const currentMonth = now.getMonth() + 1;

        // 获取新年份的可用月份
        let newYearMonths = [
          ...new Set(dates.filter((d) => d.year === year).map((d) => d.month)),
        ].sort((a, b) => a - b);

        // 如果新年份是当前年份，只显示到当前月
        if (year === currentYear) {
          newYearMonths = newYearMonths.filter(
            (month) => month <= currentMonth,
          );
        }

        // 检查当前选中的月份是否在新年份中可用
        const isCurrentMonthAvailable = newYearMonths.includes(
          this.selectedDayMonth,
        );

        // 如果当前月份不可用或者是未来月份，自动选择一个合适的月份
        if (
          !isCurrentMonthAvailable ||
          (year === currentYear && this.selectedDayMonth > currentMonth)
        ) {
          if (year === currentYear && newYearMonths.includes(currentMonth)) {
            // 如果是当前年份且当前月有数据，优先选择当前月
            this.selectedDayMonth = currentMonth;
          } else if (newYearMonths.length > 0) {
            // 否则选择最新的可用月份（最大的月份）
            this.selectedDayMonth = newYearMonths[newYearMonths.length - 1];
          }
        }

        this.renderDayView();
      });

      this.dayFilterMainEl.appendChild(yearTag);
    });

    // 创建月份筛选（放在第二行，左对齐）
    months.forEach((month) => {
      const monthTag = document.createElement("div");
      monthTag.className = "month-tag";
      monthTag.textContent = `${month}`;

      if (month === selectedMonth) {
        monthTag.classList.add("active");
      }

      monthTag.addEventListener("click", () => {
        this.selectedDayMonth = month;
        this.renderDayView();
      });

      this.dayFilterYearsEl.appendChild(monthTag);
    });
  }

  // 渲染日用电详情组合图
  renderDayComboChart(
    days,
    costs,
    peakData,
    valleyData,
    normalData,
    sharpData,
    noTimeData,
    usageUnit,
  ) {
    if (!this._isEchartsAvailable(this.dayComboChartEl, true)) return;

    // 强制触发浏览器重排，确保容器有正确的尺寸
    const forceReflow = this.dayComboChartEl.offsetWidth;

    // 检查容器尺寸是否有效
    if (
      this.dayComboChartEl.offsetWidth === 0 ||
      this.dayComboChartEl.offsetHeight === 0
    ) {
      setTimeout(() => {
        // 使用缓存数据重试
        if (this._dayChartData) {
          const {
            days,
            costs,
            peakData,
            valleyData,
            normalData,
            sharpData,
            noTimeData,
            usageUnit,
          } = this._dayChartData;
          this.renderDayComboChart(
            days,
            costs,
            peakData,
            valleyData,
            normalData,
            sharpData,
            noTimeData,
            usageUnit,
          );
        } else {
          // 如果没有缓存数据，调用 renderDayView 重新获取数据
          this.renderDayView();
        }
      }, 100);
      return;
    }

    // 销毁现有图表实例
    this._disposeChart("dayViewChart");
    this.dayViewChart = echarts.init(this.dayComboChartEl);

    // 根据 utility_type 设置系列和图例
    const isElectricity = this.utilityType === "ele";
    const barColor = USAGE_CHART_COLOR;

    const series = [];
    const legendData = [];

    // 电力与燃气一致：只显示用量系列
    const totalUsageData = noTimeData.map((val, idx) => {
      let usage = val;
      if (usage === 0) {
        usage =
          (peakData[idx] || 0) +
          (valleyData[idx] || 0) +
          (normalData[idx] || 0) +
          (sharpData[idx] || 0);
      }
      return usage;
    });

    series.push({
      name: "用量",
      type: "bar",
      stack: "usage",
      data: totalUsageData,
      yAxisIndex: 0,
      itemStyle: { color: barColor },
    });
    legendData.push({ name: "用量", icon: "none" });

    // 所有类型都显示费用系列
    series.push({
      name: "消费",
      type: "line",
      data: costs,
      yAxisIndex: 1,
      smooth: true,
      symbol: "circle",
      symbolSize: 6,
      showSymbol: true,
      lineStyle: {
        width: 3,
        color: "#804AFF",
      },
      itemStyle: {
        color: "#804AFF",
        borderColor: "#fff",
        borderWidth: 1.5,
        shadowBlur: 10,
        shadowColor: "rgba(128, 74, 255, 0.5)",
      },
      label: {
        show: true,
        position: "top",
        fontSize: 9,
        color: "#804AFF",
        fontWeight: 600,
        formatter: function (params) {
          if (params.value === 0) {
            return "";
          }
          return (params.value || 0).toFixed(2);
        },
        offset: [0, -8],
      },
      emphasis: {
        focus: "series",
        scale: true,
        scaleSize: 1.3,
        itemStyle: {
          borderColor: "#804AFF",
          borderWidth: 2,
        },
      },
    });

    legendData.push({ name: "消费", icon: "none" });

    const option = {
      tooltip: {
        trigger: "axis",
        axisPointer: {
          type: "cross",
          label: {
            backgroundColor: "#6a7985",
          },
        },
        textStyle: {
          fontSize: 12,
        },
        formatter: (function (
          selectedDayYear,
          selectedDayMonth,
          usageUnit,
          isElectricity,
        ) {
          return function (params) {
            // 日视图tooltip - 显示年-月-日格式
            const day = params[0].axisValue;
            const fullDate = `${selectedDayYear}-${pad2(selectedDayMonth)}-${pad2(day)}`;
            let result = `${fullDate}<br/><br/>`;

            // 先显示费用信息
            const costParam = params.find(
              (param) => param.seriesName === "消费",
            );
            if (costParam) {
              result += `<span style="color:#804AFF">●</span> 消费: <strong>¥${(costParam.value || 0).toFixed(2)}</strong><br/>`;
              result +=
                '<hr style="margin:3px 0;border:none;border-top:1px solid #ddd;">';
            }

            // 显示用量信息
            let hasUsage = false;
            let totalUsage = 0;
            params.forEach((param) => {
              if (param.seriesName === "用量" && param.value > 0) {
                hasUsage = true;
                result += `<span style="display:inline-block;width:10px;height:10px;background:${param.color};margin-right:5px;"></span><strong>${param.seriesName}: ${(param.value || 0).toFixed(2)} ${usageUnit}</strong><br/>`;
                totalUsage += param.value;
              } else if (
                param.seriesName !== "消费" &&
                param.seriesName !== "用量" &&
                param.value > 0
              ) {
                hasUsage = true;
                result += `<span style="display:inline-block;width:10px;height:10px;background:${param.color};margin-right:5px;"></span>${param.seriesName}: ${(param.value || 0).toFixed(2)} ${usageUnit}<br/>`;
                totalUsage += param.value;
              }
            });

            if (hasUsage) {
              result += `<hr style="margin:3px 0;border:none;border-top:1px solid #ddd;">`;
              result += `<strong>总用量: ${(totalUsage || 0).toFixed(2)} ${usageUnit}</strong>`;
            }

            return result;
          };
        })(
          this.selectedDayYear,
          this.selectedDayMonth,
          usageUnit,
          isElectricity,
        ),
      },
      legend: {
        data: legendData,
        bottom: 0,
        itemWidth: 10,
        itemHeight: 20,
        itemGap: 10,
        textStyle: {
          fontSize: 10,
          fontWeight: "bold",
        },
        formatter: function (name) {
          return "{title|" + name + "}";
        },
        textStyle: {
          rich: {
            title: {
              color: "#fff",
              fontSize: 11,
              fontWeight: "bold",
              padding: [3, 3],
              borderRadius: 2,
              backgroundColor: function (params) {
                if (params.name === "用量") {
                  return barColor;
                } else if (params.name === "消费") {
                  return "#804AFF";
                } else if (params.name === "缴费") {
                  return "#00C853";
                }
                return barColor;
              },
            },
          },
        },
      },
      grid: {
        left: "0%",
        right: "0%",
        bottom: "10%",
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: days,
        axisLine: {
          lineStyle: { color: "#666" },
        },
        axisLabel: {
          color: "#666",
          interval: 0,
          rotate: 45,
          fontSize: 9,
        },
      },
      yAxis: [
        {
          type: "value",
          name: usageUnit,
          nameLocation: "end",
          position: "left",
          axisLine: {
            lineStyle: { color: "#666" },
          },
          axisLabel: {
            color: "#666",
            formatter: axisValueFormatter,
          },
          splitLine: {
            lineStyle: { color: "rgba(0,0,0,0.1)" },
          },
        },
        {
          type: "value",
          name: "元",
          position: "right",
          axisLine: {
            lineStyle: { color: "#804AFF" },
          },
          axisLabel: {
            color: "#804AFF",
            formatter: axisValueFormatter,
          },
          splitLine: {
            show: false,
          },
        },
      ],
      series: series,
    };

    this.dayViewChart.setOption(option);
  }

  // 销毁年度图表
  destroyYearCharts() {
    this._disposeChart("yearChart");
  }

  // 销毁月度图表
  destroyMonthCharts() {
    this._disposeChart("monthChart");
  }

  // 销毁日视图图表
  destroyDayCharts() {
    this._disposeChart("dayViewChart");
  }

  // 更新日历视图
  async updateCalendarView() {
    // 如果当前是月视图，重新渲染月视图图表
    if (this.currentView === "month") {
      this.renderMonthView();
      return;
    }

    // 从统一数据接口获取数据
    if (
      !this.standardData ||
      !this.standardData.dayUsage ||
      this.standardData.dayUsage.length === 0
    ) {
      this.renderCalendar([], 0, 0);
      return;
    }

    // 从统一格式筛选当前年月的日数据
    const currentMonthStr = `${this.calCurrentYear}-${pad2(this.calCurrentMonth)}`;
    const monthDays = this.standardData.dayUsage.filter(
      (dayData) => dayData.time && dayData.time.startsWith(currentMonthStr),
    );

    // 如果配置了device_entity，检查每个日期是否有设备历史数据
    if (this.deviceEntityConfig && this.deviceEntityConfig.length > 0) {
      // 创建当前月份的所有日期数组，但只包含今日以前（含今日）的日期
      const allDaysInMonth = [];
      const lastDay = new Date(
        this.calCurrentYear,
        this.calCurrentMonth,
        0,
      ).getDate();

      // 获取当前日期，用于过滤未来日期
      const now = new Date();
      const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());

      for (let day = 1; day <= lastDay; day++) {
        const dayStr = `${this.calCurrentYear}-${pad2(this.calCurrentMonth)}-${pad2(day)}`;

        // 检查是否为未来日期（不包括今天）
        const dayDate = new Date(dayStr);
        const dayStart = new Date(
          dayDate.getFullYear(),
          dayDate.getMonth(),
          dayDate.getDate(),
        );
        if (dayStart > today) {
          // 跳过未来日期，不添加到检查列表
          continue;
        }

        // 查找是否已有对应的数据
        const existingDayData = monthDays.find((d) => d.time === dayStr);

        if (existingDayData) {
          // 如果已有数据，直接使用
          allDaysInMonth.push({
            time: dayStr,
            day: day,
            hasDeviceHistory: existingDayData.hasDeviceHistory || false,
            existingData: true,
          });
        } else {
          // 如果没有数据，创建新的对象
          allDaysInMonth.push({
            time: dayStr,
            day: day,
            hasDeviceHistory: false,
            existingData: false,
          });
        }
      }

      // 只对今日以前（含今日）的日期进行设备历史检查
      const promises = allDaysInMonth.map(async (dayObj) => {
        const dayStr = dayObj.time;
        const dayNum = dayObj.day;

        if (!dayStr) {
          dayObj.hasDeviceHistory = false;
          return;
        }

        const dayDate = new Date(dayStr);
        if (isNaN(dayDate.getTime())) {
          dayObj.hasDeviceHistory = false;
          return;
        }

        const startOfDay = new Date(
          dayDate.getFullYear(),
          dayDate.getMonth(),
          dayDate.getDate(),
        );
        const endOfDay = new Date(
          dayDate.getFullYear(),
          dayDate.getMonth(),
          dayDate.getDate() + 1,
        );

        let hasDeviceHistory = false;
        const devicePromises = this.deviceEntityConfig.map(
          async (deviceConfig) => {
            try {
              const history = await this.getHistory(
                deviceConfig.entity,
                startOfDay,
                endOfDay,
              );
              if (history && history.length > 0) {
                hasDeviceHistory = true;
              }
            } catch (error) {
              // Silently fail
            }
          },
        );

        await Promise.all(devicePromises);
        dayObj.hasDeviceHistory = hasDeviceHistory;
      });

      await Promise.all(promises);

      // 将设备历史信息更新到monthDays中的对应日期
      allDaysInMonth.forEach((dayObj) => {
        if (dayObj.existingData) {
          // 更新已有数据
          const existingDay = monthDays.find((d) => d.time === dayObj.time);
          if (existingDay) {
            existingDay.hasDeviceHistory = dayObj.hasDeviceHistory;
          }
        } else {
          // 为没有用电数据的日期创建数据对象
          if (dayObj.hasDeviceHistory) {
            monthDays.push({
              time: dayObj.time,
              day: dayObj.day,
              total_usage: 0,
              total_amount: 0,
              unit: monthDays.length > 0 ? monthDays[0].unit : "",
              hasDeviceHistory: true,
            });
          }
        }
      });
    }

    // 计算月统计
    let monthUsage = 0;
    let monthCost = 0;
    monthDays.forEach((day) => {
      monthUsage += day.total_usage || 0;
      monthCost += day.total_amount || 0;
    });

    // 计算年统计
    let yearUsage = 0;
    let yearCost = 0;
    const yearStr = this.calCurrentYear.toString();
    this.standardData.dayUsage.forEach((day) => {
      if (day.time && day.time.startsWith(yearStr)) {
        yearUsage += day.total_usage || 0;
        yearCost += day.total_amount || 0;
      }
    });

    // 优先使用实体数据覆盖（与集成一致）：当日历显示本月/上月/当前年时
    const now = new Date();
    const lastMonthDate = new Date(now.getFullYear(), now.getMonth() - 1);
    const isCurrentMonth =
      this.calCurrentYear === now.getFullYear() &&
      this.calCurrentMonth === now.getMonth() + 1;
    const isLastMonth =
      this.calCurrentYear === lastMonthDate.getFullYear() &&
      this.calCurrentMonth === lastMonthDate.getMonth() + 1;
    const isCurrentYear = this.calCurrentYear === now.getFullYear();
    const entity =
      this._hass && this.entityId ? this._hass.states[this.entityId] : null;

    if (entity && entity.attributes) {
      if (this.utilityType === "ele") {
        if (isCurrentMonth) {
          const v = this.getEntityAttribute(entity, "month_ele_num");
          const c = this.getEntityAttribute(entity, "month_ele_cost");
          if (v !== undefined || c !== undefined) {
            monthUsage = parseFloat(v) || 0;
            monthCost = parseFloat(c) || 0;
          }
        } else if (isLastMonth) {
          const v = this.getEntityAttribute(entity, "last_month_ele_num");
          const c = this.getEntityAttribute(entity, "last_month_ele_cost");
          if (v !== undefined || c !== undefined) {
            monthUsage = parseFloat(v) || 0;
            monthCost = parseFloat(c) || 0;
          }
        }
        if (isCurrentYear) {
          const v = this.getEntityAttribute(entity, "year_ele_num");
          const c = this.getEntityAttribute(entity, "year_ele_cost");
          if (v !== undefined || c !== undefined) {
            yearUsage = parseFloat(v) || 0;
            yearCost = parseFloat(c) || 0;
          }
        }
      } else if (this.utilityType === "gas") {
        if (isCurrentMonth) {
          const v = this.getEntityAttribute(entity, "month_gas_num");
          const c = this.getEntityAttribute(entity, "month_gas_cost");
          if (v !== undefined || c !== undefined) {
            monthUsage = parseFloat(v) || 0;
            monthCost = parseFloat(c) || 0;
          }
        } else if (isLastMonth) {
          const v = this.getEntityAttribute(entity, "last_month_gas_num");
          const c = this.getEntityAttribute(entity, "last_month_gas_cost");
          if (v !== undefined || c !== undefined) {
            monthUsage = parseFloat(v) || 0;
            monthCost = parseFloat(c) || 0;
          }
        }
        if (isCurrentYear) {
          const v = this.getEntityAttribute(entity, "year_gas_num");
          const c = this.getEntityAttribute(entity, "year_gas_cost");
          if (v !== undefined || c !== undefined) {
            yearUsage = parseFloat(v) || 0;
            yearCost = parseFloat(c) || 0;
          }
        }
      }
    }

    // 获取单位符号（从月份数据中获取，必须从数据中获取）
    const usageUnit =
      monthDays.length > 0 && monthDays[0]
        ? monthDays[0].unit
        : this.standardData?.unit ||
          this.getUnitByUtilityType(this.utilityType);

    // 更新统计信息
    this.calMonthUsageEl.textContent = `${(monthUsage || 0).toFixed(2)} ${usageUnit}`;
    this.calMonthCostEl.textContent = `¥${(monthCost || 0).toFixed(2)}`;
    this.calYearUsageEl.textContent = `${(yearUsage || 0).toFixed(2)} ${usageUnit}`;
    this.calYearCostEl.textContent = `¥${(yearCost || 0).toFixed(2)}`;

    // 渲染日历
    this.renderCalendar(monthDays, monthUsage, monthCost);
  }

  // 渲染日历网格
  renderCalendar(monthDays, monthUsage, monthCost) {
    // 清空日历网格，保留星期标题
    const grid = this.calendarGridEl;
    while (grid.children.length > 7) {
      grid.removeChild(grid.lastChild);
    }

    const now = new Date();
    const firstDay = new Date(this.calCurrentYear, this.calCurrentMonth - 1, 1);
    const lastDay = new Date(this.calCurrentYear, this.calCurrentMonth, 0);

    // 获取第一天是星期几（0=周日，1=周一...）
    let startDayOfWeek = firstDay.getDay();
    // 调整为0=周一，1=周二...6=周日
    startDayOfWeek = startDayOfWeek === 0 ? 6 : startDayOfWeek - 1;

    // 创建日历数据
    const calendarData = [];

    // 添加空白单元格
    for (let i = 0; i < startDayOfWeek; i++) {
      calendarData.push(null);
    }

    // 添加日期
    for (let day = 1; day <= lastDay.getDate(); day++) {
      const dayStr = `${this.calCurrentYear}-${pad2(this.calCurrentMonth)}-${pad2(day)}`;
      const dayData = monthDays.find((d) => d.time === dayStr);

      if (dayData && dayData.total_usage > 0) {
        calendarData.push({
          day: day,
          usage: dayData.total_usage,
          amount: dayData.total_amount,
          dayData: dayData,
          hasData: true,
        });
      } else {
        // 即使没有用电数据，也要创建包含设备历史信息的日历数据对象
        calendarData.push({
          day: day,
          dayData: dayData
            ? {
                time: dayData.time,
                unit: dayData.unit,
                hasDeviceHistory: dayData.hasDeviceHistory || false,
              }
            : null,
          hasData: false,
        });
      }
    }

    // 创建日期单元格

    // 先计算当月用电量的最大值和最小值
    let maxUsage = 0;
    let minUsage = Infinity;
    let maxUsageDay = null;
    let minUsageDay = null;

    calendarData.forEach((day) => {
      if (day && day.hasData && day.dayData) {
        const usage = parseFloat(day.usage) || 0;
        if (usage > maxUsage) {
          maxUsage = usage;
          maxUsageDay = day.day;
        }
        if (usage < minUsage && usage > 0) {
          minUsage = usage;
          minUsageDay = day.day;
        }
      }
    });

    // 如果所有值都是0，重置minUsage
    if (minUsage === Infinity) {
      minUsage = 0;
    }

    calendarData.forEach((day) => {
      const cell = document.createElement("div");
      cell.className = "calendar-day";

      if (day) {
        cell.classList.add("has-date");

        // 判断是否是今天
        const isToday =
          day.day === now.getDate() &&
          this.calCurrentYear === now.getFullYear() &&
          this.calCurrentMonth === now.getMonth() + 1;

        if (isToday) {
          cell.classList.add("today");
        }

        // 判断是否是当月用电量最大或最小的单元格
        if (day.hasData && day.dayData) {
          const usage = parseFloat(day.usage) || 0;
          if (usage === maxUsage && maxUsage > 0) {
            cell.classList.add("max-usage-day");
          } else if (usage === minUsage && minUsage > 0) {
            cell.classList.add("min-usage-day");
          }
        }

        // 添加预计使用天数的进度条
        // 从UI元素获取剩余天数，支持多用户/多类切换
        let remainingDays = 0;
        const remainingDaysElement =
          this.shadowRoot.querySelector("#remaining-days");
        if (remainingDaysElement) {
          const daysValue = remainingDaysElement.textContent;
          remainingDays = parseInt(daysValue, 10) || 0;
        }

        // 如果 remainingDays 存在且大于0，添加进度条
        if (remainingDays > 0) {
          const todayDate = new Date();
          const startDate = new Date(
            todayDate.getFullYear(),
            todayDate.getMonth(),
            todayDate.getDate(),
          );
          const endDate = new Date(
            startDate.getTime() + remainingDays * 24 * 60 * 60 * 1000,
          );

          // 检查当前日期是否在进度条范围内
          const cellDate = new Date(
            this.calCurrentYear,
            this.calCurrentMonth - 1,
            day.day,
          );

          // 只在当前月份且日期在今天到结束日期之间（含）时显示进度条
          if (
            cellDate >= startDate &&
            cellDate <= endDate &&
            cellDate.getMonth() + 1 === this.calCurrentMonth &&
            cellDate.getFullYear() === this.calCurrentYear
          ) {
            const progressBar = document.createElement("div");
            progressBar.className = "usage-progress-bar";
            cell.appendChild(progressBar);
          }
        }

        // 判断是否是未来日期
        const cellDate = new Date(
          this.calCurrentYear,
          this.calCurrentMonth - 1,
          day.day,
        );
        const isFutureDate = cellDate > now;

        // 如果是未来日期，添加禁用样式
        if (isFutureDate) {
          cell.classList.add("future-date");
          cell.style.opacity = "0.5";
          cell.style.cursor = "not-allowed";
        } else {
          // 非未来日期都可以点击
          cell.style.cursor = "pointer";
        }

        // 创建日期圆圈
        const dateCircle = document.createElement("span");
        dateCircle.className = "date-circle";
        dateCircle.textContent = day.day;
        cell.appendChild(dateCircle);

        // 如果有数据，显示用量和费用
        if (day.hasData) {
          cell.classList.add("has-data");

          // 获取单位符号（必须从数据中获取）
          const usageUnit = day.dayData.unit;

          const dataValue = document.createElement("div");
          dataValue.className = "data-value";
          dataValue.textContent = `${(day.usage || 0).toFixed(2)}${usageUnit}`;
          cell.appendChild(dataValue);

          const calcValue = document.createElement("div");
          calcValue.className = "calc-value";
          calcValue.textContent = `¥${(day.amount || 0).toFixed(2)}`;
          cell.appendChild(calcValue);
        }

        // 如果配置了device_entity且该日期有设备历史数据，显示H标记（无论是否有用电数据）
        if (
          this.deviceEntityConfig &&
          this.deviceEntityConfig.length > 0 &&
          day.dayData &&
          day.dayData.hasDeviceHistory
        ) {
          const deviceMarker = document.createElement("span");
          deviceMarker.className = "device-history-marker";
          deviceMarker.textContent = "H";
          cell.appendChild(deviceMarker);
        }

        // 为所有非未来日期的单元格添加点击事件
        if (!isFutureDate) {
          // 只有当有数据或者配置了device_entity时，才允许点击
          if (
            day.hasData ||
            (this.deviceEntityConfig && this.deviceEntityConfig.length > 0)
          ) {
            cell.addEventListener("click", () => {
              const dayStr = `${this.calCurrentYear}-${pad2(this.calCurrentMonth)}-${pad2(day.day)}`;
              // 如果有数据，显示完整详情；如果无数据，显示空数据提示
              this.showDayModal(dayStr, day.hasData ? day.dayData : null);
            });
          } else {
            // 无数据且没有配置device_entity时，禁用点击
            cell.style.cursor = "not-allowed";
            cell.style.opacity = "0.5";
          }
        }
      }

      grid.appendChild(cell);
    });
  }

  // 显示日详情模态框
  showDayModal(dateStr, dayData) {
    // 设置标题
    const [year, month, day] = dateStr.split("-");
    const modalTitle =
      this.textMap && this.textMap.dayModalTitle
        ? this.textMap.dayModalTitle
        : "用电详情";
    this.dayModalTitleEl.textContent = `${year}年${parseInt(month)}月${parseInt(day)}日 ${modalTitle}`;

    // 清空模态框内容
    this.dayModalBodyEl.innerHTML = "";

    // 检查是否有数据
    if (!dayData) {
      // 无数据时显示提示信息
      const noDataMessage = document.createElement("div");
      noDataMessage.style.textAlign = "center";
      noDataMessage.style.padding = "40px 20px";
      noDataMessage.style.color = "var(--text-color)";
      noDataMessage.style.opacity = "0.7";
      noDataMessage.innerHTML = `
        <div style="font-size: 16px; margin-bottom: 10px;">📊</div>
        <div style="font-size: 14px;">该日期暂无用电数据</div>
        <div style="font-size: 12px; margin-top: 5px;">可能是数据尚未更新或当日无用电记录</div>
      `;

      this.dayModalBodyEl.appendChild(noDataMessage);

      // 如果配置了设备实体，显示设备轨道（即使没有用电数据）
      if (this.deviceEntityConfig && this.deviceEntityConfig.length > 0) {
        this.renderDeviceTracks(dateStr);
      }
    } else {
      // 有数据时显示完整的详情
      // 创建柱状图区域（替代原来的饼图）
      const barChartSection = document.createElement("div");
      barChartSection.className = "pie-chart-section";

      // 添加标题
      const barChartTitle = document.createElement("div");
      barChartTitle.className = "pie-chart-title";
      barChartTitle.style.fontSize = "14px";
      barChartTitle.style.fontWeight = "600";
      barChartTitle.style.color = "var(--text-color)";
      barChartTitle.style.textAlign = "center";
      barChartTitle.style.marginBottom = "0px";
      const barTitle =
        this.textMap && this.textMap.dayBarChartTitle
          ? this.textMap.dayBarChartTitle
          : "历年今日用电对比";
      barChartTitle.textContent = barTitle;
      barChartSection.appendChild(barChartTitle);

      // 创建柱状图容器
      const barChartContainer = document.createElement("div");
      barChartContainer.className = "pie-chart-container";
      barChartContainer.id = "day-bar-chart";

      barChartSection.appendChild(barChartContainer);
      this.dayModalBodyEl.appendChild(barChartSection);

      // 获取所有年份的今天数据
      const todayData = this.getAllYearsTodayData(dateStr);

      // 渲染柱状图
      setTimeout(() => {
        this.renderDayBarChart(todayData, dateStr);
      }, 100);

      // 添加分割线
      const barDivider = document.createElement("div");
      barDivider.style.height = "1px";
      barDivider.style.backgroundColor = "var(--secondary-text)";
      barDivider.style.opacity = "0.3";
      barDivider.style.marginBottom = "10px";
      this.dayModalBodyEl.appendChild(barDivider);

      // 如果配置了设备实体，显示设备轨道
      if (this.deviceEntityConfig && this.deviceEntityConfig.length > 0) {
        this.renderDeviceTracks(dateStr);
      }
    }

    // 显示模态框（使用平滑动画）
    this.dayModalEl.style.display = "flex";

    // 强制重排以确保CSS动画生效
    this.dayModalEl.offsetHeight;

    // 应用隐藏配置
    requestAnimationFrame(() => {
      this.applyHiddenConfig();
    });
  }

  // 渲染日柱状图（替代原来的饼图）
  renderDayBarChart(
    valleyEle,
    peakEle,
    normalEle,
    sharpEle,
    noTimeEle,
    totalEle,
    hasTimeData,
    usageUnit = "",
    historyTodayData = null,
    dateStr = "",
  ) {
    const pieChartEl = this.dayModalBodyEl.querySelector("#day-pie-chart");
    if (!pieChartEl) {
      return;
    }

    // 销毁之前的图表实例
    this._disposeChart("dayChart");
    // 检查ECharts是否可用
    if (!this._isEchartsAvailable()) return;

    this.dayChart = echarts.init(pieChartEl, null, { width: 210, height: 140 });

    // 根据 utility_type 设置颜色和数据
    const isElectricity = this.utilityType === "ele";
    const pieColor = USAGE_CHART_COLOR;

    // 构建数据数组
    const data = [];

    // 电力与燃气一致：只显示用量
    data.push({
      value: totalEle > 0 ? totalEle : 1,
      name: "用量",
      itemStyle: { color: pieColor },
      label: { color: pieColor },
    });

    const option = {
      tooltip: {
        trigger: "item",
        textStyle: {
          fontSize: 10,
        },
        formatter: (function (usageUnit) {
          return function (params) {
            return (
              params.name +
              "：<br/>" +
              params.value +
              usageUnit +
              "<br/>" +
              params.percent +
              "%"
            );
          };
        })(usageUnit),
      },

      series: [
        {
          type: "pie",
          radius: ["25%", "50%"],
          avoidLabelOverlap: false,
          itemStyle: {
            borderRadius: 4,
            borderColor: "#fff",
            borderWidth: 2,
          },
          label: {
            show: true,
            formatter: (function (usageUnit) {
              return function (params) {
                return params.name + "\n" + params.value + usageUnit;
              };
            })(usageUnit),
            fontSize: 11,
            fontWeight: "normal",
            color: function (params) {
              return pieColor;
            },
          },
          labelLine: {
            show: true,
            length: 10,
            length2: 10,
          },
          emphasis: {
            label: {
              show: true,
              fontSize: 12,
              fontWeight: "bold",
            },
            scale: true,
            scaleSize: 5,
          },
          data: data,
        },
      ],
    };

    try {
      this.dayChart.setOption(option);
      // Successfully rendered

      // 确保图表正确渲染尺寸
      setTimeout(() => {
        if (this.dayChart) {
          this.dayChart.resize();
        }
      }, 50);
    } catch (error) {
      // Render failed
    }
  }

  // 渲染日柱状图（显示历年今日数据对比）
  renderDayBarChart(todayData, dateStr) {
    const barChartEl = this.dayModalBodyEl.querySelector("#day-bar-chart");
    if (!barChartEl) {
      return;
    }

    // 销毁之前的图表实例
    this._disposeChart("dayChart");
    // 检查ECharts是否可用
    if (!this._isEchartsAvailable()) return;

    this.dayChart = echarts.init(barChartEl);

    // 根据 utility_type 设置系列和颜色
    const isElectricity = this.utilityType === "ele";
    const barColor = USAGE_CHART_COLOR;

    // 如果数据为空
    if (!todayData || todayData.length === 0) {
      return;
    }

    // 从数据中提取年份、用量和金额
    const years = todayData.map((item) => {
      const date = new Date(item.time);
      return date.getFullYear().toString();
    });

    // 提取完整日期字符串（yyyy-mm-dd）用于tooltip
    const dates = todayData.map((item) => {
      const date = new Date(item.time);
      const year = date.getFullYear();
      const month = pad2(date.getMonth() + 1);
      const day = pad2(date.getDate());
      return `${year}-${month}-${day}`;
    });

    const amounts = todayData.map((item) => item.total_amount || 0);

    // 提取分时数据
    const peakData = todayData.map((item) => item.usage_ele_peak || 0);
    const valleyData = todayData.map((item) => item.usage_ele_valley || 0);
    const normalData = todayData.map((item) => item.usage_ele_norm || 0);
    const sharpData = todayData.map((item) => item.usage_ele_tip || 0);
    const noTimeData = todayData.map((item) => item.usage_ele_no || 0);

    // 获取单位符号（从数据中获取，不设置默认值）
    const usageUnit = todayData.length > 0 ? todayData[0].unit || "" : "";

    // 构建系列配置
    const series = [];
    const legendData = [];

    // 电力与燃气一致：只显示用量系列
    const totalUsageData = todayData.map((item) => item.total_usage || 0);
    series.push({
      name: "用量",
      type: "bar",
      stack: "usage",
      data: totalUsageData,
      yAxisIndex: 0,
      itemStyle: { color: barColor },
    });
    legendData.push({ name: "用量", icon: "none" });

    // 所有类型都显示费用系列（折线图）
    series.push({
      name: "消费",
      type: "line",
      data: amounts,
      yAxisIndex: 1,
      smooth: true,
      symbol: "circle",
      symbolSize: 6,
      showSymbol: true,
      lineStyle: {
        width: 3,
        color: "#804AFF",
      },
      itemStyle: {
        color: "#804AFF",
        borderColor: "#fff",
        borderWidth: 1.5,
        shadowBlur: 10,
        shadowColor: "rgba(128, 74, 255, 0.5)",
      },
      label: {
        show: true,
        position: "top",
        fontSize: 10,
        color: "#804AFF",
        fontWeight: 600,
        formatter: function (params) {
          if (params.value === 0) {
            return "";
          }
          return params.value.toFixed(2);
        },
        offset: [0, -8],
      },
      emphasis: {
        focus: "series",
        scale: true,
        scaleSize: 1.3,
        itemStyle: {
          borderColor: "#804AFF",
          borderWidth: 2,
        },
      },
    });

    legendData.push({ name: "消费", icon: "none" });

    // 图表配置
    const option = {
      tooltip: {
        trigger: "axis",
        axisPointer: {
          type: "none",
        },
        textStyle: {
          fontSize: 10,
        },
        formatter: function (params) {
          // 获取当前柱子的索引
          const dataIndex = params[0].dataIndex;

          // 第一行：从数据中提取的正确日期格式 yyyy-mm-dd
          let result = dates[dataIndex] + "<br/>";

          // 第二行：消费金额
          const costParam = params.find((param) => param.seriesName === "消费");
          if (costParam) {
            result +=
              costParam.marker +
              "消费: ¥" +
              (costParam.value || 0).toFixed(2) +
              "<br/>";
          }

          // 第三行：总用量（计算所有分时数据的和）
          const usageParams = params.filter(
            (param) => param.seriesName !== "消费" && param.value > 0,
          );
          const totalUsage = usageParams.reduce(
            (sum, param) => sum + param.value,
            0,
          );
          if (totalUsage > 0) {
            result += "用量: " + totalUsage.toFixed(2) + usageUnit + "<br/>";
          }

          // 添加分割线
          if (usageParams.length > 0) {
            result +=
              '<hr style="margin:3px 0;border:none;border-top:1px solid #ddd;">';

            // 下方显示分时用电详情
            usageParams.forEach((item) => {
              result +=
                item.marker +
                item.seriesName +
                ": " +
                item.value +
                usageUnit +
                "<br/>";
            });
          }

          return result;
        },
      },
      legend: {
        data: legendData,
        textStyle: {
          fontSize: 10,
          color: "var(--text-color)",
        },
        top: 5,
        itemWidth: 12,
        itemHeight: 8,
        formatter: function (name) {
          const colorMap = {
            峰: "#FF9800",
            谷: "#4CAF50",
            平: "#2196F3",
            尖: "#F44336",
            无分时: "#FF9800",
            用量: barColor,
            消费: "#804AFF",
          };
          return "{title|" + name + "}";
        },
        textStyle: {
          rich: {
            title: {
              color: "#fff",
              fontSize: 11,
              fontWeight: "bold",
              padding: [3, 3],
              borderRadius: 2,
              backgroundColor: function (params) {
                const colorMap = {
                  峰: "#FF9800",
                  谷: "#4CAF50",
                  平: "#2196F3",
                  尖: "#F44336",
                  无分时: "#FF9800",
                  用量: barColor,
                  消费: "#804AFF",
                };
                return colorMap[params.name] || barColor;
              },
            },
          },
        },
      },
      grid: {
        left: "0%",
        right: "0%",
        bottom: "5%",
        top: "30%",
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: years,
        axisLine: {
          lineStyle: { color: "var(--text-color)" },
        },
        axisLabel: {
          color: "var(--text-color)",
          interval: 0,
          rotate: 45,
          fontSize: 10,
        },
      },
      yAxis: [
        {
          type: "value",
          name: usageUnit,
          nameLocation: "end",
          position: "left",
          axisLine: {
            lineStyle: { color: "var(--text-color)" },
          },
          axisLabel: {
            color: "var(--text-color)",
            formatter: axisValueFormatter,
          },
          splitLine: {
            lineStyle: { color: "rgba(0,0,0,0.1)" },
          },
        },
        {
          type: "value",
          name: "元",
          position: "right",
          axisLine: {
            lineStyle: { color: "#804AFF" },
          },
          axisLabel: {
            color: "#804AFF",
            formatter: axisValueFormatter,
          },
          splitLine: {
            show: false,
          },
        },
      ],
      series: series,
    };

    try {
      this.dayChart.setOption(option);

      // 确保图表正确渲染尺寸
      setTimeout(() => {
        if (this.dayChart) {
          this.dayChart.resize();
        }
      }, 50);
    } catch (error) {
      console.error("渲染日柱状图失败:", error);
    }
  }

  // 获取历史今天的数据
  getHistoryTodayData(dateStr) {
    if (
      !this.standardData ||
      !this.standardData.dayUsage ||
      this.standardData.dayUsage.length === 0
    ) {
      return null;
    }

    // 解析日期字符串，获取月份和日期
    const [year, month, day] = dateStr.split("-").map(Number);

    // 查找历史数据中相同月份和日期的数据（排除当前年）
    const historyData = this.standardData.dayUsage.find((item) => {
      if (!item.time) return false;

      const itemDate = new Date(item.time);
      const itemYear = itemDate.getFullYear();
      const itemMonth = itemDate.getMonth() + 1;
      const itemDay = itemDate.getDate();

      // 匹配月份和日期，但不匹配年份（排除当前年）
      return itemYear !== year && itemMonth === month && itemDay === day;
    });

    return historyData || null;
  }

  // 获取所有年份的今天数据
  getAllYearsTodayData(dateStr) {
    if (
      !this.standardData ||
      !this.standardData.dayUsage ||
      this.standardData.dayUsage.length === 0
    ) {
      return [];
    }

    // 解析日期字符串，获取月份和日期
    const [year, month, day] = dateStr.split("-").map(Number);

    // 查找所有年份中相同月份和日期的数据，按年份排序
    const allData = this.standardData.dayUsage
      .filter((item) => {
        if (!item.time) return false;

        const itemDate = new Date(item.time);
        const itemMonth = itemDate.getMonth() + 1;
        const itemDay = itemDate.getDate();

        // 匹配月份和日期
        return itemMonth === month && itemDay === day;
      })
      .sort((a, b) => {
        // 按年份升序排序
        const yearA = new Date(a.time).getFullYear();
        const yearB = new Date(b.time).getFullYear();
        return yearA - yearB;
      });

    return allData;
  }

  // 隐藏日详情模态框
  hideDayModal() {
    this.dayModalEl.style.display = "none";

    // 隐藏提示框
    this.hideTooltip();
    this._disposeChart("dayChart");
    // 清空模态框内容，防止下次打开时冲突
    this.dayModalBodyEl.innerHTML = "";
  }

  // ==================== 缴费历史相关方法 ====================

  // 显示缴费历史模态框（与燃气卡片一致，有数据时展示列表，无数据时显示「暂无缴费记录」）
  async showPayHistoryModal() {
    try {
      const payHistory = await this.fetchPayHistory();
      this.renderPayHistory(payHistory || []);
      this.payHistoryModalEl.style.display = "flex";
    } catch (error) {
      console.log("缴费历史数据获取失败或不存在:", error);
      this.renderPayHistory([]);
      this.payHistoryModalEl.style.display = "flex";
    }
  }

  // 隐藏缴费历史模态框
  hidePayHistoryModal() {
    this.payHistoryModalEl.style.display = "none";
    this._disposeChart("paySourceChart");
    // 清空模态框内容
    this.payHistoryBodyEl.innerHTML = "";
  }

  // 获取缴费历史数据（优先从缴费实体获取，未配置时从余额实体的 history_charges 获取）
  async fetchPayHistory() {
    try {
      if (!this._hass) return [];
      const toPayHistory = (arr) =>
        Array.isArray(arr) && arr.length > 0
          ? arr.map((item) => ({
              time: item.time,
              cost: parseFloat(item.cost || item.amount) || 0,
              source: item.source || item.method || "其他",
            }))
          : [];

      // 1. 余额实体（主实体）的 history_charges
      if (this.entityId) {
        const entity = this._hass.states[this.entityId];
        if (entity && entity.attributes) {
          const historyCharges = entity.attributes.history_charges;
          const result = toPayHistory(historyCharges);
          if (result.length > 0) return result;
          // 2. 电力：余额实体无数据时，尝试 last_payment 实体的 history
          if (
            this.entityId.includes("_balance") &&
            this.utilityType === "ele"
          ) {
            const lastPaymentId = this.entityId.replace(
              "_balance",
              "_last_payment",
            );
            const lastPaymentEntity = this._hass.states[lastPaymentId];
            if (lastPaymentEntity && lastPaymentEntity.attributes) {
              const history = lastPaymentEntity.attributes.history;
              const fallback = toPayHistory(history);
              if (fallback.length > 0) return fallback;
            }
          }
        }
      }
      return [];
    } catch (error) {
      throw error;
    }
  }

  // 渲染缴费历史
  renderPayHistory(payHistory) {
    this.payHistoryBodyEl.innerHTML = "";
    this.payHistoryTitleEl.textContent = "缴费历史";

    if (!payHistory || payHistory.length === 0) {
      const emptyTip = document.createElement("div");
      emptyTip.className = "pay-history-empty";
      emptyTip.style.cssText =
        "padding: 40px 20px; text-align: center; color: var(--secondary-text-color, #727272); font-size: 14px;";
      emptyTip.textContent = "暂无缴费记录";
      this.payHistoryBodyEl.appendChild(emptyTip);
      return;
    }

    // 计算缴费总额和次数
    let totalCost = 0;
    let minDate = null;
    let maxDate = null;

    payHistory.forEach((item) => {
      totalCost += item.cost || 0;

      // 计算日期范围
      if (item.time) {
        const date = new Date(item.time);
        if (!minDate || date < minDate) {
          minDate = date;
        }
        if (!maxDate || date > maxDate) {
          maxDate = date;
        }
      }
    });
    const totalCount = payHistory.length;

    const formatDateStr = (date) => {
      const year = date.getFullYear();
      const month = pad2(date.getMonth() + 1);
      const day = pad2(date.getDate());
      return `${year}-${month}-${day}`;
    };

    this.payHistoryTitleEl.textContent = `缴费历史`;

    // 创建顶部容器
    const topSection = document.createElement("div");
    topSection.className = "pay-history-top-section";

    // 创建摘要区域
    const summaryDiv = document.createElement("div");
    summaryDiv.className = "pay-history-summary";
    summaryDiv.innerHTML = `
      <div class="pay-history-item">
        <div class="pay-history-label">缴费总额</div>
        <div class="pay-history-value">${totalCost.toFixed(1)}</div>
      </div>
      <div class="pay-history-item">
        <div class="pay-history-label">缴费次数</div>
        <div class="pay-history-value">${totalCount}</div>
      </div>
    `;
    topSection.appendChild(summaryDiv);

    // 创建缴费类型饼图
    const paySourceData = {};
    payHistory.forEach((item) => {
      const source = item.source || "未知";
      if (!paySourceData[source]) {
        paySourceData[source] = 0;
      }
      paySourceData[source] += item.cost || 0;
    });

    // 按金额排序
    const sortedSources = Object.entries(paySourceData).sort(
      (a, b) => b[1] - a[1],
    );

    // 为每个缴费类型分配不同颜色（复用 SOURCE_CLASS_MAP，同类同色；多个默认时用调色板区分）
    const defaultIndices = {};
    const colors = sortedSources.map((item) => {
      const source = item[0];
      const matched = SOURCE_CLASS_MAP.find((m) =>
        m.keywords.some((k) => source.includes(k)),
      );
      const sourceClass = matched?.class ?? "default";
      if (sourceClass === "default") {
        defaultIndices[sourceClass] = defaultIndices[sourceClass] || 0;
        const idx = defaultIndices[sourceClass]++;
        return SOURCE_FALLBACK_PALETTE[idx % SOURCE_FALLBACK_PALETTE.length];
      }
      return SOURCE_COLOR_MAP[sourceClass] || SOURCE_COLOR_MAP["default"];
    });

    const pieChartSection = document.createElement("div");
    pieChartSection.className = "pie-chart-section pay-history-pie-section";

    const pieChartTitle = document.createElement("div");
    pieChartTitle.className = "pie-chart-title pay-history-pie-title";
    pieChartTitle.textContent = "缴费方式";
    pieChartSection.appendChild(pieChartTitle);

    const pieChartContent = document.createElement("div");
    pieChartContent.className = "pie-chart-content";

    const pieChartContainer = document.createElement("div");
    pieChartContainer.className =
      "pie-chart-container pay-source-pie-container";
    pieChartContainer.id = "pay-source-pie-chart";
    pieChartContent.appendChild(pieChartContainer);

    pieChartSection.appendChild(pieChartContent);
    topSection.appendChild(pieChartSection);
    this.payHistoryBodyEl.appendChild(topSection);

    // 渲染饼图
    this.renderPaySourcePieChart(pieChartContainer, sortedSources, colors);

    // 创建缴费历史列表
    const listDiv = document.createElement("div");
    listDiv.className = "pay-history-list";

    // 按时间倒序排列（最新的在前面）
    const sortedHistory = [...payHistory].sort((a, b) => {
      return new Date(b.time) - new Date(a.time);
    });

    sortedHistory.forEach((item) => {
      const source = item.source || "";
      const sourceClass =
        SOURCE_CLASS_MAP.find((m) => m.keywords.some((k) => source.includes(k)))
          ?.class ?? "default";
      const recordDiv = document.createElement("div");
      recordDiv.className = "pay-record";
      recordDiv.innerHTML = `
        <div class="pay-record-source ${sourceClass}">${item.source || "其他"}</div>
        <div class="pay-record-time">${formatDateYYYYMMDD(item.time)}</div>
        <div class="pay-record-cost">${(item.cost || 0).toFixed(2)}</div>
      `;
      listDiv.appendChild(recordDiv);
    });

    this.payHistoryBodyEl.appendChild(listDiv);
  }

  // 渲染缴费方式饼图
  renderPaySourcePieChart(container, sortedSources, colors) {
    // 销毁之前的图表实例
    this._disposeChart("paySourceChart");
    // 检查ECharts是否可用
    if (!this._isEchartsAvailable()) return;

    this.paySourceChart = echarts.init(container);

    const data = sortedSources.map((item, index) => ({
      value: item[1],
      name: item[0],
      itemStyle: { color: colors[index % colors.length] },
      label: { color: colors[index % colors.length] },
    }));

    const option = {
      tooltip: {
        trigger: "item",
        textStyle: {
          fontSize: 10,
        },
        formatter: function (params) {
          return (
            params.name +
            "：<br/>" +
            "¥" +
            (params.value || 0).toFixed(2) +
            "<br/>" +
            params.percent +
            "%"
          );
        },
      },

      series: [
        {
          type: "pie",
          radius: ["35%", "70%"],
          center: ["50%", "50%"],
          avoidLabelOverlap: false,
          itemStyle: {
            borderRadius: 4,
            borderColor: "#fff",
            borderWidth: 2,
          },
          label: { show: false },
          labelLine: { show: false },
          emphasis: {
            scale: true,
            scaleSize: 5,
            itemStyle: {
              shadowBlur: 10,
              shadowOffsetX: 0,
              shadowColor: "rgba(0, 0, 0, 0.5)",
            },
          },
          data: data,
        },
      ],
    };

    this.paySourceChart.setOption(option);
    requestAnimationFrame(() => {
      if (this.paySourceChart) this.paySourceChart.resize();
    });
  }

  // 渲染设备轨道
  async renderDeviceTracks(dateStr) {
    if (!this._hass || !this.deviceEntityConfig) {
      return;
    }

    // 创建时间轴容器
    const timelineContainer = document.createElement("div");
    timelineContainer.id = "timeline-container";

    // 添加标题
    const timelineTitle = document.createElement("div");
    timelineTitle.style.textAlign = "center";
    timelineTitle.style.fontSize = "14px";
    timelineTitle.style.fontWeight = "600";
    timelineTitle.style.color = "var(--text-color)";
    timelineTitle.style.marginBottom = "10px";
    timelineTitle.textContent = "设备运行详情";
    timelineContainer.appendChild(timelineTitle);

    // 创建设备轨道容器
    const deviceTracksContainer = document.createElement("div");
    deviceTracksContainer.id = "device-tracks-container";
    timelineContainer.appendChild(deviceTracksContainer);

    // 创建时间标签容器
    const timelineAligner = document.createElement("div");
    timelineAligner.className = "timeline-aligner";
    timelineAligner.innerHTML = `
      <div class="timeline-aligner-spacer"></div>
      <div class="timeline-aligner-spacer-2"></div>
      <div class="time-labels timeline-aligner-content">
        <span>00:00</span>
        <span>08:00</span>
        <span>16:00</span>
        <span>24:00</span>
      </div>
    `;
    timelineContainer.appendChild(timelineAligner);

    // 创建合计行容器
    const totalRowContainer = document.createElement("div");
    totalRowContainer.id = "total-row-container";
    timelineContainer.appendChild(totalRowContainer);

    // 添加到模态框
    this.dayModalBodyEl.appendChild(timelineContainer);

    // 获取设备数据并渲染轨道
    await this.fetchDeviceEvents(dateStr);

    // 如果没有设备运行数据，隐藏整个 timeline-container
    if (!this.deviceEvents || this.deviceEvents.length === 0) {
      timelineContainer.style.display = "none";
    } else {
      this.initializeTracks();
    }

    // 应用隐藏配置
    this.applyHiddenConfig();
  }

  // 获取设备事件
  async fetchDeviceEvents(dateStr) {
    // 确保日期格式正确（YYYY-MM-DD）
    const dateMatch = dateStr.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
    if (!dateMatch) {
      return [];
    }

    const year = dateMatch[1];
    const month = pad2(dateMatch[2]);
    const day = pad2(dateMatch[3]);
    const formattedDateStr = `${year}-${month}-${day}`;

    const startOfDay = new Date(`${formattedDateStr}T00:00:00`);
    const endOfDay = new Date(`${formattedDateStr}T23:59:59`);

    const events = [];
    if (!this.deviceEntityConfig) return events;

    const promises = this.deviceEntityConfig.map(async (deviceConfig) => {
      const entityId = deviceConfig.entity;
      try {
        const history = await this.getHistory(entityId, startOfDay, endOfDay);
        if (history && history.length > 0) {
          const deviceEvents = this.processEntityHistory(
            entityId,
            deviceConfig,
            history,
          );
          events.push(...deviceEvents);
        }
      } catch (error) {
        // Silently fail
      }
    });

    await Promise.all(promises);
    this.deviceEvents = events;
    return events;
  }

  // 获取历史数据
  async getHistory(entityId, startTime, endTime) {
    return new Promise((resolve, reject) => {
      if (!this._hass) {
        resolve([]);
        return;
      }

      // 检查日期对象是否有效
      if (
        !startTime ||
        !endTime ||
        isNaN(startTime.getTime()) ||
        isNaN(endTime.getTime())
      ) {
        resolve([]);
        return;
      }

      this._hass.connection
        .sendMessagePromise({
          type: "history/history_during_period",
          start_time: startTime.toISOString(),
          end_time: endTime.toISOString(),
          entity_ids: [entityId],
          include_start_time_state: true,
          minimal_response: true,
          no_attributes: true,
        })
        .then((response) => {
          resolve(response[entityId] || []);
        })
        .catch((error) => {
          resolve([]);
        });
    });
  }

  // 从历史状态对象中获取状态值（兼容不同版本的Home Assistant）
  getStateValue(stateObj) {
    if (!stateObj) return null;

    // Home Assistant不同版本返回格式不同
    // 有些版本使用'state'属性，有些使用's'属性
    const stateValue =
      stateObj.state !== undefined ? stateObj.state : stateObj.s;
    return stateValue;
  }

  // 处理实体历史数据
  processEntityHistory(entityId, entityConfig, history) {
    const events = [];
    let currentEvent = null;

    history.sort((a, b) => a.lu - b.lu);

    // 获取当前设备的实时状态
    const currentState = this.getCurrentEntityState(entityId);
    const isCurrentlyOn = this.isEntityOn(currentState, entityConfig);

    for (let i = 0; i < history.length; i++) {
      const stateObj = history[i];
      const stateValue = stateObj.s;
      const timestampValue = stateObj.lu;

      const isOn = this.isEntityOn(stateValue, entityConfig);
      const timestamp = new Date(timestampValue * 1000);
      const timeStr = this.formatTime(timestamp);

      if (isOn) {
        if (!currentEvent) {
          currentEvent = {
            device: entityConfig.name || entityId,
            start: timeStr,
            start_timestamp: timestamp,
            entity_id: entityId,
            config: entityConfig,
          };
        }
      } else {
        if (currentEvent) {
          currentEvent.end = timeStr;
          currentEvent.end_timestamp = timestamp;
          events.push(currentEvent);
          currentEvent = null;
        }
      }
    }

    if (currentEvent) {
      // 只有当设备在当前日期（不是历史日期）且正在运行时才标记为进行中
      const today = new Date();
      const isToday =
        currentEvent.start_timestamp.getDate() === today.getDate() &&
        currentEvent.start_timestamp.getMonth() === today.getMonth() &&
        currentEvent.start_timestamp.getFullYear() === today.getFullYear();

      if (isToday && isCurrentlyOn) {
        // 检查是否跨越午夜
        const startOfDay = new Date(today);
        startOfDay.setHours(0, 0, 0, 0);

        if (currentEvent.start_timestamp < startOfDay) {
          // 设备在00:00:00前开启，跨越午夜后还没关闭
          // 使用00:00:00作为开启时间，当前时间作为结束时间
          currentEvent.start_timestamp = new Date(startOfDay);
          currentEvent.start = "00:00";
        }

        currentEvent.isOngoing = true;
        currentEvent.end = null;
        currentEvent.end_timestamp = null;
      } else {
        currentEvent.isOngoing = false;
        currentEvent.end = "23:59";
        // 使用历史数据的日期来设置结束时间，而不是今天的日期
        const endOfDayDate = new Date(currentEvent.start_timestamp);
        endOfDayDate.setHours(23, 59, 59, 999);
        currentEvent.end_timestamp = endOfDayDate;
      }
      events.push(currentEvent);
    }

    return events;
  }

  // 获取当前实体状态
  getCurrentEntityState(entityId) {
    if (!this._hass || !this._hass.states) return null;
    const stateObj = this._hass.states[entityId];
    return stateObj ? stateObj.state : null;
  }

  // 判断实体是否开启
  isEntityOn(state, entityConfig) {
    if (state === undefined || state === null) return false;

    if (entityConfig.on_state) {
      const onStates = entityConfig.on_state.split(",").map((s) => s.trim());
      return onStates.includes(state);
    }

    if (entityConfig.entity.includes("input_boolean")) return state === "on";

    const onStates = [
      "on",
      "open",
      "true",
      "home",
      "active",
      "playing",
      "cooling",
      "heating",
    ];
    const offStates = [
      "off",
      "closed",
      "false",
      "away",
      "idle",
      "paused",
      "unavailable",
      "unknown",
    ];

    const stateLower = String(state).toLowerCase();

    if (onStates.includes(stateLower)) return true;
    if (offStates.includes(stateLower)) return false;

    if (!isNaN(state) && state !== "") return parseFloat(state) > 0;

    return !offStates.includes(stateLower);
  }

  // 初始化轨道
  // 处理轨道点击事件
  handleTrackClick(e, deviceName) {
    if (!this.eventTooltip) return;

    const trackWrapper = e.target.closest(".track-bar-wrapper");
    if (!trackWrapper) {
      this.hideTooltip();
      return;
    }

    const trackBounds = trackWrapper.getBoundingClientRect();
    const clickX = e.clientX;

    const clickOffset = clickX - trackBounds.left;
    const trackWidth = trackBounds.width;
    const clickRatio = Math.max(0, Math.min(1, clickOffset / trackWidth));
    const newTimeSeconds = Math.round((clickRatio * 86400) / 60) * 60;

    const events = this.deviceEvents.filter((ev) => ev.device === deviceName);

    this.showDetailedTooltip(
      e,
      deviceName,
      events,
      trackBounds,
      clickX,
      clickRatio,
      newTimeSeconds,
    );
    e.stopPropagation();
  }

  // 显示详细提示框
  showDetailedTooltip(
    e,
    deviceName,
    events,
    trackBounds,
    clickX,
    clickRatio,
    newTimeSeconds,
  ) {
    if (!this.eventTooltip) return;

    e.preventDefault();

    // 清理之前的正在运行事件定时器
    this.clearOngoingEventTimers();

    // 确保主题变量已应用到 document.documentElement（供 tooltip 使用）
    if (this.lastThemeName && document.documentElement) {
      const theme =
        EleGasCard.COLOR_SCHEMES[this.lastThemeName] ||
        EleGasCard.COLOR_SCHEMES.light;
      Object.keys(theme).forEach((key) => {
        document.documentElement.style.setProperty(key, theme[key]);
      });
    }

    // 先将 tooltip 移出视野外
    this.eventTooltip.style.display = "block";
    this.eventTooltip.style.left = "-1000px";
    this.eventTooltip.style.top = "-1000px";

    // 计算总时长
    let totalDurationSeconds = 0;
    events.forEach((event) => {
      const startSec = this.timeToSeconds(event.start);
      let endSec;

      // 如果是正在运行的事件，使用当前时间计算时长
      if (event.isOngoing) {
        const now = new Date();
        endSec =
          now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
      } else {
        endSec = event.end ? this.timeToSeconds(event.end) : 86400;
      }

      if (endSec < startSec) {
        totalDurationSeconds += 86400 - startSec + endSec;
      } else {
        totalDurationSeconds += Math.max(0, endSec - startSec);
      }
    });
    const totalDurationFormatted = this.formatDuration(totalDurationSeconds);

    this.tooltipDeviceName.textContent = `${deviceName} (${totalDurationFormatted})`;
    this.tooltipEventsList.innerHTML = "";

    const TOOLTIP_MIN_WIDTH = 160;
    const TOOLTIP_MAX_WIDTH = 200;
    const ARROW_SIZE = 7;
    const HORIZONTAL_CLEARANCE = 25;
    const MARGIN = 10;
    const TOOLTIP_MAX_HEIGHT = 150;

    let highlightedElement = null;

    if (events.length === 0) {
      const li = document.createElement("li");
      li.textContent = "本日无运行记录";
      this.tooltipEventsList.appendChild(li);
    } else {
      events.forEach((ev) => {
        const li = document.createElement("li");
        const startSec = this.timeToSeconds(ev.start);
        let endSec;
        let endStr;

        // 如果是正在运行的事件，使用当前时间计算时长
        if (ev.isOngoing) {
          const now = new Date();
          endSec =
            now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
          endStr = "正在运行";

          // 为正在运行的事件添加动画效果
          li.classList.add("on-going-event");

          // 添加实时更新的定时器
          this.startOngoingEventTimer(li, ev, startSec);
        } else {
          endSec = ev.end ? this.timeToSeconds(ev.end) : 86400;
          endStr = ev.end ? ev.end : "23:59";
        }

        let durationSeconds;
        if (endSec < startSec) {
          durationSeconds = 86400 - startSec + endSec;
        } else {
          durationSeconds = Math.max(0, endSec - startSec);
        }

        const duration = this.formatDuration(durationSeconds);

        li.innerHTML = `[${ev.start} - ${endStr}] <span>(${duration})</span>`;

        // 检查点击时间是否在该事件内（使用10分钟容差）
        const TOLERANCE = 60 * 10;
        let isCurrentTimeInEvent = false;

        if (endSec < startSec) {
          if (
            newTimeSeconds >= startSec - TOLERANCE ||
            newTimeSeconds <= endSec + TOLERANCE
          ) {
            isCurrentTimeInEvent = true;
          }
        } else if (
          newTimeSeconds >= startSec - TOLERANCE &&
          newTimeSeconds <= endSec + TOLERANCE
        ) {
          isCurrentTimeInEvent = true;
        }

        if (isCurrentTimeInEvent) {
          li.classList.add("highlighted-event");
          highlightedElement = li;
        }

        this.tooltipEventsList.appendChild(li);
      });
    }

    // 获取tooltip的实际尺寸
    // 不再设置固定宽度，让tooltip自适应内容宽度
    this.eventTooltip.style.width = "auto";
    const TOOLTIP_WIDTH = this.eventTooltip.offsetWidth;
    const TOOLTIP_HEIGHT = Math.min(
      this.eventTooltip.offsetHeight,
      TOOLTIP_MAX_HEIGHT,
    );

    // 使用 fixed 定位，相对于视口，但计算时考虑滚动
    // 保存初始位置用于滚动时更新
    this.tooltipInitialTrackBounds = trackBounds;
    this.tooltipInitialClickX = clickX;
    this.tooltipInitialClickY = e.clientY;
    this.tooltipInitialClickRatio = clickRatio;
    this.tooltipInitialModalScrollTop = this.dayModalContentEl.scrollTop;
    this.tooltipInitialModalScrollLeft = this.dayModalContentEl.scrollLeft;

    // 保存 tooltip 垂直位置相对于 track 的偏移量
    // 这样滚动时可以保持 tooltip 在 track 的相同相对位置
    const trackCenterY = trackBounds.top + trackBounds.height / 2;
    this.tooltipOffsetFromTrackCenter = e.clientY - trackCenterY;

    const trackCenterX = trackBounds.left + trackBounds.width / 2;

    let targetLeft, targetTop;
    let arrowSide;

    if (clickX < trackCenterX) {
      arrowSide = "left";
      targetLeft = clickX + HORIZONTAL_CLEARANCE;
    } else {
      arrowSide = "right";
      const adjustment = 5;
      targetLeft = clickX - TOOLTIP_WIDTH - HORIZONTAL_CLEARANCE - adjustment;
    }

    // 关键修复：初始显示时也使用 track 的相对位置，并加上滚动偏移
    const scrollX = window.scrollX || window.pageXOffset;
    const scrollY = window.scrollY || window.pageYOffset;

    targetTop =
      trackCenterY +
      this.tooltipOffsetFromTrackCenter -
      TOOLTIP_HEIGHT / 2 +
      scrollY;
    targetLeft = targetLeft + scrollX;

    // 确保tooltip不超出视口边界
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    targetTop = Math.max(scrollY + MARGIN, targetTop);
    targetTop = Math.min(
      scrollY + viewportHeight - TOOLTIP_HEIGHT - MARGIN,
      targetTop,
    );
    targetLeft = Math.max(scrollX + MARGIN, targetLeft);
    targetLeft = Math.min(
      scrollX + viewportWidth - TOOLTIP_WIDTH - MARGIN,
      targetLeft,
    );

    let arrowY =
      trackCenterY + this.tooltipOffsetFromTrackCenter - targetTop + scrollY;
    arrowY = Math.max(
      ARROW_SIZE,
      Math.min(TOOLTIP_HEIGHT - ARROW_SIZE, arrowY),
    );

    this.eventTooltip.style.left = `${targetLeft}px`;
    this.eventTooltip.style.top = `${targetTop}px`;
    this.eventTooltip.style.width = `${TOOLTIP_WIDTH}px`;
    this.eventTooltip.style.maxHeight = `${TOOLTIP_MAX_HEIGHT}px`;
    this.eventTooltip.style.setProperty("--arrow-y", `${arrowY}px`);
    this.eventTooltip.dataset.arrow = arrowSide;
    this.eventTooltip.classList.add("active");

    // 添加滚动监听器，让 tooltip 跟随滚动
    this.addScrollListeners();
  }

  // 添加滚动监听器
  addScrollListeners() {
    if (!this.tooltipScrollHandler) {
      this.tooltipScrollHandler = () => {
        if (!this.eventTooltip || this.eventTooltip.style.display === "none")
          return;

        // 重新计算 track 的位置（滚动后位置已改变）
        const trackWrapper =
          this.dayModalContentEl.querySelector(".track-bar-wrapper");
        if (!trackWrapper) return;

        const currentTrackBounds = trackWrapper.getBoundingClientRect();

        // 使用新的 track 位置，但保持与原始点击的相对关系
        const trackCenterX =
          currentTrackBounds.left + currentTrackBounds.width / 2;
        const trackCenterY =
          currentTrackBounds.top + currentTrackBounds.height / 2;
        const currentClickX =
          currentTrackBounds.left +
          this.tooltipInitialClickRatio * currentTrackBounds.width;

        const ARROW_SIZE = 7;
        const HORIZONTAL_CLEARANCE = 25;
        const MARGIN = 10;
        const TOOLTIP_MAX_HEIGHT = 150;

        // 不再设置固定宽度，让tooltip自适应内容宽度
        this.eventTooltip.style.width = "auto";
        const TOOLTIP_WIDTH = this.eventTooltip.offsetWidth;
        const TOOLTIP_HEIGHT = Math.min(
          this.eventTooltip.offsetHeight,
          TOOLTIP_MAX_HEIGHT,
        );

        let targetLeft, targetTop;
        let arrowSide;

        if (currentClickX < trackCenterX) {
          arrowSide = "left";
          targetLeft = currentClickX + HORIZONTAL_CLEARANCE;
        } else {
          arrowSide = "right";
          const adjustment = 5;
          targetLeft =
            currentClickX - TOOLTIP_WIDTH - HORIZONTAL_CLEARANCE - adjustment;
        }

        // 对于 absolute 定位，需要加上 window 的滚动偏移
        const scrollX = window.scrollX || window.pageXOffset;
        const scrollY = window.scrollY || window.pageYOffset;

        targetTop =
          trackCenterY +
          this.tooltipOffsetFromTrackCenter -
          TOOLTIP_HEIGHT / 2 +
          scrollY;
        targetLeft = targetLeft + scrollX;

        // 计算箭头位置（箭头应该指向 track 中心）
        let arrowY = trackCenterY - targetTop + scrollY;
        arrowY = Math.max(
          ARROW_SIZE,
          Math.min(TOOLTIP_HEIGHT - ARROW_SIZE, arrowY),
        );

        this.eventTooltip.style.left = `${targetLeft}px`;
        this.eventTooltip.style.top = `${targetTop}px`;
        this.eventTooltip.style.setProperty("--arrow-y", `${arrowY}px`);
        this.eventTooltip.dataset.arrow = arrowSide;
      };

      // 监听 window 的滚动事件
      window.addEventListener("scroll", this.tooltipScrollHandler);
    }
  }

  // 隐藏提示框
  hideTooltip() {
    if (this.eventTooltip) {
      this.eventTooltip.style.display = "none";
      this.eventTooltip.classList.remove("active");
    }
    // 移除滚动监听器
    if (this.tooltipScrollHandler) {
      window.removeEventListener("scroll", this.tooltipScrollHandler);
      this.tooltipScrollHandler = null;
    }
    // 清理正在运行事件的定时器
    this.clearOngoingEventTimers();
  }

  // 格式化时长 - 统一显示为x.xh格式
  formatDuration(totalSeconds) {
    if (totalSeconds < 60) return `${(totalSeconds / 3600).toFixed(1)}h`;

    const hours = totalSeconds / 3600;
    return `${hours.toFixed(1)}h`;
  }

  // 为正在运行的事件启动定时器
  startOngoingEventTimer(liElement, event, startSec) {
    // 清除之前的定时器
    if (liElement.ongoingTimer) {
      clearInterval(liElement.ongoingTimer);
    }

    // 每秒更新一次时长
    liElement.ongoingTimer = setInterval(() => {
      const now = new Date();
      const currentSeconds =
        now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();

      let durationSeconds;
      if (currentSeconds < startSec) {
        durationSeconds = 86400 - startSec + currentSeconds;
      } else {
        durationSeconds = Math.max(0, currentSeconds - startSec);
      }

      const duration = this.formatDuration(durationSeconds);

      // 更新显示内容
      const span = liElement.querySelector("span");
      if (span) {
        span.textContent = `(${duration})`;
      }
    }, 1000);

    // 保存定时器引用以便清理
    this.ongoingEventTimers = this.ongoingEventTimers || [];
    this.ongoingEventTimers.push(liElement.ongoingTimer);
  }

  // 清理正在运行事件的定时器
  clearOngoingEventTimers() {
    if (this.ongoingEventTimers) {
      this.ongoingEventTimers.forEach((timer) => clearInterval(timer));
      this.ongoingEventTimers = [];
    }
  }

  async initializeTracks() {
    const deviceTracksContainer = this.dayModalBodyEl.querySelector(
      "#device-tracks-container",
    );
    if (!deviceTracksContainer) return;

    deviceTracksContainer.innerHTML = "";

    const activeDeviceNames = new Set(
      this.deviceEvents.map((event) => event.device),
    );

    if (activeDeviceNames.size === 0) {
      deviceTracksContainer.innerHTML = `<div style="text-align:center; padding:10px; color:#999; font-size:12px;">没有找到设备事件记录</div>`;
      return;
    }

    // 获取当前日期
    const currentDate = this.dayModalTitleEl.textContent.match(
      /\d{4}年\d{1,2}月\d{1,2}日/,
    );
    if (!currentDate) return;

    // 解析日期并确保月份和日期是两位数
    const dateMatch = currentDate[0].match(/(\d{4})年(\d{1,2})月(\d{1,2})日/);
    if (!dateMatch) return;

    const year = dateMatch[1];
    const month = pad2(dateMatch[2]);
    const day = pad2(dateMatch[3]);
    const dateStr = `${year}-${month}-${day}`;

    const startOfDay = new Date(`${dateStr}T00:00:00`);
    const endOfDay = new Date(`${dateStr}T23:59:59`);

    // 初始化设备轨道数据
    this.deviceTracks = [];

    // 调试信息

    // 为每个设备创建轨道
    for (const deviceName of Array.from(activeDeviceNames).sort((a, b) =>
      a.localeCompare(b, "zh-CN"),
    )) {
      const trackDiv = document.createElement("div");
      trackDiv.className = "device-track";

      const labelSpan = document.createElement("span");
      labelSpan.className = "device-label";
      labelSpan.textContent = deviceName;

      // 创建用电量和使用时长的容器
      const powerUsageContainer = document.createElement("div");
      powerUsageContainer.className = "power-usage-container";

      // 用电量显示元素
      const powerUsageSpan = document.createElement("span");

      // 使用时长显示元素
      const usageDurationSpan = document.createElement("span");
      usageDurationSpan.className = "usage-duration";

      // 查找设备配置
      const deviceConfig = this.deviceEntityConfig?.find(
        (config) => config.name === deviceName,
      );
      let powerUsage = null;
      let usageDuration = null;
      let isEstimate = false; // 标记是否为估算值

      // 调试信息

      // 如果有power_entity，查询当日00:00和23:59:59的值，计算差值
      if (deviceConfig && deviceConfig.power_entity) {
        try {
          const history = await this.getHistory(
            deviceConfig.power_entity,
            startOfDay,
            endOfDay,
          );

          if (history && history.length >= 2) {
            // 获取第一个值（最接近00:00:00）和最后一个值（最接近23:59:59）
            const firstState = history[0];
            const lastState = history[history.length - 1];

            // 使用统一的方法获取状态值，兼容不同版本的Home Assistant
            const firstStateValue = this.getStateValue(firstState);
            const lastStateValue = this.getStateValue(lastState);

            if (
              firstStateValue !== null &&
              firstStateValue !== undefined &&
              firstStateValue !== "unavailable" &&
              lastStateValue !== null &&
              lastStateValue !== undefined &&
              lastStateValue !== "unavailable"
            ) {
              const firstValue = parseFloat(firstStateValue);
              const lastValue = parseFloat(lastStateValue);

              if (!isNaN(firstValue) && !isNaN(lastValue)) {
                // 计算当日用电量 = 23:59的值 - 00:00的值
                const dailyUsage = lastValue - firstValue;
                if (dailyUsage >= 0) {
                  powerUsage = (dailyUsage || 0).toFixed(2);
                  // 实际测量值，使用 power-usage 类
                  powerUsageSpan.className = "power-usage";
                } else {
                  powerUsage = "0.00";
                  powerUsageSpan.className = "power-usage";
                }
              } else {
                powerUsageSpan.className = "power-usage";
              }
            } else {
              powerUsageSpan.className = "power-usage";
            }
          } else if (history && history.length === 1) {
            // 如果只有一条数据，显示该值（可能是瞬时值）
            const stateValue = this.getStateValue(history[0]);
            if (
              stateValue !== null &&
              stateValue !== undefined &&
              stateValue !== "unavailable"
            ) {
              const value = parseFloat(stateValue);
              if (!isNaN(value)) {
                powerUsage = value.toFixed(2);
                powerUsageSpan.className = "power-usage";
              }
            }
          } else {
            powerUsageSpan.className = "power-usage";
          }
        } catch (error) {
          powerUsageSpan.className = "power-usage";
        }
      } else if (deviceConfig && deviceConfig.power) {
        // 如果有power配置，使用功率乘以使用时长计算用电量
        const powerValue = parseFloat(deviceConfig.power);
        if (!isNaN(powerValue) && powerValue > 0) {
          // 计算使用时长（从设备事件中获取）
          const deviceEventsForTrack = this.deviceEvents.filter(
            (event) => event.device === deviceName,
          );
          if (deviceEventsForTrack.length > 0) {
            let totalSeconds = 0;
            deviceEventsForTrack.forEach((event) => {
              const startSec = this.timeToSeconds(event.start);

              // 对于正在运行的事件，使用当前时间作为结束时间
              let endSec;
              if (event.isOngoing) {
                const now = new Date();
                const currentSeconds =
                  now.getHours() * 3600 +
                  now.getMinutes() * 60 +
                  now.getSeconds();
                endSec = currentSeconds;
              } else {
                endSec = event.end ? this.timeToSeconds(event.end) : 86400;
              }

              // 处理跨越午夜的情况
              if (endSec < startSec) {
                totalSeconds += 86400 - startSec + endSec;
              } else {
                totalSeconds += Math.max(0, endSec - startSec);
              }
            });

            // 计算用电量：功率 × 时长（小时）÷ 1000
            const totalHours = totalSeconds / 3600;
            const calculatedUsage = (powerValue * totalHours) / 1000;
            powerUsage = calculatedUsage.toFixed(2);

            // 估算值，使用 power-usage-estimate 类
            powerUsageSpan.className = "power-usage-estimate";
            isEstimate = true;
          }
        }
      } else {
        // 默认使用 power-usage 类
        powerUsage = "0.00";
        powerUsageSpan.className = "power-usage";
      }

      // 计算使用时长（从设备事件中获取）
      const deviceEventsForTrack = this.deviceEvents.filter(
        (event) => event.device === deviceName,
      );
      if (deviceEventsForTrack.length > 0) {
        let totalSeconds = 0;
        deviceEventsForTrack.forEach((event) => {
          const startSec = this.timeToSeconds(event.start);

          // 对于正在运行的事件，使用当前时间作为结束时间
          let endSec;
          if (event.isOngoing) {
            const now = new Date();
            const currentSeconds =
              now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
            endSec = currentSeconds;
          } else {
            endSec = event.end ? this.timeToSeconds(event.end) : 86400;
          }

          // 处理跨越午夜的情况
          if (endSec < startSec) {
            totalSeconds += 86400 - startSec + endSec;
          } else {
            totalSeconds += Math.max(0, endSec - startSec);
          }
        });
        usageDuration = this.formatDuration(totalSeconds);
      }

      // 获取单位（从统一数据格式中获取）
      const usageUnit = this.standardData.unit || "";
      powerUsageSpan.textContent =
        powerUsage !== null ? `${powerUsage}${usageUnit}` : "";
      usageDurationSpan.textContent =
        usageDuration !== null ? usageDuration : "";

      // 将用电量和时长添加到容器中
      powerUsageContainer.appendChild(powerUsageSpan);
      powerUsageContainer.appendChild(usageDurationSpan);

      // 保存设备轨道数据
      if (powerUsage !== null) {
        this.deviceTracks.push({
          device: deviceName,
          powerUsage: powerUsage,
          usageDuration: usageDuration,
          isEstimate: isEstimate,
        });
      }

      const barWrapperDiv = document.createElement("div");
      barWrapperDiv.className = "track-bar-wrapper";
      barWrapperDiv.dataset.device = deviceName;
      barWrapperDiv.style.cursor = "pointer";

      // 添加点击事件监听器
      barWrapperDiv.addEventListener("click", (e) =>
        this.handleTrackClick(e, deviceName),
      );

      const barDiv = document.createElement("div");
      barDiv.className = "track-bar";

      barWrapperDiv.appendChild(barDiv);
      trackDiv.appendChild(labelSpan);
      trackDiv.appendChild(powerUsageContainer);
      trackDiv.appendChild(barWrapperDiv);
      deviceTracksContainer.appendChild(trackDiv);

      // 为轨道添加事件块
      deviceEventsForTrack.forEach((event) => {
        const startSec = this.timeToSeconds(event.start);

        // 如果是正在运行的事件，使用当前时间作为结束时间
        let endSec;
        if (event.isOngoing) {
          const now = new Date();
          const currentSeconds =
            now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
          endSec = currentSeconds;
        } else {
          endSec = event.end ? this.timeToSeconds(event.end) : 86400;
        }

        const left = (startSec / 86400) * 100;
        const fill = document.createElement("div");
        fill.className = `track-fill`;

        // 如果是正在运行的事件，添加on-going类
        if (event.isOngoing) {
          fill.classList.add("on-going");
        }

        // 获取设备颜色
        const color = this.getDeviceColor(deviceName);
        fill.style.backgroundColor = color;
        fill.style.left = `${left}%`;

        const durationSec = endSec - startSec;

        if (durationSec < 300) {
          fill.style.width = "4px";
        } else {
          const width = (durationSec / 86400) * 100;
          fill.style.width = `${width}%`;
        }

        barDiv.appendChild(fill);
      });
    }

    // 创建合计行
    this.createTotalRow(activeDeviceNames);
  }

  // 创建合计行
  createTotalRow(activeDeviceNames) {
    const totalRowContainer = this.dayModalBodyEl.querySelector(
      "#total-row-container",
    );
    if (!totalRowContainer) return;

    totalRowContainer.innerHTML = "";

    // 格式化时长为中文格式（xx小时xx分钟）
    const formatDurationChinese = (totalSeconds) => {
      if (totalSeconds < 60) return `${Math.round(totalSeconds)}秒`;

      const hours = Math.floor(totalSeconds / 3600);
      const minutes = Math.floor((totalSeconds % 3600) / 60);

      if (hours > 0) {
        if (minutes > 0) {
          return `${hours}小时${minutes}分钟`;
        } else {
          return `${hours}小时`;
        }
      } else {
        return `${minutes}分钟`;
      }
    };

    // 计算合计数据
    let totalDeviceCount = 0;
    let totalPowerUsage = 0;
    let totalDurationSeconds = 0;

    // 遍历所有活跃设备
    for (const deviceName of Array.from(activeDeviceNames)) {
      // 查找设备配置
      const deviceConfig = this.deviceEntityConfig?.find(
        (config) => config.name === deviceName,
      );
      if (!deviceConfig) continue;

      totalDeviceCount++;

      // 获取设备用电量
      const deviceEventsForTrack = this.deviceEvents.filter(
        (event) => event.device === deviceName,
      );
      let devicePowerUsage = 0;
      let deviceDurationSeconds = 0;

      // 计算设备使用时长
      deviceEventsForTrack.forEach((event) => {
        const startSec = this.timeToSeconds(event.start);

        // 对于正在运行的事件，使用当前时间作为结束时间
        let endSec;
        if (event.isOngoing) {
          const now = new Date();
          const currentSeconds =
            now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
          endSec = currentSeconds;
        } else {
          endSec = event.end ? this.timeToSeconds(event.end) : 86400;
        }

        // 处理跨越午夜的情况
        if (endSec < startSec) {
          deviceDurationSeconds += 86400 - startSec + endSec;
        } else {
          deviceDurationSeconds += Math.max(0, endSec - startSec);
        }
      });

      totalDurationSeconds += deviceDurationSeconds;

      // 使用设备轨道中已经计算好的用电量（包括实际值和估算值）
      const deviceTrack = this.deviceTracks?.find(
        (track) => track.device === deviceName,
      );
      if (deviceTrack && deviceTrack.powerUsage !== null) {
        // 使用设备轨道中已经计算好的用电量
        devicePowerUsage = parseFloat(deviceTrack.powerUsage) || 0;
      } else {
        // 如果找不到设备轨道数据，使用功率估算作为备用方案
        const powerValue = parseFloat(deviceConfig.power) || 0;
        if (powerValue > 0) {
          const totalHours = deviceDurationSeconds / 3600;
          devicePowerUsage = (powerValue * totalHours) / 1000;
        }
      }

      totalPowerUsage += devicePowerUsage;
    }

    // 格式化时长 - 使用专门的格式显示"xx小时xx分钟"
    const totalDurationFormatted = formatDurationChinese(totalDurationSeconds);

    // 创建合计行
    const totalRow = document.createElement("div");
    totalRow.className = "total-row";

    // 设备数量
    const totalLabel = document.createElement("span");
    totalLabel.className = "total-label";
    totalLabel.textContent = `${totalDeviceCount}个设备`;

    // 用电量合计
    const totalPowerSpan = document.createElement("span");
    totalPowerSpan.className = "total-value";
    const usageUnit = this.standardData.unit || "";
    totalPowerSpan.textContent = `${(totalPowerUsage || 0).toFixed(2)}${usageUnit}`;

    // 使用时长合计
    const totalDurationSpan = document.createElement("span");
    totalDurationSpan.className = "total-value";
    totalDurationSpan.textContent = totalDurationFormatted;

    // 创建空的轨道占位符
    const trackPlaceholder = document.createElement("div");
    trackPlaceholder.style.flexGrow = "1";

    totalRow.appendChild(totalLabel);
    totalRow.appendChild(totalPowerSpan);
    totalRow.appendChild(totalDurationSpan);
    totalRow.appendChild(trackPlaceholder);

    totalRowContainer.appendChild(totalRow);
  }

  // 获取设备颜色
  getDeviceColor(deviceName) {
    if (deviceName.includes("灯")) return "#fcc800";
    if (deviceName.includes("插座")) return "#1e2a78";
    if (deviceName.includes("人在")) return "#9079ad";
    if (deviceName.includes("监控")) return "#ff5959";
    if (deviceName.includes("空调")) return "#21e6c1";
    return "#aa4c8f";
  }

  // 时间转秒数
  timeToSeconds(time) {
    if (!time) return 0;
    const [hours, minutes] = time.split(":").map(Number);
    return hours * 3600 + minutes * 60;
  }

  // 格式化时间
  formatTime(date) {
    const hours = pad2(date.getHours());
    const minutes = pad2(date.getMinutes());
    return `${hours}:${minutes}`;
  }

  // 初始化ECharts - 使用新的加载函数
  initializeECharts() {
    // 使用全局的loadECharts函数
    return loadECharts()
      .then(() => {
        this.echartsLoaded = true;
      })
      .catch((err) => {
        console.error("ECharts初始化失败:", err);
        this.echartsLoaded = false;
      });
  }

  // 统一的配置更新函数，同时处理阶梯电价和计费周期（仅非电力类型使用）
  updateTierAndPeriodConfig(config, updateDisplay = true) {
    // 当 utility_type 为 'ele' 时，此方法不应被调用
    if (this.utilityType === "ele") {
      console.warn(
        "updateTierAndPeriodConfig 被调用，但 utility_type 为 ele，此方法不应被调用",
      );
      return;
    }

    // 更新阶梯配置
    if (config.tier1_max !== undefined) {
      this.tierConfig.tiers[0].max = parseFloat(config.tier1_max);
    }
    if (config.tier1_price !== undefined) {
      this.tierConfig.tiers[0].price = parseFloat(config.tier1_price);
    }

    if (config.tier2_max !== undefined) {
      this.tierConfig.tiers[1].max = parseFloat(config.tier2_max);
    }
    if (config.tier2_price !== undefined) {
      this.tierConfig.tiers[1].price = parseFloat(config.tier2_price);
    }

    if (config.tier3_price !== undefined) {
      this.tierConfig.tiers[2].price = parseFloat(config.tier3_price);
    }

    // 更新计费周期配置
    let periodConfig;
    if (config.billing_cycle !== undefined) {
      periodConfig = this.parseBillingCycle(config.billing_cycle);
    } else if (
      config.period_start_month !== undefined ||
      config.period_end_month !== undefined
    ) {
      periodConfig = {
        periodStartMonth: parseInt(config.period_start_month) || 7,
        periodStartDay: parseInt(config.period_start_day) || 1,
        periodEndMonth: parseInt(config.period_end_month) || 6,
        periodEndDay: parseInt(config.period_end_day) || 30,
      };
    }

    if (periodConfig) {
      this.tierConfig.periodStartMonth = periodConfig.periodStartMonth;
      this.tierConfig.periodStartDay = periodConfig.periodStartDay;
      this.tierConfig.periodEndMonth = periodConfig.periodEndMonth;
      this.tierConfig.periodEndDay = periodConfig.periodEndDay;
    }

    // 更新阶梯显示
    if (updateDisplay) {
      this.updateTierDisplay();
    }
  }

  // 更新阶梯电价配置（仅非电力类型使用） - 已合并到 updateTierAndPeriodConfig
  updateTierConfig(config) {
    this.updateTierAndPeriodConfig(config, true);
  }

  // 更新背景色配置
  updateBackgroundConfig(config) {
    if (config.background) {
      // 设置卡片背景
      this.electricityCardEl.style.background = config.background;
    } else {
      // 使用默认背景
      this.electricityCardEl.style.background = this.defaultConfig.background;
    }
  }

  // 解析计费周期字符串 (格式: M.D-M.D 如: 7.1-6.30, 1.1-12.31, 12.1-11.30)
  parseBillingCycle(billingCycleStr) {
    // 默认值
    let periodStartMonth = 7;
    let periodStartDay = 1;
    let periodEndMonth = 6;
    let periodEndDay = 30;

    if (billingCycleStr) {
      try {
        // 分割开始和结束日期
        const parts = billingCycleStr.split("-");
        if (parts.length === 2) {
          // 解析开始日期
          const startParts = parts[0].split(".");
          if (startParts.length === 2) {
            periodStartMonth = parseInt(startParts[0]);
            periodStartDay = parseInt(startParts[1]);
          }

          // 解析结束日期
          const endParts = parts[1].split(".");
          if (endParts.length === 2) {
            periodEndMonth = parseInt(endParts[0]);
            periodEndDay = parseInt(endParts[1]);
          }
        }
      } catch (error) {
        // Use default values silently
      }
    }

    return {
      periodStartMonth,
      periodStartDay,
      periodEndMonth,
      periodEndDay,
    };
  }

  // 更新计费周期配置（仅非电力类型使用） - 已合并到 updateTierAndPeriodConfig
  updatePeriodConfig(config) {
    this.updateTierAndPeriodConfig(config, false);
  }

  // 统一的计费标准数据获取工具函数（消除重复代码）
  getBillingStandardData() {
    // 检查基本条件
    if (!this._hass || !this.entityId) {
      return null;
    }

    const entity = this._hass.states[this.entityId];
    const billingStandard = this.getBillingStandardObject(entity);

    if (!billingStandard || typeof billingStandard !== "object") {
      return null;
    }

    return billingStandard;
  }

  // 更新阶梯显示（重构版本 - 使用统一的数据获取）
  updateTierDisplay() {
    const billingStandard = this.getBillingStandardData();

    // 【集中处理 utility_type 为 'ele' 的逻辑】从计费标准获取阶梯配置
    if (billingStandard) {
      // 判断计费标准类型，支持年阶梯、月阶梯和平均单价
      const billingStandardType = billingStandard["计费标准"] || "";

      // 处理平均单价计费方式
      if (
        billingStandardType === "平均单价" ||
        billingStandard["平均单价"] !== undefined
      ) {
        const usageUnit = this.standardData.unit || "°";
        const avgPrice = billingStandard["平均单价"];

        if (avgPrice !== undefined) {
          // 对于平均单价，所有阶梯显示相同的价格
          const priceHtml = `<div class="price-item-block">单价：${this.formatPrice(avgPrice)}元/${usageUnit}</div>`;

          // 更新阶梯范围（显示为不适用）
          this.tier1RangeEl.textContent = `不适用`;
          this.tier2RangeEl.textContent = `不适用`;
          this.tier3RangeEl.textContent = `不适用`;

          // 更新电价显示（所有阶梯相同）
          this.tier1PriceEl.innerHTML = priceHtml;
          this.tier2PriceEl.innerHTML = priceHtml;
          this.tier3PriceEl.innerHTML = priceHtml;

          // 更新阶梯周期显示
          this.tierPeriodEl.textContent = `计费方式: 平均单价`;

          // 移除所有红色竖线指示器
          const redLines = this.shadowRoot.querySelectorAll(
            ".red-line-indicator",
          );
          redLines.forEach((line) => line.remove());

          // 移除当前阶梯的current类
          this.tier1El.classList.remove("current");
          this.tier2El.classList.remove("current");
          this.tier3El.classList.remove("current");

          // 移除所有阶梯高亮指示器
          const currentIndicators = this.shadowRoot.querySelectorAll(
            ".current-tier-indicator",
          );
          currentIndicators.forEach((indicator) => indicator.remove());

          this.debugLog(
            `[updateTierDisplay] 平均单价: ${avgPrice}元/${usageUnit}`,
          );
          return;
        }
      }

      const isMonthlyTier = billingStandardType.includes("月阶梯");

      // 根据 utility_type 选择正确的字段名，避免串扰
      let tier2Start, tier3Start;

      if (this.utilityType === "ele") {
        // 电力类型：优先使用电量字段
        tier2Start =
          billingStandard[
            isMonthlyTier ? "月阶梯第2档起始电量" : "年阶梯第2档起始电量"
          ];
        tier3Start =
          billingStandard[
            isMonthlyTier ? "月阶梯第3档起始电量" : "年阶梯第3档起始电量"
          ];
      } else if (this.utilityType === "gas") {
        // 燃气类型：使用气量字段
        tier2Start =
          billingStandard[
            isMonthlyTier ? "月阶梯第2档起始气量" : "年阶梯第2档起始气量"
          ];
        tier3Start =
          billingStandard[
            isMonthlyTier ? "月阶梯第3档起始气量" : "年阶梯第3档起始气量"
          ];
      } else {
        // 兼容模式：如果类型不明确，按原逻辑回退（尝试所有可能）
        tier2Start =
          billingStandard[
            isMonthlyTier ? "月阶梯第2档起始电量" : "年阶梯第2档起始电量"
          ];
        tier3Start =
          billingStandard[
            isMonthlyTier ? "月阶梯第3档起始电量" : "年阶梯第3档起始电量"
          ];

        if (tier2Start === undefined) {
          tier2Start =
            billingStandard[
              isMonthlyTier ? "月阶梯第2档起始气量" : "年阶梯第2档起始气量"
            ];
        }
        if (tier3Start === undefined) {
          tier3Start =
            billingStandard[
              isMonthlyTier ? "月阶梯第3档起始气量" : "年阶梯第3档起始气量"
            ];
        }
      }

      if (tier2Start !== undefined && tier3Start !== undefined) {
        // 更新 tierConfig（从计费标准）
        this.tierConfig.tiers[0].max = parseFloat(tier2Start);
        this.tierConfig.tiers[1].max = parseFloat(tier3Start);

        // 获取用电量单位
        const usageUnit = this.standardData.unit || "°";

        // 生成阶梯范围文本
        const tier1RangeText = `0-${tier2Start}${usageUnit}`;
        const tier2RangeText = `${tier2Start + 1}-${tier3Start}${usageUnit}`;
        const tier3RangeText = `${tier3Start + 1}${usageUnit}以上`;

        this.tier1RangeEl.textContent = tier1RangeText;
        this.tier2RangeEl.textContent = tier2RangeText;
        this.tier3RangeEl.textContent = tier3RangeText;

        // 更新电价显示（支持6种计费标准：年阶梯峰平谷、年阶梯、月阶梯峰平谷、月阶梯峰平谷变动价格、月阶梯、平均单价）
        this.tier1PriceEl.innerHTML = this.renderPriceBlock({}, 1);
        this.tier2PriceEl.innerHTML = this.renderPriceBlock({}, 2);
        this.tier3PriceEl.innerHTML = this.renderPriceBlock({}, 3);

        // 更新阶梯周期显示（从计费标准获取）- 支持年阶梯和月阶梯
        const billingStandardType = billingStandard["计费标准"] || "未知";
        let startDate, endDate;

        if (billingStandardType.includes("月阶梯")) {
          // 月阶梯：周期为本月1日到本月最后一日
          const currentDate = new Date();
          const currentYear = currentDate.getFullYear();
          const currentMonth = currentDate.getMonth() + 1;
          const lastDay = new Date(currentYear, currentMonth, 0).getDate();

          startDate = `${currentYear}.${currentMonth}.1`;
          endDate = `${currentYear}.${currentMonth}.${lastDay}`;

          // 更新 tierConfig 中的周期配置
          this.tierConfig.periodStartMonth = currentMonth;
          this.tierConfig.periodStartDay = 1;
          this.tierConfig.periodEndMonth = currentMonth;
          this.tierConfig.periodEndDay = lastDay;
        } else {
          // 年阶梯：从计费标准读取
          startDate = billingStandard["当前年阶梯起始日期"];
          endDate = billingStandard["当前年阶梯结束日期"];
        }

        if (startDate && endDate) {
          // 解析日期格式 YYYY.MM.DD 并转换为 MM.DD
          const startParts = startDate.split(".");
          const endParts = endDate.split(".");

          if (startParts.length >= 3 && endParts.length >= 3) {
            const startMMDD = `${startParts[1]}.${startParts[2]}`;
            const endMMDD = `${endParts[1]}.${endParts[2]}`;
            this.tierPeriodEl.textContent = `阶梯周期: ${startMMDD}-${endMMDD}`;

            // === 重要：同时更新 tierConfig 中的周期配置，供 getCurrentTierPeriod 使用 ===
            if (!billingStandardType.includes("月阶梯")) {
              this.tierConfig.periodStartMonth = parseInt(startParts[1]) || 7;
              this.tierConfig.periodStartDay = parseInt(startParts[2]) || 1;
              this.tierConfig.periodEndMonth = parseInt(endParts[1]) || 6;
              this.tierConfig.periodEndDay = parseInt(endParts[2]) || 30;
            }
          }
        }

        return;
      }

      // 【重要】当 utility_type 为 'ele' 但无法获取计费标准数据时，直接返回，不执行回退逻辑
      return;
    }

    // 当 utility_type 为 'ele' 但无计费标准时，显示「不适用」避免串用燃气数据
    if (this.utilityType === "ele") {
      const usageUnit = this.standardData.unit || "°";
      this.tier1RangeEl.textContent = "不适用";
      this.tier2RangeEl.textContent = "不适用";
      this.tier3RangeEl.textContent = "不适用";
      this.tier1PriceEl.textContent = usageUnit ? `0元/${usageUnit}` : "0元";
      this.tier2PriceEl.textContent = usageUnit ? `0元/${usageUnit}` : "0元";
      this.tier3PriceEl.textContent = usageUnit ? `0元/${usageUnit}` : "0元";
      if (this.tierPeriodEl) this.tierPeriodEl.textContent = "计费方式: 待获取";
      return;
    }

    // 燃气等类型：使用 tierConfig
    const tier1 = this.tierConfig.tiers[0];
    const tier2 = this.tierConfig.tiers[1];
    const tier3 = this.tierConfig.tiers[2];

    // 获取用电量单位（从统一数据格式中获取，不设置默认值）
    const usageUnit = this.standardData.unit || "";

    // 更新tier-range元素（用于tier-content中）
    const tier1RangeText = usageUnit
      ? `0-${tier1.max}${usageUnit}`
      : `0-${tier1.max}`;
    const tier2RangeText = usageUnit
      ? `${tier1.max + 1}-${tier2.max}${usageUnit}`
      : `${tier1.max + 1}-${tier2.max}`;
    const tier3RangeText = usageUnit
      ? `${tier2.max + 1}${usageUnit}以上`
      : `${tier2.max + 1}以上`;

    this.tier1RangeEl.textContent = tier1RangeText;
    this.tier2RangeEl.textContent = tier2RangeText;
    this.tier3RangeEl.textContent = tier3RangeText;

    this.tier1PriceEl.textContent = usageUnit
      ? `${this.formatPrice(tier1.price)}元/${usageUnit}`
      : `${this.formatPrice(tier1.price)}元`;
    this.tier2PriceEl.textContent = usageUnit
      ? `${this.formatPrice(tier2.price)}元/${usageUnit}`
      : `${this.formatPrice(tier2.price)}元`;
    this.tier3PriceEl.textContent = usageUnit
      ? `${this.formatPrice(tier3.price)}元/${usageUnit}`
      : `${this.formatPrice(tier3.price)}元`;

    // 燃气使用日历年(1.1-12.31)，无计费标准时显示整年周期
    if (this.utilityType === "gas" && this.tierPeriodEl) {
      this.tierPeriodEl.textContent = "阶梯周期: 01.01-12.31";
    }
  }

  // 【集中处理 utility_type 为 'ele' 的逻辑】从计费标准获取阶梯配置并更新 tierConfig - 已重构
  updateTierConfigFromBillingStandard() {
    const billingStandard = this.getBillingStandardData();
    if (!billingStandard) {
      return false; // 没有计费标准数据
    }

    // 判断计费标准类型，支持年阶梯和月阶梯
    const billingStandardType = billingStandard["计费标准"] || "";
    const isMonthlyTier = billingStandardType.includes("月阶梯");

    // 根据计费标准类型选择正确的字段名
    const tier2Start =
      billingStandard[
        isMonthlyTier ? "月阶梯第2档起始电量" : "年阶梯第2档起始电量"
      ];
    const tier3Start =
      billingStandard[
        isMonthlyTier ? "月阶梯第3档起始电量" : "年阶梯第3档起始电量"
      ];

    if (tier2Start === undefined || tier3Start === undefined) {
      return false; // 缺少必要的阶梯数据
    }

    // 更新 tierConfig
    this.tierConfig.tiers[0].max = parseFloat(tier2Start);
    this.tierConfig.tiers[1].max = parseFloat(tier3Start);

    return true; // 更新成功
  }

  // 获取计费标准对象（兼容两种属性名）
  getBillingStandardObject(entity) {
    // 优先使用 entity.attributes.计费标准
    if (entity && entity.attributes && entity.attributes.计费标准) {
      return entity.attributes.计费标准;
    }
    // 如果不存在，尝试使用 entity.attributes.data.计费标准
    if (
      entity &&
      entity.attributes &&
      entity.attributes.data &&
      entity.attributes.data.计费标准
    ) {
      return entity.attributes.data.计费标准;
    }
    // 燃气回退：从 current_unit_price 实体构建计费标准（余额实体应包含，此作兜底）
    if (this.utilityType === "gas" && this._hass && this.entityId) {
      const fallbackId = this.entityId.replace(
        /_[^_]+$/,
        "_current_unit_price",
      );
      const fallbackEntity = this._hass.states[fallbackId];
      if (fallbackEntity && fallbackEntity.attributes) {
        const attrs = fallbackEntity.attributes;
        if (attrs.fixed_price !== undefined && attrs.fixed_price !== null) {
          return {
            计费标准: "平均单价",
            平均单价: parseFloat(attrs.fixed_price),
          };
        }
        const t1 = parseFloat(attrs.tier_1_price);
        const t2 = parseFloat(attrs.tier_2_price);
        const t3 = parseFloat(attrs.tier_3_price);
        if (!isNaN(t1) || !isNaN(t2) || !isNaN(t3)) {
          return {
            计费标准: "年阶梯",
            年阶梯第1档气价: t1,
            年阶梯第2档气价: t2,
            年阶梯第3档气价: t3,
            年阶梯第2档起始气量: parseFloat(attrs.tier_1_limit) || 0,
            年阶梯第3档起始气量: parseFloat(attrs.tier_2_limit) || 0,
            年阶梯累计用气量: parseFloat(attrs.yearly_usage) || 0,
          };
        }
        // 当前单价实体的 state 即单价，可作为平均单价回退
        const statePrice = parseFloat(fallbackEntity.state);
        if (!isNaN(statePrice) && statePrice > 0) {
          return { 计费标准: "平均单价", 平均单价: statePrice };
        }
      }
    }
    return null;
  }

  // 【集中处理 utility_type 为 'ele' 的逻辑】从计费标准获取指定阶梯的第一个单价（最高的）- 已重构
  getCurrentTierPriceFromBillingStandard(tierNumber, defaultPrice = 0.4983) {
    const billingStandard = this.getBillingStandardData();

    // 如果是电力类型且实体有计费标准数据，从实体获取
    if (billingStandard) {
      // 判断计费标准类型，支持年阶梯、月阶梯和平均单价
      const billingStandardType = billingStandard["计费标准"] || "";

      // 处理平均单价计费方式（直接返回平均单价，不区分阶梯）
      if (
        billingStandardType === "平均单价" ||
        billingStandard["平均单价"] !== undefined
      ) {
        const avgPrice = billingStandard["平均单价"];
        if (avgPrice !== undefined) {
          const currentPrice = parseFloat(avgPrice);
          this.debugLog(`[平均单价] 直接返回平均单价:`, currentPrice);
          return currentPrice;
        }
      }

      const isMonthlyTier = billingStandardType.includes("月阶梯");

      // 根据计费标准类型选择正确的字段前缀
      const tierPrefix = isMonthlyTier ? "月阶梯" : "年阶梯";

      // 尝试获取当前阶梯的4个分时电价（尖、峰、平、谷）
      const sharpPrice =
        billingStandard[`${tierPrefix}第${tierNumber}档尖电价`];
      const peakPrice = billingStandard[`${tierPrefix}第${tierNumber}档峰电价`];
      const normalPrice =
        billingStandard[`${tierPrefix}第${tierNumber}档平电价`];
      const valleyPrice =
        billingStandard[`${tierPrefix}第${tierNumber}档谷电价`];

      // 收集所有有效的分时电价
      const prices = [];
      if (sharpPrice !== undefined) prices.push(parseFloat(sharpPrice));
      if (peakPrice !== undefined) prices.push(parseFloat(peakPrice));
      if (normalPrice !== undefined) prices.push(parseFloat(normalPrice));
      if (valleyPrice !== undefined) prices.push(parseFloat(valleyPrice));

      // 如果有有效的分时电价，返回第一个（最高的那个）
      if (prices.length > 0) {
        // 排序（从高到低）
        prices.sort((a, b) => b - a);
        const currentPrice = prices[0]; // 取第一个（最高的）
        return currentPrice;
      }

      // 【降级匹配】原则：电力用电价，燃气用气价
      const priceKey =
        this.utilityType === "gas"
          ? `${tierPrefix}第${tierNumber}档气价`
          : `${tierPrefix}第${tierNumber}档电价`;
      const unifiedPrice = billingStandard[priceKey];
      if (unifiedPrice !== undefined) {
        const currentPrice = parseFloat(unifiedPrice);
        return currentPrice;
      }

      // 【重要】如果是电力类型但无法从计费标准获取数据，返回0（不再回退到tierConfig或默认值）
      return 0;
    }

    // 对于非电力类型（如燃气），从阶梯配置获取价格
    if (
      this.tierConfig &&
      this.tierConfig.tiers &&
      this.tierConfig.tiers[tierNumber - 1]
    ) {
      const tierPrice = this.tierConfig.tiers[tierNumber - 1].price;
      return tierPrice;
    }

    return defaultPrice; // 如果没有有效的价格，返回默认值
  }

  /* 根据计费标准和用电类型获取对应的电价信息
   * 支持6种不同的计费标准：年阶梯峰平谷、年阶梯、月阶梯峰平谷、月阶梯峰平谷变动价格、月阶梯、平均单价*/
  getElectricityPrices(billingStandard, currentLevel, electricityTypes) {
    // 检查基本条件
    if (!this._hass || !this.entityId) {
      return {};
    }

    const entity = this._hass.states[this.entityId];
    const billingStandardObj = this.getBillingStandardObject(entity);
    if (!billingStandardObj) return {};

    const prices = {};
    if (!electricityTypes || electricityTypes.length === 0) return prices;

    electricityTypes.forEach((type) => {
      switch (billingStandard) {
        case "年阶梯":
          prices.single =
            this.utilityType === "gas"
              ? billingStandardObj[`年阶梯第${currentLevel}档气价`]
              : billingStandardObj[`年阶梯第${currentLevel}档电价`];
          break;
        case "月阶梯":
          prices.single =
            this.utilityType === "gas"
              ? billingStandardObj[`月阶梯第${currentLevel}档气价`]
              : billingStandardObj[`月阶梯第${currentLevel}档电价`];
          break;
        case "平均单价":
          prices.single = billingStandardObj.平均单价;
          break;
      }
    });

    return prices;
  }

  /*渲染价格区块*/
  renderPriceBlock(prices, level) {
    // 检查基本条件
    if (!this._hass || !this.entityId) {
      return "";
    }

    const entity = this._hass.states[this.entityId];
    const billingStandardObj = this.getBillingStandardObject(entity);
    if (!billingStandardObj)
      return `<div class="price-item-block">0.0000元/${this.standardData.unit || "°"}</div>`;

    // 获取计费标准类型（支持两种属性名）
    const billingStandard =
      billingStandardObj.计费标准 || billingStandardObj.计费标准类型 || "未知";
    const usageUnit = this.standardData.unit || "°";

    // 如果是平均单价，直接显示
    if (billingStandard === "平均单价" || billingStandardObj.平均单价) {
      const singlePrice = billingStandardObj.平均单价;
      if (singlePrice) {
        return `<div class="price-item-block">单价：${this.formatPrice(singlePrice)}元/${usageUnit}</div>`;
      }
    }

    let blockPrices = {};
    let hasPrice = false;

    // 根据计费标准类型获取对应的价格
    switch (billingStandard) {
      case "年阶梯":
        // 原则：电力用电价，燃气用气价
        blockPrices.single =
          this.utilityType === "gas"
            ? billingStandardObj[`年阶梯第${level}档气价`]
            : billingStandardObj[`年阶梯第${level}档电价`];
        hasPrice = true;
        break;
      case "月阶梯":
        // 原则：电力用电价，燃气用气价
        blockPrices.single =
          this.utilityType === "gas"
            ? billingStandardObj[`月阶梯第${level}档气价`]
            : billingStandardObj[`月阶梯第${level}档电价`];
        hasPrice = true;
        break;
      default:
        // 如果没有匹配到计费标准类型，尝试直接读取价格字段。原则：电力用电价，燃气用气价
        this.debugLog(
          `[renderPriceBlock] 未知计费标准类型: ${billingStandard}，尝试直接读取价格字段`,
        );
        blockPrices.single =
          this.utilityType === "gas"
            ? billingStandardObj[`第${level}档气价`] ||
              billingStandardObj[`阶梯第${level}档气价`]
            : billingStandardObj[`第${level}档电价`] ||
              billingStandardObj[`阶梯第${level}档电价`];
        hasPrice = true;
        break;
    }

    // 生成HTML
    let html = "";
    let foundPrice = false;

    if (blockPrices.single) {
      html = `<div class="price-item-block">单价：${this.formatPrice(blockPrices.single)}元/${usageUnit}</div>`;
      foundPrice = true;
    }

    if (foundPrice) {
      return html;
    } else {
      this.debugLog(`[renderPriceBlock] 未找到第${level}档的价格数据`);
      return `<div class="price-item-block">0.0000元/${usageUnit}</div>`;
    }
  }

  /** 接收 Home Assistant 实例，用于获取实体状态、主题更新、数据刷新等 */
  set hass(hass) {
    const oldHass = this._hass;
    this._hass = hass;

    // 首次加载时应用主题（如果还没有应用过）
    if (this._config && this._config.theme && !this.lastThemeName) {
      this.updateTheme(this._config);
    }

    // 检查主题实体状态是否变化，实时响应主题变化
    if (this._config && this._config.theme) {
      this.updateThemeFromEntity();
    }

    // 根据「更新时间」实体值变化刷新（state、last_updated 或 syn/date 任一变化即刷新）
    let shouldUpdate = false;
    if (!oldHass) {
      shouldUpdate = true; // 首次加载
    } else if (this.entityId && oldHass?.states && hass?.states) {
      const oldEntity = oldHass.states[this.entityId];
      const newEntity = hass.states[this.entityId];
      if (!oldEntity && !newEntity) {
        shouldUpdate = false;
      } else if (!oldEntity || !newEntity) {
        shouldUpdate = true;
      } else {
        const oldUpdateTime = this._getEntityUpdateTime(oldEntity);
        const newUpdateTime = this._getEntityUpdateTime(newEntity);
        const updateTimeChanged =
          (oldUpdateTime !== undefined || newUpdateTime !== undefined) &&
          String(oldUpdateTime || "") !== String(newUpdateTime || "");
        const stateOrLastChanged =
          oldEntity.state !== newEntity.state ||
          (oldEntity.last_updated || "") !== (newEntity.last_updated || "");
        shouldUpdate = updateTimeChanged || stateOrLastChanged;
      }
    }

    if (shouldUpdate) {
      this.updateCard();
    }
  }

  // 获取当前年月字符串（格式：YYYY-MM）
  getCurrentMonthStr() {
    const now = new Date();
    const year = now.getFullYear();
    const month = pad2(now.getMonth() + 1);
    return `${year}-${month}`;
  }

  // 获取默认标准数据对象
  getDefaultStandardData(granularity, timeStr) {
    return {
      user: "ele_01",
      utility_type: this.utilityType,
      data_category: "usage",
      date_granularity: granularity,
      time: timeStr,
      data_source: "entity",
      total_usage: 0,
      total_amount: 0,
      unit: this.standardData.unit || "",
      usage_ele_no: 0,
    };
  }

  // 从标准格式获取当前月份数据
  getCurrentMonthStandardData() {
    const currentMonthStr = this.getCurrentMonthStr();
    const currentMonthData = this.standardData.monthUsage.find(
      (item) => item.time === currentMonthStr,
    );

    if (currentMonthData) {
      return currentMonthData;
    }

    // 返回默认值
    return this.getDefaultStandardData("month", currentMonthStr);
  }

  // 从标准格式获取上月数据
  getLastMonthStandardData() {
    const now = new Date();
    const lastMonthDate = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    const lastMonthStr = `${lastMonthDate.getFullYear()}-${pad2(lastMonthDate.getMonth() + 1)}`;

    const lastMonthData = this.standardData.monthUsage.find(
      (item) => item.time === lastMonthStr,
    );

    if (lastMonthData) {
      return lastMonthData;
    }

    // 如果找不到，返回第二个数据（如果存在）
    if (this.standardData.monthUsage.length > 1) {
      return this.standardData.monthUsage[1];
    }

    // 返回默认值
    return this.getDefaultStandardData("month", lastMonthStr);
  }

  // 从标准格式获取当前年度数据
  getCurrentYearStandardData() {
    const currentYear = new Date().getFullYear().toString();

    const currentYearData = this.standardData.yearUsage.find(
      (item) => item.time === currentYear,
    );

    if (currentYearData) {
      return currentYearData;
    }

    // 如果找不到，返回第一个年份数据（如果存在）
    if (this.standardData.yearUsage.length > 0) {
      return this.standardData.yearUsage[0];
    }

    // 返回默认值
    return this.getDefaultStandardData("year", currentYear);
  }

  // 获取当前月份用电数据（原始格式，保留兼容性）
  getCurrentMonthData(monthlist) {
    if (!monthlist || !Array.isArray(monthlist)) {
      return { monthEleNum: 0, monthEleCost: 0, total: 0 };
    }
    const currentMonthStr = this.getCurrentMonthStr();
    const currentMonthData = monthlist.find(
      (item) => item.month === currentMonthStr,
    );
    if (currentMonthData) {
      return {
        monthEleNum: currentMonthData.monthEleNum || 0,
        monthEleCost: currentMonthData.monthEleCost || 0,
        total: currentMonthData.monthEleNum || 0,
      };
    }
    return { monthEleNum: 0, monthEleCost: 0, total: 0 };
  }

  // 获取当前阶梯电价周期
  getCurrentTierPeriod() {
    const now = new Date();
    const currentYear = now.getFullYear();
    const currentMonth = now.getMonth() + 1; // 月份从0开始，所以+1

    // 使用默认值，避免 null 导致的错误。燃气使用日历年(1.1-12.31)，电力默认7.1-6.30
    const gasDefault = this.utilityType === "gas";
    const periodStartMonth =
      this.tierConfig.periodStartMonth ?? (gasDefault ? 1 : 7);
    const periodStartDay = this.tierConfig.periodStartDay ?? 1;
    const periodEndMonth =
      this.tierConfig.periodEndMonth ?? (gasDefault ? 12 : 6);
    const periodEndDay = this.tierConfig.periodEndDay ?? (gasDefault ? 31 : 30);

    let startYear, endYear;

    // 判断当前日期属于哪个计费周期
    // 比较当前月份和计费周期开始月份
    if (currentMonth >= periodStartMonth) {
      // 如果当前月份在计费周期开始月份之后或相同
      startYear = currentYear;

      // 计算结束年份：如果结束月份小于开始月份，说明跨年了
      if (periodEndMonth < periodStartMonth) {
        endYear = currentYear + 1;
      } else {
        endYear = currentYear;
      }
    } else {
      // 如果当前月份在计费周期开始月份之前
      // 说明当前属于上一个周期的后半段
      startYear = currentYear - 1;

      // 计算结束年份
      if (periodEndMonth < periodStartMonth) {
        endYear = currentYear;
      } else {
        endYear = currentYear;
      }
    }

    // 创建周期开始和结束日期
    const periodStart = new Date(
      startYear,
      periodStartMonth - 1,
      periodStartDay,
    );
    const periodEnd = new Date(endYear, periodEndMonth - 1, periodEndDay);

    return {
      start: periodStart,
      end: periodEnd,
      startYear,
      endYear,
    };
  }

  // 格式化日期为 MM.DD 格式
  formatDateMMDD(date) {
    const month = pad2(date.getMonth() + 1);
    const day = pad2(date.getDate());
    return `${month}.${day}`;
  }

  // 从daylist计算当前周期累计用电量
  calculatePeriodUsageFromDaylist(daylist) {
    if (!daylist || !Array.isArray(daylist)) {
      return 0;
    }

    const period = this.getCurrentTierPeriod();
    const periodStart = period.start;
    const periodEnd = period.end;
    const now = new Date();

    // 使用今天的日期或周期结束日期中较早的那个
    const endDate = now < periodEnd ? now : periodEnd;

    let totalUsage = 0;

    // 根据 utility_type 获取对应的用量字段
    const usageField = this.getUsageFieldByUtilityType();

    // 获取配置的日期字段名（默认为 'day'）
    const dateField = this.getDateFieldByUtilityType();

    // 遍历daylist，累加在周期内的用电量
    for (const dayData of daylist) {
      const dayStr = dayData[dateField];

      if (!dayStr) {
        console.warn(`Daylist 数据中缺少日期字段 '${dateField}':`, dayData);
        continue;
      }

      // 解析日期字符串，假设格式为 "YYYY-MM-DD"
      try {
        const [year, month, day] = dayStr.split("-").map(Number);
        const date = new Date(year, month - 1, day);

        // 检查日期是否有效
        if (isNaN(date.getTime())) {
          console.warn(`Daylist 数据中日期格式无效: ${dayStr}`);
          continue;
        }

        // 检查日期是否在周期内
        if (date >= periodStart && date <= endDate) {
          // 根据 utility_type 使用不同的字段，确保是有效的数字
          const usageValue = dayData[usageField];
          if (usageValue !== undefined && usageValue !== null) {
            const parsedUsage = parseFloat(usageValue);
            if (!isNaN(parsedUsage)) {
              totalUsage += parsedUsage;
            }
          }
        }
      } catch (error) {
        console.warn(`解析日期 ${dayStr} 时出错:`, error);
        continue;
      }
    }

    // 确保返回的是有效的数字
    return isNaN(totalUsage) ? 0 : totalUsage;
  }

  /** 根据 utility_type 或 fieldMapping 获取日期字段名（如 day） */
  getDateFieldByUtilityType() {
    // 检查是否有自定义的字段映射（字段映射存储在 this.fieldMapping 中）
    if (this.fieldMapping && this.fieldMapping.date) {
      return this.fieldMapping.date;
    }

    // 默认字段名
    return "day";
  }

  /** 根据 utility_type 或 fieldMapping 获取用量字段名（如 dayEleNum/usage） */
  getUsageFieldByUtilityType() {
    // 检查是否有自定义的字段映射（字段映射存储在 this.fieldMapping 中）
    if (this.fieldMapping && this.fieldMapping.usage) {
      return this.fieldMapping.usage;
    }

    // 默认字段名（向后兼容）
    switch (this.utilityType) {
      case "ele":
        return "dayEleNum";
      case "gas":
        return "usage";
      default:
        return "dayEleNum";
    }
  }

  /** 根据当前用量计算所在阶梯及剩余额度 */
  calculateCurrentTier(usage) {
    const tiers = this.tierConfig.tiers;

    for (let i = 0; i < tiers.length; i++) {
      if (usage <= tiers[i].max) {
        return {
          tier: i + 1,
          price: tiers[i].price,
          maxUsage: tiers[i].max,
          remaining: Math.max(0, tiers[i].max - usage),
          isLastTier: i === tiers.length - 1,
        };
      }
    }

    // 如果超过所有阶梯，返回最后一个阶梯
    const lastTier = tiers[tiers.length - 1];
    return {
      tier: tiers.length,
      price: lastTier.price,
      maxUsage: Infinity,
      remaining: 0,
      isLastTier: true,
    };
  }

  /** 根据用量与阶梯信息计算红色竖线指示器的水平位置（基于 tier-block 实际布局） */
  calculateIndicatorPosition(usage, tierInfo) {
    // 获取 tier-block 的实际位置，基于实际布局计算
    const tierBlocks = this.shadowRoot.querySelectorAll(".tier-block");
    if (tierBlocks.length < 3) {
      // 如果找不到 tier-block，回退到固定计算

      return this.calculateIndicatorPositionFallback(usage, tierInfo);
    }

    const containerRect = this.tiersContainerEl.getBoundingClientRect();

    // 检查容器宽度是否有效
    if (!containerRect || containerRect.width <= 0) {
      return this.calculateIndicatorPositionFallback(usage, tierInfo);
    }

    const block1Rect = tierBlocks[0].getBoundingClientRect();
    const block2Rect = tierBlocks[1].getBoundingClientRect();
    const block3Rect = tierBlocks[2].getBoundingClientRect();

    // 检查 rect 是否有效
    if (!block1Rect || !block2Rect || !block3Rect) {
      return this.calculateIndicatorPositionFallback(usage, tierInfo);
    }

    // 计算每个 tier-block 的边界（相对于容器）
    const block1Left =
      ((block1Rect.left - containerRect.left) / containerRect.width) * 100;
    const block1Right =
      ((block1Rect.right - containerRect.left) / containerRect.width) * 100;
    const block2Left =
      ((block2Rect.left - containerRect.left) / containerRect.width) * 100;
    const block2Right =
      ((block2Rect.right - containerRect.left) / containerRect.width) * 100;
    const block3Left =
      ((block3Rect.left - containerRect.left) / containerRect.width) * 100;
    const block3Right =
      ((block3Rect.right - containerRect.left) / containerRect.width) * 100;

    // 检查结果是否有效
    if (
      isNaN(block1Left) ||
      isNaN(block1Right) ||
      isNaN(block2Left) ||
      isNaN(block2Right) ||
      isNaN(block3Left) ||
      isNaN(block3Right)
    ) {
      return this.calculateIndicatorPositionFallback(usage, tierInfo);
    }

    // 基于阶梯配置计算位置
    const tier1Max = this.tierConfig.tiers[0].max;
    const tier2Max = this.tierConfig.tiers[1].max;

    let positionPercent = 0;

    if (tierInfo.tier === 1) {
      // 在第一阶梯内：映射到 block1 的范围内
      const progress = Math.min(usage / tier1Max, 1);
      positionPercent = block1Left + progress * (block1Right - block1Left);
    } else if (tierInfo.tier === 2) {
      // 在第二阶梯内：映射到 block2 的范围内
      const progress = Math.min((usage - tier1Max) / (tier2Max - tier1Max), 1);
      positionPercent = block2Left + progress * (block2Right - block2Left);
    } else {
      // 在第三阶梯内：映射到 block3 的范围内
      // 计算一个合理的最大值（tier2.max * 1.5 - tier2.max）
      const tier3Range = tier2Max * 0.5;
      const progress = Math.min((usage - tier2Max) / tier3Range, 1);
      positionPercent = block3Left + progress * (block3Right - block3Left);
    }

    // 确保不超过实际边界
    const finalPosition = Math.min(
      Math.max(positionPercent, block1Left),
      block3Right,
    );

    return finalPosition;
  }

  /** 阶梯指示器位置回退计算：tier-block 不可用时按固定比例估算 */
  calculateIndicatorPositionFallback(usage, tierInfo) {
    const tier1Max = this.tierConfig.tiers[0].max;
    const tier2Max = this.tierConfig.tiers[1].max;

    // 假设三个 tier-block 平均分配空间（每个约33.33%）
    // 由于居中对齐，假设左边有5%的空白
    const blockStartPercent = 5;
    const blockWidth = 30; // 每个 block 约 30%

    let positionPercent = 0;

    if (tierInfo.tier === 1) {
      // 在第一阶梯内
      const progress = Math.min(usage / tier1Max, 1);
      positionPercent = blockStartPercent + progress * blockWidth;
    } else if (tierInfo.tier === 2) {
      // 在第二阶梯内
      const progress = Math.min((usage - tier1Max) / (tier2Max - tier1Max), 1);
      positionPercent = blockStartPercent + blockWidth + progress * blockWidth;
    } else {
      // 在第三阶梯内
      const tier3Range = tier2Max * 0.5;
      const progress = Math.min((usage - tier2Max) / tier3Range, 1);
      positionPercent =
        blockStartPercent + 2 * blockWidth + progress * blockWidth;
    }

    return positionPercent;
  }

  /** 更新阶梯指示器：红竖线、倒三角、阶梯内容区，支持平均单价模式隐藏 */
  updateTierIndicator(usage) {
    // 检查是否有计费标准数据（现在支持 gas）
    if (this._hass && this.entityId) {
      const entity = this._hass.states[this.entityId];
      const billingStandard = this.getBillingStandardObject(entity);

      if (billingStandard) {
        // 如果是平均单价模式，直接返回，不显示阶梯指示器
        const billingStandardType = billingStandard["计费标准"] || "";
        if (
          billingStandardType === "平均单价" ||
          billingStandard["平均单价"] !== undefined
        ) {
          // 清除可能存在的残留指示器（红竖线、倒三角）
          const redLines = this.shadowRoot.querySelectorAll(
            ".red-line-indicator",
          );
          redLines.forEach((line) => line.remove());
          const oldTriangles = this.shadowRoot.querySelectorAll(
            ".current-indicator-triangle",
          );
          oldTriangles.forEach((t) => t.remove());

          this.tier1El.classList.remove("current");
          this.tier2El.classList.remove("current");
          this.tier3El.classList.remove("current");

          // 更新单价显示
          const avgPrice = billingStandard["平均单价"];
          if (avgPrice !== undefined) {
            this.electricityPriceEl.textContent = this.formatPrice(avgPrice);
          }
          // 平均单价模式：电力显示年度用电量，燃气显示年度气用量，并始终居中显示（切换用户时位置不变）
          const yearUsage =
            this.utilityType === "gas"
              ? (billingStandard["年阶梯累计用气量"] ??
                billingStandard["年累计用气量"] ??
                (entity && entity.attributes?.["year_gas_num"]))
              : (billingStandard["年阶梯累计用电量"] ??
                billingStandard["年累计用电量"] ??
                (entity && entity.attributes?.["year_ele_num"]));
          const displayValue =
            yearUsage !== undefined && yearUsage !== null
              ? parseFloat(yearUsage)
              : usage;
          const usageUnit =
            this.standardData.unit ||
            this.getUnitByUtilityType(this.utilityType);
          const yearLabel =
            this.utilityType === "gas" ? "年度气用量" : "年度用电量";
          if (this.currentTierEl) this.currentTierEl.textContent = yearLabel;
          if (this.currentUsageEl)
            this.currentUsageEl.textContent = (displayValue || 0).toFixed(2);
          if (this.currentUsageUnitEl)
            this.currentUsageUnitEl.textContent = usageUnit;
          if (this.priceUnitEl)
            this.priceUnitEl.textContent = usageUnit ? `元/${usageUnit}` : "元";
          // 居中显示，切换用户/卡片时保持位置一致；清除阶梯模式遗留的内联样式，避免背景色错乱
          if (this.currentIndicatorEl) {
            this.currentIndicatorEl.style.left = "50%";
            this.currentIndicatorEl.style.transform = "translateX(-50%)";
            this.currentIndicatorEl.style.backgroundColor = "";
            this.currentIndicatorEl.classList.remove(
              "tier-1",
              "tier-2",
              "tier-3",
            );
          }
          if (this.indicatorArrowEl) {
            this.indicatorArrowEl.style.left = "50%";
            this.indicatorArrowEl.style.transform = "translateX(-50%)";
            this.indicatorArrowEl.style.borderTopColor = "";
          }
          return;
        }

        const currentYearTier = billingStandard["当前年阶梯档"];
        let yearlyAccumulatedUsage;
        if (this.utilityType === "ele") {
          yearlyAccumulatedUsage = billingStandard["年阶梯累计用电量"];
        } else if (this.utilityType === "gas") {
          yearlyAccumulatedUsage =
            billingStandard["年阶梯累计用气量"] ??
            billingStandard["年阶梯累计用电量"];
        } else {
          yearlyAccumulatedUsage =
            billingStandard["年阶梯累计用电量"] ??
            billingStandard["年阶梯累计用气量"];
        }

        // 将"第X档"转换为数字
        let tierNum = 1;
        if (typeof currentYearTier === "string") {
          const match = currentYearTier.match(/第(\d+)档/);
          if (match) {
            tierNum = parseInt(match[1], 10);
          }
        }

        // 使用实体中的年阶梯累计用电量
        const actualUsage =
          yearlyAccumulatedUsage !== undefined
            ? parseFloat(yearlyAccumulatedUsage)
            : usage;

        // 重新计算 tierInfo，使用实际用电量和阶梯档位
        // 注意：tierConfig 可能已经被 updateTierDisplay 根据 billingStandard 更新过了
        const tierInfo = {
          tier: tierNum,
          price: this.tierConfig.tiers[tierNum - 1]?.price || 0.4983,
        };
        const period = this.getCurrentTierPeriod();

        // 存储当前周期用电量和阶梯
        this.currentPeriodUsage = actualUsage;
        this.currentTier = tierInfo.tier;

        // 使用辅助方法获取当前阶梯的第一个单价 (现在 gas 也支持)
        const currentPrice = this.getCurrentTierPriceFromBillingStandard(
          tierInfo.tier,
          tierInfo.price || 0,
        );

        // 更新电价显示
        this.electricityPriceEl.textContent = this.formatPrice(currentPrice);

        // 更新电价单位显示（元/单位）
        const usageUnit = this.standardData.unit || "";
        this.priceUnitEl.textContent = usageUnit ? `元/${usageUnit}` : "元";

        // 更新周期显示为 MM.DD-MM.DD 格式
        const startDateStr = this.formatDateMMDD(period.start);
        const endDateStr = this.formatDateMMDD(period.end);
        this.tierPeriodEl.textContent = `阶梯周期: ${startDateStr}-${endDateStr}`;

        // 移除所有阶梯的current类
        this.tier1El.classList.remove("current");
        this.tier2El.classList.remove("current");
        this.tier3El.classList.remove("current");

        // 为当前阶梯添加current类
        const currentTierEl = this.shadowRoot.getElementById(
          `tier-${tierInfo.tier}`,
        );
        if (currentTierEl) {
          currentTierEl.classList.add("current");
        }

        // 移除可能存在的旧红色竖线指示器
        const oldRedLines = this.shadowRoot.querySelectorAll(
          ".red-line-indicator",
        );
        oldRedLines.forEach((line) => line.remove());

        // 计算指示器位置
        this._updateIndicatorPositionAndDisplay(actualUsage, tierInfo, period);

        return;
      }
    }

    // 当无法获取计费标准数据时：电力类型仍显示年度用电量（避免串用燃气等错误值）
    if (this.utilityType === "ele") {
      const redLines = this.shadowRoot.querySelectorAll(".red-line-indicator");
      redLines.forEach((line) => line.remove());
      this.tier1El.classList.remove("current");
      this.tier2El.classList.remove("current");
      this.tier3El.classList.remove("current");
      // 优先从 standardData 获取年度用电量
      const yearStandard = this.getCurrentYearStandardData();
      const displayValue =
        yearStandard && yearStandard.total_usage !== undefined
          ? parseFloat(yearStandard.total_usage)
          : usage;
      const usageUnit =
        this.standardData.unit || this.getUnitByUtilityType(this.utilityType);
      if (this.currentTierEl) this.currentTierEl.textContent = "年度用电量";
      if (this.currentUsageEl)
        this.currentUsageEl.textContent = (displayValue || 0).toFixed(2);
      if (this.currentUsageUnitEl)
        this.currentUsageUnitEl.textContent = usageUnit;
      // 居中显示，切换用户/卡片时保持位置一致；清除阶梯模式遗留的内联样式，避免背景色错乱
      if (this.currentIndicatorEl) {
        this.currentIndicatorEl.style.left = "50%";
        this.currentIndicatorEl.style.transform = "translateX(-50%)";
        this.currentIndicatorEl.style.backgroundColor = "";
        this.currentIndicatorEl.classList.remove("tier-1", "tier-2", "tier-3");
      }
      if (this.indicatorArrowEl) {
        this.indicatorArrowEl.style.left = "50%";
        this.indicatorArrowEl.style.transform = "translateX(-50%)";
        this.indicatorArrowEl.style.borderTopColor = "";
      }
      return;
    }

    // 燃气等类型：回退到手动配置的 tierConfig
    const tierInfo = this.calculateCurrentTier(usage || 0);
    const period = this.getCurrentTierPeriod();

    // 存储当前周期用电量和阶梯
    this.currentPeriodUsage = usage || 0;
    this.currentTier = tierInfo.tier;

    // 获取单价
    const currentPrice = tierInfo.price || 0;

    // 更新电价显示
    this.electricityPriceEl.textContent = this.formatPrice(currentPrice);

    // 更新电价单位显示（元/单位）
    const usageUnit = this.standardData.unit || "";
    this.priceUnitEl.textContent = usageUnit ? `元/${usageUnit}` : "元";

    // 更新周期显示为 MM.DD-MM.DD 格式
    const startDateStr = this.formatDateMMDD(period.start);
    const endDateStr = this.formatDateMMDD(period.end);
    this.tierPeriodEl.textContent = `阶梯周期: ${startDateStr}-${endDateStr}`;

    // 移除所有阶梯的current类
    this.tier1El.classList.remove("current");
    this.tier2El.classList.remove("current");
    this.tier3El.classList.remove("current");

    // 为当前阶梯添加current类
    const currentTierEl = this.shadowRoot.getElementById(
      `tier-${tierInfo.tier}`,
    );
    if (currentTierEl) {
      currentTierEl.classList.add("current");
    }

    // 移除可能存在的旧红色竖线指示器
    const oldRedLines = this.shadowRoot.querySelectorAll(".red-line-indicator");
    oldRedLines.forEach((line) => line.remove());

    // 计算指示器位置
    this._updateIndicatorPositionAndDisplay(usage, tierInfo, period);
  }

  // 更新指示器位置和显示的内部方法
  _updateIndicatorPositionAndDisplay(usage, tierInfo, period) {
    // 确保 tiersContainerEl 已经存在且有有效宽度
    if (!this.tiersContainerEl) {
      return;
    }

    // 确保容器已渲染且有宽度
    const containerRect = this.tiersContainerEl.getBoundingClientRect();
    if (containerRect.width <= 0) {
      // 延迟后重试
      setTimeout(() => {
        if (
          this.tiersContainerEl &&
          this.tiersContainerEl.getBoundingClientRect().width > 0
        ) {
          this._updateIndicatorPositionAndDisplay(usage, tierInfo, period);
        }
      }, 100);
      return;
    }

    // 使用用户代码中的精确定位方式
    const totalWidth = 100;
    const tierWidthPercent = totalWidth / 3;

    // 计算当前阶梯指示器的位置
    let indicatorPosition = 0;

    if (tierInfo.tier === 1) {
      // 确保在第一阶梯内有正确的位置
      if (usage > 0 && this.tierConfig.tiers[0].max > 0) {
        indicatorPosition =
          (usage / this.tierConfig.tiers[0].max) * tierWidthPercent;
        // 确保不超过第一阶梯的范围
        indicatorPosition = Math.min(indicatorPosition, tierWidthPercent);
        // 确保最小位置不为0（除非用电量为0）
        if (indicatorPosition < 0.5 && usage > 0) {
          indicatorPosition = 0.5; // 设置一个最小偏移量，避免在最左侧
        }
      } else {
        indicatorPosition = 0;
      }
    } else if (tierInfo.tier === 2) {
      indicatorPosition =
        tierWidthPercent +
        ((usage - this.tierConfig.tiers[0].max) /
          (this.tierConfig.tiers[1].max - this.tierConfig.tiers[0].max)) *
          tierWidthPercent;
      // 确保在第二阶梯范围内
      indicatorPosition = Math.max(
        tierWidthPercent,
        Math.min(indicatorPosition, 2 * tierWidthPercent),
      );
    } else if (tierInfo.tier === 3) {
      indicatorPosition =
        2 * tierWidthPercent +
        ((usage - this.tierConfig.tiers[1].max) / 1000) * tierWidthPercent;
      // 确保不超过100%
      indicatorPosition = Math.min(indicatorPosition, totalWidth);
    }

    // 计算红色竖线和倒三角的最终位置
    const redLineLeft = Math.max(0, indicatorPosition);
    const triangleLeft = Math.max(0, indicatorPosition);

    // 当前阶梯指示器的背景颜色始终使用白天模式的颜色，不跟随黑夜模式变化
    let currentIndicatorBgColor;
    if (tierInfo.tier === 1) {
      currentIndicatorBgColor = "rgb(85, 197, 147)"; // 始终使用白天模式的第一阶梯颜色
    } else if (tierInfo.tier === 2) {
      currentIndicatorBgColor = "rgb(248, 195, 55)"; // 始终使用白天模式的第二阶梯颜色
    } else if (tierInfo.tier === 3) {
      currentIndicatorBgColor = "rgb(247, 147, 53)"; // 始终使用白天模式的第三阶梯颜色
    }

    // 将数字阶梯转换为带圆圈的数字
    const circleTiers = { 1: "❶", 2: "❷", 3: "❸" };

    // 创建完整的文本内容（单位从 standardData 获取，电力为"°"）
    const usageUnit =
      this.standardData?.unit || this.getUnitByUtilityType(this.utilityType);
    const fullText = `${circleTiers[tierInfo.tier]}${(usage || 0).toFixed(2)}${usageUnit}`;

    // 计算当前阶梯指示器的位置和变换
    let currentIndicatorLeft = 0;
    let currentIndicatorTransform = "";

    if (tierInfo.tier === 1) {
      // 当用电量为0时，将指示器放在最左侧
      if (usage <= 0) {
        currentIndicatorLeft = 0;
        currentIndicatorTransform = "none";
      } else {
        currentIndicatorLeft = 0;
        currentIndicatorTransform = "none";
      }
    } else if (tierInfo.tier === 3) {
      currentIndicatorLeft = 100;
      currentIndicatorTransform = "translateX(-100%)";
    } else {
      currentIndicatorLeft = indicatorPosition;
      currentIndicatorTransform = "translateX(-50%)";
    }

    // 创建红色竖线指示器（放在tiers-container中）
    const redLineIndicator = document.createElement("div");
    redLineIndicator.className = "red-line-indicator";
    redLineIndicator.style.left = `${redLineLeft}%`;
    redLineIndicator.style.position = "absolute";
    redLineIndicator.style.top = "13px";
    redLineIndicator.style.width = "3px";
    redLineIndicator.style.height = "15px";
    redLineIndicator.style.backgroundColor = "#ff0000";
    redLineIndicator.style.zIndex = "8";
    redLineIndicator.style.boxShadow = "0 0 3px rgba(255, 0, 0, 0.7)";
    redLineIndicator.style.transform = "translateX(-50%)";
    this.tiersContainerEl.appendChild(redLineIndicator);

    // 更新当前指示器内容（含单位：电力°，燃气m³，usageUnit 已在上面声明）
    this.currentTierEl.textContent =
      circleTiers[tierInfo.tier] || tierInfo.tier;
    if (this.currentUsageEl)
      this.currentUsageEl.textContent = (usage || 0).toFixed(2);
    if (this.currentUsageUnitEl)
      this.currentUsageUnitEl.textContent = usageUnit;

    // 更新当前指示器背景色
    this.currentIndicatorEl.classList.remove("tier-1", "tier-2", "tier-3");
    this.currentIndicatorEl.classList.add(`tier-${tierInfo.tier}`);

    // 设置当前指示器的位置和样式（使用用户代码的精确定位方式）
    this.currentIndicatorEl.style.position = "absolute";
    this.currentIndicatorEl.style.top = "-18px";
    this.currentIndicatorEl.style.left = `${currentIndicatorLeft}%`;
    this.currentIndicatorEl.style.transform = currentIndicatorTransform;
    this.currentIndicatorEl.style.backgroundColor = currentIndicatorBgColor;
    this.currentIndicatorEl.style.color = "white";
    this.currentIndicatorEl.style.padding = "4px 10px";
    this.currentIndicatorEl.style.borderRadius = "10px";
    this.currentIndicatorEl.style.fontSize = "11px";
    this.currentIndicatorEl.style.fontWeight = "600";
    this.currentIndicatorEl.style.whiteSpace = "nowrap";
    this.currentIndicatorEl.style.boxShadow = "0 2px 4px rgba(0, 0, 0, 0.2)";
    this.currentIndicatorEl.style.zIndex = "10";
    this.currentIndicatorEl.style.pointerEvents = "none";
    this.currentIndicatorEl.style.minWidth = "auto";
    this.currentIndicatorEl.style.maxWidth = "300px";
    this.currentIndicatorEl.style.overflow = "hidden";
    this.currentIndicatorEl.style.textOverflow = "ellipsis";

    // 当用电量为0时，确保在最左侧
    if (usage <= 0) {
      this.currentIndicatorEl.style.left = "0%";
      this.currentIndicatorEl.style.transform = "none";
    }

    // 移除可能存在的旧倒三角指示器
    const oldTriangle = this.shadowRoot.querySelector(
      ".current-indicator-triangle",
    );
    if (oldTriangle) {
      oldTriangle.remove();
    }

    // 添加倒三角指示器 - 确保与红色竖线完全对齐
    const triangleIndicator = document.createElement("div");
    triangleIndicator.className = "current-indicator-triangle";
    triangleIndicator.style.position = "absolute";
    triangleIndicator.style.top = "6px";
    triangleIndicator.style.left = `${triangleLeft}%`;
    triangleIndicator.style.transform = "translateX(-50%)";
    triangleIndicator.style.width = "0";
    triangleIndicator.style.height = "0";
    triangleIndicator.style.borderLeft = "4px solid transparent";
    triangleIndicator.style.borderRight = "4px solid transparent";
    triangleIndicator.style.borderTop = `4px solid ${currentIndicatorBgColor}`;
    triangleIndicator.style.zIndex = "9";
    triangleIndicator.style.pointerEvents = "none";
    this.tiersContainerEl.appendChild(triangleIndicator);

    // 同时设置indicator-arrow的位置，跟随red-line-indicator
    if (this.indicatorArrowEl) {
      this.indicatorArrowEl.style.left = `${redLineLeft}%`;
      // 设置indicator-arrow的颜色跟随current-indicator的tier颜色
      this.indicatorArrowEl.style.borderTopColor = currentIndicatorBgColor;
    }
  }

  // 辅助方法：从entity获取属性，支持从data节点下获取
  getEntityAttribute(entity, attributeName) {
    if (!entity || !entity.attributes) return undefined;

    // 优先从顶层节点获取
    let value = entity.attributes[attributeName];

    // 如果顶层节点没有，尝试从data节点获取
    if (value === undefined && entity.attributes.data) {
      value = entity.attributes.data[attributeName];
    }

    return value;
  }

  // 辅助方法：判断是否使用data节点
  isUsingDataNode(entity) {
    return (
      entity &&
      entity.attributes &&
      entity.attributes.data &&
      entity.attributes.data.daylist
    );
  }

  async updateCard() {
    if (!this._hass || !this.entityId) return;

    // 获取实体
    const entity = this._hass.states[this.entityId];
    let balance = 0;

    // 检查实体是否可用（不存在或状态为unknown/unavailable）
    const isEntityUnavailable =
      !entity || entity.state === "unknown" || entity.state === "unavailable";

    if (isEntityUnavailable) {
      // 实体不可用，显示"--"
      if (this.balanceEl) {
        this.balanceEl.textContent = "--";
      }
      if (this.electricityPriceEl) {
        this.electricityPriceEl.textContent = "--";
      }
      if (this.remainingDaysEl) {
        this.remainingDaysEl.textContent = "--";
      }
      if (this.remainingDaysDateEl) {
        this.remainingDaysDateEl.textContent = "";
      }

      // data-container中三个按钮的用量和金额显示为"--"
      if (this.currentMonthElectricityEl)
        this.currentMonthElectricityEl.textContent = "--";
      if (this.currentMonthCostEl) this.currentMonthCostEl.textContent = "--";
      if (this.lastMonthElectricityEl)
        this.lastMonthElectricityEl.textContent = "--";
      if (this.lastMonthCostEl) this.lastMonthCostEl.textContent = "--";
      if (this.yearElectricityEl) this.yearElectricityEl.textContent = "--";
      if (this.yearCostEl) this.yearCostEl.textContent = "--";

      // tiers-container中的阶梯值和阶梯单价显示为"--"
      if (this.tier1RangeEl) this.tier1RangeEl.textContent = "--";
      if (this.tier1PriceEl) this.tier1PriceEl.textContent = "--";
      if (this.tier2RangeEl) this.tier2RangeEl.textContent = "--";
      if (this.tier2PriceEl) this.tier2PriceEl.textContent = "--";
      if (this.tier3RangeEl) this.tier3RangeEl.textContent = "--";
      if (this.tier3PriceEl) this.tier3PriceEl.textContent = "--";

      // current-indicator中的阶梯值和合计用电值显示为"--"
      if (this.currentTierEl) this.currentTierEl.textContent = "--";
      if (this.currentUsageEl) this.currentUsageEl.textContent = "--";
      if (this.currentUsageUnitEl) this.currentUsageUnitEl.textContent = "";

      // 将阶梯指示器移动到最左侧
      if (this.currentIndicatorEl) {
        this.currentIndicatorEl.style.left = "0%";
        this.currentIndicatorEl.style.transform = "none";
        // 移除阶梯样式类，保持默认样式
        this.currentIndicatorEl.className = "current-indicator";
      }

      // 将indicator-arrow也移动到最左侧
      if (this.indicatorArrowEl) {
        this.indicatorArrowEl.style.left = "0%";
        this.indicatorArrowEl.style.transform = "none";
      }

      // 移除红色竖线和倒三角指示器
      if (this.tiersContainerEl) {
        const redLines = this.tiersContainerEl.querySelectorAll(
          ".red-line-indicator",
        );
        redLines.forEach((line) => line.remove());
        const triangles = this.tiersContainerEl.querySelectorAll(
          ".current-indicator-triangle",
        );
        triangles.forEach((triangle) => triangle.remove());
      }

      return;
    }

    if (entity) {
      // 优先从entity.state获取余额，如果state不是数值，则尝试从data节点获取
      balance = parseFloat(entity.state);
      if (isNaN(balance)) {
        // 如果state不是数值，尝试从data.balance获取
        balance = parseFloat(this.getEntityAttribute(entity, "balance")) || 0;
      }
      this.balanceEl.textContent = (balance || 0).toFixed(2);

      // 当 utility_type 为 'ele' 时，根据 is_prepaid 字段动态显示标签
      if (this.utilityType === "ele" && this.balanceLabelEl) {
        let isPrepaid = this.getEntityAttribute(entity, "is_prepaid");
        if (
          isPrepaid === undefined &&
          entity.attributes &&
          entity.attributes.data
        ) {
          isPrepaid = entity.attributes.data["is_prepaid"];
        }

        // 集成可能返回 boolean (true/false) 或字符串 '是'/'否'
        const isPrepaidVal =
          isPrepaid === true ||
          isPrepaid === "是" ||
          isPrepaid === "1" ||
          isPrepaid === 1;
        if (isPrepaidVal) {
          this.balanceLabelEl.textContent = "账户余额";
        } else {
          this.balanceLabelEl.textContent = "当期账单";
        }
      } else if (this.utilityType !== "ele" && this.balanceLabelEl) {
        // 当不为电力类型时，显示为"账户余额"
        this.balanceLabelEl.textContent = "账户余额";
      }
    }

    // 更新剩余天数
    if (entity && entity.attributes) {
      let remainingDays = 0;

      // 支持字段映射：优先使用 fieldMapping.remaining_days 指定的属性名
      const remainingDaysKey =
        (this.fieldMapping && this.fieldMapping.remaining_days) ||
        "remaining_days";
      let daysValue = this.getEntityAttribute(entity, remainingDaysKey);
      // 若映射名未取到，尝试标准名（兼容不同集成/版本）
      if (daysValue === undefined) {
        daysValue = this.getEntityAttribute(entity, "remaining_days");
      }
      // 电力类型：再尝试从 计费标准 对象获取（部分集成可能放在此处）
      if (daysValue === undefined && this.utilityType === "ele") {
        const billingStandard = this.getBillingStandardObject(entity);
        if (
          billingStandard &&
          (billingStandard.剩余天数 !== undefined ||
            billingStandard.remaining_days !== undefined)
        ) {
          daysValue =
            billingStandard.剩余天数 ?? billingStandard.remaining_days;
        }
      }
      if (daysValue !== undefined && parseInt(daysValue, 10) > 0) {
        // 实体提供了有效值（>0）时直接使用
        remainingDays = parseInt(daysValue, 10);
      } else {
        // 实体未提供或为0时，根据近7天平均用量计算剩余天数
        // 电力：优先尝试实体提供的 avg_daily_cost（集成已计算好的日均电费）
        if (this.utilityType === "ele" && balance > 0) {
          const avgDailyCost = parseFloat(
            this.getEntityAttribute(entity, "avg_daily_cost"),
          );
          if (!isNaN(avgDailyCost) && avgDailyCost > 0) {
            remainingDays = Math.max(0, Math.floor(balance / avgDailyCost));
            this.debugLog(
              `电力-使用实体 avg_daily_cost: ${avgDailyCost}, 余额: ${balance}, 计算剩余天数: ${remainingDays}`,
            );
          }
        }
        if (remainingDays === 0) {
          // 优先用 standardData.dayUsage（与日历完全同源，含 total_amount）
          let daylist =
            this.standardData &&
            this.standardData.dayUsage &&
            this.standardData.dayUsage.length > 0
              ? this.standardData.dayUsage
              : this.daylistData && this.daylistData.length > 0
                ? this.daylistData
                : this.getEntityAttribute(entity, "daylist") || [];
          if (typeof daylist === "string") {
            try {
              daylist = JSON.parse(daylist);
            } catch (e) {
              console.warn("解析 daylist 失败:", e);
              daylist = [];
            }
          }

          // 获取近7天的数据（daylist 前7条为最近，电力/燃气集成均按此顺序）
          // 与燃气集成一致：用日均费用(元/天)计算，剩余天数 = 余额 / 日均费用
          if (daylist && daylist.length > 0) {
            const recent7Days = daylist.slice(0, 7);
            const mapping = this.fieldMapping || {};
            const amountField =
              this.utilityType === "gas"
                ? mapping.amount || "amount"
                : mapping.amount || "dayEleCost";

            // 排除 cost=0 的天，仅用有费用的天计算日均
            // 电力兼容：dayEleCost、cost、dayEleAmtTotal、dayEleAmt；燃气：amount、cycleTotalValues
            let totalCost = 0;
            let validDays = 0;
            recent7Days.forEach((item) => {
              const cost =
                parseFloat(
                  item.total_amount ??
                    item[amountField] ??
                    item.dayEleCost ??
                    item.cost ??
                    item.dayEleAmtTotal ??
                    item.dayEleAmt ??
                    item.amount ??
                    item.cycleTotalValues,
                ) || 0;
              if (cost > 0) {
                totalCost += cost;
                validDays++;
              }
            });

            const avgDailyCost = validDays > 0 ? totalCost / validDays : 0;
            const balanceForCalc = balance !== 0 ? Math.abs(balance) : 0;
            if (avgDailyCost > 0 && balanceForCalc > 0) {
              remainingDays = Math.max(
                0,
                Math.floor(balanceForCalc / avgDailyCost),
              );
              this.debugLog(
                `近7天有效${validDays}天(排除cost=0)日均: ${avgDailyCost.toFixed(2)}元/天, 余额: ${balance.toFixed(2)}元, 剩余天数: ${remainingDays}`,
              );
            }
          }
        }
      }

      // 更新剩余天数显示
      if (this.remainingDaysEl) {
        this.remainingDaysEl.textContent = remainingDays;
      }

      // 计算并显示剩余天数对应的日期
      if (this.remainingDaysDateEl && remainingDays > 0) {
        const today = new Date();
        const futureDate = new Date(
          today.getTime() + remainingDays * 24 * 60 * 60 * 1000,
        );
        const month = futureDate.getMonth() + 1; // getMonth() 返回 0-11
        const day = futureDate.getDate();
        this.remainingDaysDateEl.textContent = `(${month}月${day}日)`;
      } else if (this.remainingDaysDateEl) {
        this.remainingDaysDateEl.textContent = "";
      }
    }

    // 获取 daylist 数据用于阶梯计算
    let daylist = [];
    if (entity && entity.attributes) {
      daylist = this.getEntityAttribute(entity, "daylist") || [];
      if (typeof daylist === "string") {
        try {
          daylist = JSON.parse(daylist);
        } catch (e) {
          console.warn("解析 daylist 失败:", e);
          daylist = [];
        }
      }
    }

    // 如果标准数据还没有加载，先加载数据
    if (!this.historicalDataLoaded) {
      this.loadDataForCurrentUser();
      return;
    }

    // 从标准格式获取本月数据
    let currentMonthStandard = this.getCurrentMonthStandardData();
    // 电力：month_ele_num/month_ele_cost；燃气：month_gas_num/month_gas_cost
    if (entity) {
      if (this.utilityType === "ele") {
        const monthUsage = this.getEntityAttribute(entity, "month_ele_num");
        const monthCost = this.getEntityAttribute(entity, "month_ele_cost");
        if (monthUsage !== undefined || monthCost !== undefined) {
          const parsedUsage = parseFloat(monthUsage);
          const parsedCost = parseFloat(monthCost);
          if (!isNaN(parsedUsage) || !isNaN(parsedCost)) {
            currentMonthStandard = {
              ...currentMonthStandard,
              total_usage: isNaN(parsedUsage)
                ? currentMonthStandard.total_usage || 0
                : parsedUsage,
              total_amount: isNaN(parsedCost)
                ? currentMonthStandard.total_amount || 0
                : parsedCost,
            };
          }
        }
      } else if (this.utilityType === "gas") {
        const monthUsage = this.getEntityAttribute(entity, "month_gas_num");
        const monthCost = this.getEntityAttribute(entity, "month_gas_cost");
        if (monthUsage !== undefined || monthCost !== undefined) {
          const parsedUsage = parseFloat(monthUsage);
          const parsedCost = parseFloat(monthCost);
          if (!isNaN(parsedUsage) || !isNaN(parsedCost)) {
            currentMonthStandard = {
              ...currentMonthStandard,
              total_usage: isNaN(parsedUsage)
                ? currentMonthStandard.total_usage || 0
                : parsedUsage,
              total_amount: isNaN(parsedCost)
                ? currentMonthStandard.total_amount || 0
                : parsedCost,
            };
          }
        }
      }
    }
    this.currentMonthElectricityEl.textContent = (
      currentMonthStandard.total_usage || 0
    ).toFixed(2);
    this.currentMonthCostEl.textContent = (
      currentMonthStandard.total_amount || 0
    ).toFixed(2);
    this.currentMonthEleUnitEl.textContent = currentMonthStandard.unit;
    this.currentMonthCostUnitEl.textContent = "元";

    // 更新上月数据
    let lastMonthStandard = this.getLastMonthStandardData();
    // 电力：last_month_ele_num/last_month_ele_cost；燃气：last_month_gas_num/last_month_gas_cost
    if (entity) {
      if (this.utilityType === "ele") {
        const lastMonthUsage = this.getEntityAttribute(
          entity,
          "last_month_ele_num",
        );
        const lastMonthCost = this.getEntityAttribute(
          entity,
          "last_month_ele_cost",
        );
        if (lastMonthUsage !== undefined || lastMonthCost !== undefined) {
          const parsedUsage = parseFloat(lastMonthUsage);
          const parsedCost = parseFloat(lastMonthCost);
          if (!isNaN(parsedUsage) || !isNaN(parsedCost)) {
            lastMonthStandard = {
              ...lastMonthStandard,
              total_usage: isNaN(parsedUsage)
                ? lastMonthStandard.total_usage || 0
                : parsedUsage,
              total_amount: isNaN(parsedCost)
                ? lastMonthStandard.total_amount || 0
                : parsedCost,
            };
          }
        }
      } else if (this.utilityType === "gas") {
        const lastMonthUsage = this.getEntityAttribute(
          entity,
          "last_month_gas_num",
        );
        const lastMonthCost = this.getEntityAttribute(
          entity,
          "last_month_gas_cost",
        );
        if (lastMonthUsage !== undefined || lastMonthCost !== undefined) {
          const parsedUsage = parseFloat(lastMonthUsage);
          const parsedCost = parseFloat(lastMonthCost);
          if (!isNaN(parsedUsage) || !isNaN(parsedCost)) {
            lastMonthStandard = {
              ...lastMonthStandard,
              total_usage: isNaN(parsedUsage)
                ? lastMonthStandard.total_usage || 0
                : parsedUsage,
              total_amount: isNaN(parsedCost)
                ? lastMonthStandard.total_amount || 0
                : parsedCost,
            };
          }
        }
      }
    }
    this.lastMonthElectricityEl.textContent = (
      lastMonthStandard.total_usage || 0
    ).toFixed(2);
    this.lastMonthCostEl.textContent = (
      lastMonthStandard.total_amount || 0
    ).toFixed(2);
    this.lastMonthEleUnitEl.textContent = lastMonthStandard.unit;
    this.lastMonthCostUnitEl.textContent = "元";

    // 更新年度数据
    let currentYearStandard = this.getCurrentYearStandardData();
    const currentYearStr = new Date().getFullYear().toString();
    // 电力：year_ele_num/year_ele_cost；燃气：year_gas_num/year_gas_cost
    if (entity && entity.attributes) {
      if (this.utilityType === "ele") {
        const yearUsage = this.getEntityAttribute(entity, "year_ele_num");
        const yearCost = this.getEntityAttribute(entity, "year_ele_cost");
        if (yearUsage !== undefined || yearCost !== undefined) {
          const parsedUsage = parseFloat(yearUsage);
          const parsedCost = parseFloat(yearCost);
          if (!isNaN(parsedUsage) || !isNaN(parsedCost)) {
            currentYearStandard = {
              ...currentYearStandard,
              time: currentYearStr,
              total_usage: isNaN(parsedUsage)
                ? currentYearStandard.total_usage || 0
                : parsedUsage,
              total_amount: isNaN(parsedCost)
                ? currentYearStandard.total_amount || 0
                : parsedCost,
            };
          }
        }
      } else if (this.utilityType === "gas") {
        const yearUsage = this.getEntityAttribute(entity, "year_gas_num");
        const yearCost = this.getEntityAttribute(entity, "year_gas_cost");
        if (yearUsage !== undefined || yearCost !== undefined) {
          const parsedUsage = parseFloat(yearUsage);
          const parsedCost = parseFloat(yearCost);
          if (!isNaN(parsedUsage) || !isNaN(parsedCost)) {
            currentYearStandard = {
              ...currentYearStandard,
              time: currentYearStr,
              total_usage: isNaN(parsedUsage)
                ? currentYearStandard.total_usage || 0
                : parsedUsage,
              total_amount: isNaN(parsedCost)
                ? currentYearStandard.total_amount || 0
                : parsedCost,
            };
          }
        }
      }
    }
    this.currentYearEl.textContent = currentYearStandard.time || currentYearStr;

    this.yearElectricityEl.textContent = (
      currentYearStandard.total_usage || 0
    ).toFixed(2);
    this.yearCostEl.textContent = (
      currentYearStandard.total_amount || 0
    ).toFixed(2);
    this.yearEleUnitEl.textContent = currentYearStandard.unit;
    this.yearCostUnitEl.textContent = "元";

    // 将实体数据合并到 standardData，供年度/月度图表使用（与主卡片显示一致）
    if (this.standardData && entity) {
      const usageUnit =
        this.standardData.unit || this.getUnitByUtilityType(this.utilityType);
      // 合并当前年度到 yearUsage
      let yearIdx = this.standardData.yearUsage.findIndex(
        (item) => item.time === currentYearStr,
      );
      const yearEntry = {
        user: this.getCurrentCacheKey(),
        utility_type: this.utilityType,
        data_category: "usage",
        date_granularity: "year",
        time: currentYearStr,
        data_source: "entity",
        total_usage: currentYearStandard.total_usage || 0,
        total_amount: currentYearStandard.total_amount || 0,
        unit: usageUnit,
        usage_ele_no: currentYearStandard.total_usage || 0,
      };
      if (yearIdx >= 0) {
        this.standardData.yearUsage[yearIdx] = {
          ...this.standardData.yearUsage[yearIdx],
          ...yearEntry,
        };
      } else {
        this.standardData.yearUsage.unshift(yearEntry);
        this.standardData.yearUsage.sort((a, b) =>
          (b.time || "").localeCompare(a.time || ""),
        );
      }
      // 合并本月、上月到 monthUsage
      const currentMonthStr = this.getCurrentMonthStr();
      const lastMonthDate = new Date(
        new Date().getFullYear(),
        new Date().getMonth() - 1,
        1,
      );
      const lastMonthStr = `${lastMonthDate.getFullYear()}-${pad2(lastMonthDate.getMonth() + 1)}`;
      [currentMonthStr, lastMonthStr].forEach((monthTime, i) => {
        const data = i === 0 ? currentMonthStandard : lastMonthStandard;
        let monthIdx = this.standardData.monthUsage.findIndex(
          (item) => item.time === monthTime,
        );
        const monthEntry = {
          user: this.getCurrentCacheKey(),
          utility_type: this.utilityType,
          data_category: "usage",
          date_granularity: "month",
          time: monthTime,
          data_source: "entity",
          total_usage: data.total_usage || 0,
          total_amount: data.total_amount || 0,
          unit: usageUnit,
          usage_ele_no: data.total_usage || 0,
        };
        if (monthIdx >= 0) {
          this.standardData.monthUsage[monthIdx] = {
            ...this.standardData.monthUsage[monthIdx],
            ...monthEntry,
          };
        } else {
          this.standardData.monthUsage.unshift(monthEntry);
          this.standardData.monthUsage.sort((a, b) =>
            (b.time || "").localeCompare(a.time || ""),
          );
        }
      });
      // 若当前在年度/月度视图，重新渲染图表以使用合并后的数据
      if (this.currentView === "year") {
        this.renderYearView();
      } else if (this.currentView === "month") {
        this.renderMonthView();
      }
    }

    // 计算并更新阶梯电价
    let currentPeriodUsage = 0;

    // 从 entity 的"计费标准"节点获取年阶梯累计用量（电力、燃气均支持，与年用气量/年用电量一致）
    if (this._hass && entity) {
      const billingStandard = this.getBillingStandardObject(entity);
      if (billingStandard) {
        if (this.utilityType === "ele") {
          // 电力：优先 年阶梯累计用电量，兼容 年累计用电量（ele 集成可能使用后者）
          currentPeriodUsage =
            billingStandard["年阶梯累计用电量"] !== undefined
              ? parseFloat(billingStandard["年阶梯累计用电量"])
              : billingStandard["年累计用电量"] !== undefined
                ? parseFloat(billingStandard["年累计用电量"])
                : 0;
        } else if (this.utilityType === "gas") {
          currentPeriodUsage =
            billingStandard["年阶梯累计用气量"] !== undefined
              ? parseFloat(billingStandard["年阶梯累计用气量"])
              : 0;
        } else {
          currentPeriodUsage =
            billingStandard["年阶梯累计用气量"] !== undefined
              ? parseFloat(billingStandard["年阶梯累计用气量"])
              : billingStandard["年阶梯累计用电量"] !== undefined
                ? parseFloat(billingStandard["年阶梯累计用电量"])
                : 0;
        }
      }
    }

    // 如果没有计费标准，使用 daylist 计算；电力/燃气可回退到年用量（与年度显示一致）
    if (currentPeriodUsage === 0) {
      currentPeriodUsage = this.calculatePeriodUsageFromDaylist(daylist);
      // 电力/燃气：若 daylist 无法计算或结果偏小，用年用量作为回退，确保与年度显示一致
      if (currentYearStandard && currentYearStandard.total_usage) {
        const yearUsage = parseFloat(currentYearStandard.total_usage) || 0;
        if (currentPeriodUsage === 0 || yearUsage > currentPeriodUsage) {
          currentPeriodUsage = yearUsage;
          this.debugLog(
            "阶梯用量采用年用量（与年度显示一致）:",
            currentPeriodUsage,
          );
        }
      }
      this.debugLog("从 daylist 计算当前周期用电量:", currentPeriodUsage);
    }

    // 延迟更新阶梯指示器，确保DOM已经渲染完成
    // 使用 setTimeout 给予更充足的时间确保DOM和样式都准备好
    setTimeout(() => {
      this.updateTierIndicator(currentPeriodUsage);
    }, 150);

    // 更新阶梯显示（确保单位从统一数据格式中获取）
    this.updateTierDisplay();

    // 更新主题（以便实时响应开关实体的状态变化）
    this.updateTheme(this._config);

    // 延迟应用隐藏配置，确保动态元素已创建
    requestAnimationFrame(() => {
      this.applyHiddenConfig();
    });
  }

  // 定义卡片配置架构
  static getStubConfig() {
    return {
      name: "电力燃气信息",
      // 主题配置 (不配置时自动根据时间切换: 白天light, 晚上dark)
      theme: undefined,
    };
  }
}

// ==================== 自定义元素注册 ====================
try {
  if (!customElements.get("xjelegas-card")) {
    customElements.define("xjelegas-card", EleGasCard);
  }
} catch (error) {
  console.warn("自定义组件注册警告:", error.message);
}

// 向 Home Assistant 注册卡片类型，用于 Lovelace 仪表盘
window.customCards = window.customCards || [];
window.customCards.push({
  type: "xjelegas-card",
  name: "电力燃气信息卡片",
  description:
    "展示电力燃气使用量、缴费的卡片，显示各类图表信息，支持自定义阶梯、计费周期、设备运行统计（时长、电费）、主题切换和控制各项显示",
  preview: true,
});

/**
 * 电力燃气信息卡片 - 可视化配置编辑器
 * 提供图形化配置界面，支持基础配置、多用户配置、高级配置等
 */
// ==================== SECTION 5: EleGasCardEditor 编辑器类 ====================
class EleGasCardEditor extends HTMLElement {
  /** 构造函数：初始化配置、hass、防抖派发等 */
  constructor() {
    super();
    this._config = {}; // 当前配置对象
    this._hass = null; // Home Assistant 实例引用
    this._initialized = false; // 是否已完成初始化
    this._rendered = false; // 是否已渲染
    this._pendingConfig = null; // 待应用的配置（初始化前）
    this._pendingHass = null; // 待应用的 hass（初始化前）
    this._previewCode = null; // JSON 预览区域元素引用
    this._debouncedDispatch = debounce(
      () => this._dispatchConfigChangedEvent(),
      50,
    );
    this._accordionItems = []; // 手风琴折叠项引用
  }

  disconnectedCallback() {
    if (this._debouncedDispatch) this._debouncedDispatch.cancel();
    // 移除所有 document 全局点击监听（实体下拉框关闭）
    if (this._documentClickHandlers) {
      this._documentClickHandlers.forEach(({ handler }) => {
        document.removeEventListener("click", handler);
      });
      this._documentClickHandlers = [];
    }
  }

  setConfig(config) {
    const nextConfig = config ? JSON.parse(JSON.stringify(config)) : {};
    const configChanged =
      JSON.stringify(this._config) !== JSON.stringify(nextConfig);
    this._config = nextConfig;
    if (!this._initialized) {
      this._pendingConfig = true;
      this._tryRender();
    } else if (configChanged) {
      this._updateFormValues();
    }
  }

  /** 接收 Home Assistant 实例，用于获取实体列表、更新实体选项等 */
  set hass(hass) {
    const hassChanged = this._hass !== hass;
    this._hass = hass;
    if (!this._initialized) {
      this._pendingHass = true;
      this._tryRender();
    } else if (hassChanged) {
      this._updateEntityOptions();
    }
  }

  /** 尝试渲染编辑器，需同时具备 config 和 hass 才执行 */
  _tryRender() {
    if (this._pendingConfig && this._pendingHass && this._hass) {
      this._initialized = true;
      this.render();
    }
  }

  /** 派发 config-changed 事件，通知 HA 配置已变更 */
  _dispatchConfigChangedEvent() {
    this._updatePreview();
    const nextConfig = this._config
      ? JSON.parse(JSON.stringify(this._config))
      : {};
    const event = new CustomEvent("config-changed", {
      bubbles: true,
      composed: true,
      detail: { config: nextConfig },
    });
    this.dispatchEvent(event);
  }

  /** 更新 JSON 预览区域的显示内容 */
  _updatePreview() {
    if (!this._previewCode) return;
    this._previewCode.textContent = JSON.stringify(this._config, null, 2);
  }

  /**
   * 更新配置项并派发变更事件
   * @param {string} key - 配置键，支持嵌套路径如 'multiclass.1.entity'
   * @param {*} value - 新值，空值会删除该配置项
   */
  _updateConfigValue(key, value) {
    const oldValue = this._getNestedValue(this._config, key);
    if (value === oldValue || (value === undefined && oldValue === undefined)) {
      return;
    }
    if (value === undefined || value === "" || value === null) {
      if (key.includes(".")) {
        const keys = key.split(".");
        const lastKey = keys.pop();
        const target = keys.reduce((o, p) => o && o[p], this._config);
        if (target) delete target[lastKey];
      } else {
        delete this._config[key];
      }
    } else {
      if (key.includes(".")) {
        this._setNestedValue(this._config, key, value);
      } else {
        this._config[key] = value;
      }
    }
    // 卡片名称变更时立即派发，确保预览实时更新；其他配置项防抖 50ms
    if (key === "name") {
      this._debouncedDispatch.cancel();
      this._dispatchConfigChangedEvent();
    } else {
      this._debouncedDispatch();
    }
  }

  /** 将当前配置同步到表单控件，用于 config 回传时更新输入框显示 */
  _updateFormValues() {
    const inputs = this.querySelectorAll(
      "input[data-config-key], select[data-config-key], textarea[data-config-key]",
    );
    inputs.forEach((input) => {
      const key = input.getAttribute("data-config-key");
      if (!key) return;
      // 正在编辑的输入框不覆盖，避免 config-changed 回传时用旧值覆盖用户正在输入的内容
      if (document.activeElement === input) return;
      const value = this._getNestedValue(this._config, key);
      if (input.type === "checkbox") {
        input.checked = !!value;
        const label = input.parentElement;
        if (label) {
          const text = label.querySelector("[data-switch-text]");
          if (text) {
            text.textContent = input.checked ? "开启" : "关闭";
          }
        }
      } else if (input.tagName === "SELECT") {
        if (key === "theme" && typeof value === "object") {
          input.value = "";
        } else if (value) {
          input.value = value;
        }
      } else {
        input.value = value !== undefined ? value : "";
      }
    });
    const multiSelectContainers = this.querySelectorAll("div[data-config-key]");
    multiSelectContainers.forEach((container) => {
      const key = container.getAttribute("data-config-key");
      if (!key) return;
      const value = this._getNestedValue(this._config, key);
      const selected = value ? value.split(",").map((s) => s.trim()) : [];
      const checkboxes = container.querySelectorAll('input[type="checkbox"]');
      checkboxes.forEach((cb) => {
        cb.checked = selected.includes(cb.value);
        const label = cb.parentElement;
        if (label) {
          label.style.background = cb.checked
            ? "var(--primary-color, #03a9f4)"
            : "var(--secondary-background-color, #f5f5f5)";
          label.style.color = cb.checked
            ? "#fff"
            : "var(--primary-text-color, #212121)";
        }
      });
    });
  }

  /** 根据路径获取嵌套对象中的值，如 'a.b.c' 获取 obj.a.b.c */
  _getNestedValue(obj, path) {
    if (!path) return undefined;
    return path.split(".").reduce((o, p) => o && o[p], obj);
  }

  /** 根据路径设置嵌套对象中的值，缺失的中间层会自动创建 */
  _setNestedValue(obj, path, value) {
    const keys = path.split(".");
    const lastKey = keys.pop();
    const target = keys.reduce((o, p) => {
      if (!o[p]) o[p] = {};
      return o[p];
    }, obj);
    target[lastKey] = value;
  }

  /** 刷新实体选择器的可选实体列表（hass 变更时调用） */
  _updateEntityOptions() {
    const entityInputs = this.querySelectorAll("input[data-entity-selector]");
    entityInputs.forEach((input) => {
      const domain = input.getAttribute("data-domain");
      const datalistId = input.getAttribute("list");
      const datalist = this.querySelector(`datalist#${datalistId}`);
      if (datalist) {
        datalist.innerHTML = "";
        const effectiveDomain = domain || "sensor";
        const entities = this._getEntities(effectiveDomain);
        entities.forEach((entity) => {
          const option = document.createElement("option");
          option.value = entity;
          datalist.appendChild(option);
        });
        const currentValue = input.value;
        if (currentValue && !entities.includes(currentValue)) {
          const option = document.createElement("option");
          option.value = currentValue;
          datalist.appendChild(option);
        }
      }
    });
  }

  /** 从 hass.states 获取实体列表，可按域过滤 */
  _getEntities(domain = null) {
    if (!this._hass) return [];
    const entities = Object.keys(this._hass.states);
    if (domain) {
      return entities.filter((e) => e.startsWith(`${domain}.`));
    }
    return entities;
  }

  /** 创建带标签的配置项容器 */
  _createSettingItem(label, element) {
    const container = document.createElement("div");
    container.style.cssText = `
      margin-bottom: 16px;
    `;
    const labelEl = document.createElement("label");
    labelEl.textContent = label;
    labelEl.style.cssText = `
      display: block;
      margin-bottom: 4px;
      font-weight: 500;
      color: var(--primary-text-color, #212121);
      font-size: 14px;
    `;
    container.appendChild(labelEl);
    container.appendChild(element);
    return container;
  }

  /**
   * 创建通用文本输入框
   * @param {number} debounceMs - 输入防抖毫秒数，0 表示每次输入立即触发
   */
  /** 创建通用文本输入框，支持防抖与 data-config-key 绑定 */
  _createTextInput(value, placeholder, onChange, configKey, debounceMs = 300) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value || "";
    input.placeholder = placeholder;
    if (configKey) input.setAttribute("data-config-key", configKey);
    input.style.cssText = `
      width: 100%;
      padding: 8px 12px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 4px;
      background: var(--card-background-color, #fff);
      color: var(--primary-text-color, #212121);
      font-size: 14px;
      box-sizing: border-box;
    `;
    const debouncedOnChange = debounce((v) => onChange(v), debounceMs);
    input.addEventListener("input", (e) => debouncedOnChange(e.target.value));
    input.addEventListener("blur", () => {
      debouncedOnChange.cancel();
      onChange(input.value);
    });
    return input;
  }

  /**
   * 卡片名称专用输入框
   * 使用轮询方式：focus 时每 150ms 检查输入值并同步到配置，解决 HA 在 input 事件期间不刷新预览的问题
   * 卡片名称专用输入框：focus 时轮询同步到配置，解决 HA 预览不刷新问题
   */
  _createCardNameInput(value, placeholder, onChange, configKey) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value || "";
    input.placeholder = placeholder;
    if (configKey) input.setAttribute("data-config-key", configKey);
    input.style.cssText = `
      width: 100%;
      padding: 8px 12px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 4px;
      background: var(--card-background-color, #fff);
      color: var(--primary-text-color, #212121);
      font-size: 14px;
      box-sizing: border-box;
    `;
    let pollTimer = null;
    const syncToConfig = () => {
      const val = input.value;
      if (val !== this._config?.name) {
        onChange(val);
      }
    };
    input.addEventListener("focus", () => {
      if (pollTimer) return;
      pollTimer = setInterval(syncToConfig, 150);
    });
    input.addEventListener("blur", () => {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      syncToConfig();
    });
    return input;
  }

  /** 创建实体选择器，支持搜索和下拉选择 HA 实体 */
  _createEntitySelector(selectedEntity, domain, onChange, configKey) {
    const effectiveDomain = domain || "sensor";
    const allEntities = this._getEntities(effectiveDomain);
    const container = document.createElement("div");
    container.style.cssText = `
      width: 100%;
      position: relative;
    `;
    const input = document.createElement("input");
    input.type = "text";
    input.value = selectedEntity || "";
    input.placeholder = "-- 选择或输入实体 --";
    if (configKey) input.setAttribute("data-config-key", configKey);
    if (domain) input.setAttribute("data-domain", domain);
    input.setAttribute("data-entity-selector", "true");
    input.style.cssText = `
      width: 100%;
      padding: 8px 12px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 4px;
      background: var(--card-background-color, #fff);
      color: var(--primary-text-color, #212121);
      font-size: 14px;
      box-sizing: border-box;
      cursor: pointer;
    `;
    const dropdownPanel = document.createElement("div");
    dropdownPanel.style.cssText = `
      position: absolute;
      top: 100%;
      left: 0;
      right: 0;
      max-height: 300px;
      overflow-y: auto;
      background: var(--card-background-color, #fff);
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 4px;
      margin-top: 4px;
      z-index: 1000;
      box-shadow: 0 4px 12px rgba(0,0,0,0.15);
      display: none;
    `;
    const searchBox = document.createElement("div");
    searchBox.style.cssText = `
      padding: 12px;
      border-bottom: 1px solid var(--divider-color, #e0e0e0);
      position: sticky;
      top: 0;
      background: var(--card-background-color, #fff);
      z-index: 1;
    `;
    const searchInput = document.createElement("input");
    searchInput.type = "text";
    searchInput.placeholder = "搜索...";
    searchInput.style.cssText = `
      width: 100%;
      padding: 8px 12px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 4px;
      background: var(--card-background-color, #fff);
      color: var(--primary-text-color, #212121);
      font-size: 14px;
      box-sizing: border-box;
    `;
    searchBox.appendChild(searchInput);
    dropdownPanel.appendChild(searchBox);
    const entityListContainer = document.createElement("div");
    entityListContainer.style.cssText = `
      padding: 4px 0;
    `;
    dropdownPanel.appendChild(entityListContainer);
    const renderEntityList = (entities) => {
      entityListContainer.innerHTML = "";
      if (entities.length === 0) {
        const emptyItem = document.createElement("div");
        emptyItem.textContent = "未找到匹配的实体";
        emptyItem.style.cssText = `
          padding: 12px;
          text-align: center;
          color: var(--secondary-text-color, #727272);
          font-style: italic;
        `;
        entityListContainer.appendChild(emptyItem);
        return;
      }
      entities.forEach((entity) => {
        const item = document.createElement("div");
        item.textContent = entity;
        item.style.cssText = `
          padding: 10px 16px;
          cursor: pointer;
          color: var(--primary-text-color, #212121);
          font-size: 14px;
          transition: background 0.2s;
        `;
        if (entity === selectedEntity) {
          item.style.background = "var(--primary-color, #03a9f4)";
          item.style.color = "#fff";
        }
        item.addEventListener("mouseenter", () => {
          if (entity !== selectedEntity) {
            item.style.background =
              "var(--secondary-background-color, #f5f5f5)";
          }
        });
        item.addEventListener("mouseleave", () => {
          if (entity !== selectedEntity) {
            item.style.background = "transparent";
          }
        });
        item.addEventListener("click", () => {
          input.value = entity;
          onChange(entity);
          dropdownPanel.style.display = "none";
        });
        entityListContainer.appendChild(item);
      });
    };
    renderEntityList(allEntities);
    searchInput.addEventListener("input", (e) => {
      const searchTerm = e.target.value.toLowerCase().trim();
      const filteredEntities = allEntities.filter((entity) =>
        entity.toLowerCase().includes(searchTerm),
      );
      renderEntityList(filteredEntities);
    });
    input.addEventListener("click", (e) => {
      e.stopPropagation();
      document.querySelectorAll("[data-entity-dropdown]").forEach((el) => {
        if (el !== dropdownPanel) {
          el.style.display = "none";
        }
      });
      dropdownPanel.style.display = "block";
      searchInput.value = "";
      renderEntityList(allEntities);
      searchInput.focus();
    });
    const debouncedOnChange = debounce((v) => onChange(v || undefined), 300);
    input.addEventListener("input", (e) =>
      debouncedOnChange(e.target.value.trim()),
    );
    const docClickHandler = (e) => {
      if (!container.contains(e.target)) {
        dropdownPanel.style.display = "none";
      }
    };
    document.addEventListener("click", docClickHandler);
    this._documentClickHandlers = this._documentClickHandlers || [];
    this._documentClickHandlers.push({ handler: docClickHandler });
    dropdownPanel.setAttribute("data-entity-dropdown", "true");
    container.appendChild(input);
    container.appendChild(dropdownPanel);
    return container;
  }

  /** 创建开关控件（开启/关闭） */
  _createSwitch(checked, onChange, configKey) {
    const label = document.createElement("label");
    label.style.cssText = `
      display: flex;
      align-items: center;
      cursor: pointer;
      gap: 8px;
    `;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = checked;
    if (configKey) input.setAttribute("data-config-key", configKey);
    input.style.cssText = `
      width: 18px;
      height: 18px;
      cursor: pointer;
    `;
    const text = document.createElement("span");
    text.textContent = checked ? "开启" : "关闭";
    text.style.cssText = "font-size: 14px;";
    text.setAttribute("data-switch-text", "true");
    input.addEventListener("change", (e) => {
      text.textContent = e.target.checked ? "开启" : "关闭";
      onChange(e.target.checked);
    });
    label.appendChild(input);
    label.appendChild(text);
    return label;
  }

  /** 创建下拉选择框，options 格式为 [{ value, label }] */
  _createSelect(options, selectedValue, onChange, configKey) {
    const select = document.createElement("select");
    if (configKey) select.setAttribute("data-config-key", configKey);
    select.style.cssText = `
      width: 100%;
      padding: 8px 12px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 4px;
      background: var(--card-background-color, #fff);
      color: var(--primary-text-color, #212121);
      font-size: 14px;
      cursor: pointer;
    `;
    const effectiveValue = selectedValue || options[0]?.value;
    options.forEach((opt) => {
      const option = document.createElement("option");
      option.value = opt.value;
      option.textContent = opt.label;
      if (opt.value === effectiveValue) {
        option.selected = true;
      }
      select.appendChild(option);
    });
    select.addEventListener("change", (e) => onChange(e.target.value));
    return select;
  }

  _createMultiSelect(options, selectedValues, onChange, configKey) {
    const container = document.createElement("div");
    if (configKey) container.setAttribute("data-config-key", configKey);
    container.style.cssText = `
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 8px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 4px;
      background: var(--card-background-color, #fff);
    `;
    const selected = selectedValues
      ? selectedValues.split(",").map((s) => s.trim())
      : [];
    options.forEach((opt) => {
      const checkbox = document.createElement("label");
      checkbox.style.cssText = `
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 4px 8px;
        background: ${selected.includes(opt.value) ? "var(--primary-color, #03a9f4)" : "var(--secondary-background-color, #f5f5f5)"};
        color: ${selected.includes(opt.value) ? "#fff" : "var(--primary-text-color, #212121)"};
        border-radius: 4px;
        cursor: pointer;
        font-size: 12px;
        transition: all 0.2s;
      `;
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = opt.value;
      input.checked = selected.includes(opt.value);
      input.style.display = "none";
      input.addEventListener("change", () => {
        const checkedValues = [];
        container.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
          if (cb.checked) checkedValues.push(cb.value);
        });
        checkbox.style.background = input.checked
          ? "var(--primary-color, #03a9f4)"
          : "var(--secondary-background-color, #f5f5f5)";
        checkbox.style.color = input.checked
          ? "#fff"
          : "var(--primary-text-color, #212121)";
        onChange(checkedValues.join(","));
      });
      checkbox.appendChild(input);
      checkbox.appendChild(document.createTextNode(opt.label));
      container.appendChild(checkbox);
    });
    return container;
  }

  /** 创建可折叠的手风琴区块 */
  _createAccordion(title, content) {
    const container = document.createElement("div");
    container.style.cssText = `
      margin-bottom: 16px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 8px;
      overflow: hidden;
    `;
    const header = document.createElement("div");
    header.textContent = title;
    header.style.cssText = `
      padding: 12px 16px;
      background: var(--secondary-background-color, #f5f5f5);
      cursor: pointer;
      font-weight: 500;
      display: flex;
      justify-content: space-between;
      align-items: center;
      user-select: none;
    `;
    const icon = document.createElement("span");
    icon.textContent = "▼";
    icon.style.cssText = "transition: transform 0.2s;";
    header.appendChild(icon);
    const body = document.createElement("div");
    body.style.cssText = `
      padding: 16px;
      display: none;
    `;
    body.appendChild(content);
    let isOpen = false;
    const setOpen = (open) => {
      isOpen = open;
      body.style.display = isOpen ? "block" : "none";
      icon.style.transform = isOpen ? "rotate(180deg)" : "rotate(0deg)";
    };
    const item = { setOpen };
    if (!this._accordionItems) {
      this._accordionItems = [];
    }
    this._accordionItems.push(item);
    header.addEventListener("click", () => {
      const nextOpen = !isOpen;
      this._accordionItems.forEach((other) => {
        if (other !== item) {
          other.setOpen(false);
        }
      });
      setOpen(nextOpen);
    });
    container.appendChild(header);
    container.appendChild(body);
    return container;
  }

  /** 渲染编辑器界面，包含基础配置、多用户配置、高级配置等手风琴区块 */
  render() {
    if (!this._hass) return;
    if (this._rendered) {
      return;
    }
    this._rendered = true;
    this._accordionItems = [];
    this.innerHTML = "";
    const container = document.createElement("div");
    container.style.cssText = `
      padding: 16px;
      max-width: 800px;
    `;
    const title = document.createElement("h3");
    title.textContent = "电力燃气信息卡片配置";
    title.style.cssText = `
      margin: 0 0 20px 0;
      color: var(--primary-text-color, #212121);
      font-size: 18px;
    `;
    container.appendChild(title);
    const basicContent = document.createElement("div");
    basicContent.appendChild(
      this._createSettingItem(
        "卡片名称",
        this._createCardNameInput(
          this._config.name,
          "例如：家庭用电",
          (value) => this._updateConfigValue("name", value),
          "name",
        ),
      ),
    );
    basicContent.appendChild(
      this._createSettingItem(
        "显示名称",
        this._createSwitch(
          this._config.show_name !== false,
          (value) => this._updateConfigValue("show_name", value),
          "show_name",
        ),
      ),
    );
    const themeOptions = [
      { value: "", label: "自动（根据时间）" },
      { value: "transparent", label: "半透明主题" },
      { value: "power", label: "国家电网主题" },
    ];
    const currentTheme =
      typeof this._config.theme === "object" && this._config.theme?.entity
        ? ""
        : this._config.theme || "";
    basicContent.appendChild(
      this._createSettingItem(
        "主题",
        this._createSelect(
          themeOptions,
          currentTheme,
          (value) => {
            const themeEntity =
              typeof this._config.theme === "object"
                ? this._config.theme?.entity
                : null;
            if (value) {
              this._config.theme = value;
            } else if (themeEntity) {
              this._config.theme = { entity: themeEntity };
            } else {
              delete this._config.theme;
            }
            this._dispatchConfigChangedEvent();
          },
          "theme",
        ),
      ),
    );

    container.appendChild(this._createAccordion("📋 基础配置", basicContent));
    const multiUserContent = document.createElement("div");
    const multiclassData = this._config.multiclass || {};
    const userListContainer = document.createElement("div");
    userListContainer.id = "user-list";
    this._renderUserList(userListContainer, multiclassData);
    multiUserContent.appendChild(userListContainer);
    container.appendChild(
      this._createAccordion("👥 多用户配置", multiUserContent),
    );

    // 高级配置
    const advancedContent = document.createElement("div");
    const hideOptions = [
      { value: "price-display", label: "价格区域" },
      { value: "elegas-price-display", label: "电价区域" },
      { value: "remaining-days-display", label: "预计用天数" },
      { value: "tier-indicator", label: "阶梯指示器" },
      { value: "data-container", label: "统计数据" },
    ];
    advancedContent.appendChild(
      this._createSettingItem(
        "隐藏元素（勾选要隐藏的区块）",
        this._createMultiSelect(
          hideOptions,
          this._config.hide || "",
          (value) => this._updateConfigValue("hide", value || undefined),
          "hide",
        ),
      ),
    );
    container.appendChild(
      this._createAccordion("⚙️ 高级配置", advancedContent),
    );

    this.appendChild(container);
  }

  /** 渲染多用户列表，支持标签页切换和用户增删改 */
  _renderUserList(container, multiclassData, activeIndex = 0) {
    container.innerHTML = "";
    const multiclass = multiclassData || this._config.multiclass || {};
    const userIds = Object.keys(multiclass).sort((a, b) => {
      const na = parseInt(a, 10);
      const nb = parseInt(b, 10);
      if (!isNaN(na) && !isNaN(nb)) return na - nb;
      if (!isNaN(na)) return -1;
      if (!isNaN(nb)) return 1;
      return String(a).localeCompare(b);
    });
    const createAddUserButton = (variant) => {
      const addUserBtn = document.createElement("button");
      addUserBtn.textContent = "+";
      if (variant === "tabs") {
        addUserBtn.style.cssText = `
          padding: 10px 20px;
          background: transparent;
          border: none;
          border-bottom: 3px solid transparent;
          cursor: pointer;
          font-size: 20px;
          font-weight: 300;
          color: var(--secondary-text-color, #727272);
          transition: all 0.2s;
          min-width: 50px;
          margin-left: auto;
        `;
      } else {
        addUserBtn.style.cssText = `
          padding: 10px 24px;
          background: transparent;
          border: 1px dashed var(--divider-color, #e0e0e0);
          border-radius: 8px;
          cursor: pointer;
          font-size: 28px;
          font-weight: 300;
          color: var(--secondary-text-color, #727272);
          transition: all 0.2s;
          min-width: 56px;
        `;
      }
      addUserBtn.addEventListener("mouseenter", () => {
        addUserBtn.style.color = "var(--primary-color, #03a9f4)";
        if (variant !== "tabs") {
          addUserBtn.style.borderColor = "var(--primary-color, #03a9f4)";
        }
      });
      addUserBtn.addEventListener("mouseleave", () => {
        addUserBtn.style.color = "var(--secondary-text-color, #727272)";
        if (variant !== "tabs") {
          addUserBtn.style.borderColor = "var(--divider-color, #e0e0e0)";
        }
      });
      addUserBtn.addEventListener("click", () => {
        if (!this._config.multiclass) {
          this._config.multiclass = {};
        }
        const existingIds = Object.keys(this._config.multiclass);
        let userNumber = 1;
        while (existingIds.includes(String(userNumber))) {
          userNumber++;
        }
        const userId = String(userNumber);
        this._config.multiclass[userId] = {
          entity: "",
          utility_type: "ele",
        };
        const newUserIds = Object.keys(this._config.multiclass).sort(
          (a, b) => parseInt(a) - parseInt(b),
        );
        const newUserIndex = newUserIds.indexOf(userId);
        this._renderUserList(container, this._config.multiclass, newUserIndex);
        this._dispatchConfigChangedEvent();
      });
      return addUserBtn;
    };
    if (userIds.length === 0) {
      const emptyWrapper = document.createElement("div");
      emptyWrapper.style.cssText = `
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 12px;
        padding: 20px;
      `;
      const emptyTip = document.createElement("div");
      emptyTip.textContent = "暂无用户配置，请点击下方按钮添加";
      emptyTip.style.cssText = `
        text-align: center;
        color: var(--secondary-text-color, #727272);
        font-style: italic;
      `;
      emptyWrapper.appendChild(emptyTip);
      emptyWrapper.appendChild(createAddUserButton("empty"));
      container.appendChild(emptyWrapper);
      return;
    }
    const tabsContainer = document.createElement("div");
    tabsContainer.style.cssText = `
      display: flex;
      border-bottom: 2px solid var(--divider-color, #e0e0e0);
      margin-bottom: 16px;
      gap: 4px;
    `;
    const contentContainer = document.createElement("div");
    contentContainer.style.cssText = `
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 8px;
      padding: 16px;
      background: var(--card-background-color, #fff);
    `;
    let activeTabIndex = Math.max(0, Math.min(activeIndex, userIds.length - 1));
    const renderContent = (index) => {
      const userId = userIds[index];
      const userConfig = multiclass[userId];
      if (!userConfig) {
        contentContainer.innerHTML = "";
        const errorTip = document.createElement("div");
        errorTip.textContent = `无法找到用户 "${userId}" 的配置信息`;
        errorTip.style.cssText = `
          padding: 20px;
          text-align: center;
          color: var(--error-color, #f44336);
          font-style: italic;
        `;
        contentContainer.appendChild(errorTip);
        return;
      }
      contentContainer.innerHTML = "";
      const header = document.createElement("div");
      header.style.cssText = `
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 16px;
        padding-bottom: 12px;
        border-bottom: 1px solid var(--divider-color, #e0e0e0);
      `;
      const userLabel = document.createElement("span");
      userLabel.textContent = `用户 ${index + 1}: ${userId}`;
      userLabel.style.cssText = `
        font-weight: 600;
        color: var(--primary-text-color, #212121);
        font-size: 16px;
      `;
      header.appendChild(userLabel);
      const deleteBtn = document.createElement("button");
      deleteBtn.textContent = "删除用户";
      deleteBtn.style.cssText = `
        padding: 6px 16px;
        background: var(--error-color, #f44336);
        color: white;
        border: none;
        border-radius: 4px;
        cursor: pointer;
        font-size: 13px;
      `;
      deleteBtn.addEventListener("click", () => {
        delete this._config.multiclass[userId];
        this._renderUserList(container, this._config.multiclass);
        this._dispatchConfigChangedEvent();
      });
      header.appendChild(deleteBtn);
      contentContainer.appendChild(header);
      const idInput = this._createTextInput(
        userId,
        "用户标识（英文）",
        (newId) => {
          if (newId && newId !== userId) {
            this._config.multiclass[newId] = this._config.multiclass[userId];
            delete this._config.multiclass[userId];
            const newUserIds = Object.keys(this._config.multiclass).sort(
              (a, b) => {
                const na = parseInt(a, 10);
                const nb = parseInt(b, 10);
                if (!isNaN(na) && !isNaN(nb)) return na - nb;
                if (!isNaN(na)) return -1;
                if (!isNaN(nb)) return 1;
                return String(a).localeCompare(b);
              },
            );
            const newIndex = newUserIds.indexOf(newId);
            this._renderUserList(container, this._config.multiclass, newIndex);
            this._dispatchConfigChangedEvent();
          }
        },
      );
      contentContainer.appendChild(this._createSettingItem("用户ID", idInput));
      const infoInput = this._createTextInput(
        userConfig.info,
        "显示名称（如：电费）",
        (value) => {
          if (value) {
            this._config.multiclass[userId].info = value;
          } else {
            delete this._config.multiclass[userId].info;
          }
          this._dispatchConfigChangedEvent();
        },
        `multiclass.${userId}.info`,
      );
      contentContainer.appendChild(
        this._createSettingItem("显示名称（可选）", infoInput),
      );
      const entitySelect = this._createEntitySelector(
        userConfig.entity,
        null,
        (value) => {
          this._config.multiclass[userId].entity = value;
          this._dispatchConfigChangedEvent();
        },
        `multiclass.${userId}.entity`,
      );
      contentContainer.appendChild(
        this._createSettingItem("实体", entitySelect),
      );
      const typeOptions = [
        { value: "ele", label: "电力 (ele)" },
        { value: "gas", label: "燃气 (gas)" },
      ];
      const typeSelect = this._createSelect(
        typeOptions,
        userConfig.utility_type || "ele",
        (value) => {
          this._config.multiclass[userId].utility_type = value;
          this._dispatchConfigChangedEvent();
        },
        `multiclass.${userId}.utility_type`,
      );
      contentContainer.appendChild(this._createSettingItem("类型", typeSelect));
    };
    userIds.forEach((userId, index) => {
      const tab = document.createElement("button");
      tab.textContent = index + 1;
      tab.style.cssText = `
        padding: 10px 20px;
        background: transparent;
        border: none;
        border-bottom: 3px solid transparent;
        cursor: pointer;
        font-size: 16px;
        font-weight: 500;
        color: var(--secondary-text-color, #727272);
        transition: all 0.2s;
        min-width: 50px;
      `;
      const updateTabStyle = () => {
        if (index === activeTabIndex) {
          tab.style.color = "var(--primary-color, #03a9f4)";
          tab.style.borderBottomColor = "var(--primary-color, #03a9f4)";
          tab.style.fontWeight = "600";
        } else {
          tab.style.color = "var(--secondary-text-color, #727272)";
          tab.style.borderBottomColor = "transparent";
          tab.style.fontWeight = "500";
        }
      };
      updateTabStyle();
      tab.addEventListener("click", () => {
        activeTabIndex = index;
        Array.from(tabsContainer.children).forEach((t, i) => {
          if (i === activeTabIndex) {
            t.style.color = "var(--primary-color, #03a9f4)";
            t.style.borderBottomColor = "var(--primary-color, #03a9f4)";
            t.style.fontWeight = "600";
          } else {
            t.style.color = "var(--secondary-text-color, #727272)";
            t.style.borderBottomColor = "transparent";
            t.style.fontWeight = "500";
          }
        });
        renderContent(index);
      });
      tabsContainer.appendChild(tab);
    });
    tabsContainer.appendChild(createAddUserButton("tabs"));
    container.appendChild(tabsContainer);
    container.appendChild(contentContainer);
    renderContent(activeTabIndex);
  }

  /** 创建数字输入框，支持 min/max/step 与防抖 */
  _createNumberInput(value, min, max, onChange, step = 1, configKey) {
    const input = document.createElement("input");
    input.type = "number";
    input.value = value !== undefined ? value : "";
    input.min = min;
    input.max = max;
    input.step = step;
    if (configKey) input.setAttribute("data-config-key", configKey);
    input.style.cssText = `
      width: 100%;
      padding: 8px 12px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 4px;
      background: var(--card-background-color, #fff);
      color: var(--primary-text-color, #212121);
      font-size: 14px;
      box-sizing: border-box;
    `;
    const debouncedOnChange = debounce((v) => {
      const val = parseFloat(v);
      onChange(isNaN(val) ? undefined : val);
    }, 300);
    input.addEventListener("input", (e) => debouncedOnChange(e.target.value));
    return input;
  }
}

// 注册可视化配置编辑器组件
try {
  if (!customElements.get("xjelegas-card-editor")) {
    customElements.define("xjelegas-card-editor", EleGasCardEditor);
  }
} catch (error) {
  console.warn("编辑器组件注册警告:", error.message);
}
